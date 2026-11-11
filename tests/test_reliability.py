"""Data reliability: connection recovery, per-instrument freshness, the clock and suspend guard, the candle API allowance, and the
spot-only recovery. Every test uses fakes and a fake clock: none of it has been seen against the real market."""

import socket
import threading
import time
from datetime import date

import pytest
import requests
from helpers import CFG, SPOT_TOKEN, ist, msg, sample_contracts
from test_auth import CFG as AUTH_CFG
from test_auth import OK, FakeApi

from app import history
from app.auth import AngelSession, AuthError, NetworkError, is_transient
from app.engine import UNHEALTHY_AFTER, MarketData
from app.feed import BACKOFF, MAX_AUTH_FAILURES, Feed
from app.gaps import recover
from app.health import CALM_SECONDS, STALL_SECONDS, TimeGuard
from app.ratelimit import COOLDOWNS, MIN_GAP, CandleLimiter, is_refusal


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def engine(t=ist(10, 0), guard=None):
    clock = Clock(t)
    md = MarketData(CFG, clock, guard=guard)
    md.set_contracts(sample_contracts(), date(2026, 11, 6), 24510.0)
    return md, clock


def option_token(md, role="ATM_CE"):
    return next(c.token for c in md.contracts if c.role == role)


def tick(md, clock, ts, token=SPOT_TOKEN, price=24500.0, exch=None):
    """One tick that arrives at `ts` (its exchange time, unless given: a delayed or skewed feed)."""
    clock.t = ts
    md.on_message(msg(token, price, ts if exch is None else exch, exch=2 if token != SPOT_TOKEN else 1))


# -- network failures are not authentication failures -----------------------------------------------------------------------------
@pytest.mark.parametrize("error", [socket.gaierror(11001, "getaddrinfo failed"), requests.exceptions.ConnectionError("down"), requests.exceptions.Timeout("slow"),
                                   TimeoutError("t"), ConnectionResetError("reset")])  # fmt: skip
def test_a_network_failure_at_login_is_a_network_error_never_an_auth_error(error):
    with pytest.raises(NetworkError):
        AngelSession(AUTH_CFG, lambda api_key: FakeApi(error, [])).login()
    assert is_transient(error)


def test_a_gateway_or_rate_limit_answer_is_temporary_but_a_rejected_login_is_not():
    class DataException(Exception):  # what the SDK raises when the answer is not JSON (a gateway page, the rate limit)
        pass

    with pytest.raises(NetworkError):
        AngelSession(AUTH_CFG, lambda api_key: FakeApi(DataException("Couldn't parse the JSON response: b'Access denied because of exceeding access rate'"), [])).login()
    with pytest.raises(NetworkError, match="rate"):
        AngelSession(AUTH_CFG, lambda api_key: FakeApi({"status": False, "message": "Access denied because of exceeding access rate"}, [])).login()
    with pytest.raises(AuthError, match="rejected"):
        AngelSession(AUTH_CFG, lambda api_key: FakeApi({"status": False, "message": "Invalid TOTP"}, [])).login()
    with pytest.raises(AuthError, match="login failed"):  # a secret that is not base32 is the account's fault, not the network's
        AngelSession(AUTH_CFG, lambda api_key: FakeApi(ValueError("Non-base32 digit found"), [])).login()


def test_a_refused_candle_request_is_reported_to_the_shared_limiter_and_other_calls_are_not():
    clock, naps = Clock(1000.0), []
    limiter = CandleLimiter(clock, naps.append)

    class Api(FakeApi):
        def getCandleData(self, params):
            raise Exception("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")

    s = AngelSession(AUTH_CFG, lambda api_key: Api(OK, [{"status": True, "data": {"ltp": 1}}]), limiter=limiter)
    assert s.call("ltpData", "NSE", "x", "y")["status"]  # not a candle request: no allowance is used
    assert limiter.requests == 0
    with pytest.raises(Exception, match="exceeding access rate"):
        s.call("getCandleData", {"x": 1})
    assert (limiter.requests, limiter.refusals) == (1, 1) and limiter.status()["cooling_down"]


