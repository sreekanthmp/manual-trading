"""Prism DB history (read-only), Angel One backfill, and the refresh at the start of a trading session.
Everything here uses fakes: no database or broker is touched."""

import re
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers import CFG, FRI, MON, SPOT_TOKEN, THU, Clock, hhmm, ist, sample_contracts, session_candles

from app import history
from app.candles import IST, Candle
from app.config import Config
from app.engine import MarketData
from app.series import completed_series

UTC = timezone.utc
TUE = (2026, 11, 10)
PG = Config("k", "c", "p", "JBSWY3DPEHPK3PXP", pg_password="secret")
STEP = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "ONE_DAY": 86400}
SECS = {"1m": 60, "5m": 300, "15m": 900}


def when(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%a %H:%M")


# -- Prism DB: read-only, bounded, IST-correct ---------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def execute(self, sql, params):
        self.conn.executed.append((sql, params))
        if self.conn.fail:
            raise RuntimeError("query failed")
        self.rows = self.conn.rows.get(params[1], [])

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows=None, fail=False):
        self.rows, self.executed, self.session, self.closed, self.fail = rows or {}, [], None, False, fail

    def set_session(self, **kw):
        self.session = kw

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def test_load_db_is_read_only_bounded_and_returns_ascending_ist_correct_candles():
    utc = lambda h, m: datetime(2026, 11, 5, h, m, tzinfo=UTC)  # noqa: E731  (03:45 UTC is 09:15 IST)
    rows = {  # a database returns newest first (ORDER BY datetime DESC)
        "15min": [
            (datetime(2026, 11, 5, 15, 15, tzinfo=IST), 5, 6, 4, 5.5, 7),  # last candle of the day, IST zone
            (utc(10, 0), 1, 1, 1, 1, 0),  # 15:30 IST: the session has ended, dropped
            (utc(3, 45), 100, 102, 99, 101, 0),
            (utc(3, 45), 100, 102, 99, 101, 0),  # duplicate start, dropped
            (utc(3, 0), 1, 1, 1, 1, 0),  # 08:30 IST, before the open, dropped
        ],
        "1day": [(datetime(2026, 11, 4, 0, 0, tzinfo=IST), 23742.25, 23970, 23700, 23960.6, 0)],
    }
    conn, seen = FakeConn(rows), {}

    def connect(**kw):
        seen.update(kw)
        return conn

    now = ist(10, 0, day=FRI)
    hist = history.load_db(PG, now, connect=connect)

    assert conn.session == {"readonly": True, "autocommit": True} and conn.closed
    assert "default_transaction_read_only=on" in seen["options"] and "statement_timeout" in seen["options"]
    assert (seen["host"], seen["dbname"], seen["password"]) == ("localhost", "prism", "secret")  # credentials come from the config
    assert [p[1] for _, p in conn.executed] == ["1min", "5min", "15min", "1day"]  # only the timeframes that are needed
    for sql, params in conn.executed:
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b", sql, re.I)
        assert "ORDER BY datetime DESC LIMIT" in sql and params[0] == "NIFTY" and params[3] == history.MAX_ROWS  # bounded rows
    lookback = {p[1]: datetime.fromtimestamp(now, IST) - p[2] for _, p in conn.executed}
    assert lookback == {"1min": timedelta(days=5), "5min": timedelta(days=14), "15min": timedelta(days=70), "1day": timedelta(days=45)}

    assert [c.start for c in hist["15m"]] == [ist(9, 15, day=THU), ist(15, 15, day=THU)]  # ascending, one per start
    assert hist["15m"][1].end == ist(15, 30, day=THU) and hist["15m"][1].close == 5.5  # the last candle ends at the close
    assert hist["15m"][0].source == "db" and isinstance(hist["15m"][0].open, float)  # Decimal / int -> float
    (d,) = hist["1d"]
    assert datetime.fromtimestamp(d.start, IST).date() == date(2026, 11, 4) and d.end - d.start == 86400
    assert hist["1m"] == [] and hist["5m"] == []  # no rows is not an error: holidays and gaps just have no candles


