"""The three strategies, on hand-built scenarios (see scenarios.py for the candle tables).

Each test is written for the bullish case and also run for the bearish case on the mirrored scenario;
expected prices are the hand-worked bullish numbers, reflected for the bearish side. NIFTY ~24,500,
ATR(5M) = 10, ATR(15M) = 25, so the touch tolerance is 6.25 points and the invalidation buffer 2.5."""

from dataclasses import replace

import pytest
from scenarios import (LEVEL, M, OPEN, P, RESISTANCE, SUPPORT, bull, bull_breakout, bull_pullback, bull_range, c5, c15,
                       mirror, pullback_5m, pullback_15m, t5)

from app import registry
from app import strategy as st
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Level, Params, Setup, View

SIDES = ["bull", "bear"]
OPT = {"bull": "CE", "bear": "PE"}
WORD = {"bull": "breakout", "bear": "breakdown"}


def real(side, ctx):
    return ctx if side == "bull" else mirror(ctx)


def at(side, x):
    return x if side == "bull" else M - x


def box(side, lo, hi):
    return (lo, hi) if side == "bull" else (M - hi, M - lo)


def scan(fn, ctx, side, p=P, prior=None):
    return fn(View(real(side, ctx), side), p, prior)


def extend(ctx, *rows):
    """Append completed 5M candles (bullish coordinates) after the last one."""
    i = round((ctx.c5[-1].start - OPEN) / 300) + 1
    bars = list(ctx.c5) + [c5(i + k, *r) for k, r in enumerate(rows)]
    return replace(ctx, c5=bars, price=bars[-1].close, now=bars[-1].end + 3)


# ====================================================================================================================
# A. Trend pullback
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_pullback_is_confirmed_by_a_completed_candle_closing_above_the_base(side):
    """Hammer at 24,562 (a swing level and the 15M EMA20 are within 6.25 points), a small candle, then a bullish
    candle (body 11 = 1.1x ATR5, closing at 86% of its range) above the base high 24,590. Risk = 24,590 - 24,559.5
    = 30.5 points (1.2x ATR15, inside 0.75x ATR5..1.5x ATR15); the leg's high 24,650 is 60 points (2.0R) away."""
    s = scan(st.scan_pullback, bull_pullback(19), side).setup
    assert (s.status, s.direction, s.strategy) == (CONFIRMED, OPT[side], "trend_pullback")
    assert s.trigger == pytest.approx(at(side, 24590))
    assert s.invalidation == pytest.approx(at(side, 24559.5))  # the pullback low 24,562 less a quarter of ATR5
    assert s.zone == pytest.approx(box(side, 24590, 24600))  # from the trigger up to one ATR5 beyond it
    assert s.blockers == [] and s.confirmed_bar_end == t5(19) + 300
    assert any("rejection candle" in r for r in s.reasons) and any("pulled back 88.0 pts" in r for r in s.reasons)
    assert "completed 5M" in s.confirm_text and f"{at(side, 24590):,.2f}" in s.confirm_text


@pytest.mark.parametrize("side", SIDES)
def test_pullback_waits_after_the_rejection_candle_and_names_the_trigger(side):
    at_hammer = scan(st.scan_pullback, bull_pullback(17), side).setup
    assert at_hammer.status == WAITING and at_hammer.trigger == pytest.approx(at(side, 24583))  # the hammer's own high
    after_small = scan(st.scan_pullback, bull_pullback(18), side).setup
    # slot 18 closes at 58% of its range (< 60%): it is not a confirmation, but it raises the base high to 24,590
    assert after_small.status == WAITING and after_small.trigger == pytest.approx(at(side, 24590))


@pytest.mark.parametrize("side", SIDES)
def test_pullback_is_only_detected_when_price_is_at_the_zone_but_buyers_have_not_appeared(side):
    ctx = bull_pullback(16)
    ctx = extend(ctx, (24570, 24583, 24562, 24565))  # same low and range, but closing at the low with no lower wick
    s = scan(st.scan_pullback, ctx, side).setup
    assert s.status == DETECTED and s.trigger == pytest.approx(at(side, 24583))
    assert not any("rejection" in r for r in s.reasons)


@pytest.mark.parametrize("side", SIDES)
def test_touching_the_zone_is_not_an_entry_a_weak_close_above_the_base_does_not_confirm(side):
    weak_close = (24585, 24598, 24584, 24591)  # closes above the base high but at 50% of its range (needs 60%)
    ctx = replace(bull_pullback(18), c5=pullback_5m(18) + [c5(19, *weak_close)], price=24591)
    s = scan(st.scan_pullback, ctx, side).setup
    assert s.status == WAITING and s.trigger == pytest.approx(at(side, 24598))
    small_body = (24588, 24593, 24587, 24591)  # closes above 24,590 with a body of 3 (needs 4 = 0.4x ATR5)
    ctx = replace(bull_pullback(18), c5=pullback_5m(18) + [c5(19, *small_body)], price=24591)
    assert scan(st.scan_pullback, ctx, side).setup.status == WAITING


@pytest.mark.parametrize("side", SIDES)
def test_a_close_above_the_base_without_any_sign_of_buyers_is_not_enough(side):
    """No rejection candle at the low and a candle that is large enough to confirm but not a momentum candle (0.7x ATR5)."""
    ctx = bull_pullback(16)
    ctx = extend(ctx, (24570, 24583, 24562, 24565), (24566, 24590, 24565, 24571))  # body 5, closes above 24,583
    s = scan(st.scan_pullback, ctx, side).setup
    assert s.status == DETECTED
    ctx = extend(ctx, (24571, 24600, 24570, 24598))  # now a strong candle: body 27 = 2.7x ATR5, closes at 90% of its range
    assert scan(st.scan_pullback, ctx, side).setup.status in (CONFIRMED, EXPIRED)  # momentum is evidence by itself