# -- the shared allowance for candle requests -------------------------------------------------------------------------------------
def test_candle_requests_are_spaced_and_a_refusal_makes_everyone_wait_a_growing_cooldown():
    clock, naps = Clock(1000.0), []

    def nap(s):
        naps.append(s)
        clock.t += s

    lim = CandleLimiter(clock, nap)
    assert lim.wait() == 0.0 and naps == []  # the first request goes at once
    assert lim.wait() == pytest.approx(MIN_GAP) and naps[-1] == pytest.approx(MIN_GAP)  # the next one is spaced, in order
    lim.refused("Access denied because of exceeding access rate")
    assert lim.wait() == pytest.approx(COOLDOWNS[0], abs=MIN_GAP + 1e-9)  # everyone waits out the cool-down
    lim.refused("again")
    lim.refused("and again")
    assert lim.cooldown_until - clock.t == pytest.approx(COOLDOWNS[2])  # it grows while the refusals go on
    for _ in range(10):
        lim.refused("still")
    assert lim.cooldown_until - clock.t == pytest.approx(COOLDOWNS[-1])  # and is bounded
    lim.ok()
    assert lim.consecutive == 0 and lim.status()["refusals"] == 13 and is_refusal("Access denied because of EXCEEDING ACCESS RATE")


def test_two_threads_never_fire_candle_requests_together():
    lim, stamps = CandleLimiter(), []
    lock = threading.Lock()

    def go():
        lim.wait()
        with lock:
            stamps.append(time.time())

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert all(g >= MIN_GAP - 0.05 for g in gaps)


# -- the feed: temporary outages are retried for ever, rejected credentials are not ----------------------------------------------
class FakeSession:
    jwt, feed_token = "jwt", "feed"

    def __init__(self, errors=()):
        self.errors, self.invalidated, self.logins = list(errors), 0, 0

    def ensure(self):
        if self.errors:
            raise self.errors.pop(0)
        self.logins += 1

    def invalidate(self):
        self.invalidated += 1


class Hub:
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
            step = hub.script.pop(0)
            if not getattr(step, "no_open", False):  # a DNS failure happens before the connection ever opens
                self.on_open(self)
            step(self)
        finally:
            hub.active -= 1


def make(script, session):
    md = MarketData(CFG)
    hub = Hub(script)
    sleeps = []
    feed = Feed(CFG, session, md, hub.factory, lambda s: (sample_contracts(), 24510.0), sleeps.append)
    hub.feed = feed
    return feed, md, hub, sleeps


def good_tick(ws):
    ws.on_data(ws, msg(SPOT_TOKEN, 24500.0, time.time() - 1))


def test_a_long_network_outage_never_stops_the_feed_and_each_attempt_is_on_record():
    outage = MAX_AUTH_FAILURES * 3  # far more failures than a rejected login would ever be allowed
    session = FakeSession([NetworkError("login failed (Angel One not reachable): gaierror: getaddrinfo failed") for _ in range(outage)])

    def healthy(ws):
        good_tick(ws)
        ws.hub.feed._stop.set()

    feed, md, hub, sleeps = make([healthy], session)
    feed.run()
    assert md.state != "auth_failed" and hub.created == 1 and md.accepted == 1  # it kept retrying until the network came back, then connected
    assert max(sleeps) == BACKOFF[-1] and all(s <= BACKOFF[-1] for s in sleeps)  # bounded back-off
    kinds = [e["kind"] for e in md.feed_events]
    assert kinds.count("network_error") == outage and "gave_up" not in kinds
    assert [e["attempt"] for e in md.feed_events if e["kind"] == "network_error"][:3] == [1, 2, 3]  # the attempt number is recorded
    assert "getaddrinfo" in md.feed_events[0]["message"]
    assert session.invalidated == 0  # the session was fine: a network outage must not force a new login


