"""Merging history with live candles, 1H aggregation, missing intervals and contiguity.

Expected values are worked out by hand from the NSE session (09:15-15:30 IST = 375 minutes)."""

import math

from helpers import FRI, MON, SPOT_TOKEN, THU, TUE, WED, hhmm, ist, session_candles, tick

from app import series
from app.candles import Candle, CandleBuilder


def c(h, m, secs=900, day=FRI, o=1.0, hi=2.0, lo=0.5, close=1.5, vol=10, source="db", partial=False):
    s = ist(h, m, day=day)
    return Candle(s, s + secs, o, hi, lo, close, vol, source=source, partial=partial)


# -- aggregation ---------------------------------------------------------------------------------------------
def test_aggregate_15m_into_1h_anchored_at_0915():
    cs = [c(9, 15, o=10, hi=12, lo=9, close=11, vol=5), c(9, 30, o=11, hi=15, lo=10, close=14, vol=6),
          c(9, 45, o=14, hi=14, lo=8, close=9, vol=7), c(10, 0, o=9, hi=13, lo=9, close=12, vol=8)]  # fmt: skip
    (h,) = series.aggregate(cs, 3600, 900)
    assert (h.start, h.end) == (ist(9, 15), ist(10, 15))
    assert (h.open, h.high, h.low, h.close, h.volume) == (10, 15, 8, 12, 26)  # first open, extremes, last close, sum
    assert h.source == "aggregated"


def test_aggregate_never_builds_an_hour_from_missing_constituents():
    assert series.aggregate([c(9, 15), c(9, 30), c(9, 45)], 3600, 900) == []  # the 10:00 candle is missing
    assert series.aggregate([c(9, 15), c(9, 30), c(10, 0)], 3600, 900) == []  # the 09:45 candle is missing
    two_hours = series.aggregate([c(9, 15), c(9, 30), c(9, 45), c(10, 0), c(10, 15), c(10, 45)], 3600, 900)
    assert [x.start for x in two_hours] == [ist(9, 15)]  # the 10:15 hour lacks its 10:30 and 11:00 candles


def test_last_hour_of_the_session_is_a_single_15m_candle():
    (h,) = series.aggregate([c(15, 15, o=1, hi=5, lo=0, close=4, vol=3)], 3600, 900)
    assert (h.start, h.end) == (ist(15, 15), ist(15, 30)) and h.close == 4


def test_aggregate_5m_into_15m_and_ignores_duplicates_and_off_session_candles():
    five = [Candle(ist(9, 15) + 300 * i, ist(9, 15) + 300 * i + 300, i, i + 1, i - 1, i, 1) for i in range(3)]
    (q,) = series.aggregate(five + [five[0], five[1]], 900, 300)  # duplicated inputs change nothing
    assert (q.open, q.high, q.low, q.close, q.volume) == (0, 3, -1, 2, 3)
    assert series.aggregate([c(8, 0), c(16, 0), c(10, 0, day=(2026, 11, 7))], 3600, 900) == []  # pre-open, post-close, Saturday


# -- merging history with live candles -----------------------------------------------------------------------
def live_builder(ticks):
    b = CandleBuilder([SPOT_TOKEN])
    for ts, px in ticks:
        b.on_tick(tick(SPOT_TOKEN, px, ts))
    return b


def test_merge_gives_one_candle_per_start_history_wins_and_live_fills_gaps():
    # live: first tick exactly at 09:30 -> the 09:30 and 09:45 candles are complete; 10:00 is still forming
    b = live_builder([(ist(9, 30), 1), (ist(9, 45), 2), (ist(10, 0), 3)])
    hist = {"15m": [c(9, 15, close=7), c(9, 30, close=7)]}  # history overlaps the live 09:30 candle
    out = series.completed_series(hist, b, SPOT_TOKEN, "15m")
    assert [x.start for x in out] == [ist(9, 15), ist(9, 30), ist(9, 45)]  # ordered, no duplicates, forming 10:00 left out
    assert [(x.source, x.close) for x in out] == [("db", 7), ("db", 7), ("live", 2)]  # history wins the tie


def test_partial_live_candles_are_never_used():
    b = live_builder([(ist(9, 30, 30), 1), (ist(9, 45), 2), (ist(10, 0), 3)])  # first tick mid-candle: 09:30 is partial
    assert b.closed[(SPOT_TOKEN, "15m")][0].partial
    out = series.completed_series({"15m": [c(9, 15)]}, b, SPOT_TOKEN, "15m")
    assert [x.start for x in out] == [ist(9, 15), ist(9, 45)]  # 09:30 is missing rather than wrong


