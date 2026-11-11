"""What every candle request answered is in the log (history backfill and live gap recovery).

Angel One's answers used to be invisible: a request that came back with no candles, or with candles outside the session, looked exactly like success, and a refused request
that went through on its third try left no trace at all. Logging only: nothing here changes what is fetched, kept or decided."""

import logging
from datetime import datetime

from helpers import CFG, FRI, SPOT_TOKEN, THU, ist, msg, sample_contracts
from test_gaps import HistorySession
from test_history import AngelHistory, Refusing, db_hist
from test_operational_reliability import WorldApi, fresh_md, world_db

from app import history
from app.candles import IST
from app.engine import MarketData
from app.gaps import describe_span, recover


def lines(caplog, logger, start):
    return [r for r in caplog.records if r.name == logger and r.getMessage().startswith(start)]


def run_fill(session, now=None):
    return history.fill_gap(session, SPOT_TOKEN, db_hist(THU), now or ist(10, 0, 30, day=FRI), sleep=lambda s: None)


# -- the history backfill ------------------------------------------------------------------------------------------
def test_every_backfill_request_says_what_was_asked_what_came_back_and_what_was_kept(caplog):
    caplog.set_level(logging.INFO, logger="app.history")
    run_fill(AngelHistory())
    got = {r.getMessage().split()[1]: r for r in lines(caplog, "app.history", "backfill ")}
    assert set(got) == {"1m", "5m", "15m"}  # one line per window, nothing for the daily candle (today's is still forming)
    assert got["1m"].getMessage() == "backfill 1m 2026-11-06 09:15..2026-11-06 10:00: answered on try 1 with 46 candle(s) (11-06 09:15..11-06 10:00), 46 inside the session, 45 new kept"
    assert got["5m"].getMessage() == "backfill 5m 2026-11-06 09:15..2026-11-06 10:00: answered on try 1 with 10 candle(s) (11-06 09:15..11-06 10:00), 10 inside the session, 9 new kept"
    assert got["15m"].getMessage() == "backfill 15m 2026-11-06 09:15..2026-11-06 10:00: answered on try 1 with 4 candle(s) (11-06 09:15..11-06 10:00), 4 inside the session, 3 new kept"
    assert all(r.levelno == logging.INFO for r in got.values())  # an ordinary answer is not a warning


def test_a_request_that_went_through_after_refusals_says_on_which_try(caplog):
    caplog.set_level(logging.INFO, logger="app.history")
    run_fill(Refusing(refusals=2))
    got = {r.getMessage().split()[1]: r.getMessage() for r in lines(caplog, "app.history", "backfill ")}
    assert "answered on try 3 with" in got["1m"]  # refused twice, answered on the third: that is no longer invisible
    assert "answered on try 1 with" in got["5m"] and "answered on try 1 with" in got["15m"]


def test_an_answer_with_no_candles_is_a_warning_not_a_quiet_success(caplog):
    class Empty(AngelHistory):
        def call(self, method, params):
            self.requests.append(params)
            return {"status": True, "message": "SUCCESS", "data": []}

    caplog.set_level(logging.INFO, logger="app.history")
    errors = []
    added = history.fill_gap(Empty(), SPOT_TOKEN, db_hist(THU), ist(10, 0, 30, day=FRI), sleep=lambda s: None, errors=errors)
    assert added == 0 and errors == []  # unchanged: an empty answer is still not an error
    got = lines(caplog, "app.history", "backfill ")
    assert len(got) == 3 and all(r.levelno == logging.WARNING for r in got)
    assert all("answered on try 1 with 0 candle(s) (none), 0 inside the session, 0 new kept" in r.getMessage() for r in got)


def test_candles_outside_the_session_are_counted_apart_from_the_ones_inside(caplog):
    class BeforeTheOpen(AngelHistory):
        def call(self, method, params):
            self.requests.append(params)
            day = params["fromdate"][:10]
            return {"status": True, "data": [[f"{day}T06:{m:02d}:00+05:30", 1, 2, 0.5, 1.5, 9] for m in range(5)]}  # 06:00-06:04 IST: not a trading time

    caplog.set_level(logging.INFO, logger="app.history")
    assert history.fill_gap(BeforeTheOpen(), SPOT_TOKEN, db_hist(THU), ist(10, 0, 30, day=FRI), sleep=lambda s: None) == 0
    got = [r.getMessage() for r in lines(caplog, "app.history", "backfill ")]
    assert len(got) == 3 and all("answered on try 1 with 5 candle(s) (11-06 06:00..11-06 06:04), 0 inside the session, 0 new kept" in m for m in got)