def test_a_dns_failure_inside_the_websocket_is_a_network_problem_and_the_session_is_kept():
    def dns(ws):
        raise socket.gaierror(11001, "getaddrinfo failed")

    dns.no_open = True

    def healthy(ws):
        good_tick(ws)
        ws.hub.feed._stop.set()

    session = FakeSession()
    feed, md, hub, _ = make([dns, dns, dns, dns, healthy], session)
    feed.run()
    assert session.invalidated == 0 and hub.max_active == 1 and hub.created == 5  # four failures in a row, still no new login, never two connections
    events = md.feed_events
    assert [e["kind"] for e in events][:4] == ["network_error"] * 4 and "getaddrinfo" in events[0]["message"]
    assert events[4]["kind"] == "reconnected" and events[-1]["kind"] == "disconnected" and events[-1]["attempt"] == 1  # coming back is on record, and a healthy connection restarts the count


def test_rejected_credentials_still_stop_after_a_few_tries_with_an_explicit_state_and_reason():
    session = FakeSession([AuthError("login rejected: Invalid TOTP") for _ in range(10)])
    feed, md, hub, sleeps = make([], session)
    feed.run()
    assert md.state == "auth_failed" and hub.created == 0 and len(sleeps) == MAX_AUTH_FAILURES - 1
    assert md.feed_events[-1]["kind"] == "gave_up" and "Invalid TOTP" in md.feed_reason
    now = ist(10, 0)
    md.clock = lambda: now
    issues = md.health_issues(now)
    assert len(issues) == 1 and "stopped" in issues[0] and "Invalid TOTP" in issues[0]  # explicitly unhealthy, with the reason and what to do


def test_an_expired_session_forces_a_fresh_login_and_the_instruments_are_subscribed_again():
    def expired(ws):
        ws.on_error(ws, "Handshake status 401 Unauthorized")

    def healthy(ws):
        good_tick(ws)
        ws.hub.feed._stop.set()

    session = FakeSession()
    feed, md, hub, _ = make([expired, healthy], session)
    feed.run()
    assert session.invalidated >= 1 and len(hub.subs) == 4 and hub.subs[:2] == hub.subs[2:]  # new login, the same instruments subscribed again
    assert "401" in md.feed_events[0]["message"]


def test_a_feed_that_has_been_down_too_long_during_the_session_is_unhealthy_and_says_why():
    md, clock = engine(ist(10, 0))
    md.set_state("connected")
    clock.t = ist(10, 0, 5)
    md.set_state("disconnected", "network: getaddrinfo failed")
    md.note_feed_event("network_error", "getaddrinfo failed", 1)
    clock.t = ist(10, 0, 5) + UNHEALTHY_AFTER - 1
    assert md.health_issues(clock.t) == []  # a short drop is not yet a problem
    clock.t = ist(10, 0, 5) + UNHEALTHY_AFTER + 1
    [issue] = md.health_issues(clock.t)
    assert "no live connection" in issue and "getaddrinfo" in issue
    assert md.health(clock.t)["trusted"] is False and md.health(clock.t)["feed"]["events"][-1]["kind"] == "network_error"
    assert md.health_issues(ist(15, 45)) == []  # the market is closed: nothing to distrust


# -- every instrument is judged by its own ticks ---------------------------------------------------------------------------------
def test_option_ticks_never_make_a_silent_spot_look_fresh():
    md, clock = engine()
    md.set_state("connected")
    ce = option_token(md)
    tick(md, clock, ist(10, 0, 1))  # the last spot tick
    for s in range(2, 60, 2):
        tick(md, clock, ist(10, 0, s), ce, 100.0 + s)  # the option keeps ticking while the spot is silent
    now = ist(10, 0, 59)
    assert md.spot_age(now) == pytest.approx(58.0)  # the spot's own age, not the option's
    status = {i["role"]: i for i in md.instrument_status(now)}
    assert status["SPOT"]["status"] == "stale" and status["ATM_CE"]["status"] == "fresh"
    assert any("NIFTY spot is stale" in w for w in md.health(now)["warnings"])
    assert md.watchdog() is True and "no NIFTY spot tick" in md.watchdog_reason  # the connection is reset because the SPOT went quiet