def test_1h_is_built_from_the_merged_15m_series_across_the_history_live_boundary():
    b = live_builder([(ist(10, 0), 12), (ist(10, 15), 13)])  # live closes the 10:00 15M candle (one tick, at 12)
    hist = {"15m": [c(9, 15, o=10, hi=12, lo=9, close=11), c(9, 30, o=11, hi=15, lo=10, close=14), c(9, 45, o=14, hi=14, lo=8, close=9)]}
    (h,) = series.completed_series(hist, b, SPOT_TOKEN, "1h")
    assert (h.start, h.high, h.low, h.source) == (ist(9, 15), 15, 8, "aggregated")  # 3 history + 1 live constituent
    assert (h.open, h.close) == (10, 12)  # first history open, and the live 10:00 candle's only tick


def test_the_builders_own_1h_candles_are_not_used():
    ticks = [(ist(9, 15), 50), (ist(9, 30), 51), (ist(9, 45), 52), (ist(10, 0), 53), (ist(10, 15), 54)]
    b = live_builder(ticks)
    assert b.closed[(SPOT_TOKEN, "1h")]  # the builder did close a 1h candle...
    assert series.completed_series({"15m": []}, b, SPOT_TOKEN, "1h")  # ...from live 15M candles, aggregated again
    out = series.completed_series({"15m": [c(9, 15, close=9), c(9, 30, close=9), c(9, 45, close=9), c(10, 0, close=9)]}, b, SPOT_TOKEN, "1h")
    assert [(x.close, x.source) for x in out] == [(9, "aggregated")]  # history 15M wins; a single source of 1H


# -- missing intervals ---------------------------------------------------------------------------------------------
AFTER_CLOSE = ist(16, 0, day=MON)


def test_a_complete_session_has_nothing_missing():
    m = series.missing_intervals(session_candles(FRI), 60, AFTER_CLOSE)
    assert (m["expected"], m["missing"], m["runs"]) == (375, 0, [])


def test_missing_intervals_are_counted_and_grouped_into_runs():
    cs = session_candles(FRI, drop={"09:20", "09:21", "11:00"})
    m = series.missing_intervals(cs, 60, AFTER_CLOSE)
    assert (m["missing"], m["latest_session_missing"], m["expected"]) == (3, 3, 375)
    assert [(hhmm(r["start"]), hhmm(r["end"]), r["count"]) for r in m["runs"]] == [("09:20", "09:22", 2), ("11:00", "11:01", 1)]


def test_the_session_edges_are_missing_intervals_too():
    friday = session_candles(FRI, drop={f"09:{m}" for m in range(15, 20)} | {f"15:{m}" for m in range(20, 30)})
    m = series.missing_intervals(session_candles(THU) + friday, 60, AFTER_CLOSE)  # Friday: first 5 and last 10 minutes absent
    assert [(hhmm(r["start"]), hhmm(r["end"]), r["count"]) for r in m["runs"]] == [("09:15", "09:20", 5), ("15:20", "15:30", 10)]
    assert (m["missing"], m["latest_session_missing"]) == (15, 15)


def test_the_first_candle_of_the_window_defines_where_it_starts():
    only_friday = session_candles(FRI, drop={f"09:{m}" for m in range(15, 20)})  # nothing earlier to measure against
    assert series.missing_intervals(only_friday, 60, AFTER_CLOSE)["missing"] == 0


def test_no_false_gap_across_the_overnight_break():
    cs = session_candles(THU) + session_candles(FRI)
    m = series.missing_intervals(cs, 60, AFTER_CLOSE)
    assert (m["expected"], m["missing"]) == (750, 0)  # 15:30 -> 09:15 is never expected


def test_no_false_gap_across_a_weekend():
    cs = session_candles(FRI) + session_candles(MON)
    m = series.missing_intervals(cs, 60, ist(16, 0, day=MON))
    assert (m["expected"], m["missing"]) == (750, 0)  # Saturday and Sunday have no candles and are not examined


def test_no_false_gap_on_a_market_holiday():
    cs = session_candles(MON) + session_candles(WED)  # nothing at all on Tuesday
    m = series.missing_intervals(cs, 60, ist(16, 0, day=WED))
    assert (m["expected"], m["missing"]) == (750, 0)
    assert series.missing_intervals(session_candles(TUE)[100:], 60, ist(16, 0, day=TUE))["missing"] == 0  # window may start mid-session