@pytest.mark.parametrize("side", SIDES)
def test_closing_below_the_invalidation_after_the_confirmation_invalidates(side):
    s = scan(st.scan_pullback, extend(bull_pullback(19), (24596, 24597, 24550, 24555)), side).setup
    assert s.status == INVALIDATED and f"{at(side, 24559.5):,.2f}" in s.note  # 24,555 closed below 24,559.5


@pytest.mark.parametrize("side", SIDES)
def test_a_wick_below_the_invalidation_that_closes_above_it_does_not_invalidate(side):
    s = scan(st.scan_pullback, extend(bull_pullback(19), (24596, 24597, 24552, 24592)), side).setup
    assert s.status == CONFIRMED  # only closes count; the confirmed low is the one it was confirmed at
    assert s.invalidation == pytest.approx(at(side, 24559.5))


@pytest.mark.parametrize("side", SIDES)
def test_failed_follow_through_invalidates(side):
    # 24,586 is 4 below the trigger 24,590: more than 0.25x ATR5 (2.5) back below it, but above the invalidation
    s = scan(st.scan_pullback, extend(bull_pullback(19), (24596, 24597, 24580, 24586)), side).setup
    assert s.status == INVALIDATED and "follow-through" in s.note
    ok = scan(st.scan_pullback, extend(bull_pullback(19), (24596, 24597, 24580, 24588)), side).setup
    assert ok.status == CONFIRMED  # 24,588 is within 2.5 of the trigger: noise


@pytest.mark.parametrize("side", SIDES)
def test_breaking_the_legs_origin_invalidates_the_structure(side):
    ctx = replace(bull_pullback(19), c15=pullback_15m() + [c15(6, 24580, 24585, 24480, 24490)])  # closes 10 below 24,500 (tol 6.25)
    sc = scan(st.scan_pullback, ctx, side)
    assert sc.setup.status == INVALIDATED and "leg's origin" in sc.setup.note
    wick_only = replace(bull_pullback(19), c15=pullback_15m() + [c15(6, 24580, 24585, 24480, 24498)])  # a wick below, closes above
    assert scan(st.scan_pullback, wick_only, side).setup.status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
def test_an_extended_confirmation_is_not_an_entry(side):
    """The confirming candle closes 22 points (2.2x ATR5) above the trigger; the entry area ends 10 above it."""
    ctx = replace(bull_pullback(18), c5=pullback_5m(18) + [c5(19, 24585, 24615, 24584, 24612)], price=24612)
    s = scan(st.scan_pullback, ctx, side).setup
    assert s.status == EXPIRED and "2.2x ATR5" in s.note


@pytest.mark.parametrize("side", SIDES)
def test_a_confirmation_stays_open_for_four_completed_candles(side):
    stay = [(24596, 24600, 24592, 24594), (24594, 24598, 24592, 24596), (24596, 24599, 24593, 24595)]
    assert scan(st.scan_pullback, extend(bull_pullback(19), *stay), side).setup.status == CONFIRMED  # 3 candles after it
    gone = scan(st.scan_pullback, extend(bull_pullback(19), *stay, (24595, 24598, 24592, 24596)), side).setup
    assert gone.status == EXPIRED and "entry window" in gone.note  # the 4th has completed


@pytest.mark.parametrize("side", SIDES)
def test_a_stalled_pullback_expires(side):
    drift = [(24575, 24580, 24572, 24576)] * 19  # never closes above the base, never makes a lower low
    s = scan(st.scan_pullback, extend(bull_pullback(17), *drift), side).setup
    assert s.status == EXPIRED and "18 candles" in s.note
    assert scan(st.scan_pullback, extend(bull_pullback(17), *drift[:17]), side).setup.status == WAITING  # not yet


@pytest.mark.parametrize("side", SIDES)
def test_no_pullback_means_no_setup(side):
    at_high = scan(st.scan_pullback, bull_pullback(11), side)  # the 24,650 candle is the latest: nothing to buy
    assert at_high.setup is None and at_high.applicable and "no pullback yet" in at_high.note
    shallow = replace(bull_pullback(11), c5=pullback_5m(11) + [c5(12, 24640, 24644, 24640, 24642)], price=24642)
    sc = scan(st.scan_pullback, shallow, side)  # 10 points = 0.4x ATR15, under the 0.5x needed
    assert sc.setup is None and "too shallow" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_pullback_in_mid_air_is_not_at_a_meaningful_level(side):
    ctx = bull_pullback(19, ema={"1h": (24400.0, 24300.0), "15m": (24480.0, 24450.0), "5m": (24595.0, 24585.0)}, levels=[])
    sc = scan(st.scan_pullback, ctx, side)
    assert sc.setup is None and "not at a meaningful level" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_the_pullback_zone_can_be_the_band_between_the_15m_emas(side):
    ctx = bull_pullback(19, levels=[], ema={"1h": (24400.0, 24300.0), "15m": (24600.0, 24540.0), "5m": (24595.0, 24585.0)})
    s = scan(st.scan_pullback, ctx, side).setup  # the low 24,562 lies between EMA50 24,540 and EMA20 24,600
    assert s.status == CONFIRMED and any("between the 15M EMA20" in r for r in s.reasons)


@pytest.mark.parametrize(
    "over,reason",
    [
        (dict(trend={"1h": "sideways", "15m": "bullish", "5m": "sideways", "1m": "sideways"}), "1H is sideways"),
        (dict(trend={"1h": "bearish", "15m": "bearish", "5m": "sideways", "1m": "sideways"}), "1H is"),
        (dict(trend={"1h": "bullish", "15m": "bearish", "5m": "sideways", "1m": "sideways"}), "15M is"),
        (dict(adx={"1h": 15.0, "15m": 28.0, "5m": 20.0, "1m": 15.0}), "ADX 15.0"),
        (dict(pattern={"1h": "HH_HL", "15m": "LH_LL", "5m": "mixed", "1m": "mixed"}), "does not support"),
    ],
)
@pytest.mark.parametrize("side", SIDES)
def test_a_pullback_needs_a_strong_higher_timeframe_trend_in_its_own_direction(side, over, reason):
    sc = scan(st.scan_pullback, bull_pullback(19, **over), side)
    assert not sc.applicable and sc.setup is None and reason.lower() in sc.note.lower()


