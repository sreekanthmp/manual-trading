"""Indicators, market structure, market condition and the analysis as a whole.

Expected values are worked out by hand (see the comments) or come from an independent pandas
implementation; none are produced by the code under test."""

import math
import random
from datetime import date

import pytest
from helpers import FRI, MON, THU, hhmm, ist, ramp, session_candles

from app import analysis as an
from app import series
from app.candles import Candle, bucket

AFTER = ist(16, 0, day=MON)  # the Friday session has ended; today is Monday


def approx(x):
    return pytest.approx(x, abs=1e-3)


def plain(rows):
    """Candles from (high, low, close) rows. Timestamps are irrelevant to the indicator maths."""
    return [Candle(i, i + 1, c, h, low, c) for i, (h, low, c) in enumerate(rows)]


def bars(path, width=0.5):
    return [Candle(i, i + 1, c, c + width, c - width, c) for i, c in enumerate(path)]


def on_grid(path, secs=900, width=0.5, end_day=MON):
    """Candles for a close path laid on consecutive 09:15-grid session slots, the last one on the weekday
    before `end_day`, so calendar-aware code (missing intervals, contiguity) sees a complete series."""
    per_day = math.ceil(22500 / secs)
    days, d = [], date(*end_day)
    while len(days) * per_day < len(path):
        d = date.fromordinal(d.toordinal() - 1)
        if d.weekday() < 5:
            days.append(d)
    slots = [s.start for day in reversed(days) for s in session_candles((day.year, day.month, day.day), secs)]
    slots = slots[-len(path):]
    return [Candle(s, bucket(s, secs)[1], c, c + width, c - width, c, source="db") for s, c in zip(slots, path)]


# -- indicators, expected values worked out by hand ------------------------------------------------------------
def test_ema_seeded_with_sma():
    # n=3: seed (1+2+3)/3 = 2, alpha = 0.5 -> 4*.5 + 2*.5 = 3 -> 5*.5 + 3*.5 = 4
    assert an.ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert an.ema([1, 2], 3) == [None, None]


def test_rsi_wilder_by_hand():
    # changes +1 +1 -1: average gain 2/3, loss 1/3 -> RS 2 -> 66.667
    # next +1: gain (2/3*2+1)/3 = 7/9, loss (1/3*2)/3 = 2/9 -> RS 3.5 -> 77.778
    # next +1: gain (7/9*2+1)/3 = 23/27, loss (2/9*2)/3 = 4/27 -> RS 5.75 -> 85.185
    r = an.rsi([10, 11, 12, 11, 12, 13], 3)
    assert r[:3] == [None] * 3 and [round(x, 3) for x in r[3:]] == [66.667, 77.778, 85.185]
    assert an.rsi([5, 4, 3, 2, 1], 3)[-1] == 0.0 and an.rsi([1, 2, 3, 4, 5], 3)[-1] == 100.0
    assert an.rsi([7, 7, 7, 7, 7], 3)[-1] == 50.0  # no movement at all is neutral, not "overbought"


def test_atr_wilder_by_hand():
    # TR: c1 = max(2, |11-9|, |9-9|) = 2 ; c2 = 2 ; c3 = max(3, |13-11|, |10-11|) = 3 -> ATR3 = 7/3
    # c4 (12, 11, 11.5): TR = max(1, |12-12|, |11-12|) = 1 -> (7/3*2 + 1)/3 = 17/9
    a = an.atr(plain([(10, 8, 9), (11, 9, 10), (12, 10, 11), (13, 10, 12), (12, 11, 11.5)]), 3)
    assert a[:3] == [None] * 3 and a[3] == approx(7 / 3) and a[4] == approx(17 / 9)


@pytest.mark.parametrize("after_gap", [150, 50], ids=["gap_up", "gap_down"])
def test_intraday_atr_leaves_the_overnight_gap_out(after_gap):
    """Hourly candles, 2 points tall (7 per session). Session 1 ends with four candles at 100; session 2 opens at 150 (or 50)
    and stays there. Wilder ATR with n = 3. The first candle of session 2 against the previous close of 100:
    max(2, |151-100|, |149-100|) = 51 (or max(2, |51-100|, |49-100|) = 51); by its own range: 2.
      plain:    2 -> (2*2 + 51)/3 = 55/3 -> 116/9 = 12.889 -> 250/27 = 9.259 -> 6.840 -> 5.226 -> 4.151 -> 3.434
      intraday: 2 throughout"""
    cs = on_grid([100] * 4 + [after_gap] * 7, 3600, 1.0)
    plain_atr, intra = an.atr(cs, 3), an.atr(cs, 3, intraday=True)
    assert plain_atr[3] == approx(2.0) and plain_atr[4] == approx(55 / 3) and plain_atr[5] == approx(116 / 9) and plain_atr[6] == approx(250 / 27)
    assert plain_atr[10] == approx(3.434)
    assert intra[:3] == [None] * 3 and intra[3:] == [approx(2.0)] * 8


