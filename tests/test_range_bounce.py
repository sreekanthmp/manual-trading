"""Stage 2.3: Range Bounce, on hand-built candles (the numbers are worked out by hand in the comments, as in scenarios.py). NIFTY ~24,600, ATR(5M) = 10, ATR(15M) = 25: the
touch tolerance is 6.25, the break margin 3.75, the fail depth 25, the approach zone 25, a confirming candle's body 4, the entry-chase limit 10, the invalidation buffer 2.5.
Every scenario is also run mirrored (the bearish twin) through the real SetupBook.

The market: 1H bullish, 15M sideways (alignment "pausing"), a 15M compression box 24,560 .. 24,630 (70 points = 2.8x ATR15). A leg up to the box high 24,630 (slot 25, 11:15),
a decline of eight falling candles (slots 26-33) to 24,576, then the dip to the box low:
    A   slot 34  24578 24580 24562 24570   low 24,562 (2 above the box low); closes 24,570: under the leg's origin 24,580 less the tolerance (24,573.75)
        slot 35  24570 24574 24560 24572   hammer: low 24,560 = the box low, lower wick 71%, closes at 86% of its range
        slot 36  24572 24578 24570 24576   body 4, closes above the base high 24,574: the confirmation. Trigger 24,574, invalidation 24,557.5 (risk 16.5)
        Trend Pullback calls the structure broken at slot 34 (the origin is the 15M swing low 24,580), while the box low holds: this is the case Range Bounce is for.
    B   slot 34  24578 24580 24552 24566   a wick 8 under the box low (over the 6.25 tolerance, so Trend Pullback sees no meaningful level), closes 24,566: no close beyond the edge
        slot 35  24566 24584 24564 24582   body 16, closes at 90%, above the base high 24,580: the confirmation. Trigger 24,580, invalidation 24,549.5 (risk 30.5)
    C   = A's candles, with the leg's origin at 24,500: Trend Pullback takes the dip (its low is within the tolerance of the box low), and Range Bounce stands aside"""

from dataclasses import replace

import pytest
from helpers import ist
from scenarios import M, P, bull, bull_range, c5, c15, t5, t15
from test_trend_family import at, box, decision_after, live, oriented, play, scan_one

from app import registry
from app.candles import Candle
from app import strategy as st
from app import trading as tr
from app.strategies import range_bounce as rb
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Level, Scan, Setup

SIDES = ["bull", "bear"]
RB, TP, FB, RR = "range_bounce", "trend_pullback", "failed_breakout", "range_reversal"
SIDE_NAME = {"bull": "CE", "bear": "PE"}
LO, HI = 24560.0, 24630.0
BASE = (24600, 24606, 24594, 24600)
LEG = {24: (24606, 24620, 24604, 24618), 25: (24618, 24630, 24616, 24626)}
DECLINE = {26: (24626, 24628, 24618, 24620), 27: (24620, 24622, 24612, 24614), 28: (24614, 24616, 24606, 24608), 29: (24608, 24610, 24600, 24602),
           30: (24602, 24604, 24594, 24596), 31: (24596, 24598, 24588, 24590), 32: (24590, 24592, 24582, 24584), 33: (24584, 24586, 24576, 24578)}  # fmt: skip
DIP_A = {34: (24578, 24580, 24562, 24570), 35: (24570, 24574, 24560, 24572), 36: (24572, 24578, 24570, 24576)}
DIP_B = {34: (24578, 24580, 24552, 24566), 35: (24566, 24584, 24564, 24582)}
ORIGIN_BROKEN, ORIGIN_FAR = 24580.0, 24500.0  # the 15M swing low the leg started from: above the box low, or well under it


def box15(closes=None):
    """Twelve 15M candles inside the box (the last completed one starts at 12:00): the box low 24,560 is made at j=5 and the box high 24,630 at j=9."""
    bars = [c15(j, 24600, 24620, 24570, (closes or {}).get(j, 24600)) for j in range(12)]
    bars[5], bars[9] = c15(5, 24600, 24620, 24560, 24600), c15(9, 24600, 24630, 24570, 24600)
    return bars


