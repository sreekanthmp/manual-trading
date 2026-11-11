"""Stage 2.1: Momentum Continuation, EMA Trend Continuation and Volatility Expansion, on hand-built candles (the numbers are worked out by hand in the comments, as
in scenarios.py). NIFTY ~24,600, ATR(5M) = 10, ATR(15M) = 25: the touch tolerance is 6.25, the minimum pullback depth 12.5, the entry-chase limit 10, the
invalidation buffer 2.5, a strong candle's body 7 and a confirming candle's body 4. Every scenario is also run mirrored (the bearish twin) through the real
SetupBook, so the lifecycle, the announcements and the decision are exercised, not only the scanners.

The leg: 5M candles rising to a high of 24,650 at slot 11 (10:10), whose origin is the 15M swing low 24,500 (a 150 point, 6x ATR15 leg).
    slot  open   high   low    close
     12   24640  24646  24638  24643   the pause begins: low 24,638 is 12 under the high (0.48x ATR15, under the 0.5 that makes a Trend Pullback)
     13   24643  24647  24640  24641
     14   24641  24645  24639  24644
     15   24644  24648  24642  24647   the pause high is 24,648
     16   24647  24662  24646  24660   body 13 (1.3x ATR5), closes at 88% of its range, through the prior high 24,650 (and the pause high)"""

from collections import Counter
from dataclasses import replace

import pytest
from scenarios import M, P, bull, c5, mirror, pullback_15m, rise, t5

from app import registry
from app import strategy as st
from app import trading as tr
from app.registry import WITH_TREND, StrategyDef
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Context, Scan, Setup, View
from app.strategies.trend_family import trend_leg

SIDES = ["bull", "bear"]
MC, ETC, VE, TP = "momentum_continuation", "ema_trend_continuation", "volatility_expansion", "trend_pullback"
LEG_HIGH = (24630, 24650, 24626, 24640)
PAUSE = {12: (24640, 24646, 24638, 24643), 13: (24643, 24647, 24640, 24641), 14: (24641, 24645, 24639, 24644), 15: (24644, 24648, 24642, 24647), 16: (24647, 24662, 24646, 24660)}


def at(side, x):
    return x if side == "bull" else M - x


def box(side, lo, hi):
    return (lo, hi) if side == "bull" else (M - hi, M - lo)


def trend_ctx(through, rows=None, ema5=(24625.0, 24615.0), slope15=6.0, stack15=30, slope5=3.0, **over):
    """The leg and the pause up to the close of 5M slot `through`. ema5 is the 5M (EMA20, EMA50): the pause low 24,638 is above the EMA20 zone (24,631.25) when the
    EMA20 is 24,625, and inside it when the EMA20 is 24,636."""
    bars = rise(0, 11, 24560, 7) + [c5(11, *LEG_HIGH)] + [c5(i, *(rows or PAUSE)[i]) for i in range(12, through + 1)]
    d = dict(
        c5=bars, c15=pullback_15m(), now=bars[-1].end + 3, price=bars[-1].close,
        ema={"1h": (24400.0, 24300.0), "15m": (24570.0, 24520.0), "5m": ema5, "1m": (24596.0, 24594.0)},
        ema_slope={"15m": {"ema20": slope15, "ema50": 4.0, "stack_bars": stack15}, "5m": {"ema20": slope5, "ema50": 2.0, "stack_bars": 40}},
    )  # fmt: skip
    d.update(over)
    return bull(**d)


def oriented(ctx, side):
    return ctx if side == "bull" else mirror(ctx)


def play(build, throughs, side="bull", p=P, **kw):
    """The scenario candle by candle through the real SetupBook (what the engine does once per completed candle)."""
    book, steps = tr.SetupBook(p), []
    for n in throughs:
        ctx = oriented(build(n, **kw), side)
        res = book.update(ctx, {"session": tr.session_info(ctx.now, p)}, ctx.now)
        res["live_now"] = {slot: (x.status, x.trigger) for slot, x in book.active.items()}  # the book mutates the objects it keeps: what each step showed is recorded here
        steps.append((ctx, res))
    return book, steps


def scan_one(name, ctx, side, prior=None):
    return st.scan_all(oriented(ctx, side), P, {} if prior is None else {(name, side): prior})[(name, side)]


def live(book, name, side):
    return book.active.get((name, side))