def test_the_window_start_is_not_a_gap():
    cs = session_candles(THU)[200:] + session_candles(FRI)  # starts mid-Thursday
    m = series.missing_intervals(cs, 60, AFTER_CLOSE)
    assert (m["expected"], m["missing"]) == (175 + 375, 0)


def test_an_unfinished_session_is_only_expected_up_to_now_with_a_grace_period():
    cs = [x for x in session_candles(FRI) if x.start <= ist(10, 0)]  # candles up to 10:00, then the feed stops
    # 10:01:09 - the 10:00 candle ended at 10:01:00 and is not yet expected (10 s grace): nothing missing
    assert series.missing_intervals(cs[:-1], 60, ist(10, 1, 9))["missing"] == 0
    assert series.missing_intervals(cs[:-1], 60, ist(10, 1, 10))["missing"] == 1  # now it is
    m = series.missing_intervals(cs, 60, ist(10, 5, 30))  # 10:01..10:04 should exist by now
    assert (m["missing"], m["expected"]) == (4, 50) and hhmm(m["runs"][0]["start"]) == "10:01"
    assert m["runs"][0]["count"] == 4


def test_the_real_world_pattern_of_361_candles_a_day():
    cs = session_candles(THU, drop={f"15:{m}" for m in range(15, 29)}) + session_candles(FRI, drop={f"15:{m}" for m in range(15, 29)})
    assert len(cs) == 722
    m = series.missing_intervals(cs, 60, AFTER_CLOSE)
    assert (m["missing"], m["latest_session_missing"]) == (28, 14)  # 14 per session, the earlier one counted separately
    assert [(r["count"], hhmm(r["start"])) for r in m["runs"]] == [(14, "15:15"), (14, "15:15")]  # runs never span the night
    assert m["latest_session"] == "2026-11-06" and m["latest_session_expected"] == 375


def test_earlier_sessions_count_in_the_window_but_not_as_latest_session():
    cs = session_candles(THU, drop={"10:00", "10:01", "10:02"}) + session_candles(FRI)
    m = series.missing_intervals(cs, 60, AFTER_CLOSE)
    assert (m["missing"], m["latest_session_missing"]) == (3, 0)


def test_other_timeframes_use_their_own_slots():
    for secs, slots in ((300, 75), (900, 25), (3600, 7)):
        full = session_candles(FRI, secs)
        assert series.missing_intervals(full, secs, AFTER_CLOSE)["expected"] == slots == math.ceil(22500 / secs)
        assert series.missing_intervals(full, secs, AFTER_CLOSE)["missing"] == 0
    hourly = session_candles(FRI, 3600, drop={"11:15"})
    m = series.missing_intervals(hourly, 3600, AFTER_CLOSE)
    assert (m["missing"], hhmm(m["runs"][0]["start"]), hhmm(m["runs"][0]["end"])) == (1, "11:15", "12:15")


def test_missing_intervals_never_changes_or_invents_candles():
    cs = session_candles(FRI, drop={"10:00"})
    before = [(x.start, x.close) for x in cs]
    series.missing_intervals(cs, 60, AFTER_CLOSE)
    series.contiguity(cs, 60)
    assert [(x.start, x.close) for x in cs] == before and len(cs) == 374
    assert series.missing_intervals([], 60, AFTER_CLOSE) == {
        "expected": 0, "missing": 0, "latest_session": None, "latest_session_expected": 0, "latest_session_missing": 0, "runs": [],
    }  # fmt: skip


# -- contiguity ---------------------------------------------------------------------------------------------------
def test_contiguity_marks_holes_inside_a_session_only():
    cs = session_candles(THU, drop={"10:00"})[:60] + session_candles(FRI, drop={"09:15", "09:16"})[:5]
    flags = series.contiguity(cs, 60)
    hole = next(i for i, x in enumerate(cs) if hhmm(x.start) == "10:01")
    assert flags[hole] is False  # 09:59 -> 10:01
    assert all(f for i, f in enumerate(flags) if i != hole and hhmm(cs[i].start) != "09:17")  # everything else follows its predecessor
    first_of_friday = next(i for i, x in enumerate(cs) if x.start == ist(9, 17, day=FRI))
    assert flags[first_of_friday] is True  # the overnight junction is a price gap, not a missing interval
    assert flags[0] is True