def test_load_db_needs_a_password_and_always_closes_the_connection():
    with pytest.raises(RuntimeError, match="PG_PASSWORD"):
        history.load_db(Config("k", "c", "p", "t"), 0, connect=lambda **kw: pytest.fail("must not connect"))
    conn = FakeConn(fail=True)
    with pytest.raises(RuntimeError, match="query failed"):
        history.load_db(PG, ist(10, 0, day=FRI), connect=lambda **kw: conn)
    assert conn.closed


# -- Angel One backfill ----------------------------------------------------------------------------------------------
class AngelHistory:
    """Pretends to be getCandleData: one candle per interval between fromdate and todate (even at night)."""

    def __init__(self, fail_for=()):
        self.requests, self.fail_for = [], fail_for

    def call(self, method, params):
        assert method == "getCandleData" and params["exchange"] == "NSE" and params["symboltoken"] == SPOT_TOKEN
        self.requests.append(params)
        if params["interval"] in self.fail_for:
            raise ConnectionError("candle API down")
        fmt = "%Y-%m-%d %H:%M"
        t = datetime.strptime(params["fromdate"], fmt).replace(tzinfo=IST).timestamp()
        end = datetime.strptime(params["todate"], fmt).replace(tzinfo=IST).timestamp()
        rows = []
        while t <= end:
            weekend = datetime.fromtimestamp(t, IST).weekday() >= 5
            if not (params["interval"] == "ONE_DAY" and weekend):  # the real daily endpoint only returns trading days
                rows.append([datetime.fromtimestamp(t, IST).isoformat(), 1, 2, 0.5, 1.5, 9])
            t += STEP[params["interval"]]
        return {"status": True, "data": rows}

    def first_from(self):
        out = {}
        for r in self.requests:
            out.setdefault(r["interval"], r["fromdate"])
        return out


def daily(day):
    s = ist(0, 0, day=day)
    return Candle(s, s + 86400, 23000, 24000, 22900, 23500, 0, source="db")


def db_hist(*days):
    """What Prism DB holds: full sessions for the given days, every timeframe, plus the daily candles."""
    return {**{tf: [c for d in days for c in session_candles(d, secs)] for tf, secs in SECS.items()}, "1d": [daily(d) for d in days]}


def test_fill_gap_resumes_at_the_next_interval_of_each_timeframe():
    hist, now, sess = db_hist(THU), ist(10, 0, 30, day=FRI), AngelHistory()
    history.fill_gap(sess, SPOT_TOKEN, hist, now, sleep=lambda s: None)
    assert sess.first_from() == {"ONE_MINUTE": "2026-11-06 09:15", "FIVE_MINUTE": "2026-11-06 09:15", "FIFTEEN_MINUTE": "2026-11-06 09:15"}
    # no daily request: today's daily candle is still forming and would be thrown away, and a refused request would mark the day stale
    sess = AngelHistory()
    missing = db_hist(THU)
    missing["1d"] = [daily((2026, 11, 3))]  # Prism DB lacks yesterday's daily candle: that one is asked for (and kept)
    history.fill_gap(sess, SPOT_TOKEN, missing, ist(10, 0, 30, day=FRI), sleep=lambda s: None)
    assert sess.first_from()["ONE_DAY"] == "2026-11-04 00:00" and [r["fromdate"] for r in sess.requests if r["interval"] == "ONE_DAY"] == ["2026-11-04 00:00"]
    # a resume point in the middle of a session starts at the very next minute, not at the day's open
    sess = AngelHistory()
    mid = {"1m": [c for c in session_candles(FRI) if c.start <= ist(9, 59, day=FRI)], "5m": [], "15m": [], "1d": []}
    history.fill_gap(sess, SPOT_TOKEN, mid, ist(11, 0, day=FRI), sleep=lambda s: None)
    assert sess.first_from()["ONE_MINUTE"] == "2026-11-06 10:00"