def test_the_candles_already_held_are_not_counted_as_new(caplog):
    caplog.set_level(logging.INFO, logger="app.history")
    hist, now = db_hist(THU), ist(10, 0, 30, day=FRI)
    history.fill_gap(AngelHistory(), SPOT_TOKEN, hist, now, sleep=lambda s: None)
    caplog.clear()
    history.fill_gap(AngelHistory(), SPOT_TOKEN, hist, now, sleep=lambda s: None)  # the same candles again: it resumes after what it holds, so asks only for the forming one
    got = [r.getMessage() for r in lines(caplog, "app.history", "backfill ")]
    assert got and all(m.endswith(", 0 new kept") for m in got)


def test_a_window_that_failed_is_still_reported_as_a_failure_and_not_as_an_answer(caplog):
    caplog.set_level(logging.INFO, logger="app.history")
    errors = []
    history.fill_gap(Refusing(refusals=10**6), SPOT_TOKEN, db_hist(THU), ist(10, 0, 30, day=FRI), sleep=lambda s: None, errors=errors)
    assert len(errors) == 3  # unchanged
    assert not [r for r in caplog.records if "answered on try" in r.getMessage()]  # a window that never answered is not described as an answer
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and "failed (rate_limit)" in r.getMessage()]) == 3  # the existing warning is still there


def test_logging_changes_nothing_that_is_fetched_or_kept():
    a, b = db_hist(THU), db_hist(THU)
    first, second = AngelHistory(), AngelHistory()
    assert history.fill_gap(first, SPOT_TOKEN, a, ist(10, 0, 30, day=FRI), sleep=lambda s: None) == history.fill_gap(second, SPOT_TOKEN, b, ist(10, 0, 30, day=FRI), sleep=lambda s: None)
    assert first.requests == second.requests
    assert {tf: [(c.start, c.source) for c in cs] for tf, cs in a.items()} == {tf: [(c.start, c.source) for c in cs] for tf, cs in b.items()}


# -- a refresh that fails validation: what is held, next to what is missing ------------------------------------------------
def test_a_refresh_that_fails_validation_says_what_the_history_holds_for_today(caplog):
    caplog.set_level(logging.INFO, logger="app.history")
    w, md, clock = fresh_md()
    api = WorldApi(w, skip=("10:35",))  # Angel One answers, but without one 5M candle inside the decision window
    assert history.refresh(md=md, cfg=CFG, session=api, now=clock, sleep=lambda s: None, loader=lambda cfg, now: world_db(w)) is False
    held = lines(caplog, "app.history", "history holds for today:")
    assert len(held) == 1
    text = held[0].getMessage()
    assert "1m " in text and "5m " in text and "15m " in text and "(none)" not in text.split("1m ")[1].split(",")[0]  # the 1M candles are there
    assert held[0].levelno == logging.INFO
    assert any("candle validation failed" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)  # the existing warning is untouched


def test_a_refresh_that_is_complete_does_not_add_the_held_line(caplog):
    caplog.set_level(logging.INFO, logger="app.history")
    w, md, clock = fresh_md()
    assert history.refresh(md=md, cfg=CFG, session=WorldApi(w), now=clock, sleep=lambda s: None, loader=lambda cfg, now: world_db(w)) is True
    assert not lines(caplog, "app.history", "history holds for today:")


def test_held_today_counts_only_todays_candles_per_timeframe():
    hist = db_hist(THU)
    hist["1m"] = hist["1m"][:3]  # 09:15, 09:16, 09:17 on Thursday
    out = history.held_today(hist, ist(10, 0, day=THU))
    assert out == "1m 3 (11-05 09:15..11-05 09:17), 5m 75 (11-05 09:15..11-05 15:25), 15m 25 (11-05 09:15..11-05 15:15)"
    assert history.held_today(hist, ist(10, 0, day=FRI)) == "1m 0 (none), 5m 0 (none), 15m 0 (none)"  # nothing of today's yet