def test_a_pullback_against_the_trend_is_never_taken():
    """In a bullish market the bearish pullback strategy is inactive; mirrored, the bullish one is."""
    assert not st.scan_pullback(View(bull_pullback(19), "bear"), P, None).applicable
    assert not st.scan_pullback(View(mirror(bull_pullback(19)), "bull"), P, None).applicable


@pytest.mark.parametrize("side", SIDES)
def test_location_gates_hold_a_confirmation_back_with_reasons(side):
    far = scan(st.scan_pullback, bull_pullback(19), side, replace(P, max_risk_atr15=1.0)).setup  # 30.5 pts > 1.0 x 25
    assert far.status == WAITING and any("too far from the structure" in b for b in far.blockers)
    near = scan(st.scan_pullback, bull_pullback(19), side, replace(P, min_risk_atr5=4.0)).setup  # 30.5 < 4 x 10
    assert near.status == WAITING and any("inside normal noise" in b for b in near.blockers)
    crowded = bull_pullback(19, levels=[Level(24565.0, "swing level x2", 2), Level(24600.0, "swing level x2", 2)])
    room = scan(st.scan_pullback, crowded, side).setup  # the next level is only 10 points above the trigger (0.3R)
    assert room.status == WAITING and any("only 10.0 pts to the next level" in b for b in room.blockers)
    roomy = scan(st.scan_pullback, bull_pullback(19, levels=[Level(24565.0, "swing level x2", 2), Level(24640.0, "swing level x2", 2)]), side).setup
    assert roomy.status == CONFIRMED  # 50 points (1.6R) of room


@pytest.mark.parametrize("side", SIDES)
def test_data_and_session_blockers_stop_a_confirmation(side):
    ctx = bull_pullback(19, can_confirm=False, blockers=["5M candles missing 10:20-10:25"])
    s = scan(st.scan_pullback, ctx, side).setup
    assert s.status == WAITING and s.blockers == ["5M candles missing 10:20-10:25"]


@pytest.mark.parametrize("side", SIDES)
def test_stricter_confirmation_needs_follow_through(side):
    strict = dict(strict=True, strict_why="volatility is high")
    first = scan(st.scan_pullback, bull_pullback(19, **strict), side).setup
    assert first.status == WAITING and first.trigger == pytest.approx(at(side, 24590))  # the break candle alone is not enough
    assert "next completed 5M candle must also close beyond it" in first.confirm_text
    held = scan(st.scan_pullback, extend(bull_pullback(19, **strict), (24596, 24601, 24594, 24598)), side).setup
    assert held.status == CONFIRMED and held.trigger == pytest.approx(at(side, 24590))
    failed = scan(st.scan_pullback, extend(bull_pullback(19, **strict), (24596, 24597, 24580, 24586)), side).setup
    assert failed.status == WAITING  # no follow-through: not confirmed (and nothing was invalidated either)


@pytest.mark.parametrize("side", SIDES)
def test_cautions_are_reported_but_do_not_block(side):
    ctx = bull_pullback(19, rsi={"1h": 60.0, "15m": 72.0, "5m": 50.0, "1m": 50.0},
                        trend={"1h": "bullish", "15m": "bullish", "5m": "sideways", "1m": "bearish"})  # fmt: skip
    s = scan(st.scan_pullback, ctx, side).setup
    assert s.status == CONFIRMED and s.blockers == []
    assert any("momentum is already stretched" in a for a in s.against) and any("1M is" in a for a in s.against)


@pytest.mark.parametrize("side", SIDES)
def test_a_new_high_starts_a_new_leg_and_a_new_setup_identity(side):
    old = scan(st.scan_pullback, bull_pullback(19), side).setup
    higher = extend(bull_pullback(19), (24596, 24660, 24595, 24658))  # a new high above 24,650
    sc = scan(st.scan_pullback, higher, side)
    assert sc.setup is None and "no pullback yet" in sc.note  # price is at its new high: the old pullback is history
    lower_high = extend(bull_pullback(19), (24596, 24610, 24592, 24600), (24600, 24602, 24570, 24572))
    assert scan(st.scan_pullback, lower_high, side).setup.key == old.key  # same leg, same identity


# ====================================================================================================================
# B. Breakout and retest
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_breakout_without_a_retest_is_unconfirmed(side):
    """The 11:00 15M candle closes at 24,635: body 45 (1.8x ATR15), at 90% of its range, 35 above the 24,600 level."""
    for through in (24, 26):
        s = scan(st.scan_breakout, bull_breakout(through), side).setup
        assert s.status == DETECTED and s.trigger is None
        assert s.zone == pytest.approx(box(side, LEVEL - 6.25, LEVEL + 6.25))  # the retest zone
        assert s.invalidation == pytest.approx(at(side, LEVEL - 6.25 - 2.5))
        assert any("no retest" in a for a in s.against) and "retests" in s.confirm_text


@pytest.mark.parametrize("side", SIDES)
def test_retest_that_holds_then_a_completed_candle_above_the_retest_high_confirms(side):
    waiting = scan(st.scan_breakout, bull_breakout(27), side).setup
    # slot 27: low 24,601 is inside 24,600 + 6.25; lower wick 10 of 14 points: a rejection
    assert waiting.status == WAITING and waiting.trigger == pytest.approx(at(side, 24615))
    assert waiting.invalidation == pytest.approx(at(side, 24597.5))  # min(24,600, retest low 24,601) less 2.5
    done = scan(st.scan_breakout, bull_breakout(28), side).setup
    assert done.status == CONFIRMED and done.trigger == pytest.approx(at(side, 24615))  # slot 28 closes at 24,620
    assert done.zone == pytest.approx(box(side, 24615, 24625)) and done.blockers == []
    assert done.level == pytest.approx(at(side, LEVEL)) and done.strategy == "breakout_retest"