# ====================================================================================================================
# Momentum Continuation
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_pause_under_the_prior_high_is_announced_after_two_candles_and_confirmed_by_a_strong_candle_through_the_high(side):
    book, steps = play(trend_ctx, range(12, 17), side)
    scans = [r["scans"][(MC, side)] for _, r in steps]
    assert scans[0].setup is None and "a pause needs at least 2" in scans[0].note and (MC, side) not in steps[0][1]["live_now"]  # one candle under a new high is not a pause: nothing is announced
    for i in (1, 2, 3):  # slots 13, 14, 15
        status, trigger = steps[i][1]["live_now"][(MC, side)]
        assert status == DETECTED and trigger == pytest.approx(at(side, 24650))  # the trigger is the PRIOR high, above the pause's own 24,648
    s = live(book, MC, side)
    assert (s.status, s.direction, s.strategy) == (CONFIRMED, "CE" if side == "bull" else "PE", MC)
    assert s.trigger == pytest.approx(at(side, 24650)) and s.invalidation == pytest.approx(at(side, 24635.5))  # the pause low 24,638 less a quarter of ATR5
    assert s.zone == pytest.approx(box(side, 24650, 24660)) and s.blockers == [] and s.candle_confirmed
    assert s.group == f"leg:{side}:{int(t5(11))}" and s.level == pytest.approx(at(side, 24500)) and s.level_label == "leg origin"
    text = " ".join(s.reasons)
    assert "paused 12.0 pts (0.48x ATR15)" in text and "a pause, not a pullback" in text and "150.0 pt leg (6.0x ATR15)" in text and "strong" in text
    assert "through the prior" in s.confirm_text and f"{at(side, 24650):,.2f}" in s.confirm_text
    assert s.plan["risk_points"] == pytest.approx(14.5) and s.plan["targets"] == []  # nothing is invented where no level was detected ahead
    mine = [e for e in book.events if e["strategy"] == MC]
    assert [e["status"] for e in reversed(mine)] == [DETECTED, CONFIRMED]  # announced once each, in order


@pytest.mark.parametrize("side", SIDES)
def test_the_two_other_trend_strategies_say_why_they_do_not_take_the_same_pause(side):
    _, steps = play(trend_ctx, range(13, 16), side)
    for _, r in steps:
        assert r["scans"][(ETC, side)].setup is None and "has not reached the 5M EMA20 zone" in r["scans"][(ETC, side)].note
        assert r["scans"][(TP, side)].setup is None  # 0.48x ATR15 deep: too shallow to be a Trend Pullback
    assert "too shallow" in steps[-1][1]["scans"][(TP, side)].note


@pytest.mark.parametrize("side", SIDES)
def test_a_candle_that_closes_above_the_pause_but_under_the_prior_high_does_not_confirm(side):
    rows = {**PAUSE, 16: (24641, 24649, 24640, 24648)}  # a strong candle (body 7), but its close 24,648 is under the prior high 24,650
    book, _ = play(trend_ctx, range(12, 17), side, rows=rows)
    s = live(book, MC, side)
    assert s.status == DETECTED and s.trigger == pytest.approx(at(side, 24650))


@pytest.mark.parametrize("side", SIDES)
def test_a_candle_through_the_high_without_strength_does_not_confirm(side):
    rows = {**PAUSE, 16: (24647, 24655, 24646, 24652)}  # closes above 24,650 but its body is 5: over a confirming candle's 4, under a strong candle's 7
    book, _ = play(trend_ctx, range(12, 17), side, rows=rows)
    assert live(book, MC, side).status == DETECTED  # a pause has no rejection candle to fall back on: it needs a strong candle


@pytest.mark.parametrize("side", SIDES)
def test_in_stricter_confirmation_the_next_candle_must_also_hold_the_break(side):
    book, _ = play(trend_ctx, range(12, 17), side, strict=True, strict_why="midday low-activity window")
    s = live(book, MC, side)
    assert s.status == WAITING and not s.candle_confirmed and "must also close beyond it" in s.confirm_text
    rows = {**PAUSE, 17: (24660, 24663, 24655, 24657)}  # the next candle closes at 24,657: beyond the trigger, and within the 10 point entry area
    book, _ = play(trend_ctx, range(12, 18), side, rows=rows, strict=True, strict_why="midday low-activity window")
    assert live(book, MC, side).status == CONFIRMED
    rows = {**PAUSE, 17: (24660, 24661, 24645, 24648)}  # ... and one that closes back under the trigger is no follow-through: it never confirms
    book, _ = play(trend_ctx, range(12, 18), side, rows=rows, strict=True, strict_why="x")
    s = live(book, MC, side)
    assert s.status == WAITING and not s.candle_confirmed and not tr.setup_dict(s, 24648.0, True, True)["actionable"]  # (a setup only moves forward: it waits on, unconfirmed)


