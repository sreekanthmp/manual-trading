"""Stage 7.3: a live connection that stays up across midnight chooses the new trading day's contracts.

The contracts are chosen when a connection is made. Angel One does not always close the connection at midnight, so a Prism left running would keep the
options of an expired expiry for ever. The watchdog now notices that the IST date has moved past the day the contracts were chosen and makes the feed
reconnect: the reconnect chooses afresh (the existing path) and subscribes the new contracts from scratch. Everything here runs on fake sessions and a fake socket."""

import threading
import time
from datetime import date, datetime

from helpers import CFG, SPOT_TOKEN, Clock, ist, master_rows, msg

from app.candles import IST
from app.engine import MarketData
from app.feed import Feed
from app.instruments import select_contracts

DAY1, DAY2 = (2026, 11, 9), (2026, 11, 10)


def contracts_for(day, expiries):
    """The contracts the master of that day gives. master_rows numbers tokens from the first expiry on, so the two days would share tokens: the options of the
    new day get tokens of their own (as in the real master, where every contract has its own)."""
    rows = master_rows(expiries=expiries)
    if day == DAY2:
        rows = [{**r, "token": "7" + r["token"]} if r["exch_seg"] == "NFO" else r for r in rows]
    return select_contracts(rows, 24510.0, datetime(*day, 10, 0, tzinfo=IST))


class Session:
    jwt, feed_token = "jwt", "feed"

    def ensure(self):
        pass

    def invalidate(self):
        pass


class Hub:
    """Plays the server: each connection stays open until the feed closes it, then the next script step runs."""

    def __init__(self):
        self.created, self.subs, self.sockets = 0, [], []

    def factory(self, session):
        self.created += 1
        ws = Socket(self)
        self.sockets.append(ws)
        return ws


class Socket:
    def __init__(self, hub):
        self.hub, self.closed, self.opened = hub, threading.Event(), threading.Event()

    def subscribe(self, cid, mode, token_list):
        self.hub.subs.append((self.hub.created, mode, [t for g in token_list for t in g["tokens"]]))

    def close_connection(self):
        self.closed.set()

    def connect(self):
        self.on_open(self)
        self.opened.set()
        self.closed.wait(10)