def test_fill_gap_keeps_only_completed_candles_and_never_repeats_itself():
    hist, now, sess = db_hist(THU), ist(10, 0, 30, day=FRI), AngelHistory()
    added = history.fill_gap(sess, SPOT_TOKEN, hist, now, sleep=lambda s: None)
    m1 = hist["1m"]
    assert added > 0 and [c.start for c in m1] == sorted({c.start for c in m1})  # ordered, one per start
    assert m1[-1].start == ist(9, 59, day=FRI) and all(c.end <= now for c in m1)  # the forming 10:00 minute is not included
    assert all(c.source == "db" for c in m1[:375]) and all(c.source == "angel" for c in m1[375:])
    assert [datetime.fromtimestamp(c.start, IST).date() for c in hist["1d"]] == [date(2026, 11, 5)]  # today's daily is not complete
    again = AngelHistory()
    assert history.fill_gap(again, SPOT_TOKEN, hist, now, sleep=lambda s: None) == 0  # nothing new, nothing duplicated
    assert again.first_from()["ONE_MINUTE"] == "2026-11-06 10:00"  # it only asks for what comes after what it holds


def test_fill_gap_is_chunked_and_bounded():
    empty = {"1m": [], "5m": [], "15m": [], "1d": []}
    sess = AngelHistory()
    history.fill_gap(sess, SPOT_TOKEN, empty, ist(10, 0, day=(2026, 12, 21)), sleep=lambda s: None)  # nothing held at all
    assert datetime.strptime(sess.first_from()["ONE_MINUTE"], "%Y-%m-%d %H:%M") == datetime(2026, 12, 16, 9, 15)  # five days back
    old = {"1m": [Candle(ist(15, 29, day=THU), ist(15, 30, day=THU), 1, 2, 0.5, 1.5, 0)], "5m": [], "15m": [], "1d": []}
    sess = AngelHistory()
    history.fill_gap(sess, SPOT_TOKEN, old, ist(10, 0, 30, day=(2026, 12, 30)), sleep=lambda s: None)  # eight weeks later
    windows = [r for r in sess.requests if r["interval"] == "ONE_MINUTE"]
    parse = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
    assert parse(windows[0]["fromdate"]) >= datetime(2026, 12, 2)  # clamped to the last 28 days
    assert all((parse(r["todate"]) - parse(r["fromdate"])).days <= 5 for r in windows) and len(windows) == 6  # small windows


def test_fill_gap_collects_failures_and_carries_on():
    hist, errors = db_hist(THU), []
    sess = AngelHistory(fail_for={"FIVE_MINUTE"})
    added = history.fill_gap(sess, SPOT_TOKEN, hist, ist(10, 0, 30, day=FRI), sleep=lambda s: None, errors=errors)
    assert added > 0 and len(errors) == 1 and errors[0].startswith("5m 2026-11-06 09:15")
    assert len(hist["5m"]) == 75 and len(hist["1m"]) > 375  # the other timeframes were still filled


class Refusing(AngelHistory):
    """Angel One's rate limit: the first `refusals` requests are refused with its real message, then it answers."""

    def __init__(self, refusals):
        super().__init__()
        self.refusals = refusals

    def call(self, method, params):
        if self.refusals > 0:
            self.refusals -= 1
            self.requests.append(params)
            raise ValueError("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")
        return super().call(method, params)


def test_a_request_refused_for_going_too_fast_is_tried_again_after_a_pause():
    hist, errors, sleeps, sess = db_hist(THU), [], [], Refusing(refusals=2)
    added = history.fill_gap(sess, SPOT_TOKEN, hist, ist(10, 0, 30, day=FRI), sleep=sleeps.append, errors=errors)
    assert errors == [] and added > 0  # two refusals in a row, the third try went through: nothing is marked stale
    assert sleeps[:2] == [history.RETRY_PAUSE, history.RETRY_PAUSE] and len(sess.requests) == 2 + 3  # the same 1M request three times, then 5M and 15M


def test_a_request_that_keeps_being_refused_is_reported_after_its_retries():
    errors, sess = [], Refusing(refusals=10**6)
    history.fill_gap(sess, SPOT_TOKEN, db_hist(THU), ist(10, 0, 30, day=FRI), sleep=lambda s: None, errors=errors)
    assert len(errors) == 3 and all("exceeding access rate" in e for e in errors)  # 1M, 5M, 15M, each after 1 + RETRIES tries
    assert len(sess.requests) == 3 * (1 + history.RETRIES)


def test_the_refresh_keeps_trying_for_two_hours_not_thirty_minutes():
    assert history.MAX_ATTEMPTS * history.RETRY_SECONDS >= 2 * 3600