@pytest.mark.parametrize("side", SIDES)
def test_the_end_of_a_momentum_setup_is_explained(side):
    # price closes under the invalidation 24,635.5 after the confirmation
    rows = {**PAUSE, 17: (24660, 24661, 24630, 24634)}
    book, _ = play(trend_ctx, range(12, 18), side, rows=rows)
    assert live(book, MC, side) is None and "closed " in book.recent[0].note and book.recent[0].status == INVALIDATED
    # the next candle closes at +9 (still inside the 10 point entry area): the setup stands
    rows = {**PAUSE, 17: (24660, 24662, 24656, 24659)}
    book, _ = play(trend_ctx, range(12, 18), side, rows=rows)
    assert live(book, MC, side).status == CONFIRMED
    # ... and one that closes 11 beyond the trigger has run away: do not chase
    rows = {**PAUSE, 17: (24660, 24664, 24659, 24661)}
    book, _ = play(trend_ctx, range(12, 18), side, rows=rows)
    assert book.recent[0].status == EXPIRED and "the entry opportunity has passed" in book.recent[0].note
    # no resumption at all within mc_max_bars candles of the low
    flat = {i: (24640, 24645, 24639, 24642) for i in range(13, 40)}
    flat[12] = PAUSE[12]
    book, _ = play(trend_ctx, range(12, 26), side, rows=flat)
    assert live(book, MC, side) is None and any("no resumption within 12 candles" in e["text"] for e in book.events)


@pytest.mark.parametrize("side", SIDES)
def test_a_blocked_momentum_setup_keeps_its_blocker_and_is_never_actionable(side):
    book, steps = play(trend_ctx, range(12, 17), side, can_confirm=False, blockers=["historical data is stale: today's refresh has not completed"])
    s = live(book, MC, side)
    assert s.status == WAITING and s.candle_confirmed and s.blockers == ["historical data is stale: today's refresh has not completed"]
    assert not tr.setup_dict(s, steps[-1][0].price, True, True)["actionable"] and tr.setup_dict(s, steps[-1][0].price, True, True)["stage"] == "BLOCKED"


@pytest.mark.parametrize("side", SIDES)
def test_momentum_continuation_applies_only_in_a_strong_trend_and_says_why_not(side):
    def note(**kw):
        sc = scan_one(MC, trend_ctx(14, **kw), side)
        return sc.applicable, sc.note

    assert note()[0] is True
    ok, text = note(adx={"1h": 30.0, "15m": 20.0, "5m": 12.0, "1m": 10.0})
    assert not ok and "15M ADX 20.0 is below 25" in text  # a trend, but not strong enough for momentum
    ok, text = note(trend={"1h": "bullish", "15m": "sideways", "5m": "sideways", "1m": "sideways"})
    assert not ok and "needs both" in text
    ok, text = note(trend={"1h": "bearish", "15m": "bearish", "5m": "bearish", "1m": "bearish"})
    assert not ok
    ok, text = note(slope15=-2.0)
    assert not ok and "is not " in text and "there is no trend to continue" in text  # the 15M EMA20 is not moving with the trend
    ok, text = note(slope5=0.0)
    assert not ok
    ok, text = note(pattern={"1h": "HH_HL", "15m": "LH_LL", "5m": "mixed", "1m": "mixed"})
    assert not ok and "structure" in text
    ok, text = note(ema_slope={})
    assert ok and "not available yet" in text  # missing data is said, and nothing is invented
    ok, text = note(atr={"1h": 60.0, "15m": 25.0, "5m": None, "1m": 4.0})
    assert ok and "ATR is not available yet" in text


def test_a_leg_that_is_too_small_is_not_continued():
    ctx = trend_ctx(14, swings={"15m": [("L", 0.0 + t5(0), 24630.0), ("H", t5(9), 24650.0)]})  # a 20 point leg: under 1.0x ATR15 (25)
    sc = scan_one(MC, ctx, "bull")
    assert sc.applicable and sc.setup is None and "too small to continue" in sc.note


def test_a_close_under_the_leg_origin_ends_the_structure():
    flat = {**PAUSE, 14: (24641, 24645, 24470, 24480)}
    book, _ = play(trend_ctx, range(12, 15), "bull", rows=flat)
    assert live(book, MC, "bull") is None


