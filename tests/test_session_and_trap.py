"""Stage 2.2: Opening Range Breakout and Failed Breakout (Trap Reversal), on hand-built candles (the numbers are worked out by hand in the comments, as in
scenarios.py). NIFTY ~24,600, ATR(5M) = 10, ATR(15M) = 25: a confirming candle's body is 4, a strong candle's 7, the entry-chase limit 10, the invalidation buffer 2.5,
the break margin 3.75 (0.15x ATR15), the fail depth 25 (1.0x ATR15), the risk limits 7.5 (0.75x ATR5) and 37.5 (1.5x ATR15). Every scenario is also run mirrored (the bearish
twin) through the real SetupBook, so the lifecycle, the announcements and the decision are exercised, not only the scanners.

Opening range (slots 0-2, 09:15-09:30): high 24,620, low 24,586 (34 points = 1.36x ATR15), middle 24,603, so the invalidation is 24,600.5 and the risk 19.5.
    slot  time    open   high   low    close
      3   09:30   24614  24616  24606  24610   inside; its high is 4 under the edge (more than the 2.5 that counts as testing it)
      4   09:35   24610  24619  24608  24614   inside, but the high 24,619 is within 2.5 of the edge: it tested the edge (DETECTED)
      5   09:40   24614  24628  24613  24626   body 12, closes at 87% of its range, 6 beyond the edge: the breakout

Failed breakdown of the support 24,600 (slots 0-19 are quiet candles at 24,611):
      20  11:00   24611  24613  24604  24606
      21  11:00   24606  24607  24592  24594   first close under the level by more than the margin (24,594 < 24,596.25)
      22  11:05   24594  24597  24588  24590   the low of the failed break: 24,588, 12 under the level
      23  11:10   24599  24606  24598  24601   closes back above 24,600 (the reclaim), but its body is only 2: not a confirming candle
      24  11:15   24601  24610  24600  24608   body 7, closes at 80% of its range, above the base high 24,606: the confirmation"""

from dataclasses import replace

import pytest
from helpers import ist
from scenarios import M, P, bull, bull_range, c5, t5
from test_trend_family import at, box, decision_after, live, oriented, play, scan_one

from app import registry
from app import strategy as st
from app import trading as tr
from app.candles import Candle
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Level

SIDES = ["bull", "bear"]
ORB, FB, RR = "opening_range_breakout", "failed_breakout", "range_reversal"
SIDE_NAME = {"bull": "CE", "bear": "PE"}


# ====================================================================================================================
# Opening Range Breakout
# ====================================================================================================================
OR_ROWS = {0: (24590, 24604, 24586, 24600), 1: (24600, 24612, 24596, 24608), 2: (24608, 24620, 24602, 24616)}
INSIDE = {3: (24614, 24616, 24606, 24610), 4: (24610, 24619, 24608, 24614)}
BREAK = (24614, 24628, 24613, 24626)
FILL = (24610, 24615, 24606, 24611)  # quiet, inside the range, 5 under the edge
HOLD = (24626, 24629, 24622, 24627)


def fill(a, b):
    return {i: FILL for i in range(a, b)}


def orb_ctx(through, rows=None, levels=None, **over):
    """The opening range and what followed it, up to the close of 5M slot `through`. The opening range comes from the real `trading.opening_range` and its two edges are
    added to the levels once it is complete, as `trading.build_context` does."""
    table = {**OR_ROWS, **INSIDE, 5: BREAK, **(rows or {})}
    bars = [c5(i, *table[i]) for i in range(through + 1)]
    now = bars[-1].end + 3
    orng = tr.opening_range(bars, now, P)
    lv = [Level(24700.0, "swing level x2", 2)] if levels is None else list(levels)
    if orng and orng["complete"]:
        lv += [Level(orng["high"], "opening range high"), Level(orng["low"], "opening range low")]
    d = dict(c5=bars, now=now, price=bars[-1].close, opening_range=orng, levels=lv)
    d.update(over)
    return bull(**d)


def orb_events(book, side=None):
    return [e["status"] for e in reversed(book.events) if e["strategy"] == ORB]