# -- refresh ---------------------------------------------------------------------------------------------------------
def make_md(t):
    clock = Clock(t)
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), date(2026, 11, 6), 24510.0)
    return md, clock


def loader_of(*days):
    return lambda cfg, now: db_hist(*days)


def refresh(md, clock, sess, loader):
    return history.refresh(PG, md, sess, now=clock, sleep=lambda s: None, loader=loader)


def test_a_successful_refresh_makes_the_history_current_and_updates_previous_day_levels():
    md, clock = make_md(ist(10, 0, 30, day=FRI))
    assert md.analysis()["available"] is False  # nothing loaded yet
    assert refresh(md, clock, AngelHistory(), loader_of(THU)) is True
    assert md.history_date == date(2026, 11, 6) and md.history_loaded_at == clock.t
    assert md.history_status == {"state": "ready", "error": None}
    a = md.analysis()
    assert a["history"]["stale"] is False and a["history"]["as_of_date"] == "2026-11-06"
    assert a["reference"]["previous_day"]["date"] == "2026-11-05"  # on Friday the previous day is Thursday

    clock.t = ist(9, 20, day=MON)  # the next trading session: Prism DB now holds Friday too
    assert md.analysis()["history"]["stale"] is True  # new session, history is from Friday: flagged until refreshed
    assert refresh(md, clock, AngelHistory(), loader_of(THU, FRI)) is True
    a = md.analysis()
    assert a["history"]["stale"] is False and a["history"]["as_of_date"] == "2026-11-09" and a["history"]["loaded_at"] == clock.t
    assert a["reference"]["previous_day"]["date"] == "2026-11-06"  # previous-day levels moved to Friday
    assert a["history"]["coverage"]["1d"]["last"] == ist(0, 0, day=FRI)


def test_refresh_merges_history_with_live_candles_without_duplicates():
    md, clock = make_md(ist(9, 40, 30, day=MON))
    for minute in range(0, 6):  # live ticks 09:30-09:35: the candles 09:30..09:34 complete
        clock.t = ist(9, 30, day=MON) + 60 * minute
        md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": 2450000 + minute,
                       "exchange_timestamp": int(clock.t * 1000)})  # fmt: skip
    clock.t = ist(9, 40, 30, day=MON)
    assert refresh(md, clock, AngelHistory(), loader_of(FRI)) is True  # the backfill returns Monday 09:15-09:39 as well
    merged = completed_series(md.history, md.builder, SPOT_TOKEN, "1m")
    starts = [c.start for c in merged]
    assert starts == sorted(set(starts))  # one candle per start, in order
    monday = [c for c in merged if c.start >= ist(0, 0, day=MON)]
    assert [hhmm(c.start) for c in monday][:3] == ["09:15", "09:16", "09:17"] and len(monday) == 25  # 09:15..09:39
    assert {c.source for c in monday if hhmm(c.start) in ("09:30", "09:31")} == {"angel"}  # history wins over the live copy


def test_first_load_shows_the_db_data_while_the_backfill_runs_without_sharing_mutable_lists():
    md, clock = make_md(ist(10, 0, 30, day=FRI))
    seen = {}

    class Probe(AngelHistory):
        def call(self, method, params):
            seen.setdefault("state", md.history_status["state"])
            seen.setdefault("early", md.history)
            seen.setdefault("early_len", len(md.history["1m"]))
            return super().call(method, params)

    assert refresh(md, clock, Probe(), loader_of(THU))
    assert seen["state"] == "db_loaded" and seen["early_len"] == 375  # the analysis could already use the DB data
    assert len(seen["early"]["1m"]) == 375 and md.history is not seen["early"] and len(md.history["1m"]) > 375  # not mutated underneath readers