# ====================================================================================================================
# EMA Trend Continuation
# ====================================================================================================================
EMA_TOUCH = (24636.0, 24626.0)  # the 5M EMA20 zone is 24,629.75 .. 24,642.25: the pause low 24,638 is inside it, and above the EMA50 less the tolerance (24,619.75)


@pytest.mark.parametrize("side", SIDES)
def test_a_shallow_dip_to_the_5m_ema20_is_announced_at_once_and_confirmed_through_the_prior_high(side):
    book, steps = play(trend_ctx, range(12, 17), side, ema5=EMA_TOUCH)
    assert [r["live_now"][(ETC, side)][0] for _, r in steps[:4]] == [DETECTED] * 4  # a touch of the EMA is the filter: no waiting for a number of candles
    s = live(book, ETC, side)
    assert (s.status, s.direction) == (CONFIRMED, "CE" if side == "bull" else "PE")
    assert s.trigger == pytest.approx(at(side, 24650)) and s.invalidation == pytest.approx(at(side, 24635.5)) and s.zone == pytest.approx(box(side, 24650, 24660))
    assert s.group == f"leg:{side}:{int(t5(11))}"
    text = " ".join(s.reasons)
    assert f"dipped 12.0 pts (0.48x ATR15) to the 5M EMA20 {at(side, 24636):,.2f} (low {at(side, 24638):,.2f}) and held" in text and f"the EMA50 {at(side, 24626):,.2f}" in text
    assert "rejection" in s.confirm_text and "through the prior" in s.confirm_text
    # and the pause that does NOT touch the EMA stays Momentum Continuation's alone
    assert live(book, MC, side) is None and all(r["scans"][(MC, side)].setup is None for _, r in steps)
    assert "EMA Trend Continuation's" in steps[-2][1]["scans"][(MC, side)].note


@pytest.mark.parametrize("side", SIDES)
def test_ema_trend_continuation_needs_stacked_rising_emas_and_a_dip_that_held_the_ema50(side):
    def sc(**kw):
        return scan_one(ETC, trend_ctx(14, ema5=kw.pop("ema5", EMA_TOUCH), **kw), side)

    assert sc().setup is not None
    s = sc(stack15=5)
    assert not s.applicable and "for 5 candles, under 8" in s.note  # the EMAs have not been stacked for long enough
    s = sc(slope5=-1.0)
    assert not s.applicable and "is not " in s.note
    s = sc(adx={"1h": 30.0, "15m": 19.0, "5m": 12.0, "1m": 10.0})
    assert not s.applicable and "below 20" in s.note  # the lower ADX bar is ETC's own (20), against momentum's 25
    s = sc(ema5=(24660.0, 24650.0))  # the dip went through the 5M EMA50 (less tolerance 24,643.75): that is not a continuation
    assert s.applicable and s.setup is None and "went through the 5M EMA50" in s.note
    s = sc(ema5=(24618.0, 24608.0))  # the dip has not come down to the EMA20 zone yet
    assert s.applicable and s.setup is None and "has not reached the 5M EMA20 zone" in s.note


# ====================================================================================================================
# One dip is never taken by two strategies
# ====================================================================================================================
def pause_rows(low):
    return {12: (24642, 24646, low, 24644), 13: (24644, 24647, low + 2, 24645), 14: (24645, 24648, low + 1, 24646), 15: (24646, 24649, low + 2, 24647)}


@pytest.mark.parametrize("side", SIDES)
def test_no_dip_is_taken_by_more_than_one_of_trend_pullback_momentum_and_ema_continuation(side):
    seen = Counter()
    for low in (24626, 24630, 24634, 24636, 24637.5, 24638, 24640, 24641):  # depth 24, 20, 16, 14, 12.5, 12, 10, 9
        for e20 in (24612.0, 24625.0, 24632.0, 24640.0, 24650.0):
            ctx = oriented(trend_ctx(15, rows=pause_rows(low), ema5=(e20, e20 - 10)), side)
            scans = st.scan_all(ctx, P, {})
            mine = [n for n in (TP, MC, ETC) if scans[(n, side)].setup is not None]
            assert len(mine) <= 1, f"low {low}, EMA20 {e20}: {mine} all describe the same dip"
            seen.update(mine)
    assert seen[MC] >= 1 and seen[ETC] >= 1  # the grid really covers both territories