@pytest.mark.parametrize("side", SIDES)
def test_a_candle_closing_out_of_the_opening_range_is_announced_when_the_edge_is_tested_and_confirmed_by_the_breakout(side):
    book, steps = play(orb_ctx, range(2, 6), side)
    scan = [r["scans"][(ORB, side)] for _, r in steps]
    assert scan[0].setup is None and "is still forming" in scan[0].note  # 09:30:03: the range is not complete: observed, not traded
    assert scan[1].setup is None and scan[1].applicable and "10.0 pts" in scan[1].note and "waiting for a completed 5M candle to close" in scan[1].note
    status, trigger = steps[2][1]["live_now"][(ORB, side)]
    assert status == DETECTED and trigger == pytest.approx(at(side, 24620))  # the edge was tested
    s = live(book, ORB, side)
    assert (s.status, s.direction, s.strategy, s.group) == (CONFIRMED, SIDE_NAME[side], ORB, None)
    assert s.trigger == pytest.approx(at(side, 24620)) and s.invalidation == pytest.approx(at(side, 24600.5))  # the middle of the range (24,603) less a quarter of ATR5
    assert s.zone == pytest.approx(box(side, 24620, 24630)) and s.blockers == [] and s.candle_confirmed
    assert s.level == pytest.approx(at(side, 24620)) and s.level_label == ("opening range high" if side == "bull" else "opening range low") and s.extreme == pytest.approx(at(side, 24586))
    text = " ".join(s.reasons)
    assert "opening range 09:15-09:30" in text and "34.0 pts, 1.4x ATR15" in text and "initial invalidation: the middle of the opening range" in text
    assert "breakout candle 09:40: closed 6.0 pts" in text and "body 1.2x ATR5" in text
    assert "the invalidation is the middle of the opening range" in s.confirm_text
    assert s.plan["risk_points"] == pytest.approx(19.5) and s.plan["targets"][0]["price"] == pytest.approx(at(side, 24700)) and s.plan["targets"][0]["reward_risk"] == pytest.approx(80 / 19.5)
    assert s.confirm_candle["close"] == pytest.approx(at(side, 24626)) and s.break_candle is None
    assert orb_events(book) == [DETECTED, CONFIRMED]  # announced once each, in order
    d = tr.setup_dict(s, steps[-1][0].price, True, True)
    assert d["actionable"] and d["stage"] == "READY_TO_ACT" and d["price_state"] == "in_entry_area" and "group" not in d


@pytest.mark.parametrize("side", SIDES)
def test_nothing_starts_while_the_range_forms_and_a_range_with_missing_candles_is_said_to_be_incomplete(side):
    sc = scan_one(ORB, orb_ctx(2), side)
    assert sc.applicable and sc.setup is None and "still forming" in sc.note
    sc = scan_one(ORB, orb_ctx(4, opening_range={"high": 24620.0, "low": 24586.0, "complete": False}), side)  # the clock says the range ended, the candles say they did not all arrive
    assert sc.applicable and sc.setup is None and "incomplete (candles are missing)" in sc.note
    sc = scan_one(ORB, orb_ctx(4, opening_range=None), side)
    assert sc.setup is None and "incomplete (candles are missing)" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_candle_completed_inside_the_observation_period_cannot_confirm(side):
    ctx = orb_ctx(5)
    assert scan_one(ORB, replace(ctx, confirm_after=t5(6)), side).setup is None  # the breakout candle ended exactly when the observation did
    s = scan_one(ORB, replace(ctx, confirm_after=t5(6) - 1), side).setup
    assert s is not None and s.status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("row,status,said", [
    ((24614, 24628, 24613, 24626), CONFIRMED, ""),
    ((24619, 24625, 24618, 24622), DETECTED, "not a confirming candle (body 0.3x ATR5, needs 0.4x; closed 57% of its range, needs 60%)"),  # closes beyond the edge, but weak
    ((24614, 24640, 24613, 24622), DETECTED, "not a confirming candle"),  # a long upper wick: a rejection, not a breakout
    ((24630, 24632, 24622, 24625), DETECTED, "not a confirming candle"),  # closes beyond the edge but it is a falling candle
    ((24614, 24621, 24613, 24620), DETECTED, ""),  # closes ON the edge: not out of the range
])  # fmt: skip
def test_only_a_solid_candle_closing_beyond_the_edge_confirms(side, row, status, said):
    s = scan_one(ORB, orb_ctx(5, rows={5: row}), side).setup
    assert s.status == status
    assert (not said) or any(said in a for a in s.against)


@pytest.mark.parametrize("side", SIDES)
def test_the_trigger_stays_the_edge_of_the_range_whatever_the_weak_candles_before_the_break_did(side):
    rows = {5: (24621, 24630, 24620, 24624), 6: (24624, 24631, 24623, 24629)}  # a weak close beyond the edge, then a solid one 9 beyond it
    book, steps = play(orb_ctx, range(2, 7), side, rows=rows)
    assert steps[-2][1]["live_now"][(ORB, side)] == (DETECTED, pytest.approx(at(side, 24620)))
    s = live(book, ORB, side)
    assert s.status == CONFIRMED and s.trigger == pytest.approx(at(side, 24620)) and s.confirm_candle["close"] == pytest.approx(at(side, 24629))