def wait_for(condition, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def make(selections, now=ist(10, 0, day=DAY1)):
    """A feed on a clock the test moves. `selections`: what the selector hands back on its 1st, 2nd... call (an Exception instance is raised instead)."""
    clock = Clock(now)
    md = MarketData(CFG, clock)
    hub, calls = Hub(), []

    def selector(session):
        calls.append(clock.t)
        step = selections[min(len(calls), len(selections)) - 1]
        if isinstance(step, Exception):
            raise step
        return step, 24510.0

    feed = Feed(CFG, Session(), md, hub.factory, selector, lambda s: time.sleep(0.01))
    return feed, md, hub, clock, calls


def run(feed):
    feed.start()
    return feed


def stop(feed):
    feed.stop()
    feed._thread.join(5)
    assert not feed._thread.is_alive()


def tokens(contracts):
    return sorted(c.token for c in contracts)


# -- what makes a rollover due -------------------------------------------------------------------------------------------------------------
def test_a_new_day_with_the_old_days_contracts_on_a_live_connection_is_due():
    feed, md, hub, clock, calls = make([contracts_for(DAY1, ("10NOV2026", "17NOV2026"))])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    assert md.selected_on == date(*DAY1) and feed.rollover_due() is False  # the day the contracts were chosen
    clock.t = ist(23, 59, 59, day=DAY1)
    assert feed.rollover_due() is False  # not yet midnight
    clock.t = ist(0, 0, 1, day=DAY2)
    assert feed.rollover_due() is True
    stop(feed)


def test_it_is_never_due_without_a_live_connection_or_without_contracts():
    feed, md, hub, clock, calls = make([contracts_for(DAY1, ("10NOV2026", "17NOV2026"))])
    clock.t = ist(0, 0, 1, day=DAY2)
    assert feed.rollover_due() is False  # nothing chosen yet, and not connected
    md.set_contracts(contracts_for(DAY1, ("10NOV2026", "17NOV2026")), date(*DAY1), 24510.0)
    for state in ("starting", "connecting", "disconnected", "auth_failed", "config_error"):
        md.state = state
        assert feed.rollover_due() is False, state  # a feed that is down chooses afresh on its own reconnect
    md.state = "connected"
    assert feed.rollover_due() is True
    md.contracts = []
    assert feed.rollover_due() is False
    md.contracts, md.selected_on = contracts_for(DAY1, ("10NOV2026", "17NOV2026")), None
    assert feed.rollover_due() is False  # a day nobody recorded is not compared with


def test_the_same_day_is_not_a_rollover_even_late_in_the_evening_or_after_the_close():
    feed, md, hub, clock, calls = make([contracts_for(DAY1, ("10NOV2026", "17NOV2026"))])
    md.set_contracts(contracts_for(DAY1, ("10NOV2026", "17NOV2026")), date(*DAY1), 24510.0)
    md.state = "connected"
    for t in (ist(0, 0, 0, day=DAY1), ist(9, 15, day=DAY1), ist(15, 31, day=DAY1), ist(23, 59, 59, day=DAY1)):
        clock.t = t
        assert feed.rollover_due() is False


# -- what the feed does about it -----------------------------------------------------------------------------------------------------------
def test_at_midnight_the_connection_is_dropped_and_the_new_days_contracts_are_chosen_and_subscribed():
    old = contracts_for(DAY1, ("10NOV2026", "17NOV2026"))  # the nearest expiry is the 10th
    new = contracts_for(DAY2, ("17NOV2026", "24NOV2026"))  # the 10th is over: the nearest is the 17th
    assert tokens(old) != tokens(new)
    feed, md, hub, clock, calls = make([old, new])
    run(feed)
    assert wait_for(lambda: md.state == "connected" and hub.created == 1)
    assert tokens(md.contracts) == tokens(old) and md.selected_on == date(*DAY1)
    feed.watchdog()
    assert hub.created == 1 and not hub.sockets[0].closed.is_set()  # nothing happens on the same day

    clock.t = ist(0, 0, 5, day=DAY2)
    feed.watchdog()  # what the ticker does once a second
    assert hub.sockets[0].closed.is_set()  # the old connection is closed ...
    assert wait_for(lambda: hub.created == 2 and md.state == "connected" and md.selected_on == date(*DAY2))
    assert len(calls) == 2  # ... and the reconnect chose again
    assert tokens(md.contracts) == tokens(new) and md.selected_on == date(*DAY2)
    sub_tokens = {t for (n, mode, ts) in hub.subs if n == 2 for t in ts}
    assert sub_tokens == {c.token for c in new}  # the second connection subscribed the new day's contracts, none of the old day's options
    assert not ({c.token for c in old if c.kind} & sub_tokens)
    assert SPOT_TOKEN in sub_tokens
    assert feed.rollover_due() is False
    stop(feed)


def test_the_rollover_is_on_record_once_and_says_what_it_did():
    old, new = contracts_for(DAY1, ("10NOV2026", "17NOV2026")), contracts_for(DAY2, ("17NOV2026", "24NOV2026"))
    feed, md, hub, clock, calls = make([old, new])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    clock.t = ist(0, 0, 5, day=DAY2)
    feed.watchdog()
    clock.t += 1
    feed.watchdog()  # the next second: the close may not have been processed yet (the state is still "connected"): it is not asked twice
    clock.t += 1
    feed.watchdog()
    assert wait_for(lambda: hub.created == 2 and md.state == "connected")
    feed.watchdog()
    events = [e for e in md.feed_events if e["kind"] == "rollover"]
    assert len(events) == 1
    assert "2026-11-09" in events[0]["message"] and "new trading day" in events[0]["message"]
    assert hub.created == 2  # one reconnect, not one per call
    stop(feed)


def test_a_failed_selection_keeps_retrying_and_never_drops_the_old_contracts_in_the_meantime():
    old = contracts_for(DAY1, ("10NOV2026", "17NOV2026"))
    new = contracts_for(DAY2, ("17NOV2026", "24NOV2026"))
    feed, md, hub, clock, calls = make([old, RuntimeError("could not read NIFTY spot price: network"), RuntimeError("again"), new])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    clock.t = ist(0, 0, 5, day=DAY2)
    feed.watchdog()
    assert wait_for(lambda: md.selected_on == date(*DAY2), 8.0)  # the supervisor's back-off loop retried until the selection worked
    assert len(calls) == 4 and tokens(md.contracts) == tokens(new)
    assert wait_for(lambda: md.state == "connected")
    assert len([e for e in md.feed_events if e["kind"] == "rollover"]) == 1  # the failures did not make more rollovers
    stop(feed)


def test_the_old_contracts_stay_while_the_selection_has_not_worked_yet():
    old = contracts_for(DAY1, ("10NOV2026", "17NOV2026"))
    boom = RuntimeError("no network")
    feed, md, hub, clock, calls = make([old, boom])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    clock.t = ist(0, 0, 5, day=DAY2)
    feed.watchdog()
    assert wait_for(lambda: len(calls) >= 3)  # it keeps trying every back-off step
    assert tokens(md.contracts) == tokens(old) and md.selected_on == date(*DAY1)  # nothing half-chosen: the old set is still the one on record
    assert md.state != "connected"
    stop(feed)


def test_an_ordinary_day_never_reconnects_or_chooses_again():
    feed, md, hub, clock, calls = make([contracts_for(DAY1, ("10NOV2026", "17NOV2026"))])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    for i, t in enumerate((ist(9, 14, day=DAY1), ist(12, 0, day=DAY1), ist(15, 29, day=DAY1), ist(23, 59, 58, day=DAY1))):
        clock.t = t
        md.on_message(msg(SPOT_TOKEN, 24500.0 + i, t - 1))  # (a live feed: the existing no-tick watchdog has nothing to say)
        feed.watchdog()
    time.sleep(0.1)
    assert hub.created == 1 and len(calls) == 1
    assert not [e for e in md.feed_events if e["kind"] == "rollover"]
    stop(feed)


def test_the_selection_uses_the_engines_clock_not_the_wall_clock():
    feed, md, hub, clock, calls = make([contracts_for(DAY1, ("10NOV2026", "17NOV2026"))], now=ist(10, 0, day=DAY1))
    feed._prepare()
    assert md.selected_on == date(*DAY1)  # (the wall clock is another day entirely)
    clock.t = ist(10, 0, day=DAY2)
    md.state = "connected"
    assert feed.rollover_due() is True
    feed._prepare()
    assert md.selected_on == date(*DAY2) and len(calls) == 2
    assert feed.rollover_due() is False


def test_the_spot_stream_is_not_lost_by_the_reconnect_it_still_decodes_ticks():
    old, new = contracts_for(DAY1, ("10NOV2026", "17NOV2026")), contracts_for(DAY2, ("17NOV2026", "24NOV2026"))
    feed, md, hub, clock, calls = make([old, new])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    clock.t = ist(0, 0, 5, day=DAY2)
    feed.watchdog()
    assert wait_for(lambda: hub.created == 2 and md.state == "connected")
    assert md.spot_token == SPOT_TOKEN
    md.on_message(msg(SPOT_TOKEN, 24500.0, clock.t - 1))
    assert md.accepted == 1 and md.latest[SPOT_TOKEN].ltp == 24500.0
    stop(feed)


def test_a_connection_that_did_not_close_is_asked_again_after_a_minute_and_not_before():
    feed, md, hub, clock, calls = make([contracts_for(DAY1, ("10NOV2026", "17NOV2026"))])
    run(feed)
    assert wait_for(lambda: md.state == "connected")
    hub.sockets[0].close_connection = lambda: None  # a socket that ignores the close
    clock.t = ist(0, 0, 5, day=DAY2)
    feed.watchdog()
    asked = lambda: len([e for e in md.feed_events if e["kind"] == "rollover"])  # noqa: E731
    assert asked() == 1
    for dt in (1, 30, 59):
        clock.t = ist(0, 0, 5, day=DAY2) + dt
        feed.watchdog()
    assert asked() == 1  # not within the minute
    clock.t = ist(0, 0, 5, day=DAY2) + 60
    feed.watchdog()
    assert asked() == 2  # and again once the minute is over, for as long as the old contracts are still on a live connection
    hub.sockets[0].closed.set()
    stop(feed)