def test_intraday_atr_is_the_plain_atr_when_a_session_opens_where_the_last_one_closed():
    cs = on_grid([100] * 11, 3600, 1.0)  # a normal session: no gap, every true range is the candle's own range
    assert an.atr(cs, 3, intraday=True) == an.atr(cs, 3) and an.atr(cs, 3)[-1] == approx(2.0)
    gentle = on_grid([100] * 4 + [101] * 7, 3600, 1.0)  # a 1-point gap: |102-100| = 2, still the candle's own range
    assert an.atr(gentle, 3, intraday=True) == an.atr(gentle, 3)


def test_a_hole_inside_a_session_is_not_an_overnight_gap():
    full = on_grid([100] * 4 + [100, 100, 110, 120, 120, 120, 120], 3600, 1.0)
    holed = [x for i, x in enumerate(full) if i != 6]  # the 110 candle is missing: the move from 100 to 120 happens across the hole
    # the candle after the hole: max(2, |121-100|, |119-100|) = 21 -> ATR3 = (2*2 + 21)/3 = 25/3 in both forms
    assert an.atr(holed, 3)[6] == approx(25 / 3) and an.atr(holed, 3, intraday=True) == an.atr(holed, 3)


# A series whose last session opens 60 points above the previous close and has only six candles so far (15M, 0.5-point wicks):
# every true range is 1.5, except the gap candle (plain 60.5, intraday 1.0).
GAPPED = [100, 101] * 44 + [100] + [160, 161] * 12 + [160]


def test_adx_and_di_by_hand():
    # n=2. c1 (12,9,11): +DM 2, -DM 0, TR 3 ; c2 (11,8,9): +DM 0, -DM 1, TR 3
    #   -> sums TR 6, +DM 2, -DM 1 -> +DI 33.33, -DI 16.67, DX = 16.67/50 = 33.33
    # c3 (12,9,11): +DM 1, -DM 0, TR 3 -> TR 6-3+3 = 6, +DM 2-1+1 = 2, -DM 1-.5+0 = .5 -> +DI 33.33, -DI 8.33
    #   DX = 25/41.667 = 60 -> first ADX = (33.33 + 60)/2 = 46.667
    adx_s, pdi, mdi = an.adx(plain([(10, 8, 9), (12, 9, 11), (11, 8, 9), (12, 9, 11)]), 2)
    assert (pdi[2], mdi[2]) == (approx(33.333), approx(16.667))
    assert (pdi[3], mdi[3]) == (approx(33.333), approx(8.333))
    assert adx_s[2] is None and adx_s[3] == approx(46.667)


def test_adx_of_a_pure_trend_is_100_with_one_sided_di():
    up = [Candle(i, i + 1, 0, 11 + i, 10 + i, 11 + i) for i in range(40)]  # each bar 1 higher, closing at its high
    a, p, m = an.adx(up, 14)
    assert (a[-1], p[-1], m[-1]) == (approx(100), approx(100), approx(0))
    down = [Candle(i, i + 1, 0, 100 - i, 99 - i, 99 - i) for i in range(40)]
    a, p, m = an.adx(down, 14)
    assert (a[-1], p[-1], m[-1]) == (approx(100), approx(0), approx(100))