def test_a_silent_option_is_flagged_while_the_spot_is_fresh_and_the_spot_still_decides():
    md, clock = engine()
    md.set_state("connected")
    ce = option_token(md)
    tick(md, clock, ist(10, 0, 1), ce, 120.0)
    for s in range(1, 59):
        tick(md, clock, ist(10, 0, s), price=24500.0 + s)
    now = ist(10, 0, 58)
    status = {i["role"]: i for i in md.instrument_status(now)}
    assert status["SPOT"]["status"] == "fresh" and status["ATM_CE"]["status"] == "stale" and status["ATM_PE"]["status"] == "no data"
    warns = md.health(now)["warnings"]
    assert any("ATM_CE" in w and "stale" in w for w in warns) and any("ATM_PE" in w and "no quote" in w for w in warns)
    assert md.spot_age(now) == pytest.approx(0.0) and md.watchdog() is False  # the spot is alive: nothing to reconnect


def test_everything_stale_and_the_market_closed():
    md, clock = engine()
    md.set_state("connected")
    tick(md, clock, ist(10, 0, 1))
    tick(md, clock, ist(10, 0, 1), option_token(md), 100.0)
    assert {i["status"] for i in md.instrument_status(ist(10, 5))} >= {"stale"}
    assert {i["status"] for i in md.instrument_status(ist(16, 0))} == {"market closed"}  # after the close, silence is expected, not stale
    assert md.health(ist(16, 0))["warnings"] == []


def test_rejected_ticks_do_not_refresh_the_age_and_a_late_spot_tick_cannot_corrupt_it():
    md, clock = engine()
    tick(md, clock, ist(10, 0, 5), price=24500.0)
    first = md.last_spot_recv
    tick(md, clock, ist(10, 0, 5), price=24500.0)  # duplicate
    tick(md, clock, ist(10, 0, 4), price=24400.0)  # out of order
    assert md.last_spot_recv == first and md.spot_age(ist(10, 0, 20)) == pytest.approx(15.0)  # neither counted as a fresh tick
    assert md.snapshot()["connection"]["ticks_rejected"] == {"duplicate": 1, "out_of_order": 1}


# -- the clock and suspend guard -----------------------------------------------------------------------------------------------------
class Mono:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_normal_seconds_raise_no_alarm_and_a_stall_is_a_suspension():
    mono, g = Mono(), None
    g = TimeGuard(mono)
    t = ist(10, 0)
    for _ in range(30):
        t += 1.2
        mono.t += 1.2
        assert g.check(t, t) is None
    t += 400  # the PC slept for 400 s: neither timer saw it, or both did
    event = g.check(t, t - 401)
    assert event and event["kind"] == "suspended" and event["from"] == pytest.approx(t - 400) and not g.trusted
    assert g.check(t + 1, t - 401) is None and not g.trusted  # still distrusted: no calm period, no fresh tick


def test_a_clock_that_jumps_or_runs_backwards_is_caught_even_when_the_monotonic_timer_is_steady():
    mono, g, t = Mono(), None, ist(10, 0)
    g = TimeGuard(mono)
    g.check(t, t)
    mono.t += 1.0
    back = g.check(t - 30, t)  # the clock was set back 30 s while one second really passed
    assert back and back["kind"] == "clock_back" and "moved back" in back["reason"]
    g2 = TimeGuard(mono := Mono())
    g2.check(t, t)
    mono.t += 1.0
    jump = g2.check(t + 9.0, t)  # the wall clock moved 9 s but the timer only 1 s
    assert jump and jump["kind"] == "clock_jump" and "jumped" in jump["reason"]


def test_time_is_trusted_again_only_after_a_calm_period_and_a_fresh_spot_tick():
    mono, t = Mono(), ist(10, 0)
    g = TimeGuard(mono)
    g.check(t, t)
    t += STALL_SECONDS + 5
    mono.t += STALL_SECONDS + 5
    assert g.check(t, t - 20)["kind"] == "suspended"
    since = t
    for _ in range(int(CALM_SECONDS) + 10):  # time looks normal, but no spot tick has arrived since the event
        t += 1.0
        mono.t += 1.0
        g.check(t, since - 1)
    assert not g.trusted  # a quiet clock alone is not proof that the data is right
    g.check(t + 1.0, t)  # now a spot tick newer than the event has arrived
    assert g.trusted and g.events[-1]["kind"] == "trusted_again"