@pytest.mark.parametrize("side", SIDES)
def test_a_deeper_pullback_hands_the_leg_to_trend_pullback_and_the_continuation_setup_ends(side):
    rows = {**PAUSE, 15: (24644, 24648, 24628, 24647)}  # the pause deepens to 24,628: 22 under the high (0.88x ATR15): that is a pullback now
    book, _ = play(trend_ctx, range(12, 16), side, rows=rows)
    assert live(book, MC, side) is None
    ended = [e for e in book.events if e["strategy"] == MC and e["status"] == EXPIRED]
    assert ended and "Trend Pullback's" in ended[0]["text"]  # it says where the dip went


@pytest.mark.parametrize("side", SIDES)
def test_at_most_one_of_the_three_is_live_at_every_step_of_a_whole_move(side):
    for ema5 in ((24625.0, 24615.0), EMA_TOUCH):
        _, steps = play(trend_ctx, range(12, 17), side, ema5=ema5)
        for _, r in steps:
            live_now = [s.strategy for s in r["setups"] if s.strategy in (TP, MC, ETC) and s.side == side]
            assert len(live_now) <= 1, live_now


def test_the_leg_is_found_exactly_as_trend_pullback_finds_it():
    """The continuation strategies must describe the same leg as Trend Pullback (they are kept apart from it by depth, so they have to agree on what the leg is)."""
    from scenarios import bull_pullback

    for side in SIDES:
        for through in (17, 18, 19):
            ctx = oriented(bull_pullback(through), side)
            tp = st.scan_pullback(View(ctx, side), P, None).setup
            leg = trend_leg(View(ctx, side))
            assert tp is not None and not isinstance(leg, str)
            assert tp.key == f"trend_pullback:{side}:{int(leg.bar.start)}"  # the same candle is the leg's high
            assert View(ctx, side).real(leg.floor) == pytest.approx(tp.level)  # and the same swing low is its origin
            assert View(ctx, side).real(leg.high) == pytest.approx(at(side, 24650))


def test_the_same_move_guard_keeps_one_live_setup_per_group_and_lets_the_next_appear_when_the_first_ends(monkeypatch):
    saved = list(registry.REGISTRY)
    state = {"a": DETECTED, "b": DETECTED}

    def fake(name):
        def scan(v, p, prior):
            if v.side != "bull":
                return Scan(False, "bull only")
            return Scan(True, "", Setup(name, "bull", f"{name}:bull:7", state[name[-1]], 7.0, trigger=24650.0, zone=(24650.0, 24660.0), invalidation=24635.0, group="leg:bull:7"))
        return scan

    try:
        registry.register(StrategyDef("scratch_a", "Scratch A", fake("scratch_a"), WITH_TREND, priority=70))
        registry.register(StrategyDef("scratch_b", "Scratch B", fake("scratch_b"), WITH_TREND, priority=71))
        book, ctx = tr.SetupBook(P), trend_ctx(14)
        env = {"session": tr.session_info(ctx.now, P)}
        res = book.update(ctx, env, ctx.now)
        assert ("scratch_a", "bull") in book.active and ("scratch_b", "bull") not in book.active  # the first to see the move announces it
        assert res["scans"][("scratch_b", "bull")].setup is None and "already covered by Scratch A (detected)" in res["scans"][("scratch_b", "bull")].note
        assert [e["strategy"] for e in book.events if e["strategy"].startswith("scratch")] == ["scratch_a"]
        state["a"] = INVALIDATED  # the first one ends: the same update lets the second one appear
        book.update(ctx, env, ctx.now + 1)
        assert ("scratch_a", "bull") not in book.active and book.active[("scratch_b", "bull")].group == "leg:bull:7"
        assert tr.setup_dict(book.active[("scratch_b", "bull")], 24655.0, True, True)["group"] == "leg:bull:7"
        assert "group" not in tr.setup_dict(Setup(TP, "bull", "trend_pullback:bull:1", WAITING, 1.0), 24655.0, True, True)  # the originals' setups look as they always did
    finally:
        registry.REGISTRY[:] = saved
        registry._refresh()