def test_indicators_agree_with_an_independent_pandas_implementation():
    pd = pytest.importorskip("pandas")
    rnd, px, rows = random.Random(11), 24000.0, []
    for _ in range(700):
        o, px = px, px + rnd.gauss(0, 8)
        rows.append((max(o, px) + abs(rnd.gauss(0, 3)), min(o, px) - abs(rnd.gauss(0, 3)), px))
    cs = plain(rows)
    df = pd.DataFrame(rows, columns=["h", "l", "c"])
    n = 14

    def wilder(x):  # Wilder smoothing as an exponential mean with alpha = 1/n, seeded with the first value
        return x.ewm(alpha=1 / n, adjust=False).mean()

    delta = df.c.diff()
    rsi_ref = 100 - 100 / (1 + wilder(delta.clip(lower=0)) / wilder(-delta.clip(upper=0)))
    tr = pd.concat([df.h - df.l, (df.h - df.c.shift()).abs(), (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    atr_ref = wilder(tr)
    up, down = df.h.diff(), -df.l.diff()
    pdi_ref = 100 * wilder(up.where((up > down) & (up > 0), 0.0)) / atr_ref
    mdi_ref = 100 * wilder(down.where((down > up) & (down > 0), 0.0)) / atr_ref
    adx_ref = wilder(100 * (pdi_ref - mdi_ref).abs() / (pdi_ref + mdi_ref))

    adx_s, pdi, mdi = an.adx(cs)
    # The seeding conventions differ (SMA of the first n values vs the first value); after 700 bars the
    # difference has decayed to nothing, so the final values agree to far more digits than asserted.
    assert an.ema([c for *_, c in rows], 20)[-1] == pytest.approx(df.c.ewm(span=20, adjust=False).mean().iloc[-1], rel=1e-9)
    assert an.ema([c for *_, c in rows], 50)[-1] == pytest.approx(df.c.ewm(span=50, adjust=False).mean().iloc[-1], rel=1e-9)
    assert an.rsi([c for *_, c in rows])[-1] == pytest.approx(rsi_ref.iloc[-1], rel=1e-9)
    assert an.atr(cs)[-1] == pytest.approx(atr_ref.iloc[-1], rel=1e-9)
    assert pdi[-1] == pytest.approx(pdi_ref.iloc[-1], rel=1e-9)
    assert mdi[-1] == pytest.approx(mdi_ref.iloc[-1], rel=1e-9)
    assert adx_s[-1] == pytest.approx(adx_ref.iloc[-1], rel=1e-9)


# -- session reference: open, VWAP, average price -------------------------------------------------------------------
def minute(day, i, o, h, low, c, vol=0):
    s = ist(9, 15, day=day) + 60 * i
    return Candle(s, s + 60, o, h, low, c, vol)


def test_vwap_is_volume_weighted_and_only_with_real_volume():
    # typical prices (h+l+c)/3: 10 and 20; volumes 100 and 300 -> (10*100 + 20*300)/400 = 17.5 ; plain average 15
    cs = [minute(FRI, 0, 10, 12, 8, 10, vol=100), minute(FRI, 1, 20, 22, 18, 20, vol=300)]
    ref = an.session_reference({"1m": cs}, None)
    assert ref["vwap"] == approx(17.5) and ref["session_avg_price"] == approx(15.0) and ref["vwap_note"] is None


def test_an_index_has_no_vwap_only_a_separately_named_session_average():
    cs = [minute(FRI, 0, 10, 12, 8, 10), minute(FRI, 1, 20, 22, 18, 20)]  # volume 0: NIFTY spot
    ref = an.session_reference({"1m": cs}, None)
    assert ref["vwap"] is None and ref["session_avg_price"] == approx(15.0)
    assert "index" in ref["vwap_note"]
    partial_volume = [minute(FRI, 0, 10, 12, 8, 10, vol=100), minute(FRI, 1, 20, 22, 18, 20)]
    partial_volume[1].volume = None  # volume missing on some candles is not "meaningful volume"
    assert an.session_reference({"1m": partial_volume}, None)["vwap"] is None


def test_session_values_belong_to_the_latest_session_only():
    yesterday = [minute(THU, 0, 1000, 1002, 998, 1000)]
    today = [minute(FRI, 0, 10, 12, 8, 10), minute(FRI, 1, 20, 22, 18, 20)]
    ref = an.session_reference({"1m": yesterday + today}, None)
    assert ref["session_date"] == "2026-11-06" and ref["session_avg_price"] == approx(15.0) and ref["session_open"] == 10


def test_session_open_comes_from_the_0915_candle_of_the_finest_timeframe_that_has_it():
    m1 = [minute(FRI, 1, 11, 12, 10, 11)]  # the 09:15 minute is missing
    m5 = [Candle(ist(9, 15), ist(9, 20), 9.5, 12, 9, 11)]
    assert an.session_reference({"1m": m1, "5m": m5}, None)["session_open"] == 9.5
    assert an.session_reference({"1m": [minute(FRI, 0, 9.0, 10, 8, 9.5)], "5m": m5}, None)["session_open"] == 9.0
    forming = {"1m": Candle(ist(9, 15), ist(9, 16), 24500, 24501, 24499, 24500.5)}
    new_day = an.session_reference({"1m": [minute(THU, 0, 1, 2, 0, 1)]}, forming)  # first minute of today still forming
    assert (new_day["session_date"], new_day["session_open"], new_day["session_avg_price"]) == ("2026-11-06", 24500, None)
    late = {"1m": Candle(ist(9, 20), ist(9, 21), 5, 6, 4, 5)}
    assert an.session_reference({"1m": [minute(THU, 0, 1, 2, 0, 1)]}, late)["session_open"] is None  # not the open
    assert an.session_reference({}, None)["session_date"] is None


# -- structure ------------------------------------------------------------------------------------------------------
UP = [10, 11, 12, 11, 10, 11, 12, 13, 12, 11, 12, 13, 14, 13, 12]  # tops 12.5, 13.5, 14.5 ; bottoms 9.5, 10.5


def test_swings_higher_highs_and_higher_lows():
    sw = an.swings(bars(UP))
    assert sw == [(2, "H", 12.5), (4, "L", 9.5), (7, "H", 13.5), (9, "L", 10.5), (12, "H", 14.5)]
    st = an.structure(bars(UP))
    assert (st["pattern"], st["swing_high"], st["swing_low"]) == ("HH_HL", 14.5, 10.5)
    assert [(s["kind"], s["price"]) for s in st["recent_swings"]] == [("H", 12.5), ("L", 9.5), ("H", 13.5), ("L", 10.5), ("H", 14.5)]


def test_lower_highs_lower_lows_mixed_and_insufficient():
    assert an.structure(bars([20 - x for x in UP]))["pattern"] == "LH_LL"
    expanding = [10, 11, 12, 11, 10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10]  # a higher high, then a lower low
    assert an.structure(bars(expanding))["pattern"] in ("mixed", "insufficient")
    assert an.structure(bars([1, 2]))["pattern"] == "insufficient"


def test_a_swing_is_never_reported_before_the_candles_that_confirm_it():
    rnd, px, rows = random.Random(5), 100.0, []
    for _ in range(160):
        o, px = px, px + rnd.gauss(0, 1)
        rows.append((max(o, px) + abs(rnd.gauss(0, 0.4)), min(o, px) - abs(rnd.gauss(0, 0.4)), px))
    cs, k = plain(rows), an.SWING_K
    full = an.swings(cs, k)
    assert len(full) > 10
    for n in range(2 * k + 1, len(cs) + 1):  # as if only the first n candles had completed
        known = an.swings(cs[:n], k)
        assert known == [s for s in full if s[0] + k <= n - 1]  # exactly the pivots the later candles had confirmed
        assert all(s[0] <= n - 1 - k for s in known)  # and none needs a candle after the last one


def test_a_hole_inside_a_pivots_window_prevents_the_pivot():
    cs = bars(UP)
    flags = [True] * len(cs)
    flags[8] = False  # a missing interval between candle 7 and candle 8
    sw = an.swings(cs, 2, flags)
    assert (7, "H", 13.5) not in sw and (9, "L", 10.5) not in sw  # both windows (5..9 and 7..11) contain the hole
    assert (2, "H", 12.5) in sw and (4, "L", 9.5) in sw and (12, "H", 14.5) in sw  # unaffected pivots remain
    assert an.structure(cs, 2, flags)["pattern"] == "insufficient"  # only one low left


def test_breakout_and_breakdown_need_a_fresh_close_beyond_the_swing():
    up = bars([10, 11, 12, 11, 10, 10.5, 11, 11.5, 13])  # swing high 12.5, then a close at 13
    assert an.breakout(up, an.structure(up), None) == "breakout"
    down = bars([20 - x for x in [10, 11, 12, 11, 10, 10.5, 11, 11.5, 13]])
    assert an.breakout(down, an.structure(down), None) == "breakdown"
    assert an.breakout(bars([10, 11, 12, 11, 10, 10.5, 11, 11.5, 12]), {"swing_high": 12.5, "swing_low": None}, None) is None
    far = bars([10, 11, 12, 11, 10, 13, 13.5, 13.8, 14])  # already above for 3 bars: not fresh
    assert an.breakout(far, an.structure(far), None) is None
    assert an.breakout(up, an.structure(up), 5.0) is None  # 13 is not beyond 12.5 + 0.1 x ATR(5)


def test_breakout_is_not_declared_across_a_hole():
    up = bars([10, 11, 12, 11, 10, 10.5, 11, 11.5, 13])
    flags = [True] * len(up)
    flags[-2] = False  # a missing interval inside the last four candles
    assert an.breakout(up, an.structure(up), None, flags) is None


def test_consolidation_zone():
    tight = bars([100, 100.5] * 8)  # range 1.5 + 2 x 0.5 = 2.0 against an ATR of 1
    assert an.consolidation(tight, 1.0) == {"high": 101.0, "low": 99.5, "bars": 12}
    assert an.consolidation(bars(range(100, 120)), 1.0) is None  # trending: range far above 3 x ATR
    assert an.consolidation(tight[:5], 1.0) is None and an.consolidation(tight, None) is None
    flags = [True] * len(tight)
    flags[-4] = False  # a hole could be hiding part of the range
    assert an.consolidation(tight, 1.0, flags) is None


def test_levels_merge_nearby_swings_and_keep_only_meaningful_ones():
    zig = [10, 12, 14, 12, 10, 12, 14.1, 12, 10.05, 12, 14, 12, 10]  # tops near 14, bottoms near 10
    clusters = an.key_levels(on_grid(zig, 3600, 0), on_grid(zig, 900, 0), atr_15m=0.5)
    by_price = sorted((round(c["price"], 2), c["touches"]) for c in clusters)
    # tops 14, 14.1, 14 (x2 charts) -> mean 14.033 ; bottoms 10, 10.05 (x2 charts) -> mean 10.025
    assert by_price == [(10.03, 4), (14.03, 6)]
    split = an.split_levels(clusters, 12.0)
    assert [x["price"] for x in split["resistance"]] == [14.03] and [x["price"] for x in split["support"]] == [10.03]


def test_split_levels_follows_the_live_price_and_caps_each_side():
    clusters = [{"price": p, "touches": 2, "from_1h": False} for p in (95, 96, 97, 98, 99, 101, 102, 103, 104, 105)]
    s = an.split_levels(clusters, 100.0)
    assert [x["price"] for x in s["resistance"]] == [101, 102, 103]  # nearest three above, not all five
    assert [x["price"] for x in s["support"]] == [99, 98, 97]
    assert [x["price"] for x in an.split_levels(clusters, 102.5)["support"]] == [102, 101, 99]  # same clusters, new side
    assert an.split_levels(clusters, None) == {"support": [], "resistance": []}
    single = an.key_levels(on_grid([10, 12, 14, 12, 10, 12, 14.5, 12, 11, 12, 13, 12, 11.5], 3600, 0), [], 0.5)
    assert all(lv["touches"] >= 2 or lv["from_1h"] for lv in single)  # single touches survive only from the 1H chart


# -- one timeframe ------------------------------------------------------------------------------------------------
def test_timeframe_classifies_steady_trends_and_ranges():
    t = an.timeframe(on_grid([100 + 0.5 * i for i in range(200)]), 900, AFTER)
    assert t["trend"] == "bullish" and t["warm"] and t["ema20"] > t["ema50"] and t["adx"] > 90
    assert t["plus_di"] > t["minus_di"] and t["rsi14"] == 100.0 and t["score"] == 2  # EMA stack + DI; no swings in a straight line
    assert an.timeframe(on_grid([200 - 0.5 * i for i in range(200)]), 900, AFTER)["trend"] == "bearish"
    r = an.timeframe(on_grid([100, 101] * 100), 900, AFTER)
    assert r["trend"] == "sideways" and r["consolidation"] is not None


def test_warm_up_needs_150_candles_but_indicators_still_show_earlier():
    short = an.timeframe(on_grid([100 + 0.5 * i for i in range(149)]), 900, AFTER)
    assert (short["warm"], short["trend"], short["bars"]) == (False, "unknown", 149)
    assert short["ema50"] is not None and short["adx"] is not None  # computable, just not trusted for the trend yet
    assert an.timeframe(on_grid([100 + 0.5 * i for i in range(150)]), 900, AFTER)["trend"] == "bullish"
    assert an.timeframe([], 900, AFTER) == {
        "bars": 0, "warm": False, "trend": "unknown", "last_completed": None, "missing": an.missing_intervals([], 900, AFTER)
    }  # fmt: skip


def test_timeframe_reports_missing_intervals_without_changing_the_indicators():
    full = on_grid([100 + 0.5 * i for i in range(200)])
    holed = [x for i, x in enumerate(full) if i != 120]
    a, b = an.timeframe(full, 900, AFTER), an.timeframe(holed, 900, AFTER)
    assert a["missing"]["missing"] == 0 and b["missing"]["missing"] == 1
    assert b["bars"] == 199 and len(holed) == 199  # nothing was filled in
    assert b["adx"] != a["adx"] or b["ema20"] != a["ema20"]  # computed on the candles that exist


def test_a_gap_inflates_the_plain_atr_ratio_but_not_the_intraday_one():
    # 95 candles -> 81 ATR values: 75 at 1.5, then six after the gap.
    # plain ATR: (1.5*13 + 60.5)/14 = 5.714 -> 5.413 -> 5.134 -> 4.874 -> 4.633 -> 4.409; their average with the 75 is 1.762 -> ratio 2.50
    # intraday ATR: (1.5*13 + 1)/14 = 1.464 -> 1.467 -> 1.469 -> 1.471 -> 1.473 -> 1.475; average 1.498 -> ratio 0.985
    cs = on_grid(GAPPED, 900, 0.5)[:-19]
    t = an.timeframe(cs, 900, AFTER)
    assert t["atr14"] == approx(an.atr(cs)[-1]) and t["atr14_intraday"] == approx(an.atr(cs, intraday=True)[-1])
    assert t["atr14"] == approx(4.409) and t["atr_ratio"] == pytest.approx(2.50, abs=0.02)
    assert t["atr14_intraday"] == pytest.approx(1.475, abs=0.005) and t["atr_ratio_intraday"] == pytest.approx(0.985, abs=0.01)
    calm = an.timeframe(on_grid([100, 101] * 60, 900, 0.5), 900, AFTER)  # no gap anywhere: nothing to separate
    assert calm["atr14_intraday"] == pytest.approx(calm["atr14"], rel=0.01) and calm["atr_ratio_intraday"] == pytest.approx(1.0, abs=0.01)


def test_structure_thresholds_use_the_atr_without_the_gap(monkeypatch):
    """Consolidation (3 x ATR), the breakout buffer (0.1 x ATR) and the level clustering (0.5 x ATR) are all 'how big is a candle' measures."""
    seen = {}
    monkeypatch.setattr(an, "breakout", lambda cs, st, a, contig=None: seen.__setitem__("breakout", a))
    monkeypatch.setattr(an, "consolidation", lambda cs, a, contig=None: seen.__setitem__("consolidation", a))
    monkeypatch.setattr(an, "key_levels", lambda h1, m15, a: seen.__setitem__("levels", a) or [])
    out = an.analyze({"15m": on_grid(GAPPED, 900, 0.5)[:-19]}, [], None, AFTER)
    t = out["timeframes"]["15m"]
    assert t["atr14_intraday"] < t["atr14"] / 2
    assert seen == {"breakout": t["atr14_intraday"], "consolidation": t["atr14_intraday"], "levels": t["atr14_intraday"]}


# -- timeframes together --------------------------------------------------------------------------------------------
def tf(trend, ratio=1.0, rsi=55.0, adx=25.0, pattern="mixed", breakout=None, atr=10.0, close=1000.0):
    return {
        "trend": trend, "ema20": 1010.0, "ema50": 1000.0, "plus_di": 30.0, "minus_di": 15.0, "adx": adx, "rsi14": rsi,
        "atr14": atr, "atr_ratio": ratio, "structure": {"pattern": pattern}, "breakout": breakout, "consolidation": None,
        "last_completed": {"close": close}, "missing": {"latest_session_missing": 0},
    }  # fmt: skip


def frames(h, m, f="sideways", o="sideways", **kw):
    return {"1h": tf(h, **kw), "15m": tf(m, **kw), "5m": tf(f), "1m": tf(o)}


@pytest.mark.parametrize(
    "h,m,label",
    [("bullish", "bullish", "aligned_bullish"), ("bearish", "bearish", "aligned_bearish"), ("bullish", "bearish", "conflicting"),
     ("bearish", "bullish", "conflicting"), ("sideways", "sideways", "range_bound"), ("bullish", "sideways", "pausing"),
     ("sideways", "bearish", "developing"), ("unknown", "bullish", "unknown")],
)  # fmt: skip
def test_alignment_labels(h, m, label):
    assert an.alignment(frames(h, m))["label"] == label


def test_conflicting_timeframes_are_explained_as_pullback_or_reversal_risk():
    pullback = an.alignment(frames("bullish", "bearish"))
    assert "conflicting" in pullback["text"] and "pullback" in pullback["text"]
    t = frames("bullish", "bearish")
    t["15m"]["structure"]["pattern"] = "LH_LL"  # 15M structure broke against the 1H uptrend
    assert "reversal risk is higher" in an.alignment(t)["text"]
    t = frames("bearish", "bullish")
    t["15m"]["breakout"] = "breakout"
    assert "reversal risk is higher" in an.alignment(t)["text"]


def test_trend_follows_the_1h_unless_the_15m_is_against_it():
    assert an.condition(frames("bullish", "bullish"))["trend"] == "bullish"
    assert an.condition(frames("bearish", "sideways"))["trend"] == "bearish"  # 15M pausing is not "against"
    assert an.condition(frames("bullish", "bearish"))["trend"] == "sideways"  # conflict: direction is not confirmed
    assert an.condition(frames("sideways", "bullish"))["trend"] == "sideways"
    assert an.condition(frames("sideways", "sideways"))["trend"] == "sideways"


def test_trend_and_volatility_are_separate_dimensions():
    assert (c := an.condition(frames("bullish", "bullish", ratio=1.6)))["trend"] == "bullish" and c["volatility"] == "high"
    assert (c := an.condition(frames("bearish", "bearish", ratio=0.5)))["trend"] == "bearish" and c["volatility"] == "low"
    assert (c := an.condition(frames("sideways", "sideways", ratio=1.6)))["trend"] == "sideways" and c["volatility"] == "high"
    assert an.condition(frames("bullish", "bullish", ratio=1.0))["volatility"] == "normal"
    c = an.condition(frames("unknown", "bullish", ratio=1.6))  # the 1H is still warming up
    assert (c["trend"], c["volatility"]) == ("unknown", "high")  # volatility does not depend on the 1H


def test_volatility_bands_are_configurable_assumptions(monkeypatch):
    assert an.condition(frames("bullish", "bullish", ratio=1.3))["volatility"] == "high"  # boundary is inclusive
    assert an.condition(frames("bullish", "bullish", ratio=0.7))["volatility"] == "low"
    monkeypatch.setattr(an, "VOL_HIGH", 2.0)
    monkeypatch.setattr(an, "VOL_LOW", 0.2)
    assert an.condition(frames("bullish", "bullish", ratio=1.6))["volatility"] == "normal"


def test_every_classification_comes_with_its_reasons():
    c = an.condition(frames("bullish", "bullish", f="bullish", o="bearish", ratio=1.6, rsi=75.0, atr=15.0))
    text = "\n".join(c["reasons"])
    assert "higher-timeframe bullish alignment" in text
    assert "Trend bullish. 1H: EMA20 1010.00 > EMA50 1000.00, +DI 30.0 > -DI 15.0, ADX 25.0, structure mixed" in text
    assert "Short term: 5M bullish agrees with the bullish trend; 1M bearish." in text
    assert "1H RSI 75.0 is above 70 (stretched)" in text and "15M RSI 75.0 is above 70 (stretched)" in text
    assert "Volatility high: 15M ATR 15.00 (1.50% of price) is 1.60x its recent average." in text
    assert "below 30" in "\n".join(an.condition(frames("bearish", "bearish", rsi=22.0))["reasons"])
    assert "unknown until both 1H and 15M" in "\n".join(an.condition(frames("unknown", "unknown"))["reasons"])


def test_the_volatility_label_ignores_the_overnight_gap_and_says_so():
    t = frames("bullish", "bullish")
    t["15m"].update(atr14=73.0, atr_ratio=2.4, atr14_intraday=48.0, atr_ratio_intraday=1.0)  # a gap-up morning: the gap alone made it 2.4x
    c = an.condition(t)
    assert c["volatility"] == "normal"  # the candles are not unusually large
    assert any("15M ATR 48.00 (4.80% of price, overnight gap excluded; 73.00 if counted) is 1.00x its recent average." in r for r in c["reasons"])
    t["15m"].update(atr_ratio_intraday=1.6)  # the candles themselves are large: that is volatility
    assert an.condition(t)["volatility"] == "high"


# -- previous day and warnings ------------------------------------------------------------------------------------
def daily(day, h, low, c):
    s = ist(0, 0, day=day)
    return Candle(s, s + 86400, (h + low) / 2, h, low, c, 0)


def test_previous_day_is_the_last_daily_candle_before_today():
    d = [daily(THU, 110, 90, 100), daily(FRI, 120, 95, 118)]
    p = an.previous_day(d, date(2026, 11, 9), {date(2026, 11, 5), date(2026, 11, 6)})  # Monday
    assert (p["date"], p["pdh"], p["pdl"], p["pdc"], p["stale"]) == ("2026-11-06", 120, 95, 118, False)
    assert an.previous_day(d, date(2026, 11, 6), {date(2026, 11, 5)})["date"] == "2026-11-05"  # on Friday: Thursday
    assert an.previous_day(d, date(2026, 11, 5), set()) is None  # nothing before Thursday


def test_previous_day_levels_are_flagged_when_the_latest_session_has_no_daily_candle():
    p = an.previous_day([daily(THU, 110, 90, 100)], date(2026, 11, 9), {date(2026, 11, 5), date(2026, 11, 6)})
    assert p["stale"] and p["date"] == "2026-11-05" and "2026-11-06" in p["note"]
    weekend_gap = an.previous_day([daily(FRI, 120, 95, 118)], date(2026, 11, 9), {date(2026, 11, 6)})
    assert not weekend_gap["stale"]  # Saturday and Sunday are simply not sessions


# -- the whole analysis -----------------------------------------------------------------------------------------------
def full_inputs():
    m1, m5, m15 = ramp(60, 3, 0.2, MON), ramp(300, 10, 1.0, MON), ramp(900, 25, 3.0, MON)
    return {"1m": m1, "5m": m5, "15m": m15, "1h": series.aggregate(m15, 3600, 900)}


def test_complete_data_gives_a_bullish_reliable_analysis():
    inputs = full_inputs()
    a = an.analyze(inputs, [daily(FRI, 24600, 23900, 24500)], None, AFTER)
    assert a["warnings"] == []
    assert (a["condition"]["trend"], a["condition"]["volatility"], a["condition"]["alignment"]["label"]) == ("bullish", "normal", "aligned_bullish")
    assert all(a["timeframes"][k]["missing"]["missing"] == 0 and a["timeframes"][k]["warm"] for k in an.SECS)
    assert a["reference"]["previous_day"]["pdh"] == 24600 and a["reference"]["session_date"] == "2026-11-06"
    assert a["reference"]["vwap"] is None and a["reference"]["session_avg_price"] is not None


def test_missing_candles_are_reported_never_filled_and_do_not_change_the_rules():
    inputs = full_inputs()
    last_minutes = {hhmm(x.start) for x in inputs["1m"][-5:]}
    inputs["1m"] = inputs["1m"][:-5]
    n_1m = len(inputs["1m"])
    a = an.analyze(inputs, [daily(FRI, 24600, 23900, 24500)], None, AFTER)
    assert len(inputs["1m"]) == n_1m == 1120  # the input was not extended
    assert a["timeframes"]["1m"]["missing"]["latest_session_missing"] == 5 and last_minutes == {"15:25", "15:26", "15:27", "15:28", "15:29"}
    assert any(w.startswith("1M: 5 of 375 intervals missing in the latest session (2026-11-06)") and "15:25-15:29 (5)" in w for w in a["warnings"])
    assert not any(w.startswith(("1H", "15M", "5M")) for w in a["warnings"])
    assert a["condition"]["trend"] == "bullish"  # 1M gaps do not alter the 1H/15M direction
    assert not any("Data note" in r for r in a["condition"]["reasons"])


def test_gaps_in_the_direction_timeframes_are_called_out_in_the_reasons():
    inputs = full_inputs()
    m15 = [x for x in inputs["15m"] if not (hhmm(x.start) == "11:00" and x.start > ist(0, 0, day=FRI))]  # one Friday candle
    inputs["15m"], inputs["1h"] = m15, series.aggregate(m15, 3600, 900)
    a = an.analyze(inputs, [daily(FRI, 24600, 23900, 24500)], None, AFTER)
    assert a["timeframes"]["15m"]["missing"]["latest_session_missing"] == 1
    assert a["timeframes"]["1h"]["missing"]["latest_session_missing"] == 1  # the 10:15 hour cannot be built
    assert any("Data note" in r for r in a["condition"]["reasons"])
    assert a["condition"]["trend"] == "bullish"


def test_the_warm_up_requirement_is_reported():
    a = an.analyze({"1m": [], "5m": [], "15m": ramp(900, 3, 1.0, MON), "1h": []}, [], None, AFTER)
    assert any(w.startswith("15M: only 75 completed candles, 150 are needed") for w in a["warnings"])
    assert a["condition"]["trend"] == "unknown"