def test_exchange_time_that_disagrees_with_the_pc_clock_is_a_skew_but_one_late_tick_is_not():
    g = TimeGuard(Mono())
    t = ist(10, 0)
    assert g.note_skew(t, 2.0) is None  # normal delivery delay
    assert g.note_skew(t, 40.0) is None and g.note_skew(t, 2.0) is None  # one late tick (a reconnect snapshot) and it is over
    assert g.note_skew(t, 40.0) is None and g.note_skew(t, 41.0) is None
    event = g.note_skew(t, 42.0)  # three in a row: the PC clock is ahead of the exchange, or the feed is delayed
    assert event and event["kind"] == "skew" and "older than the PC clock" in event["reason"] and not g.trusted
    g = TimeGuard(Mono())
    assert [g.note_skew(t, -30.0) for _ in range(3)][-1]["reason"].startswith("the NIFTY ticks are 30 s ahead")


def test_a_suspension_never_leaves_a_completed_candle_across_the_unseen_period():
    mono, clock = Mono(), Clock(ist(10, 0, 0))
    md = MarketData(CFG, clock, guard=TimeGuard(mono))
    md.set_contracts(sample_contracts(), date(2026, 11, 6), 24510.0)
    md.set_state("connected")
    for s in range(0, 20, 2):  # ticks during the first 20 seconds of the 10:00 minute
        mono.t, clock.t = float(s), ist(10, 0, s)
        md.on_message(msg(SPOT_TOKEN, 24500.0 + s, clock.t))
        md.watchdog()
    mono.t, clock.t = 20.0 + 400.0, ist(10, 7, 0)  # the PC slept until 10:07
    assert md.watchdog() is True  # the old connection cannot be trusted: reconnect
    closed = md.builder.closed[(SPOT_TOKEN, "1m")]
    assert [c.start for c in closed] == [ist(10, 0)] and closed[0].partial  # the 10:00 candle is kept but flagged: it did not see its own end
    assert not any(c.start > ist(10, 0) for c in closed)  # no candle was made for 10:01-10:06: nothing was seen
    assert md.gaps.gaps[-1]["reason"] == "suspended" and (md.gaps.gaps[-1]["start"], md.gaps.gaps[-1]["end"]) == (ist(10, 0, 18), ist(10, 7, 0))
    assert any("suspended" in e["message"] for e in md.errors)
    assert [i for i in md.health_issues(clock.t) if "clock cannot be trusted" in i]


# -- recovery: spot only, one allowance, bounded ---------------------------------------------------------------------------------
STEP = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "ONE_HOUR": 3600}


class Api:
    """getCandleData: one candle per interval between fromdate and todate; optionally refuses for its rate."""

    def __init__(self, refuse=False):
        self.requests, self.refuse = [], refuse

    def call(self, method, params):
        assert method == "getCandleData"
        self.requests.append(params)
        if self.refuse:
            raise Exception("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")
        from datetime import datetime

        from app.candles import IST

        fmt, step = "%Y-%m-%d %H:%M", STEP[params["interval"]]
        t = datetime.strptime(params["fromdate"], fmt).replace(tzinfo=IST).timestamp()
        end = datetime.strptime(params["todate"], fmt).replace(tzinfo=IST).timestamp()
        rows = []
        while t <= end:
            rows.append([datetime.fromtimestamp(t, IST).isoformat(), 1, 2, 0.5, 1.5, 77])
            t += step
        return {"status": True, "data": rows}


def gap_engine():
    md, clock = engine(ist(10, 0))
    ce, pe = option_token(md), option_token(md, "ATM_PE")
    for ts in (ist(10, 0, 0), ist(10, 0, 10), ist(10, 3, 30)):
        for token in (SPOT_TOKEN, ce, pe):
            tick(md, clock, ts, token, 24500.0 if token == SPOT_TOKEN else 100.0)  # the options tick through the same gap as the spot
    clock.t = ist(10, 5)
    return md, clock, {SPOT_TOKEN, ce, pe}