@pytest.mark.parametrize("side", SIDES)
def test_a_false_breakout_that_closes_back_below_the_level_is_invalidated(side):
    ctx = extend(bull_breakout(26), (24612, 24615, 24588, 24590))  # closes 10 below 24,600 (tolerance 6.25)
    s = scan(st.scan_breakout, ctx, side).setup
    assert s.status == INVALIDATED and f"false {WORD[side]}" in s.note
    assert (" back below " if side == "bull" else " back above ") in s.note  # the direction words follow the side


@pytest.mark.parametrize("side", SIDES)
def test_a_retest_that_fails_to_hold_invalidates(side):
    ctx = replace(bull_breakout(26), c5=bull_breakout(26).c5 + [c5(27, 24612, 24614, 24585, 24592)], price=24592)
    assert scan(st.scan_breakout, ctx, side).setup.status == INVALIDATED
    held = replace(bull_breakout(26), c5=bull_breakout(26).c5 + [c5(27, 24612, 24614, 24590, 24598)], price=24598)
    assert scan(st.scan_breakout, held, side).setup.status in (DETECTED, WAITING)  # a wick below, closes above 24,593.75


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize(
    "candle,reason",
    [
        ((24590, 24650, 24588, 24608), "weak part of its range"),  # body 18 but it closes at 32% of its range: a long upper wick
        ((24598, 24620, 24596, 24608), "only 0.40x ATR15"),  # closes above 24,600 but the body is 10 points
    ],
)
def test_a_weak_breakout_is_rejected(side, candle, reason):
    ctx = replace(bull_breakout(26), c15=bull_breakout(26).c15[:-1] + [c15(7, *candle)])
    sc = scan(st.scan_breakout, ctx, side)
    assert sc.setup is None and f"weak {WORD[side]}" in sc.note and reason in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_breakout_no_bigger_than_the_recent_candles_is_weak(side):
    big_recent = [c15(j, 24500, 24600, 24520, 24590) for j in range(7)]  # recent bodies of 90 points
    ctx = replace(bull_breakout(26), c15=big_recent + [c15(7, 24590, 24640, 24588, 24635)])
    sc = scan(st.scan_breakout, ctx, side)
    assert sc.setup is None and "no bigger than the recent candles" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_price_that_runs_away_without_a_retest_is_a_missed_setup(side):
    """The 5M candle after the breakout closes at 24,676, more than 1x ATR15 (25) beyond the breakout candle's high 24,640."""
    ctx = extend(bull_breakout(24), (24648, 24690, 24646, 24676))
    s = scan(st.scan_breakout, ctx, side).setup
    assert s.status == EXPIRED and "missed" in s.note
    near = extend(bull_breakout(24), (24648, 24670, 24646, 24664))  # 24,664 is within 24,640 + 25
    assert scan(st.scan_breakout, near, side).setup.status == DETECTED


@pytest.mark.parametrize("side", SIDES)
def test_a_retest_that_never_comes_expires(side):
    quiet = [(24640, 24645, 24630, 24641)] * 25  # lows stay 24 points above the level; closes stay inside the limits
    s = scan(st.scan_breakout, extend(bull_breakout(24), *quiet), side).setup
    assert s.status == EXPIRED and "no retest" in s.note
    assert scan(st.scan_breakout, extend(bull_breakout(24), *quiet[:20]), side).setup.status == DETECTED


@pytest.mark.parametrize(
    "trend,ok",
    [
        (("bullish", "bullish"), True), (("bullish", "sideways"), True), (("sideways", "sideways"), True), (("sideways", "bullish"), True),
        (("bearish", "sideways"), False), (("bullish", "bearish"), False), (("unknown", "bullish"), False), (("bearish", "bearish"), False),
    ],
)  # fmt: skip
def test_a_breakout_is_never_taken_against_a_higher_timeframe(trend, ok):
    ctx = bull_breakout(28, trend={"1h": trend[0], "15m": trend[1], "5m": "sideways", "1m": "sideways"})
    assert st.scan_breakout(View(ctx, "bull"), P, None).applicable is ok


@pytest.mark.parametrize("side", SIDES)
def test_breakouts_use_and_name_the_phase2_levels(side):
    ctx = bull_breakout(28, levels=[Level(LEVEL, "previous day high"), Level(24700.0, "swing level x2", 2)])
    s = scan(st.scan_breakout, ctx, side).setup
    assert s.status == CONFIRMED and s.level_label == "previous day high" and any("previous day high" in r for r in s.reasons)
    nothing = bull_breakout(28, levels=[Level(24700.0, "swing level x2", 2)])  # nothing was crossed
    sc = scan(st.scan_breakout, nothing, side)
    assert sc.setup is None and f"no 15M close {'above' if side == 'bull' else 'below'} a meaningful level" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_tracked_breakout_keeps_its_level_when_the_clusters_move(side):
    first = scan(st.scan_breakout, bull_breakout(27), side).setup
    shifted = bull_breakout(27, levels=[Level(LEVEL + 1.0, "swing level x3", 3), Level(24700.0, "swing level x2", 2)])
    fresh = scan(st.scan_breakout, shifted, side).setup
    kept = scan(st.scan_breakout, shifted, side, prior=first).setup
    assert fresh.level == pytest.approx(at(side, LEVEL + 1.0)) and kept.level == pytest.approx(first.level)
    assert kept.invalidation == pytest.approx(first.invalidation) and kept.key == first.key