def test_describe_span_gives_the_first_and_last_start_in_ist_or_none():
    assert describe_span([]) == "none"
    assert describe_span([ist(9, 20, day=FRI), ist(9, 15, day=FRI), ist(11, 0, day=FRI)]) == "11-06 09:15..11-06 11:00"  # order does not matter
    assert describe_span([ist(9, 15, day=FRI)]) == "11-06 09:15..11-06 09:15"
    assert datetime.fromtimestamp(ist(9, 15, day=FRI), IST).hour == 9  # (the clock the helper prints in)


# -- live gap recovery ---------------------------------------------------------------------------------------------------
def gap_md():
    clock = type("C", (), {"t": ist(10, 0), "__call__": lambda self: self.t})()
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), datetime(2026, 11, 6).date(), 24510.0)
    for ts in (ist(10, 0, 0), ist(10, 0, 10), ist(10, 3, 30)):  # the connection was down 10:00:10 -> 10:03:30
        clock.t = ts
        md.on_message(msg(SPOT_TOKEN, 24500 + ts % 7, ts))
    clock.t = ist(10, 5)
    return md


def test_recovery_says_what_it_asked_for_what_the_answer_held_and_what_it_filled_in(caplog):
    caplog.set_level(logging.INFO, logger="app.gaps")
    md = gap_md()
    assert recover(HistorySession(), md, sleep=lambda s: None) > 0
    asked = lines(caplog, "app.gaps", "recovery ")
    texts = [r.getMessage() for r in asked]
    answer = [t for t in texts if "the answer held" in t]
    filled = [t for t in texts if "filled in" in t]
    assert answer and filled
    assert answer == ["recovery Nifty 50 1m 2026-11-06 10:00..2026-11-06 10:02: asked for 3 candle(s), the answer held 3 (11-06 10:00..11-06 10:02), 3 of them asked for"]
    assert filled == ["recovery Nifty 50 1m: 3 of 3 candle(s) filled in"]
    assert all(r.levelno == logging.INFO for r in asked)


def test_recovery_counts_differ_when_the_answer_is_not_what_was_asked_for(caplog):
    """The counts are three different numbers: asked for 3, an answer holding 4 (two of them not asked for, one asked-for candle missing), 2 filled in."""

    class Partial:
        def call(self, method, params):
            rows = [f"2026-11-06T{h:02d}:{m:02d}:00+05:30" for h, m in ((9, 58), (9, 59), (10, 0), (10, 2))]  # no 10:01, and 09:58 / 09:59 were not asked for
            return {"status": True, "data": [[ts, 1, 2, 0.5, 1.5, 77] for ts in rows]}

    caplog.set_level(logging.INFO, logger="app.gaps")
    assert recover(Partial(), gap_md(), sleep=lambda s: None) == 2
    texts = [r.getMessage() for r in lines(caplog, "app.gaps", "recovery ")]
    assert "recovery Nifty 50 1m 2026-11-06 10:00..2026-11-06 10:02: asked for 3 candle(s), the answer held 4 (11-06 09:58..11-06 10:02), 2 of them asked for" in texts
    assert "recovery Nifty 50 1m: 2 of 3 candle(s) filled in" in texts


def test_a_recovery_answer_with_no_candles_is_a_warning(caplog):
    class Empty:
        def call(self, method, params):
            return {"status": True, "data": []}

    caplog.set_level(logging.INFO, logger="app.gaps")
    md = gap_md()
    assert recover(Empty(), md, sleep=lambda s: None) == 0  # unchanged
    got = [r for r in lines(caplog, "app.gaps", "recovery ") if "the answer held 0 (none)" in r.getMessage()]
    assert got and all(r.levelno == logging.WARNING for r in got)


def test_a_failed_recovery_request_still_logs_only_its_failure(caplog):
    class Down:
        def call(self, *a):
            raise ConnectionError("api down")

    caplog.set_level(logging.INFO, logger="app.gaps")
    assert recover(Down(), gap_md(), sleep=lambda s: None) == 0
    assert not [r for r in caplog.records if "the answer held" in r.getMessage()]  # there was no answer to describe
    assert any("candle recovery failed" in r.getMessage() for r in caplog.records)