def test_only_the_spot_candles_are_recovered_because_nothing_reads_the_option_candles():
    md, _, _ = gap_engine()
    api = Api()
    assert recover(api, md, sleep=lambda s: None) > 0
    assert api.requests and {r["symboltoken"] for r in api.requests} == {SPOT_TOKEN}  # not a single request for an option
    assert md.recovery["recovered"] > 0 and md.recovery["requests"] == len(api.requests) and md.recovery["failures"] == 0
    assert recover(api, md, sleep=lambda s: None) == 0  # nothing left for the spot: and no repeated request for a candle that is fixed
    asked = len(api.requests)
    recover(api, md, sleep=lambda s: None)
    assert len(api.requests) == asked


def test_the_recovery_stands_aside_while_the_history_refresh_runs():
    md, _, _ = gap_engine()
    api = Api()
    md.history_busy = True
    assert recover(api, md, sleep=lambda s: None) == 0 and api.requests == [] and md.recovery["deferred"] == 1
    md.history_busy = False
    assert recover(api, md, sleep=lambda s: None) > 0  # and carries on afterwards: nothing was given up


def test_a_rate_limit_refusal_ends_the_sweep_and_is_recorded_not_hammered():
    md, _, _ = gap_engine()
    api = Api(refuse=True)
    assert recover(api, md, sleep=lambda s: None) == 0
    assert len(api.requests) == 1  # one refusal: the rest of the sweep is left for later, not fired into the same refusal
    assert md.recovery["failures"] == 1 and "exceeding access rate" in md.recovery["last_error"]
    assert any("recovery" in e["message"] for e in md.errors)


class Silent(Api):
    """The API answers, but without the candle that was asked for."""

    def call(self, method, params):
        self.requests.append(params)
        return {"status": True, "data": []}


def test_a_candle_the_api_answers_without_is_asked_for_at_most_its_few_tries():
    md, _, _ = gap_engine()
    api = Silent()
    for _ in range(10):
        recover(api, md, sleep=lambda s: None)
    one_min = [r for r in api.requests if r["interval"] == "ONE_MINUTE"]
    assert 1 <= len(one_min) <= 3  # bounded: a candle the API does not have is not requested for ever


def test_a_refused_request_does_not_use_up_a_candles_tries_so_it_is_still_recovered_when_the_api_answers():
    md, _, _ = gap_engine()
    refusing = Api(refuse=True)
    for _ in range(10):  # ten sweeps, every one refused (each refusal ends its sweep after one request)
        assert recover(refusing, md, sleep=lambda s: None) == 0
    assert len(refusing.requests) == 10  # one request per sweep, never a burst
    assert recover(Api(), md, sleep=lambda s: None) > 0  # the candles were not given up: they are fetched as soon as the API answers
    assert recover(Api(), md, sleep=lambda s: None) == 0


def test_the_history_refresh_tells_the_recovery_to_stand_aside_while_it_runs():
    md, clock = engine(ist(10, 0))
    seen = []

    def loader(cfg, now):
        seen.append(md.history_busy)
        raise RuntimeError("Prism DB is down")  # the refresh fails: the flag must still be cleared

    assert history.refresh(CFG, md, Api(), now=clock, sleep=lambda s: None, loader=loader) is False
    assert seen == [True] and md.history_busy is False


def test_the_recovery_status_reaches_the_dashboard():
    md, _, _ = gap_engine()
    recover(Api(refuse=True), md, sleep=lambda s: None)
    snap = md.snapshot()
    rec = snap["health"]["recovery"]
    assert rec["failures"] == 1 and rec["pending_candles"] >= 1 and rec["history_refreshing"] is False and "refusals" in rec
    assert rec["requests"] == 1 and rec["api_requests"] == 0  # the recovery's own count and the shared limiter's count are two different numbers
    import json

    json.dumps(snap)
    assert [i["role"] for i in snap["health"]["instruments"]] == ["SPOT", "ATM_CE", "ATM_PE", "ITM_CE", "ITM_PE"]