@pytest.mark.parametrize("side", SIDES)
def test_a_close_far_beyond_the_edge_is_not_chased_and_the_end_is_explained(side):
    book, _ = play(orb_ctx, range(2, 6), side, rows={5: (24616, 24634, 24615, 24632)})  # solid, but it closes 12 beyond the edge (limit 10)
    assert live(book, ORB, side) is None and book.recent[0].status == EXPIRED
    assert "price closed 12.0 pts (1.2x ATR5) beyond the trigger: the entry opportunity has passed" in book.recent[0].note
    book, _ = play(orb_ctx, range(2, 6), side, rows={5: (24640, 24648, 24630, 24636)})  # a falling candle that closes 16 beyond: never a confirming candle, already gone
    assert live(book, ORB, side) is None and book.recent[0].note.startswith("missed:") and "opportunity has passed" in book.recent[0].note
    assert tr.setup_stage(book.recent[0], False, True, True)[0] == "MISSED"
    sc = scan_one(ORB, orb_ctx(5, rows={5: (24640, 24648, 24630, 24636)}), side)  # seen for the first time: never announced late
    assert sc.setup.status == EXPIRED and orb_events(play(orb_ctx, [5], side, rows={5: (24640, 24648, 24630, 24636)})[0]) == []


@pytest.mark.parametrize("side", SIDES)
def test_after_the_confirmation_a_close_back_through_the_middle_or_under_the_trigger_ends_it(side):
    book, _ = play(orb_ctx, range(2, 7), side, rows={6: (24626, 24627, 24598, 24600)})  # closes 24,600 under the invalidation 24,600.5
    assert live(book, ORB, side) is None and book.recent[0].status == INVALIDATED and "(the middle of the opening range)" in book.recent[0].note
    book, _ = play(orb_ctx, range(2, 7), side, rows={6: (24626, 24627, 24612, 24616)})  # closes 24,616: under the trigger by more than a quarter of ATR5 (24,617.5)
    assert book.recent[0].status == INVALIDATED and "failed follow-through" in book.recent[0].note
    book, _ = play(orb_ctx, range(2, 7), side, rows={6: (24626, 24627, 24618, 24619)})  # 24,619: back near the edge, but not through the line
    assert live(book, ORB, side).status == CONFIRMED
    book, _ = play(orb_ctx, range(2, 11), side, rows={6: HOLD, 7: HOLD, 8: HOLD, 9: HOLD, 10: HOLD})  # the entry window after the confirmation (4 candles) passes
    assert live(book, ORB, side) is None and book.recent[0].status == EXPIRED and "entry window after the confirmation has passed" in book.recent[0].note


@pytest.mark.parametrize("side", SIDES)
def test_wandering_to_the_middle_of_the_range_before_any_breakout_is_not_a_failure_but_the_other_edge_breaking_first_is(side):
    book, _ = play(orb_ctx, range(2, 6), side, rows={3: (24610, 24611, 24598, 24600)})  # closes 24,600: under the middle (24,600.5) but nothing was entered yet
    assert live(book, ORB, side).status == CONFIRMED
    book, steps = play(orb_ctx, range(2, 6), side, rows={3: (24600, 24601, 24580, 24584)})  # closes under the range low 24,586: it broke the other way first
    sc = steps[-1][1]["scans"][(ORB, side)]
    assert sc.setup.status == INVALIDATED and "opposite edge of the opening range" in sc.note
    assert live(book, ORB, side) is None and orb_events(book) == [] and f"{ORB}:{side}:{int(t5(0))}" in book.terminal  # never announced, and not retried later in the day


@pytest.mark.parametrize("side", SIDES)
def test_the_breakout_must_be_confirmed_by_eleven_and_a_confirmed_one_carries_on_after_it(side):
    last = {**fill(5, 20), 20: BREAK}  # the candle ending exactly at 11:00 is the last that may confirm
    book, _ = play(orb_ctx, [4, 19, 20], side, rows=last)
    assert live(book, ORB, side).status == CONFIRMED
    late = {**fill(5, 21), 21: BREAK}  # ... the one that ends at 11:05 is too late, and what was being watched ends
    book, _ = play(orb_ctx, [4, 19, 20, 21], side, rows=late)
    assert live(book, ORB, side) is None and book.recent[0].status == EXPIRED and "was not confirmed by 11:00" in book.recent[0].note
    assert scan_one(ORB, orb_ctx(21, rows=late), side).setup.status == EXPIRED
    kept = {**fill(5, 19), 19: BREAK, 20: HOLD, 21: HOLD}  # confirmed at 10:55: the window only limits starting a breakout, not living through the entry window
    book, _ = play(orb_ctx, [18, 19, 20, 21], side, rows=kept)
    assert live(book, ORB, side).status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