def test_a_tracked_setup_and_its_group_survive_a_restart_without_being_announced_again():
    book, steps = play(trend_ctx, range(12, 16), "bull")
    s, ctx = live(book, MC, "bull"), steps[-1][0]
    record = {"setups": [tr.setup_dict(s, ctx.price, True, True)], "events": list(reversed(book.events)), "market": {"regime": "trend_up"}}
    fresh = tr.SetupBook(P)
    assert fresh.restore([record], ctx.now) == 1
    r = fresh.active[(MC, "bull")]
    assert r.resumed and r.group == s.group and r.anchor_time == s.anchor_time and r.status == DETECTED
    before = len(fresh.events)
    nxt = trend_ctx(16)
    fresh.update(nxt, {"session": tr.session_info(nxt.now, P)}, nxt.now)
    assert fresh.active[(MC, "bull")].status == CONFIRMED  # it carries on from where the journal left it ...
    assert [e["status"] for e in list(fresh.events)[: len(fresh.events) - before]] == [CONFIRMED]  # ... and only the new fact is announced


# ====================================================================================================================
# Volatility Expansion
# ====================================================================================================================
BOX = {20: (24590, 24596, 24586, 24592), 21: (24592, 24597, 24587, 24590), 22: (24590, 24595, 24585, 24593), 23: (24593, 24598, 24588, 24591),
       24: (24591, 24596, 24586, 24594), 25: (24594, 24600, 24590, 24596), 26: (24596, 24599, 24589, 24592), 27: (24592, 24597, 24587, 24590),
       28: (24590, 24594, 24584, 24588), 29: (24588, 24595, 24586, 24593), 30: (24593, 24598, 24588, 24595), 31: (24595, 24599, 24590, 24594)}  # fmt: skip
EXPANSION = (24596, 24612, 24594, 24610)  # slot 32


def expansion_ctx(through, after=None, box_rows=None, e=EXPANSION, **over):
    """Twelve quiet candles (slots 20-31) inside 24,584 .. 24,600 (16 points: 1.6x ATR5, a box is at most 3.5x), then the expansion candle at slot 32: range 18 (1.8x
    ATR5, needs 1.5x), body 14, closing at 89% of its range, 10 above the box high. The trigger is 24,600, the invalidation the middle of the box less the buffer
    (24,592 - 2.5 = 24,589.5): 10.5 points of risk."""
    rows = {**(box_rows or BOX), 32: e, **(after or {})}
    bars = rise(0, 20, 24560, 1) + [c5(i, *rows[i]) for i in range(20, through + 1)]
    d = dict(c5=bars, now=bars[-1].end + 3, price=bars[-1].close, c15=pullback_15m(), levels=[])
    d.update(over)
    return bull(**d)


@pytest.mark.parametrize("side", SIDES)
def test_a_wide_candle_closing_out_of_a_quiet_box_is_a_volatility_expansion(side):
    book, steps = play(expansion_ctx, [32], side)
    s = live(book, VE, side)
    assert (s.status, s.direction, s.strategy) == (CONFIRMED, "CE" if side == "bull" else "PE", VE)
    assert s.trigger == pytest.approx(at(side, 24600)) and s.invalidation == pytest.approx(at(side, 24589.5))
    assert s.zone == pytest.approx(box(side, 24600, 24610)) and s.blockers == [] and s.level == pytest.approx(at(side, 24600)) and s.level_label == "compression box high"
    text = " ".join(s.reasons)
    assert "expansion candle" in text and "range 1.8x ATR5, body 1.4x ATR5" in text and "12-candle compression box" in text and "1.6x ATR5" in text
    assert "invalidation is the middle of the box" in s.confirm_text and s.plan["risk_points"] == pytest.approx(10.5)
    d = tr.setup_dict(s, steps[-1][0].price, True, True)
    assert d["actionable"] and d["stage"] == "READY_TO_ACT" and d["price_state"] == "in_entry_area"


