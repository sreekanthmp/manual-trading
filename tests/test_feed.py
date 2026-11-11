import threading
import time

from helpers import CFG, SPOT_TOKEN, msg, sample_contracts

from app.auth import AuthError
from app.engine import MarketData
from app.feed import MAX_AUTH_FAILURES, Feed


class FakeSession:
    jwt, feed_token = "jwt", "feed"

    def __init__(self, login_error=None):
        self.login_error, self.invalidated, self.logins = login_error, 0, 0

    def ensure(self):
        if self.login_error:
            raise self.login_error
        self.logins += 1

    def invalidate(self):
        self.invalidated += 1


class Hub:
    """Plays the part of the server: scripts what each successive connection does."""

    def __init__(self, script):
        self.script, self.active, self.max_active, self.subs, self.created = list(script), 0, 0, [], 0

    def factory(self, session):
        self.created += 1
        return FakeWS(self)


class FakeWS:
    def __init__(self, hub):
        self.hub, self.closed = hub, threading.Event()

    def subscribe(self, cid, mode, token_list):
        self.hub.subs.append((mode, token_list))

    def close_connection(self):
        self.closed.set()

    def connect(self):
        hub = self.hub
        hub.active += 1
        hub.max_active = max(hub.max_active, hub.active)
        try:
            self.on_open(self)
            hub.script.pop(0)(self)
        finally:
            hub.active -= 1


def good_tick(ws, price=24500.0):
    ws.on_data(ws, msg(SPOT_TOKEN, price, time.time() - 1))


def make(script, session=None):
    md = MarketData(CFG)
    hub = Hub(script)
    sleeps = []
    feed = Feed(CFG, session or FakeSession(), md, hub.factory, lambda s: (sample_contracts(), 24510.0), sleeps.append)
    return feed, md, hub, sleeps


def test_reconnects_and_resubscribes_after_drop_and_expired_session():
    def drop(ws):
        good_tick(ws)
        raise ConnectionError("connection reset")  # network glitch

    def expired(ws):
        ws.on_error(ws, "Handshake status 401 Unauthorized")  # server refuses the old session

    def healthy(ws):
        good_tick(ws, 24501.0)
        ws.hub.feed._stop.set()  # test ends after this connection

    session = FakeSession()
    feed, md, hub, sleeps = make([drop, expired, healthy], session)
    hub.feed = feed
    feed.run()

    assert hub.created == 3 and md.reconnects == 2
    assert hub.max_active == 1  # never two connections at once
    assert len(hub.subs) == 6  # two subscriptions per connection: the spot in quote mode, the options in snap-quote mode (bid / ask / open interest)
    pairs = [hub.subs[i : i + 2] for i in (0, 2, 4)]
    assert pairs[0] == pairs[1] == pairs[2]  # the same instruments every time
    (quote_mode, spot_groups), (snap_mode, option_groups) = pairs[0]
    assert quote_mode == 2 and spot_groups == [{"exchangeType": 1, "tokens": [SPOT_TOKEN]}]
    assert snap_mode == 3 and {g["exchangeType"] for g in option_groups} == {2} and sum(len(g["tokens"]) for g in option_groups) == 4
    assert session.invalidated >= 1  # 401 forced a fresh login
    assert md.accepted == 2 and md.state == "disconnected"
    assert len(sleeps) == 2 and sleeps[0] <= sleeps[1]  # backs off instead of spinning


def test_contracts_selected_once_per_day_not_on_every_reconnect():
    calls = []
    feed, md, hub, _ = make([lambda ws: None, lambda ws: ws.hub.feed._stop.set()])
    hub.feed = feed
    inner = feed.selector
    feed.selector = lambda s: calls.append(1) or inner(s)
    feed.run()
    assert hub.created == 2 and len(calls) == 1


def test_repeated_login_rejection_gives_up_instead_of_hammering_the_account():
    feed, md, hub, sleeps = make([], FakeSession(AuthError("login rejected: Invalid TOTP")))
    feed.run()
    assert md.state == "auth_failed" and hub.created == 0
    assert len(sleeps) == MAX_AUTH_FAILURES - 1
    assert any("login" in e["message"] for e in md.errors)


def test_start_twice_does_not_open_a_second_connection():
    release = threading.Event()
    feed, md, hub, _ = make([lambda ws: release.wait(5)])
    feed.start()
    deadline = time.time() + 3
    while hub.active == 0 and time.time() < deadline:
        time.sleep(0.01)
    feed.start()  # ignored: a supervisor is already running
    time.sleep(0.1)
    assert hub.created == 1 and hub.max_active == 1
    release.set()
    feed.stop()
    feed._thread.join(3)
    assert not feed._thread.is_alive()


def test_stale_feed_triggers_reconnect_through_watchdog():
    feed, md, hub, _ = make([lambda ws: None])
    ws = hub.factory(None)
    feed.ws = ws
    md.watchdog = lambda: True  # engine says: no ticks during market hours
    feed.watchdog()
    assert ws.closed.is_set()
    assert any("reconnecting" in e["message"] for e in md.errors)