def test_in_stricter_confirmation_the_next_candle_must_also_hold_the_break(side):
    kw = dict(strict=True, strict_why="volatility is high")
    book, _ = play(orb_ctx, range(2, 6), side, **kw)
    s = live(book, ORB, side)
    assert s.status == WAITING and not s.candle_confirmed and "must also close beyond it" in s.confirm_text
    book, _ = play(orb_ctx, range(2, 7), side, rows={6: HOLD}, **kw)
    s = live(book, ORB, side)
    assert s.status == CONFIRMED and s.confirm_candle["close"] == pytest.approx(at(side, 24627)) and s.break_candle["close"] == pytest.approx(at(side, 24626))
    book, steps = play(orb_ctx, range(2, 7), side, rows={6: (24626, 24627, 24617, 24618)}, **kw)  # the next candle closes 24,618: not beyond the edge
    assert live(book, ORB, side).status == WAITING and not live(book, ORB, side).candle_confirmed  # (a setup only moves forward: it waits on, unconfirmed)
    assert "no follow-through" in steps[-1][1]["scans"][(ORB, side)].setup.against[-1]


@pytest.mark.parametrize("side", SIDES)
def test_the_existing_risk_limits_decide_whether_the_middle_of_the_range_is_a_usable_invalidation(side):
    tight = {0: (24601, 24603, 24600, 24602), 1: (24602, 24605, 24601, 24604), 2: (24604, 24606, 24602, 24605), 3: (24604, 24605, 24602, 24604)}  # 6 points: the risk is 5.5
    sc = scan_one(ORB, orb_ctx(3, rows=tight), side)
    assert sc.applicable and sc.setup is None and "inside normal noise" in sc.note and "not a usable risk" in sc.note
    wide = {0: (24580, 24590, 24560, 24585), 1: (24585, 24620, 24575, 24615), 2: (24615, 24640, 24610, 24630), 3: (24630, 24635, 24625, 24632)}  # 80 points: the risk is 42.5
    sc = scan_one(ORB, orb_ctx(3, rows=wide), side)
    assert sc.applicable and sc.setup is None and "too far from the structure" in sc.note
    book, _ = play(orb_ctx, range(2, 6), side)
    s = live(book, ORB, side)
    shrunk = dict(atr={"1h": 60.0, "15m": 10.0, "5m": 10.0, "1m": 4.0})  # the 15M ATR falls to 10 later: the limit becomes 15 points, the risk is 19.5
    assert scan_one(ORB, orb_ctx(5, **shrunk), side).setup is None  # a new setup would not start ...
    kept = scan_one(ORB, orb_ctx(5, **shrunk), side, prior=s).setup
    assert kept is not None and kept.status == CONFIRMED and kept.blockers == []  # ... but the one already announced keeps the risk it was started with


@pytest.mark.parametrize("side", SIDES)
def test_a_level_too_close_above_the_edge_and_the_session_and_data_blockers_hold_a_confirmed_breakout_back(side):
    book, steps = play(orb_ctx, range(2, 6), side, levels=[Level(24630.0, "swing level x2", 2)])  # 10 points of room for 19.5 at risk
    s = live(book, ORB, side)
    assert s.status == WAITING and s.candle_confirmed and any("only 10.0 pts to the next level" in b for b in s.blockers)
    d = tr.setup_dict(s, steps[-1][0].price, True, True)
    assert not d["actionable"] and d["stage"] == "BLOCKED"
    book, steps = play(orb_ctx, range(2, 6), side, can_confirm=False, blockers=["historical data is stale: today's refresh has not completed"])
    s = live(book, ORB, side)
    assert s.status == WAITING and s.candle_confirmed and s.blockers == ["historical data is stale: today's refresh has not completed"]
    assert tr.setup_dict(s, steps[-1][0].price, True, True)["stage"] == "BLOCKED"