@pytest.mark.parametrize("side", SIDES)
def test_a_candle_out_of_the_box_that_is_not_an_expansion_starts_nothing(side):
    weak = (24596, 24604, 24594, 24602)  # closes above the box, but its range is 10 (1.0x ATR5, needs 1.5x)
    sc = scan_one(VE, expansion_ctx(32, e=weak), side)
    assert sc.applicable and sc.setup is None and "is not an expansion candle" in sc.note and "needs 1.5x" in sc.note
    sc = scan_one(VE, expansion_ctx(31), side)  # still inside the box
    assert sc.setup is None and "compression: the last 12 candles" in sc.note and "waiting for a wide-range candle" in sc.note
    loud = {i: (24590, 24620 if i % 2 else 24560, 24560, 24600) for i in range(20, 32)}
    sc = scan_one(VE, expansion_ctx(31, box_rows=loud), side)
    assert sc.setup is None and "no compression box" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_in_stricter_confirmation_an_expansion_waits_for_the_next_candle_to_hold(side):
    book, _ = play(expansion_ctx, [32], side, strict=True, strict_why="volatility is high")
    s = live(book, VE, side)
    assert s.status == WAITING and not s.candle_confirmed  # the expansion candle alone is not enough
    after = {33: (24609, 24611, 24604, 24606)}  # closes at 24,606: beyond the box high, and not chasing
    book, _ = play(expansion_ctx, [32, 33], side, after=after, strict=True, strict_why="volatility is high")
    assert live(book, VE, side).status == CONFIRMED
    book, _ = play(expansion_ctx, [32, 33], side, after={33: (24609, 24610, 24590, 24596)}, strict=True, strict_why="volatility is high")
    assert live(book, VE, side) is None and any("no longer present: no follow-through" in e["text"] for e in book.events)


@pytest.mark.parametrize("side", SIDES)
def test_the_end_of_an_expansion_setup_is_explained(side):
    book, _ = play(expansion_ctx, [32, 33], side, after={33: (24610, 24611, 24585, 24588)})  # closes under 24,589.5, the middle of the box
    assert live(book, VE, side) is None and book.recent[0].status == INVALIDATED and "middle of the compression box" in book.recent[0].note
    book, _ = play(expansion_ctx, [32, 33], side, after={33: (24610, 24611, 24594, 24596)})  # closes at 24,596: under the trigger by more than a quarter of ATR5 (24,597.5), but above the invalidation
    assert book.recent[0].status == INVALIDATED and "failed follow-through" in book.recent[0].note
    book, _ = play(expansion_ctx, [32, 33], side, after={33: (24610, 24630, 24609, 24625)})  # 25 beyond the trigger: do not chase
    assert book.recent[0].status == EXPIRED and "the entry opportunity has passed" in book.recent[0].note


@pytest.mark.parametrize("side", SIDES)
def test_a_box_that_is_too_tight_leaves_too_little_risk_distance_and_the_existing_check_holds_it_back(side):
    tight = {i: (24596, 24599, 24594, 24597) for i in range(20, 32)}
    tight[25], tight[28] = (24597, 24600, 24595, 24598), (24596, 24598, 24594, 24595)  # a 6 point box (24,594 .. 24,600): half of it plus the buffer is 5.5, under 0.75x ATR5
    book, steps = play(expansion_ctx, [32], side, box_rows=tight)
    s = live(book, VE, side)
    assert s.status == WAITING and s.candle_confirmed and any("inside normal noise" in b for b in s.blockers)  # the risk-distance rule of every strategy applies
    assert not tr.setup_dict(s, steps[-1][0].price, True, True)["actionable"]