def box_ctx(through, dip=None, floor=ORIGIN_BROKEN, rows=None, c15_close=None, **over):
    table = {i: BASE for i in range(24)}
    table.update(LEG)
    table.update(DECLINE)
    table.update(DIP_A if dip is None else dip)
    table.update(rows or {})
    bars = [c5(i, *table[i]) for i in range(through + 1)]
    d = dict(
        c5=bars, c15=box15(c15_close), now=bars[-1].end + 3, price=bars[-1].close,
        trend={"1h": "bullish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment="pausing",
        pattern={"1h": "HH_HL", "15m": "mixed", "5m": "mixed", "1m": "mixed"}, adx={"1h": 30.0, "15m": 14.0, "5m": 12.0, "1m": 10.0},
        swings={"15m": [("L", t15(2), floor), ("H", t15(4), HI)]}, consolidation={"15m": (LO, HI)},
        ema={"1h": (24500.0, 24450.0), "15m": (24600.0, 24598.0), "5m": (24590.0, 24592.0), "1m": (24596.0, 24594.0)},
        levels=[Level(LO, "15M range low"), Level(HI, "15M range high")],
    )  # fmt: skip
    d.update(over)
    return bull(**d)


def events(book, name=RB):
    return [e["status"] for e in reversed(book.events) if e["strategy"] == name]


# ====================================================================================================================
# The bounce, where Trend Pullback cannot see it
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_bounce_off_the_box_low_after_the_leg_origin_broke_is_announced_and_confirmed(side):
    book, steps = play(box_ctx, range(33, 37), side)
    scans = [r["scans"][(RB, side)] for _, r in steps]
    assert scans[0].setup is None and "has not touched it" in scans[0].note  # 24,576 is 16 above the box low: not at the edge yet
    assert steps[1][1]["live_now"][(RB, side)] == (DETECTED, pytest.approx(at(side, 24580)))  # slot 34: the edge was touched, no sign of buyers yet
    assert steps[2][1]["live_now"][(RB, side)] == (WAITING, pytest.approx(at(side, 24574)))  # slot 35: the hammer
    s = live(book, RB, side)
    assert (s.status, s.direction, s.strategy, s.group) == (CONFIRMED, SIDE_NAME[side], RB, None)
    assert s.trigger == pytest.approx(at(side, 24574)) and s.invalidation == pytest.approx(at(side, 24557.5)) and s.zone == pytest.approx(box(side, 24574, 24584))
    assert s.level == pytest.approx(at(side, 24560)) and s.level_label == ("15M box low" if side == "bull" else "15M box high") and s.extreme == pytest.approx(at(side, 24560))
    assert s.blockers == [] and s.candle_confirmed and s.plan["risk_points"] == pytest.approx(16.5)
    t = s.plan["targets"][0]
    assert t["price"] == pytest.approx(at(side, 24630)) and t["label"] == ("15M box high" if side == "bull" else "15M box low") and t["reward_risk"] == pytest.approx(56 / 16.5)
    text = " ".join(s.reasons)
    assert "while the 15M is sideways" in text and "pausing, the bounce goes with it" in text and "(2.8x ATR15)" in text and "and did not close beyond it" in text and "rejection" in text
    assert events(book) == [DETECTED, WAITING, CONFIRMED]  # announced once each, in order
    d = tr.setup_dict(s, steps[-1][0].price, True, True)
    assert d["actionable"] and d["stage"] == "READY_TO_ACT" and "group" not in d
    assert live(book, TP, side) is None  # and Trend Pullback has nothing of its own on these candles
    assert "closed below the leg's origin" in steps[1][1]["scans"][(TP, side)].note or "closed above the leg's origin" in steps[1][1]["scans"][(TP, side)].note


@pytest.mark.parametrize("side", SIDES)
def test_a_wick_through_the_edge_by_more_than_the_touch_tolerance_with_no_close_beyond_it_is_a_bounce(side):
    book, steps = play(box_ctx, range(33, 36), side, dip=DIP_B, floor=ORIGIN_FAR)
    assert steps[1][1]["live_now"][(RB, side)] == (WAITING, pytest.approx(at(side, 24580)))  # the wick candle itself shows buyers: its lower wick is half of its range
    s = live(book, RB, side)
    assert s.status == CONFIRMED and s.trigger == pytest.approx(at(side, 24580)) and s.invalidation == pytest.approx(at(side, 24549.5)) and s.extreme == pytest.approx(at(side, 24552))
    assert s.plan["risk_points"] == pytest.approx(30.5) and "8.0 pts" in " ".join(s.reasons)  # the low went 8 pts through the edge
    assert all(r["scans"][(TP, side)].setup is None for _, r in steps)  # Trend Pullback sees "no meaningful level": 8 under the box low is outside its tolerance
    assert "not at a meaningful level" in steps[-1][1]["scans"][(TP, side)].note
    assert all(r["scans"][(FB, side)].setup is None for _, r in steps)  # and nothing closed beyond the level: no trap either


@pytest.mark.parametrize("side", SIDES)
def test_the_end_of_a_bounce_is_explained(side):
    def after(row):
        return play(box_ctx, range(33, 38), side, rows={37: row})[0]

    book = after((24576, 24577, 24556, 24557))  # closes 24,557: under the invalidation 24,557.5, but not 3.75 under the edge
    assert live(book, RB, side) is None and book.recent[0].status == INVALIDATED and "invalidation level" in book.recent[0].note
    book = after((24576, 24577, 24550, 24555))  # closes 24,555: beyond the edge itself
    assert book.recent[0].status == INVALIDATED and "the bounce failed" in book.recent[0].note and "5.0 pts" in book.recent[0].note
    book = after((24576, 24577, 24568, 24570))  # 24,570: under the trigger by more than a quarter of ATR5 (24,571.5)
    assert book.recent[0].status == INVALIDATED and "failed follow-through" in book.recent[0].note
    book = after((24576, 24592, 24575, 24590))  # 16 beyond the trigger: do not chase
    assert book.recent[0].status == EXPIRED and "the entry opportunity has passed" in book.recent[0].note
    book = after((24576, 24580, 24574, 24578))
    assert live(book, RB, side).status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
def test_in_stricter_confirmation_the_next_candle_must_hold_the_break(side):
    kw = dict(dip=DIP_B, floor=ORIGIN_FAR, strict=True, strict_why="volatility is high")
    book, _ = play(box_ctx, range(33, 36), side, **kw)
    s = live(book, RB, side)
    assert s.status == WAITING and not s.candle_confirmed and "must also close beyond it" in s.confirm_text
    book, _ = play(box_ctx, range(33, 37), side, rows={36: (24582, 24590, 24580, 24586)}, **kw)
    assert live(book, RB, side).status == CONFIRMED
    book, _ = play(box_ctx, range(33, 37), side, rows={36: (24582, 24583, 24570, 24572)}, **kw)  # the next candle closes back under the trigger
    assert live(book, RB, side).status == WAITING and not live(book, RB, side).candle_confirmed  # (a setup only moves forward: it waits on, unconfirmed)


@pytest.mark.parametrize("side", SIDES)
def test_the_existing_gates_hold_a_confirmed_bounce_back(side):
    book, steps = play(box_ctx, range(33, 37), side, levels=[Level(LO, "15M range low"), Level(HI, "15M range high"), Level(24584.0, "swing level x2", 2)])  # 10 of room for 16.5 at risk
    s = live(book, RB, side)
    assert s.status == WAITING and s.candle_confirmed and any("only 10.0 pts to the next level" in b for b in s.blockers)
    assert tr.setup_dict(s, steps[-1][0].price, True, True)["stage"] == "BLOCKED"
    book, steps = play(box_ctx, range(33, 37), side, can_confirm=False, blockers=["historical data is stale: today's refresh has not completed"])
    s = live(book, RB, side)
    assert s.status == WAITING and s.candle_confirmed and s.blockers == ["historical data is stale: today's refresh has not completed"]
    assert not tr.setup_dict(s, steps[-1][0].price, True, True)["actionable"]


# ====================================================================================================================
# What is not a bounce, and the regime
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_close_beyond_the_edge_is_a_break_not_a_bounce_and_a_failed_one_is_failed_breakouts(side):
    trap = {34: (24578, 24580, 24550, 24555), 35: (24556, 24572, 24554, 24570)}  # closes 24,555 (beyond the box low by more than 3.75), then takes it back
    _, steps = play(box_ctx, range(33, 36), side, dip=trap, floor=ORIGIN_FAR)
    sc = steps[-1][1]["scans"][(RB, side)]
    assert sc.applicable and sc.setup is None and "a close beyond the edge is a break or a failed break (Failed Breakout's), not a bounce" in sc.note
    fb = steps[-1][1]["scans"][(FB, side)].setup
    assert fb is not None and fb.status == WAITING  # the same candles are the trap reversal's
    deep = {34: (24578, 24580, 24530, 24566), 35: DIP_B[35]}  # the wick goes 30 under the edge: over 1.0x ATR15
    sc = play(box_ctx, range(33, 36), side, dip=deep, floor=ORIGIN_FAR)[1][-1][1]["scans"][(RB, side)]
    assert sc.setup.status == INVALIDATED and "a genuine break, not a bounce" in sc.note
    sc = scan_one(RB, box_ctx(36, c15_close={11: 24550.0}), side)  # a 15M candle closed 10 under the edge
    assert sc.setup.status == INVALIDATED and "a genuine break, not a bounce" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_it_needs_the_1h_trending_with_the_side_the_15m_sideways_in_a_box_and_says_why_not(side):
    def sc(**kw):
        return scan_one(RB, box_ctx(36, **kw), side)

    assert sc().setup is not None
    for trend, align in ((("sideways", "sideways"), "range_bound"), (("bullish", "bullish"), "aligned_bullish"), (("bearish", "sideways"), "pausing"), (("sideways", "bullish"), "developing")):
        s = sc(trend={"1h": trend[0], "15m": trend[1], "5m": "sideways", "1m": "sideways"}, alignment=align)
        assert not s.applicable and "a range bounce needs a" in s.note and "1H trend with the 15M sideways" in s.note
    s = sc(pattern={"1h": "HH_HL", "15m": "LH_LL", "5m": "mixed", "1m": "mixed"})
    assert not s.applicable and "structure" in s.note
    s = sc(breakout={"15m": "breakdown", "1h": None})
    assert not s.applicable and "is in progress, not a box" in s.note
    s = sc(consolidation={"15m": None})
    assert s.applicable and s.setup is None and "no 15M compression box" in s.note
    s = sc(consolidation={"15m": (24600.0, 24630.0)})  # 30 points: 1.2x ATR15, under 1.5
    assert s.applicable and s.setup is None and "too small for a bounce to have room" in s.note
    s = sc(atr={"1h": 60.0, "15m": 25.0, "5m": None, "1m": 4.0})
    assert s.applicable and s.setup is None and "ATR or price is not available" in s.note


@pytest.mark.parametrize("side", SIDES)
def test_a_visit_to_the_edge_that_is_long_gone_and_the_middle_of_the_box_offer_nothing(side):
    flat = {i: (24600, 24606, 24594, 24600) for i in range(37, 51)}
    sc = scan_one(RB, box_ctx(50, rows=flat), side)
    assert sc.applicable and sc.setup is None and "57% of the way across the 15M box: the middle of a box offers no edge" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_yesterdays_candles_at_the_edge_are_not_todays_bounce(side):
    ctx = box_ctx(5)  # today so far: six quiet candles at 24,600
    day = (2026, 11, 5)
    yesterday = [Candle(ist(14, 30 + 5 * i, day=day), ist(14, 35 + 5 * i, day=day), *row, 0, source="db")
                 for i, row in enumerate([DIP_A[34], DIP_A[35], DIP_A[36]])]  # the same dip, hammer and confirmation, but yesterday afternoon
    assert scan_one(RB, replace(ctx, c5=yesterday + ctx.c5), side).setup is None
    sc = scan_one(RB, ctx, side)
    assert sc.setup is None and ("has not come back" in sc.note or "middle of a box" in sc.note)


@pytest.mark.parametrize("side", SIDES)
def test_a_tracked_bounce_keeps_its_edge_when_the_box_has_dissolved(side):
    book, _ = play(box_ctx, range(33, 36), side)
    s = live(book, RB, side)
    assert s.status == WAITING
    kept = scan_one(RB, box_ctx(36, consolidation={"15m": None}), side, prior=s).setup  # the 12-candle window moved on: a new setup would not start ...
    assert kept is not None and kept.status == CONFIRMED and kept.level == pytest.approx(at(side, 24560))  # ... but the one being tracked carries on from its own edge
    assert scan_one(RB, box_ctx(36, consolidation={"15m": None}), side).setup is None


# ====================================================================================================================
# One movement is never announced twice
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_dip_trend_pullback_takes_is_not_announced_again_as_a_bounce(side):
    book, steps = play(box_ctx, range(33, 37), side, floor=ORIGIN_FAR)  # the same candles as A, with the leg's origin well under the box low: Trend Pullback holds
    assert live(book, TP, side) is not None
    for _, r in steps:
        assert r["scans"][(RB, side)].setup is None
    assert "Trend Pullback already has this dip" in steps[-1][1]["scans"][(RB, side)].note
    assert live(book, RB, side) is None and events(book) == []


@pytest.mark.parametrize("side", SIDES)
def test_a_bounce_is_the_boxs_only_where_trend_pullback_has_nothing_that_is_live(side):
    """Swept over where the dip ends and where the leg started: at every step of every run, Trend Pullback and Range Bounce are never both live, and
    Failed Breakout is never live with either on a bounce that did not close beyond the edge."""
    seen = {RB: 0, TP: 0}
    for low in (24574, 24566, 24562, 24560, 24556, 24552, 24546, 24540):
        for floor in (ORIGIN_BROKEN, ORIGIN_FAR):
            dip = {34: (24578, 24580, low, low + 8), 35: (low + 8, low + 20, low + 6, low + 18), 36: (low + 18, low + 26, low + 16, low + 24)}
            _, steps = play(box_ctx, range(33, 37), side, dip=dip, floor=floor)
            for _, r in steps:
                mine = {s.strategy for s in r["setups"] if s.side == side and s.strategy in (RB, TP, FB)}
                assert len(mine) <= 1, f"low {low}, origin {floor}: {mine} are live on one dip"
                seen.update({n: seen[n] + 1 for n in mine & {RB, TP}})
    assert seen[RB] >= 1 and seen[TP] >= 1  # the sweep really covers both territories


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("low,origin_broken,owner", [
    (24574, True, None), (24574, False, None),  # 14 above the box low: not at the edge yet
    (24566, True, TP), (24566, False, TP),  # 6 above: inside Trend Pullback's tolerance, and the dip's first candle closes 24,574: above the origin less the tolerance (24,573.75)
    (24562, True, RB), (24560, True, RB), (24556, True, RB),  # the dip's candle closes under 24,573.75: Trend Pullback calls the structure broken, the box holds
    (24562, False, TP), (24560, False, TP), (24556, False, TP),  # the origin is far below: Trend Pullback takes every low within 6.25 of the edge
    (24552, True, RB), (24552, False, RB),  # a wick 8 through the edge: outside Trend Pullback's tolerance, no close beyond the edge
    (24546, True, FB), (24546, False, FB), (24540, True, FB), (24540, False, FB),  # the dip's candle closes under the edge by more than 3.75: Failed Breakout's trap
])  # fmt: skip
def test_exactly_one_strategy_announces_a_dip_to_the_box_low(side, low, origin_broken, owner):
    """The three divide every depth of dip at the box low between them (24,560): who announces it, for a dip whose first candle closes 8 above its low."""
    dip = {34: (24578, 24580, low, low + 8), 35: (low + 8, low + 20, low + 6, low + 18), 36: (low + 18, low + 26, low + 16, low + 24)}
    _, steps = play(box_ctx, range(33, 37), side, dip=dip, floor=ORIGIN_BROKEN if origin_broken else ORIGIN_FAR)
    ever = {s.strategy for _, r in steps for s in r["setups"] if s.side == side} & {RB, TP, FB}
    assert ever == (set() if owner is None else {owner})


@pytest.mark.parametrize("side", SIDES)
def test_range_bounce_and_range_reversal_never_describe_the_same_candles(side):
    """Range Reversal needs 1H and 15M both sideways, Range Bounce a trending 1H and a sideways 15M: swept over the regimes, never both."""
    seen = {RB: 0, RR: 0}
    for t1, t15x, align in (("bullish", "sideways", "pausing"), ("sideways", "sideways", "range_bound"), ("bullish", "bullish", "aligned_bullish"), ("sideways", "bullish", "developing")):
        for ctx in (box_ctx(36, trend={"1h": t1, "15m": t15x, "5m": "sideways", "1m": "sideways"}, alignment=align, adx={"1h": 15.0, "15m": 14.0, "5m": 12.0, "1m": 10.0}),
                    bull_range(35, trend={"1h": t1, "15m": t15x, "5m": "sideways", "1m": "sideways"}, alignment=align)):  # fmt: skip
            scans = st.scan_all(oriented(ctx, side), P, {})
            mine = [n for n in (RB, RR) if scans[(n, side)].setup is not None]
            assert len(mine) <= 1, f"{t1}/{t15x}/{align}: {mine}"
            seen.update({n: seen[n] + 1 for n in mine})
    assert seen[RB] >= 1 and seen[RR] >= 1


def test_a_tracked_bounce_ends_when_trend_pullback_takes_the_same_low(monkeypatch):
    for side in SIDES:
        book, _ = play(box_ctx, range(33, 36), side)
        assert live(book, RB, side).status == WAITING
        sign = 1 if side == "bull" else -1
        theirs = Setup(TP, side, f"{TP}:{side}:1", WAITING, 1.0, extreme=at(side, 24560.0) + sign * 3.0)  # the same low, to within the tolerance
        monkeypatch.setattr(rb, "scan_pullback", lambda v, p, prior, theirs=theirs: Scan(True, "", theirs))
        ctx = oriented(box_ctx(36), side)
        book.update(ctx, {"session": tr.session_info(ctx.now, P)}, ctx.now)
        assert live(book, RB, side) is None and book.recent[0].status == EXPIRED and "Trend Pullback now holds the same low" in book.recent[0].note
        monkeypatch.undo()
        book, _ = play(box_ctx, range(33, 36), side)
        far = Setup(TP, side, f"{TP}:{side}:1", WAITING, 1.0, extreme=at(side, 24560.0) + sign * 30.0)  # a different low: not the same dip
        monkeypatch.setattr(rb, "scan_pullback", lambda v, p, prior, far=far: Scan(True, "", far))
        ctx = oriented(box_ctx(36), side)
        book.update(ctx, {"session": tr.session_info(ctx.now, P)}, ctx.now)
        assert live(book, RB, side).status == CONFIRMED
        monkeypatch.undo()


def test_a_strategy_can_see_the_live_setups_of_the_others_through_its_view():
    saved, seen = list(registry.REGISTRY), {}

    def peek(v, p, prior):
        seen[v.side] = dict(v.live)
        return Scan(False, "a test strategy that only looks")

    try:
        registry.register(registry.StrategyDef("scratch_peek", "Scratch peek", peek, registry.WITH_TREND, priority=80))
        other = Setup(TP, "bull", "trend_pullback:bull:1", WAITING, 1.0)
        st.scan_all(box_ctx(36), P, {(TP, "bull"): other})
        assert seen["bull"] == {(TP, "bull"): other} and seen["bear"] == {(TP, "bull"): other}
        assert st.View(box_ctx(36), "bull").live == {}  # (a bare view has none)
    finally:
        registry.REGISTRY[:] = saved
        registry._refresh()


# ====================================================================================================================
# Restart, and through the whole pipeline
# ====================================================================================================================
def test_a_tracked_bounce_survives_a_restart_without_being_announced_again():
    book, steps = play(box_ctx, range(33, 36), "bull")
    s, ctx = live(book, RB, "bull"), steps[-1][0]
    record = {"setups": [tr.setup_dict(s, ctx.price, True, True)], "events": list(reversed(book.events)), "market": {"regime": "trend_up"}}
    fresh = tr.SetupBook(P)
    assert fresh.restore([record], ctx.now) == 1
    r = fresh.active[(RB, "bull")]
    assert r.resumed and r.anchor_time == s.anchor_time == t5(32) and r.status == WAITING
    before = len(fresh.events)
    nxt = box_ctx(36)
    fresh.update(nxt, {"session": tr.session_info(nxt.now, P)}, nxt.now)
    assert fresh.active[(RB, "bull")].status == CONFIRMED
    assert [e["status"] for e in list(fresh.events)[: len(fresh.events) - before]] == [CONFIRMED]


@pytest.mark.parametrize("side", SIDES)
def test_a_bounce_reaches_a_buy_through_the_same_decision_pipeline(side):
    out = decision_after(box_ctx, range(33, 37), side)
    d = out["decision"]
    assert d["action"] == ("BUY CE" if side == "bull" else "BUY PE") and d["strategy"] == RB and d["strategy_name"] == "Range Bounce"
    assert [m["id"] for m in out["strategies"]] == list(registry.NAMES) and next(m for m in out["strategies"] if m["id"] == RB)["state"] == "CONFIRMED"
    assert out["explain"]["actionable"] and out["setups"][0]["stage"] == "READY_TO_ACT" and [s["strategy"] for s in out["setups"]] == [RB]


@pytest.mark.parametrize("side", SIDES)
def test_a_bounce_cannot_produce_a_buy_when_the_data_the_session_or_the_price_does_not_allow_it(side):
    bad = decision_after(box_ctx, range(33, 37), side, data_ok=False, issues=["historical data is stale: today's refresh has not completed"])["decision"]
    assert bad["action"] == "NO TRADE" and "not reliable" in bad["headline"]
    extreme = decision_after(box_ctx, range(33, 37), side, vol_extreme=True)["decision"]
    assert extreme["action"] != ("BUY CE" if side == "bull" else "BUY PE")
    old = decision_after(box_ctx, range(33, 37), side, tick_age=60.0)
    assert old["decision"]["action"] == "WAIT" and not any(s["actionable"] for s in old["setups"])
    blocked = decision_after(box_ctx, range(33, 37), side, can_confirm=False, blockers=["late session"])
    assert not blocked["decision"]["action"].startswith("BUY")


def test_the_scenario_contexts_are_real_contexts_and_the_box_candles_are_coherent():
    ctx = box_ctx(36)
    assert isinstance(ctx, st.Context) and replace(ctx).consolidation["15m"] == (LO, HI) and M == 50000.0
    assert min(b.low for b in ctx.c15) == LO and max(b.high for b in ctx.c15) == HI  # the 15M candles really make the box they claim