def test_angel_candles_are_carried_over_so_they_are_not_downloaded_again():
    md, clock = make_md(ist(15, 45, day=FRI))
    assert refresh(md, clock, AngelHistory(), loader_of(THU))  # the DB lags: Thursday only, Friday comes from Angel
    clock.t = ist(9, 20, day=MON)
    sess = AngelHistory()
    assert refresh(md, clock, sess, loader_of(THU))  # the DB is still behind
    assert sess.first_from()["ONE_MINUTE"] == "2026-11-07 09:15"  # resumes after the carried Friday; Friday is not asked again
    friday = [c for c in md.history["1m"] if ist(0, 0, day=FRI) <= c.start < ist(0, 0, day=MON)]
    assert len(friday) == 375 and all(c.source == "angel" for c in friday)  # kept from the earlier refresh
    starts = [c.start for c in md.history["1m"]]
    assert starts == sorted(set(starts))


def test_a_failed_refresh_keeps_the_old_history_and_says_it_is_stale():
    md, clock = make_md(ist(10, 0, 30, day=FRI))
    assert refresh(md, clock, AngelHistory(), loader_of(THU))
    old = md.history
    clock.t = ist(9, 20, day=MON)

    def down(cfg, now):
        raise ConnectionError("connection refused")

    assert refresh(md, clock, AngelHistory(), down) is False
    assert md.history is old and md.history_date == date(2026, 11, 6)  # kept, and not marked as refreshed
    assert md.history_status["state"] == "stale" and "Prism DB: ConnectionError: connection refused" in md.history_status["error"]
    assert any("connection refused" in e["message"] for e in md.errors)
    a = md.analysis()
    assert a["available"] is True and a["reliable"] is False and a["history"]["stale"] is True
    assert any("last refreshed for 2026-11-06" in w and "refresh failed: Prism DB: ConnectionError" in w for w in a["warnings"])
    assert a["reference"]["previous_day"]["stale"] is True  # never presented as current


def test_a_failed_first_load_is_reported_not_hidden():
    md, clock = make_md(ist(10, 0, 30, day=FRI))

    def down(cfg, now):
        raise RuntimeError("PG_PASSWORD is not set; cannot read Prism DB")

    assert refresh(md, clock, AngelHistory(), down) is False
    a = md.analysis()
    assert a["available"] is False and a["reliable"] is False
    assert md.history_status["state"] == "error" and "PG_PASSWORD" in a["warnings"][0]


def test_an_incomplete_backfill_is_reported_and_leaves_the_history_due_for_another_try():
    md, clock = make_md(ist(10, 0, 30, day=FRI))
    assert refresh(md, clock, AngelHistory(fail_for={"FIVE_MINUTE"}), loader_of(THU)) is False
    assert md.history_status["state"] == "ready" and "backfill incomplete" in md.history_status["error"]
    assert md.history_date is None  # not marked current
    assert history.refresh_due(md, clock.t + 400, attempts=1, last_try=clock.t)  # retried later
    assert md.analysis()["history"]["stale"] is True


def test_no_spot_token_means_the_backfill_is_skipped_and_reported():
    clock = Clock(ist(10, 0, 30, day=FRI))
    md, sleeps = MarketData(CFG, clock), []  # no contracts selected yet
    assert history.refresh(PG, md, AngelHistory(), now=clock, sleep=sleeps.append, loader=loader_of(THU)) is False
    assert len(sleeps) == 150 and "no spot token" in md.history_status["error"]  # it waited, then gave up and said so
    assert md.history is not None  # the DB data is still usable


# -- when a refresh is due -------------------------------------------------------------------------------------------
def due(hist, hist_date, now, attempts=0, last_try=None):
    return history.refresh_due(SimpleNamespace(history=hist, history_date=hist_date), now, attempts, last_try)


def test_refresh_due_rules():
    thu, fri = date(2026, 11, 5), date(2026, 11, 6)
    some, sat = {"1m": []}, (2026, 11, 7)
    assert due(None, None, ist(3, 0, day=FRI))  # startup, any time
    assert not due(None, None, ist(3, 0, day=FRI), attempts=history.MAX_ATTEMPTS)  # but not forever
    assert not due(None, None, ist(3, 0, day=FRI), attempts=1, last_try=ist(3, 0, day=FRI) - 299)  # retry spacing
    assert due(None, None, ist(3, 0, day=FRI), attempts=1, last_try=ist(3, 0, day=FRI) - 300)
    assert not due(some, fri, ist(10, 0, day=FRI))  # already refreshed for today
    assert not due(some, thu, ist(8, 0, day=FRI))  # a new day, but the session has not started
    assert not due(some, thu, ist(9, 14, 59, day=FRI)) and due(some, thu, ist(9, 15, day=FRI))  # it starts at 09:15
    assert not due(some, fri, ist(10, 0, day=sat))  # Saturday: no session
    assert due(some, fri, ist(9, 20, day=MON))
    assert not due(some, fri, ist(9, 20, day=MON), attempts=1, last_try=ist(9, 20, day=MON) - 100)
    assert due(some, fri, ist(9, 25, day=MON), attempts=1, last_try=ist(9, 20, day=MON))