@pytest.mark.parametrize("side", SIDES)
def test_a_breakout_into_a_nearby_level_is_held_back_by_the_room_check(side):
    ctx = bull_breakout(28, levels=[Level(LEVEL, "swing level x3", 3), Level(24625.0, "swing level x2", 2)])
    s = scan(st.scan_breakout, ctx, side).setup  # the candle crossed both; the first one (24,600) is the broken level
    # the retest trigger is 24,615 and the stop-out 24,597.5 (17.5 pts), but 24,625 is only 10 points above the trigger
    assert s.level == pytest.approx(at(side, LEVEL)) and s.status == WAITING
    assert any("only 10.0 pts to the next level" in b for b in s.blockers)


# ====================================================================================================================
# C. Range reversal
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_failed_breakdown_at_the_range_support_is_a_reversal_setup(side):
    """Slot 32 closes 10 below 24,500 (beyond the 6.25 tolerance); slot 33 (the low, 24,484) reclaims it."""
    sc = scan(st.scan_range, bull_range(32), side)
    assert sc.setup is None and "break in progress" in sc.note  # still beyond the edge: not a reversal yet
    waiting = scan(st.scan_range, bull_range(33), side).setup
    assert waiting.status == WAITING and any("failed" in r and "reclaimed" in r for r in waiting.reasons)
    assert waiting.trigger == pytest.approx(at(side, 24508))
    done = scan(st.scan_range, bull_range(35), side).setup
    # slot 34 stays under the base high 24,508; slot 35 (24,518, body 9, 73% of its range) closes above 24,509
    assert done.status == CONFIRMED and done.trigger == pytest.approx(at(side, 24509))
    assert done.invalidation == pytest.approx(at(side, 24484 - 2.5)) and done.zone == pytest.approx(box(side, 24509, 24519))
    assert done.strategy == "range_reversal" and done.direction == OPT[side]
    assert any("a range between 24,500.00 and 24,620.00" in r or "a range between 25,380.00 and 25,500.00" in r for r in done.reasons)  # low to high, either side


def rejection_range(through):
    """A plain rejection at the support, with no break beyond it: a hammer at 24,497 then a break of the base."""
    rows = {30: (24520, 24524, 24508, 24510), 31: (24512, 24514, 24497, 24510), 32: (24510, 24516, 24508, 24513), 33: (24513, 24524, 24511, 24522)}
    base = [c5(i, 24540 - i, 24545 - i, 24535 - i, 24538 - i) for i in range(30)]
    bars = base + [c5(i, *rows[i]) for i in range(30, through + 1)]
    return bull_range(5, c5=bars, price=bars[-1].close, now=bars[-1].end + 3)


@pytest.mark.parametrize("side", SIDES)
def test_a_rejection_candle_at_the_support_is_enough_without_a_breakdown(side):
    waiting = scan(st.scan_range, rejection_range(32), side).setup
    # base high so far: max(hammer 24,514, slot 32's 24,516) = 24,516
    assert waiting.status == WAITING and waiting.trigger == pytest.approx(at(side, 24516))
    done = scan(st.scan_range, rejection_range(33), side).setup  # slot 33: 24,522, body 9, closes at 85% of its range
    assert done.status == CONFIRMED and done.trigger == pytest.approx(at(side, 24516))
    assert done.invalidation == pytest.approx(at(side, 24497 - 2.5)) and any("rejection candle" in r for r in done.reasons)