@pytest.mark.parametrize("side", SIDES)
def test_volatility_expansion_follows_the_higher_timeframes(side):
    assert scan_one(VE, expansion_ctx(32), side).setup is not None
    sideways = expansion_ctx(32, trend={"1h": "sideways", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment="range_bound")
    assert scan_one(VE, sideways, side).applicable  # not against anything: allowed
    against = expansion_ctx(32, trend={"1h": "bearish", "15m": "sideways", "5m": "sideways", "1m": "sideways"})
    sc = scan_one(VE, against, side)
    assert not sc.applicable and "against the higher-timeframe direction" in sc.note
    sc = scan_one(VE, expansion_ctx(32, trend={"1h": "unknown", "15m": "unknown", "5m": "unknown", "1m": "unknown"}), side)
    assert not sc.applicable and "not enough completed" in sc.note
    sc = scan_one(VE, expansion_ctx(32, atr={"1h": 60.0, "15m": 25.0, "5m": None, "1m": 4.0}), side)
    assert sc.applicable and sc.setup is None and "ATR is not available yet" in sc.note
    short = expansion_ctx(32)
    short = replace(short, c5=short.c5[-8:])
    assert "not enough completed 5M candles" in scan_one(VE, short, side).note


@pytest.mark.parametrize("side", SIDES)
def test_an_expansion_completed_inside_the_opening_range_is_observed_not_traded(side):
    ctx = expansion_ctx(32)
    sc = scan_one(VE, replace(ctx, confirm_after=ctx.c5[-1].end + 1), side)
    assert sc.setup is None  # the candle that would confirm completed before the opening range ended


def test_an_expansion_that_is_old_when_first_seen_is_not_announced_late():
    after = {33: (24610, 24611, 24609, 24610), 34: (24610, 24611, 24609, 24610), 35: (24610, 24611, 24609, 24610), 36: (24610, 24611, 24609, 24610)}
    book, _ = play(expansion_ctx, [36], "bull", after=after)
    assert live(book, VE, "bull") is None and not any(e["strategy"] == VE for e in book.events)  # the entry window passed: nothing is announced, nothing is shown as live


# ====================================================================================================================
# Through the whole pipeline: the decision, and the checks that no strategy can bypass
# ====================================================================================================================
def decision_after(build, throughs, side="bull", data_ok=True, issues=(), tick_age=1.0, vol_extreme=False, **kw):
    _, steps = play(build, throughs, side, **kw)
    ctx, core = steps[-1]
    t = ctx.trend
    opposite = {"bullish": "bearish", "bearish": "bullish"}
    trend = t["1h"] if t["1h"] in opposite and t["15m"] != opposite[t["1h"]] else "sideways"
    a = {"price": ctx.price, "condition": {"trend": trend, "volatility": ctx.volatility, "alignment": {"label": ctx.alignment, "text": "x"}},
         "timeframes": {tf: {"trend": t[tf], "adx": ctx.adx.get(tf), "atr14_intraday": ctx.atr.get(tf), "atr14": ctx.atr.get(tf)} for tf in t}}  # fmt: skip
    env = {"session": tr.session_info(ctx.now, P), "data": {"ok": data_ok, "issues": list(issues)}, "vol_extreme": vol_extreme, "vol_ratio": 1.0, "opening_range": None, "gap": None}
    return tr.present(core, a, env, ctx.now, P, tick_age)


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("build,throughs,name,kw", [(trend_ctx, range(12, 17), MC, {}), (trend_ctx, range(12, 17), ETC, {"ema5": EMA_TOUCH}), (expansion_ctx, [32], VE, {})])
def test_each_new_strategy_reaches_a_buy_through_the_same_decision_pipeline(side, build, throughs, name, kw):
    out = decision_after(build, throughs, side, **kw)
    d = out["decision"]
    assert d["action"] == ("BUY CE" if side == "bull" else "BUY PE") and d["strategy"] == name and d["strategy_name"] == registry.NAMES[name]
    assert [m["id"] for m in out["strategies"]] == list(registry.NAMES) and next(m for m in out["strategies"] if m["id"] == name)["state"] == "CONFIRMED"
    assert out["explain"]["actionable"] and out["setups"][0]["stage"] == "READY_TO_ACT"


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("build,throughs,kw", [(trend_ctx, range(12, 17), {}), (trend_ctx, range(12, 17), {"ema5": EMA_TOUCH}), (expansion_ctx, [32], {})])
def test_no_new_strategy_can_produce_a_buy_when_the_data_the_session_or_the_price_does_not_allow_it(side, build, throughs, kw):
    bad = decision_after(build, throughs, side, data_ok=False, issues=["historical data is stale: today's refresh has not completed"], **kw)["decision"]
    assert bad["action"] == "NO TRADE" and "not reliable" in bad["headline"]
    extreme = decision_after(build, throughs, side, vol_extreme=True, **kw)["decision"]
    assert extreme["action"] != ("BUY CE" if side == "bull" else "BUY PE")
    old = decision_after(build, throughs, side, tick_age=60.0, **kw)  # the live price stopped: nothing is actionable
    assert old["decision"]["action"] == "WAIT" and not any(s["actionable"] for s in old["setups"])
    blocked = decision_after(build, throughs, side, can_confirm=False, blockers=["late session"], **kw)
    assert not blocked["decision"]["action"].startswith("BUY")


def test_the_new_strategies_do_not_change_what_the_original_three_say_about_their_own_scenarios():
    """(The full comparison is test_existing_strategies_golden.py; this is the quick version on the pullback scenario the originals are built around.)"""
    from scenarios import bull_pullback

    out = decision_after(lambda n: bull_pullback(n), [19], "bull")
    assert out["decision"]["action"] == "BUY CE" and out["decision"]["strategy"] == TP  # a deep pullback is still Trend Pullback's, and nothing else claims it
    assert not [s for s in out["setups"] if s["strategy"] != TP]


def test_the_scenario_contexts_are_real_contexts():
    assert isinstance(trend_ctx(14), Context) and isinstance(expansion_ctx(32), Context)