def test_session_started_and_ist_date():
    assert history.session_started(ist(9, 15, day=FRI)) and not history.session_started(ist(9, 14, 59, day=FRI))
    assert history.session_started(ist(20, 0, day=FRI)) and not history.session_started(ist(12, 0, day=(2026, 11, 8)))
    assert history.ist_date(ist(0, 30, day=FRI)) == date(2026, 11, 6)  # just after midnight IST is still Friday in IST
    assert history.ist_date(ist(0, 30, day=FRI) - 3600) == date(2026, 11, 5)


# -- the refresh loop ------------------------------------------------------------------------------------------------
def run_loop(start, end, outcomes=None):
    """Poll every 30 s from `start` to `end` on a fake clock. `outcomes(n, now)` scripts the n-th database read."""
    clock = Clock(start)
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), date(2026, 11, 6), 24510.0)
    stop, calls = threading.Event(), []

    def loader(cfg, now):
        calls.append(now)
        if outcomes is not None and not outcomes(len(calls), now):
            raise ConnectionError("db down")
        return db_hist(THU)

    def wait(secs):
        clock.t += secs
        if clock.t >= end:
            stop.set()

    history.run(PG, md, AngelHistory(), stop, now=clock, wait=wait, loader=loader, sleep=lambda s: None)
    return md, calls


def test_the_loop_reads_the_clock_all_the_time_but_queries_the_database_only_when_due():
    md, calls = run_loop(ist(8, 0, day=FRI), ist(10, 0, day=TUE))
    # Fri 08:00 startup load; Mon 09:15 and Tue 09:15 new sessions; nothing in between (about 11,000 polls)
    assert [when(t) for t in calls] == ["Fri 08:00", "Mon 09:15", "Tue 09:15"]
    assert md.history_date == date(2026, 11, 10)


def test_the_loop_retries_a_failed_refresh_and_then_stops():
    md, calls = run_loop(ist(8, 0, day=FRI), ist(12, 0, day=MON), outcomes=lambda n, now: n not in (2, 3))  # the 2nd and 3rd reads fail
    assert [when(t) for t in calls] == ["Fri 08:00", "Mon 09:15", "Mon 09:20", "Mon 09:25"]
    assert md.history_date == date(2026, 11, 9) and md.history_status["error"] is None


def test_the_loop_gives_up_after_the_daily_attempt_limit_and_keeps_reporting_it():
    md, calls = run_loop(ist(8, 0, day=FRI), ist(23, 0, day=MON), outcomes=lambda n, now: n == 1)  # only the startup load works
    assert len(calls) == 1 + history.MAX_ATTEMPTS  # then no more that day, however long the database stays down
    assert md.history_date == date(2026, 11, 6) and md.history_status["state"] == "stale"
    md.clock = Clock(ist(23, 0, day=MON))
    a = md.analysis()
    assert a["history"]["stale"] is True and a["reliable"] is False  # still reported, never silently stale


def test_a_crash_inside_a_refresh_does_not_kill_the_loop(monkeypatch):
    clock, stop, n = Clock(ist(8, 0, day=FRI)), threading.Event(), []
    md = MarketData(CFG, clock)

    def boom(*a, **k):
        n.append(1)
        raise KeyError("unexpected")

    monkeypatch.setattr(history, "refresh", boom)

    def wait(secs):
        clock.t += 400  # past the retry spacing every time
        if len(n) >= 3:
            stop.set()

    history.run(PG, md, AngelHistory(), stop, now=clock, wait=wait)
    assert len(n) == 3 and "unexpected" in md.history_status["error"]