@pytest.mark.parametrize("side", SIDES)
def test_the_middle_of_a_range_has_no_edge(side):
    mid = [c5(i, 24560, 24566, 24554, 24560) for i in range(40)]
    ctx = bull_range(5, c5=mid, price=24560.0, now=mid[-1].end + 3)  # 50% of the way across 24,500-24,620
    sc = scan(st.scan_range, ctx, side)
    assert sc.setup is None and sc.applicable and "middle of a range" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_range_reversals_need_a_sideways_market_without_a_breakout(side):
    trending = bull_range(35, trend={"1h": "bullish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment="pausing")
    sc = scan(st.scan_range, trending, side)
    assert not sc.applicable and "not range-bound" in sc.note
    strong = scan(st.scan_range, bull_range(35, adx={"1h": 15.0, "15m": 27.0, "5m": 12.0, "1m": 10.0}), side)
    assert not strong.applicable and "15M ADX 27.0" in strong.note
    breaking = scan(st.scan_range, bull_range(35, breakout={"15m": "breakdown", "1h": None}), side)
    assert not breaking.applicable and "Phase 2 flags a 15M" in breaking.note and "is in progress" in breaking.note


@pytest.mark.parametrize("side", SIDES)
def test_a_genuine_break_of_the_range_invalidates_the_reversal(side):
    closed_below = replace(bull_range(35), c15=bull_range(35).c15 + [c15(9, 24512, 24514, 24480, 24488)])  # a 15M close below 24,493.75
    s = scan(st.scan_range, closed_below, side).setup
    assert s.status == INVALIDATED and "genuine break" in s.note
    deep = extend(bull_range(35), (24518, 24520, 24470, 24516))  # a push 30 points under the edge (limit: 25)
    assert scan(st.scan_range, deep, side).setup.status == INVALIDATED


@pytest.mark.parametrize("side", SIDES)
def test_a_range_must_have_sensible_boundaries(side):
    wide = bull_range(35, levels=[Level(SUPPORT, "swing level x3", 3), Level(24800.0, "swing level x3", 3)])  # 300 points = 12x ATR15
    sc = scan(st.scan_range, wide, side)
    assert sc.setup is None and "wider than 6x ATR15" in sc.note
    narrow = bull_range(35, levels=[Level(SUPPORT, "swing level x3", 3), Level(24520.0, "swing level x3", 3)])  # 20 points: no range
    assert "no level at least 1.5x ATR15" in scan(st.scan_range, narrow, side).note


@pytest.mark.parametrize("side", SIDES)
def test_price_that_has_left_the_range_edge_ends_the_approach(side):
    away = [c5(36 + k, 24527, 24530, 24526, 24529) for k in range(12)]  # lows above the 24,525 approach zone, still in the outer quarter
    bars = bull_range(35).c5 + away
    ctx = replace(bull_range(35), c5=bars, price=bars[-1].close, now=bars[-1].end + 3)
    sc = scan(st.scan_range, ctx, side)
    assert sc.setup is None and "has not come back" in sc.note


# ====================================================================================================================
# Strategy selection by market condition, and robustness
# ====================================================================================================================
def trends(h, m, f="sideways", o="sideways"):
    return {"1h": h, "15m": m, "5m": f, "1m": o}


@pytest.mark.parametrize(
    "name,over,expect",
    [
        ("strong bullish trend", dict(), {"pb": (1, 0), "bo": (1, 0), "rr": (0, 0)}),
        ("bullish trend, 15M pausing", dict(trend=trends("bullish", "sideways"), alignment="pausing"), {"pb": (1, 0), "bo": (1, 0), "rr": (0, 0)}),
        ("sideways market", dict(trend=trends("sideways", "sideways"), alignment="range_bound", adx={"1h": 15.0, "15m": 14.0}), {"pb": (0, 0), "bo": (1, 1), "rr": (1, 1)}),
        ("sideways but trending ADX", dict(trend=trends("sideways", "sideways"), alignment="range_bound", adx={"1h": 15.0, "15m": 30.0}), {"pb": (0, 0), "bo": (1, 1), "rr": (0, 0)}),
        ("15M leading a sideways 1H", dict(trend=trends("sideways", "bullish"), alignment="developing"), {"pb": (0, 0), "bo": (1, 0), "rr": (0, 0)}),
        ("1H and 15M conflict", dict(trend=trends("bullish", "bearish"), alignment="conflicting"), {"pb": (0, 0), "bo": (0, 0), "rr": (0, 0)}),
        ("not enough data", dict(trend=trends("unknown", "unknown"), alignment="unknown"), {"pb": (0, 0), "bo": (0, 0), "rr": (0, 0)}),
    ],
)  # fmt: skip
def test_strategy_selection_follows_the_market_condition(name, over, expect):
    ctx = bull_pullback(19, **over)
    scans = st.scan_all(ctx, P, {})
    got = {k: (int(scans[(s, "bull")].applicable), int(scans[(s, "bear")].applicable)) for k, s in
           (("pb", "trend_pullback"), ("bo", "breakout_retest"), ("rr", "range_reversal"))}
    assert got == expect, name


def test_strategy_selection_for_a_strong_bearish_trend():
    scans = st.scan_all(mirror(bull_pullback(19)), P, {})  # the same market, mirrored: a consistent bearish context
    got = {s: (int(scans[(s, "bull")].applicable), int(scans[(s, "bear")].applicable)) for s in ("trend_pullback", "breakout_retest", "range_reversal")}
    assert got == {"trend_pullback": (0, 1), "breakout_retest": (0, 1), "range_reversal": (0, 0)}


def test_one_failing_scanner_cannot_silence_the_others(monkeypatch):
    def boom(v, p, prior):
        raise RuntimeError("bug")

    patched = [replace(d, scanner=boom) if d.name == "trend_pullback" else d for d in registry.REGISTRY]  # the scanners now come from the registry
    monkeypatch.setattr(registry, "REGISTRY", patched)
    scans = st.scan_all(bull_pullback(19), P, {})
    assert not scans[("trend_pullback", "bull")].applicable and "scanner error" in scans[("trend_pullback", "bull")].note
    assert scans[("breakout_retest", "bull")].applicable  # the others ran


def test_parameters_can_be_overridden_from_the_environment():
    env = {"STRATEGY_VOL_EXTREME": "3.5", "STRATEGY_CONFIRM_VALID_BARS": "6", "STRATEGY_LAST_ENTRY": "14:30", "STRATEGY_OPENING_RANGE_LEVELS": "no"}
    p = Params.from_env(env=env)
    assert (p.vol_extreme, p.confirm_valid_bars, p.last_entry, p.opening_range_levels) == (3.5, 6, "14:30", False)
    assert p.min_pullback_atr15 == Params().min_pullback_atr15  # everything else stays as it was
    with pytest.raises(ValueError, match="STRATEGY_VOL_EXTREME"):
        Params.from_env(env={"STRATEGY_VOL_EXTREME": "high"})


def test_the_setup_dataclass_reports_its_direction_and_identity():
    s = Setup("trend_pullback", "bear", "trend_pullback:bear:123", DETECTED, 123.0)
    assert (s.direction, s.id) == ("PE", "trend_pullback:bear:123")


# ====================================================================================================================
# A confirmed setup must not disappear because a measurement drifted after it was confirmed
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_confirmed_pullback_survives_the_emas_moving_after_the_confirmation(side):
    just_in = dict(levels=[], ema={"1h": (24400.0, 24300.0), "15m": (24700.0, 24650.0), "5m": (24569.0, 24568.0)})  # low 24,562 is 6 under EMA20: inside 6.25
    drifted = dict(levels=[], ema={"1h": (24400.0, 24300.0), "15m": (24700.0, 24650.0), "5m": (24571.0, 24570.0)})  # 8 under both EMAs: outside
    first = scan(st.scan_pullback, bull_pullback(19, **just_in), side).setup
    assert first.status == CONFIRMED
    later = bull_pullback(19, **drifted)
    assert scan(st.scan_pullback, later, side).setup is None  # judged afresh, the low is no longer at a zone...
    kept = scan(st.scan_pullback, later, side, prior=first).setup
    assert kept.status == CONFIRMED and kept.key == first.key  # ...but the low a tracked setup rests on is not re-judged
    assert (kept.trigger, kept.invalidation, kept.extreme) == pytest.approx((first.trigger, first.invalidation, first.extreme))
    waiting = scan(st.scan_pullback, bull_pullback(17, **just_in), side).setup  # the same before the confirmation
    assert waiting.status == WAITING
    assert scan(st.scan_pullback, bull_pullback(17, **drifted), side, prior=waiting).setup.status == WAITING


@pytest.mark.parametrize("side", SIDES)
def test_a_new_lower_low_is_still_judged_against_the_zone_even_when_a_setup_is_tracked(side):
    """What is kept is the low the setup was found at. A deeper low made afterwards is a new fact and is judged normally."""
    drifted = dict(levels=[], ema={"1h": (24400.0, 24300.0), "15m": (24700.0, 24650.0), "5m": (24571.0, 24570.0)})
    just_in = dict(levels=[], ema={"1h": (24400.0, 24300.0), "15m": (24700.0, 24650.0), "5m": (24569.0, 24568.0)})
    tracked = scan(st.scan_pullback, bull_pullback(17, **just_in), side).setup  # resting on the 24,562 low
    deeper = extend(bull_pullback(17, **drifted), (24572, 24574, 24540, 24545))  # slot 18 makes a new low at 24,540, far under the zone
    sc = scan(st.scan_pullback, deeper, side, prior=tracked)
    assert sc.setup is None and "not at a meaningful level" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_range_reversal_is_judged_where_price_touched_the_edge_not_where_it_is_now(side):
    """A narrow range, 24,500-24,540 (1.6x ATR15), whose outer quarter ends at 24,510. The hammer touched 24,497 (3 points
    under the edge), but the candles that follow close at 24,513 and 24,522: price is already well past the quarter
    when the base forms and when it breaks, which is simply what a bounce off the edge looks like."""
    levels = [Level(SUPPORT, "swing level x3", 3), Level(24540.0, "swing level x3", 3)]
    waiting = scan(st.scan_range, replace(rejection_range(32), levels=levels), side).setup
    assert waiting.status == WAITING and waiting.trigger == pytest.approx(at(side, 24516))  # price 24,513 = 33% of the way across
    ctx = replace(rejection_range(33), levels=levels)
    done = scan(st.scan_range, ctx, side).setup
    assert ctx.price == 24522 and (ctx.price - SUPPORT) / 40 > 0.5  # 55% of the way across
    assert done.status == CONFIRMED and done.trigger == pytest.approx(at(side, 24516)) and done.blockers == []
    assert done.zone == pytest.approx(box(side, 24516, 24526)) and done.level == pytest.approx(at(side, SUPPORT))
    assert any("came back to the range" in r and "3.0 pts" in r for r in done.reasons)  # the touch is what is described


@pytest.mark.parametrize("side", SIDES)
def test_a_tracked_range_reversal_keeps_its_edge_when_price_passes_a_level_inside_the_range(side):
    """Once price closes at 24,526 the highest level under it is the 24,530 level inside the range, which would make
    the 24,497 low look like a break of 24,530. A setup that is being tracked keeps the edge it was found at."""
    first = scan(st.scan_range, rejection_range(33), side).setup
    assert first.status == CONFIRMED and first.level == pytest.approx(at(side, SUPPORT))
    levels = [Level(SUPPORT, "swing level x3", 3), Level(24530.0, "swing level x2", 2), Level(RESISTANCE, "swing level x3", 3)]
    later = extend(replace(rejection_range(33), levels=levels), (24522, 24528, 24521, 24526))
    kept = scan(st.scan_range, later, side, prior=first).setup
    assert kept.status == CONFIRMED and kept.key == first.key
    assert (kept.level, kept.trigger, kept.invalidation) == pytest.approx((first.level, first.trigger, first.invalidation))


# ====================================================================================================================
# The opening range is observed, not traded
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_break_that_completed_inside_the_opening_range_cannot_confirm_an_entry(side):
    """The break candle (slot 19) ends at t5(20). Entries open when that moment has passed, but a candle made of
    opening-range trading is not a confirmation: the next completed candle must do it."""
    wide = dict(atr={"1h": 60.0, "15m": 30.0, "5m": 10.0, "1m": 4.0})  # the taller base (trigger 24,598) is 38.5 points above the stop: 1.3x ATR15
    inside = replace(bull_pullback(19, **wide), confirm_after=t5(20))
    s = scan(st.scan_pullback, inside, side).setup
    assert s.status == WAITING and s.trigger == pytest.approx(at(side, 24598))  # the base now includes the skipped candle's high
    assert any("completed inside the opening range" in a for a in s.against)
    assert scan(st.scan_pullback, replace(bull_pullback(19, **wide), confirm_after=t5(20) - 1), side).setup.status == CONFIRMED  # one second earlier: allowed
    later = scan(st.scan_pullback, extend(inside, (24596, 24608, 24595, 24606)), side).setup  # slot 20 breaks the new base after the window
    assert later.status == CONFIRMED and later.trigger == pytest.approx(at(side, 24598)) and later.confirmed_bar_end == t5(21)


# ====================================================================================================================
# The timeframes' roles: 1H direction, 15M structure, 5M confirmation; the 1M never decides
# ====================================================================================================================
def outcome(scans):
    return {k: (s.applicable, s.setup.status if s.setup else None, s.setup.trigger if s.setup else None) for k, s in scans.items()}


@pytest.mark.parametrize("one_minute", ["bullish", "bearish", "sideways", "unknown"])
def test_the_1m_trend_never_changes_which_strategies_run_or_whether_they_confirm(one_minute):
    base = bull_pullback(19)
    reference = outcome(st.scan_all(base, P, {}))
    assert reference[("trend_pullback", "bull")][1] == CONFIRMED
    assert outcome(st.scan_all(replace(base, trend={**base.trend, "1m": one_minute}), P, {})) == reference
    conflict = {"1h": "bullish", "15m": "bearish", "5m": "bullish"}  # 1H and 15M disagree: a bullish 1M cannot repair that
    quiet = outcome(st.scan_all(replace(base, trend={**conflict, "1m": one_minute}, alignment="conflicting"), P, {}))
    assert not any(applicable for applicable, _, _ in quiet.values())


@pytest.mark.parametrize("side", SIDES)
def test_a_5m_trend_against_the_setup_does_not_decide_either(side):
    base = bull_pullback(19)
    for five in ("bearish", "sideways", "bullish"):
        s = scan(st.scan_pullback, replace(base, trend={**base.trend, "5m": five}), side).setup
        assert s.status == CONFIRMED  # the 5M candle is the confirmation, not the 5M trend label


# ====================================================================================================================
# What the confirmation says about itself (the journal and the dashboard are read afterwards, so the wording matters)
# ====================================================================================================================
def test_the_bearish_side_describes_itself_in_bearish_words():
    s = scan(st.scan_pullback, bull_pullback(19), "bear").setup
    assert any("bearish rejection candle" in r and "upper wick" in r for r in s.reasons) and not any("bullish" in r for r in s.reasons)
    bull = scan(st.scan_pullback, bull_pullback(19), "bull").setup
    assert any("bullish rejection candle" in r and "lower wick" in r for r in bull.reasons)
    ctx = extend(bull_pullback(16), (24570, 24583, 24562, 24565), (24566, 24590, 24565, 24571), (24571, 24600, 24570, 24598))  # no rejection candle: a momentum candle
    for side, word in (("bull", "bullish"), ("bear", "bearish")):
        momentum = scan(st.scan_pullback, ctx, side).setup
        assert momentum.status in (CONFIRMED, EXPIRED) and any(f"strong {word} candle" in r for r in momentum.reasons)


@pytest.mark.parametrize("side", SIDES)
def test_a_stricter_confirmation_names_both_candles_and_a_normal_one_names_the_trigger_candle(side):
    strict = dict(strict=True, strict_why="midday low-activity window")
    held = scan(st.scan_pullback, extend(bull_pullback(19, **strict), (24596, 24601, 24594, 24598)), side).setup
    rows = {q["name"]: q["text"] for q in held.quality}
    assert "Trigger candle" not in rows and held.status == CONFIRMED
    assert rows["Break candle"].startswith("10:50-10:55 5M, O ") and "body 1.1x ATR5" in rows["Break candle"]  # the candle that has to look like a break
    assert rows["Follow-through candle"].startswith("10:55-11:00 5M, O ") and "closed" in rows["Follow-through candle"] and "the trigger" in rows["Follow-through candle"]
    assert held.confirmed_bar_end == t5(21)  # the confirmation completed with the second candle
    normal = scan(st.scan_pullback, bull_pullback(19), side).setup
    names = [q["name"] for q in normal.quality]
    assert "Trigger candle" in names and "Break candle" not in names
    trigger = next(q["text"] for q in normal.quality if q["name"] == "Trigger candle")
    assert trigger.startswith("10:50-10:55 5M, O ") and "closed 86% of the way " + ("up" if side == "bull" else "down") + " its range" in trigger


@pytest.mark.parametrize("side,peak,trough", [("bull", "high", "low"), ("bear", "low", "high")])
def test_the_words_for_the_legs_extreme_and_the_pullbacks_extreme_follow_the_direction(side, peak, trough):
    s = scan(st.scan_pullback, bull_pullback(19), side).setup
    assert any(f"pulled back 88.0 pts" in r and f"{peak} to " in r for r in s.reasons), s.reasons  # from the leg's extreme to the pullback's
    sc = scan(st.scan_pullback, bull_pullback(19, levels=[], ema={"1h": (24400.0, 24300.0), "15m": (24700.0, 24650.0), "5m": (24400.0, 24390.0)}), side)
    assert sc.setup is None and f"pullback {trough} " in sc.note and "not at a meaningful level" in sc.note
    higher = extend(bull_pullback(19), (24596, 24660, 24595, 24658))
    gone = scan(st.scan_pullback, higher, side, prior=s)
    assert f"new {peak} at " in gone.note and f"no pullback yet from the new {peak}" in gone.note
    touched = scan(st.scan_range, rejection_range(33), side).setup
    assert any(f"({trough} " in r for r in touched.reasons), touched.reasons  # the touch of the range edge


# ====================================================================================================================
# The confirming candle, as data (the journal and the performance reports read it; the text rows are for people)
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_the_confirming_candle_is_exported_with_its_real_prices(side):
    s = scan(st.scan_pullback, bull_pullback(19), side).setup  # slot 19: open 24,585, high 24,598, low 24,584, close 24,596
    expected = {"start": t5(19), "end": t5(20), "open": at(side, 24585), "close": at(side, 24596)}
    expected["high"], expected["low"] = (24598, 24584) if side == "bull" else (at(side, 24584), at(side, 24598))  # a mirrored candle swaps its extremes
    assert s.confirm_candle == pytest.approx(expected) and s.break_candle is None  # an ordinary confirmation is one candle
    assert s.confirm_candle["low"] < s.confirm_candle["high"] and s.confirmed_bar_end == s.confirm_candle["end"]


@pytest.mark.parametrize("side", SIDES)
def test_a_stricter_confirmation_exports_both_candles(side):
    strict = dict(strict=True, strict_why="midday low-activity window")
    held = scan(st.scan_pullback, extend(bull_pullback(19, **strict), (24596, 24601, 24594, 24598)), side).setup  # slot 20: the follow-through
    assert held.break_candle["start"] == t5(19) and held.break_candle["end"] == t5(20) and held.break_candle["close"] == pytest.approx(at(side, 24596))
    assert held.confirm_candle["start"] == t5(20) and held.confirm_candle["close"] == pytest.approx(at(side, 24598))
    assert held.confirmed_bar_end == held.confirm_candle["end"]
    waiting = scan(st.scan_pullback, bull_pullback(19, **strict), side).setup  # nothing has confirmed yet: there is no candle to export
    assert waiting.confirm_candle is None and waiting.break_candle is None