@pytest.mark.parametrize("side", SIDES)
def test_opening_range_breakout_follows_the_higher_timeframes_like_breakout_and_retest(side):
    assert scan_one(ORB, orb_ctx(5), side).setup is not None
    sideways = orb_ctx(5, trend={"1h": "sideways", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment="range_bound")
    assert scan_one(ORB, sideways, side).setup is not None  # not against anything: allowed
    against = orb_ctx(5, trend={"1h": "bearish", "15m": "sideways", "5m": "sideways", "1m": "sideways"})
    sc = scan_one(ORB, against, side)
    assert not sc.applicable and "against the higher-timeframe direction" in sc.note
    sc = scan_one(ORB, orb_ctx(5, trend={"1h": "unknown", "15m": "unknown", "5m": "unknown", "1m": "unknown"}), side)
    assert not sc.applicable and "not enough completed" in sc.note
    sc = scan_one(ORB, orb_ctx(5, atr={"1h": 60.0, "15m": 25.0, "5m": None, "1m": 4.0}), side)
    assert sc.applicable and sc.setup is None and "ATR is not available yet" in sc.note


def test_a_tracked_opening_range_setup_survives_a_restart_without_being_announced_again():
    book, steps = play(orb_ctx, range(2, 5), "bull")
    s, ctx = live(book, ORB, "bull"), steps[-1][0]
    record = {"setups": [tr.setup_dict(s, ctx.price, True, True)], "events": list(reversed(book.events)), "market": {"regime": "trend_up"}}
    fresh = tr.SetupBook(P)
    assert fresh.restore([record], ctx.now) == 1
    r = fresh.active[(ORB, "bull")]
    assert r.resumed and r.anchor_time == s.anchor_time == t5(0) and r.status == DETECTED
    before = len(fresh.events)
    nxt = orb_ctx(5)
    fresh.update(nxt, {"session": tr.session_info(nxt.now, P)}, nxt.now)
    assert fresh.active[(ORB, "bull")].status == CONFIRMED
    assert [e["status"] for e in list(fresh.events)[: len(fresh.events) - before]] == [CONFIRMED]


# ====================================================================================================================
# Failed Breakout (Trap Reversal)
# ====================================================================================================================
FB_BASE = (24611, 24614, 24607, 24611)
FB_ROWS = {20: (24611, 24613, 24604, 24606), 21: (24606, 24607, 24592, 24594), 22: (24594, 24597, 24588, 24590), 23: (24599, 24606, 24598, 24601), 24: (24601, 24610, 24600, 24608)}
QUIET = (24603, 24605, 24601, 24603)  # under the base high 24,606, above the low 24,588


def fb_ctx(through, rows=None, **over):
    """Quiet candles above the support 24,600, a close under it, the reclaim and the confirmation. No 15M swings, so Trend Pullback and the continuation strategies stay out."""
    table = {i: FB_BASE for i in range(20)}
    table.update(FB_ROWS)
    table.update(rows or {})
    bars = [c5(i, *table[i]) for i in range(through + 1)]
    d = dict(c5=bars, now=bars[-1].end + 3, price=bars[-1].close, swings={"15m": []}, levels=[Level(24600.0, "swing level x3", 3), Level(24700.0, "swing level x2", 2)])
    d.update(over)
    return bull(**d)


def fb_events(book):
    return [e["status"] for e in reversed(book.events) if e["strategy"] == FB]


@pytest.mark.parametrize("side", SIDES)
def test_a_close_under_a_level_taken_back_within_a_few_candles_is_a_trap_and_the_reversal_is_confirmed_through_the_base(side):
    book, steps = play(fb_ctx, range(21, 25), side)
    scan = [r["scans"][(FB, side)] for _, r in steps]
    for sc in scan[:2]:  # 11:00 and 11:05: the level broke and has not been taken back: a break in progress, not a failed one
        assert sc.applicable and sc.setup is None and "has not closed back" in sc.note and "a break in progress, not a failed one" in sc.note
    assert steps[2][1]["live_now"][(FB, side)] == (WAITING, pytest.approx(at(side, 24606)))  # the reclaim candle is weak: the story exists, the confirmation does not
    s = live(book, FB, side)
    assert (s.status, s.direction, s.strategy, s.group) == (CONFIRMED, SIDE_NAME[side], FB, None)
    assert s.trigger == pytest.approx(at(side, 24606)) and s.invalidation == pytest.approx(at(side, 24585.5))  # the base high; the low of the failed break less a quarter of ATR5
    assert s.zone == pytest.approx(box(side, 24606, 24616)) and s.blockers == [] and s.candle_confirmed
    assert s.level == pytest.approx(at(side, 24600)) and s.level_label == "swing level x3" and s.extreme == pytest.approx(at(side, 24588))
    word = "breakdown" if side == "bull" else "breakout"
    text = " ".join(s.reasons)
    assert f"failed {word} of swing level x3 {at(side, 24600):,.2f}: a 5M candle closed" in text and "at 11:00" in text and "at 11:10 (2 candles later)" in text
    assert "went 12.0 pts (0.48x ATR15)" in text and "1H bullish and 15M bullish" in text.replace("bearish", "bullish")
    assert "never below the reclaimed swing level x3" in s.confirm_text
    assert s.plan["risk_points"] == pytest.approx(20.5) and s.plan["targets"][0]["price"] == pytest.approx(at(side, 24700))
    assert fb_events(book) == [WAITING, CONFIRMED]
    d = tr.setup_dict(s, steps[-1][0].price, True, True)
    assert d["actionable"] and d["stage"] == "READY_TO_ACT" and "group" not in d


@pytest.mark.parametrize("side", SIDES)
def test_a_strong_reclaim_candle_confirms_at_once_and_the_trigger_is_never_below_the_reclaimed_level(side):
    book, _ = play(fb_ctx, range(21, 24), side, rows={23: (24590, 24603, 24589, 24602)})  # body 12, closes at 93%, above the level: reclaim and confirmation in one candle
    s = live(book, FB, side)
    assert s.status == CONFIRMED and s.trigger == pytest.approx(at(side, 24600))  # the base highs (24,597) are under the level: the level is the floor of the trigger
    assert s.zone == pytest.approx(box(side, 24600, 24610)) and s.plan["risk_points"] == pytest.approx(14.5)


@pytest.mark.parametrize("side", SIDES)
def test_a_recovery_that_is_too_slow_or_a_push_that_is_too_deep_or_too_shallow_is_not_a_trap(side):
    slow = {i: (24597, 24599, 24595, 24598) for i in range(22, 29)}  # hovers just under the level (inside the margin) for 7 candles
    slow[29] = (24598, 24606, 24597, 24604)
    sc = scan_one(FB, fb_ctx(29, rows=slow), side)
    assert sc.applicable and sc.setup is None and "taken back 8 candles later (over 6)" in sc.note and "a slow recovery, not a trap" in sc.note
    deep = {22: (24594, 24597, 24570, 24590)}  # the low is 30 under the level: over 1.0x ATR15 (25)
    book, steps = play(fb_ctx, range(21, 25), side, rows=deep)
    sc = steps[-1][1]["scans"][(FB, side)]
    assert sc.setup.status == INVALIDATED and "pushed 30.0 pts (1.2x ATR15)" in sc.note and "a genuine break, not a failed one" in sc.note
    assert live(book, FB, side) is None and fb_events(book) == []  # never announced
    shallow = {21: (24606, 24607, 24593, 24597), 22: (24597, 24598, 24588, 24597)}  # closes at 24,597: inside the margin (24,596.25): not a close beyond the level
    sc = scan_one(FB, fb_ctx(24, rows=shallow), side)
    assert sc.setup is None and "nothing has been trapped" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_level_that_price_has_only_ever_been_under_is_not_broken_and_yesterdays_candles_do_not_count(side):
    sc = scan_one(FB, fb_ctx(24, levels=[Level(24700.0, "swing level x2", 2)]), side)  # every candle is "under" 24,700, none ever broke it
    assert sc.setup is None and "nothing has been trapped" in sc.note
    ctx = fb_ctx(5, rows={i: FB_BASE for i in range(6)})
    yesterday = [Candle(ist(15, 15, day=(2026, 11, 5)), ist(15, 20, day=(2026, 11, 5)), 24611, 24614, 24607, 24610, 0, source="db"),  # above the level
                 Candle(ist(15, 20, day=(2026, 11, 5)), ist(15, 25, day=(2026, 11, 5)), 24610, 24611, 24585, 24590, 0, source="db")]  # ... a close under it
    sc = scan_one(FB, replace(ctx, c5=yesterday + ctx.c5), side)  # and today's first candle is back above it: across the overnight gap that is not a trap
    assert sc.setup is None and "nothing has been trapped" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_price_rising_up_through_a_level_from_below_is_not_a_failed_breakdown(side):
    below = {i: (24590, 24593, 24588, 24591) for i in range(18)}  # eighteen closes under the level (24,591 < 24,596.25) ...
    below[18], below[19] = (24591, 24608, 24590, 24606), (24606, 24612, 24604, 24610)  # ... then a close above it and a solid candle: a rise through the level, not a trap
    sc = scan_one(FB, fb_ctx(19, rows=below), side)
    assert sc.applicable and sc.setup is None and "nothing has been trapped" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_the_confirmation_needs_a_candle_through_the_base_high_and_what_follows_is_explained(side):
    book, _ = play(fb_ctx, range(21, 25), side, rows={24: (24601, 24607, 24600, 24605)})  # closes 24,605: under the base high 24,606
    assert live(book, FB, side).status == WAITING and not live(book, FB, side).candle_confirmed
    book, _ = play(fb_ctx, range(21, 26), side, rows={25: (24608, 24609, 24580, 24584)})  # closes under 24,585.5
    assert live(book, FB, side) is None and book.recent[0].status == INVALIDATED and "invalidation level" in book.recent[0].note
    book, _ = play(fb_ctx, range(21, 26), side, rows={25: (24608, 24609, 24600, 24602)})  # 24,602: under the trigger by more than a quarter of ATR5 (24,603.5)
    assert book.recent[0].status == INVALIDATED and "failed follow-through" in book.recent[0].note
    book, _ = play(fb_ctx, range(21, 26), side, rows={25: (24608, 24626, 24607, 24625)})  # 19 beyond the trigger
    assert book.recent[0].status == EXPIRED and "the entry opportunity has passed" in book.recent[0].note
    book, _ = play(fb_ctx, range(21, 26), side, rows={25: (24608, 24612, 24606, 24609)})
    assert live(book, FB, side).status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
def test_in_stricter_confirmation_the_failed_breakout_waits_for_the_next_candle(side):
    kw = dict(strict=True, strict_why="volatility is high")
    book, _ = play(fb_ctx, range(21, 25), side, **kw)
    s = live(book, FB, side)
    assert s.status == WAITING and not s.candle_confirmed and "(stricter confirmation)" in s.confirm_text
    book, _ = play(fb_ctx, range(21, 26), side, rows={25: (24608, 24614, 24607, 24612)}, **kw)
    s = live(book, FB, side)
    assert s.status == CONFIRMED and s.break_candle["close"] == pytest.approx(at(side, 24608)) and s.confirm_candle["close"] == pytest.approx(at(side, 24612))


@pytest.mark.parametrize("side", SIDES)
def test_a_trap_that_is_older_than_the_window_stays_tracked_until_it_ends(side):
    rows = {i: QUIET for i in range(25, 45)}
    rows[24] = QUIET  # the confirming candle never comes: the reversal waits under its base high
    book, _ = play(fb_ctx, range(21, 35), side, rows=rows)
    assert live(book, FB, side).status == WAITING  # the failed break is 13 candles back: outside the 12-candle window that finds a NEW trap ...
    assert scan_one(FB, fb_ctx(34, rows=rows), side).setup is None  # ... which a fresh look no longer sees
    book, _ = play(fb_ctx, range(21, 43), side, rows=rows)  # ... and it ends in the usual way: no resumption within 18 candles of the low
    assert live(book, FB, side) is None and any("no resumption within 18 candles of the" in e["text"] for e in book.events)


@pytest.mark.parametrize("side", SIDES)
def test_failed_breakout_follows_the_original_side_rule_with_no_exception(side):
    ok = fb_ctx(24)
    assert scan_one(FB, ok, side).setup is not None
    for one in ({"1h": "bearish", "15m": "bullish"}, {"1h": "bullish", "15m": "bearish"}, {"1h": "bearish", "15m": "bearish"}):
        sc = scan_one(FB, replace(ok, trend={**ok.trend, **one}), side)
        assert not sc.applicable and "against the higher-timeframe direction" in sc.note  # the reversal side is only allowed where 1H and 15M allow it
    sideways = replace(ok, trend={"1h": "sideways", "15m": "bullish", "5m": "sideways", "1m": "sideways"}, alignment="pausing")
    assert scan_one(FB, sideways, side).setup is not None
    sc = scan_one(FB, replace(ok, trend={"1h": "unknown", "15m": "unknown", "5m": "unknown", "1m": "unknown"}), side)
    assert not sc.applicable and "not enough completed" in sc.note
    sc = scan_one(FB, replace(ok, atr={"1h": 60.0, "15m": None, "5m": 10.0, "1m": 4.0}), side)
    assert sc.applicable and sc.setup is None and "ATR is not available yet" in sc.note


def test_a_tracked_failed_breakout_survives_a_restart_without_being_announced_again():
    book, steps = play(fb_ctx, range(21, 24), "bull")
    s, ctx = live(book, FB, "bull"), steps[-1][0]
    assert s.status == WAITING
    record = {"setups": [tr.setup_dict(s, ctx.price, True, True)], "events": list(reversed(book.events)), "market": {"regime": "trend_up"}}
    fresh = tr.SetupBook(P)
    assert fresh.restore([record], ctx.now) == 1
    r = fresh.active[(FB, "bull")]
    assert r.resumed and r.anchor_time == s.anchor_time == t5(21) and r.status == WAITING and r.level == 24600.0
    before = len(fresh.events)
    nxt = fb_ctx(24)
    fresh.update(nxt, {"session": tr.session_info(nxt.now, P)}, nxt.now)
    assert fresh.active[(FB, "bull")].status == CONFIRMED
    assert [e["status"] for e in list(fresh.events)[: len(fresh.events) - before]] == [CONFIRMED]


# ====================================================================================================================
# One movement is never announced twice
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_failed_breakout_and_range_reversal_never_describe_the_same_candles(side):
    """The range scenario is a failed breakdown of the support 24,500. In the range regime Range Reversal takes it and Failed Breakout stands aside; in any other
    regime the same candles are Failed Breakout's and Range Reversal is out. Swept over every combination: never both."""
    seen = {RR: 0, FB: 0}
    for t1 in ("sideways", "bullish"):
        for t15 in ("sideways", "bullish"):
            for alignment in ("range_bound", "pausing", "aligned_bullish"):
                ctx = bull_range(35, trend={"1h": t1, "15m": t15, "5m": "sideways", "1m": "sideways"}, alignment=alignment)
                scans = st.scan_all(oriented(ctx, side), P, {})
                mine = [n for n in (RR, FB) if scans[(n, side)].setup is not None]
                assert len(mine) <= 1, f"{t1}/{t15}/{alignment}: {mine} both describe the same failed breakdown"
                seen.update({n: seen[n] + 1 for n in mine})
    assert seen[RR] >= 1 and seen[FB] >= 1  # the sweep really covers both territories
    sc = scan_one(FB, bull_range(35), side)
    assert not sc.applicable and "Range Reversal's" in sc.note
    assert scan_one(RR, bull_range(35), side).setup is not None  # and Range Reversal still takes its own scenario exactly as before


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("build,throughs,kw,expected", [(orb_ctx, range(2, 6), {}, {ORB}), (fb_ctx, range(21, 25), {}, {FB})])
def test_each_scenario_is_announced_by_its_own_strategy_alone_at_every_step(side, build, throughs, kw, expected):
    _, steps = play(build, throughs, side, **kw)
    for _, r in steps:
        assert {s.strategy for s in r["setups"] if s.side == side} <= expected


# ====================================================================================================================
# Through the whole pipeline: the decision, and the checks that no strategy can bypass
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("build,throughs,name", [(orb_ctx, range(2, 6), ORB), (fb_ctx, range(21, 25), FB)])
def test_each_new_strategy_reaches_a_buy_through_the_same_decision_pipeline(side, build, throughs, name):
    out = decision_after(build, throughs, side)
    d = out["decision"]
    assert d["action"] == ("BUY CE" if side == "bull" else "BUY PE") and d["strategy"] == name and d["strategy_name"] == registry.NAMES[name]
    assert [m["id"] for m in out["strategies"]] == list(registry.NAMES) and next(m for m in out["strategies"] if m["id"] == name)["state"] == "CONFIRMED"
    assert out["explain"]["actionable"] and out["setups"][0]["stage"] == "READY_TO_ACT" and out["setups"][0]["strategy"] == name


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("build,throughs", [(orb_ctx, range(2, 6)), (fb_ctx, range(21, 25))])
def test_neither_new_strategy_can_produce_a_buy_when_the_data_the_session_or_the_price_does_not_allow_it(side, build, throughs):
    bad = decision_after(build, throughs, side, data_ok=False, issues=["historical data is stale: today's refresh has not completed"])["decision"]
    assert bad["action"] == "NO TRADE" and "not reliable" in bad["headline"]
    extreme = decision_after(build, throughs, side, vol_extreme=True)["decision"]
    assert extreme["action"] != ("BUY CE" if side == "bull" else "BUY PE")
    old = decision_after(build, throughs, side, tick_age=60.0)  # the live price stopped: nothing is actionable
    assert old["decision"]["action"] == "WAIT" and not any(s["actionable"] for s in old["setups"])
    blocked = decision_after(build, throughs, side, can_confirm=False, blockers=["late session"])
    assert not blocked["decision"]["action"].startswith("BUY")


def test_the_scenario_contexts_are_real_contexts_and_mirror_to_the_same_prices():
    assert isinstance(orb_ctx(5), st.Context) and isinstance(fb_ctx(24), st.Context)
    m = oriented(orb_ctx(5), "bear")
    assert m.opening_range == {"high": M - 24586.0, "low": M - 24620.0, "complete": True}
