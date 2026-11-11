"""Stage 3: Trend Exhaustion Reversal on hand-built candles (the numbers are worked out by hand in the comments, as in scenarios.py). NIFTY ~24,600, ATR(5M) = 10, ATR(15M) = 25:
a confirming candle's body is 4, the entry-chase limit 10, the invalidation buffer 2.5, the risk limits 7.5 and 37.5. Every scenario is also run mirrored (the bearish twin) through the
real SetupBook, so the lifecycle, the announcements, the permission and the decision are exercised, not only the scanner.

The market (bullish orientation = the CALL trade after a BEARISH exhaustion; its mirror is the put trade after a bullish one). 15M swing structure, confirmed pivots in time order:
    H_a 24,760 (j=2)   L_a 24,600 (j=4)   H_b 24,650 (j=6)   L_b 24,520 (j=9)   L_c 24,580 (j=11)   H_c 24,715 (j=13)
    lower high H_b < H_a and lower low L_b < L_a (the exhausted down structure), then a higher low L_c > L_b and a higher high H_c > H_b (the structure turned).
    The exhaustion extreme is L_b = 24,520: the leg from H_a was 240 pts = 9.6x ATR15. The change is confirmed when the later pivot (j=13) has two candles after it: the end of j=15, i.e. 5M slot 48.
5M candles from the higher low's 15M candle (slot 33):
    slots 33-41   a rally from 24,580 to the 5M swing high 24,715 (slot 40, the 15M higher high)
    slots 42-44   a pullback to 24,662
    slot 46       the 5M swing high 24,682: the TRIGGER (confirmed once slots 47 and 48 are in)
    slots 47-50   a drift down to the 5M pullback low 24,662
    slot 51       24,668 -> 24,692, low 24,666, close 24,690: body 22, closes at 92% of its range, 8 above the trigger: the BREAK.
    risk: the invalidation is the pullback low 24,662 less 2.5 = 24,659.5, so the risk from the trigger 24,682 is 22.5; the next level up is the 15M higher high 24,715: 33 away (1.47x the risk)."""

from dataclasses import replace

import pytest
from scenarios import P, bull, c5, c15, t15
from test_trend_family import at, box, decision_after, live, play, scan_one

from app import trading as tr
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Level

SIDES = ["bull", "bear"]
TER = "trend_exhaustion_reversal"
SIDE_NAME = {"bull": "CE", "bear": "PE"}

C15 = [
    (24700, 24730, 24690, 24720), (24720, 24750, 24715, 24745), (24745, 24760, 24730, 24740), (24740, 24745, 24650, 24660), (24660, 24668, 24600, 24615),
    (24615, 24645, 24610, 24640), (24640, 24650, 24620, 24630), (24630, 24635, 24570, 24580), (24580, 24590, 24545, 24550), (24550, 24565, 24520, 24535),
    (24535, 24575, 24530, 24570), (24585, 24600, 24580, 24595), (24595, 24660, 24590, 24655), (24655, 24715, 24650, 24700), (24700, 24705, 24665, 24670),
    (24670, 24690, 24660, 24685), (24685, 24686, 24662, 24670),
]  # fmt: skip
SWINGS = [("H", t15(2), 24760.0), ("L", t15(4), 24600.0), ("H", t15(6), 24650.0), ("L", t15(9), 24520.0), ("L", t15(11), 24580.0), ("H", t15(13), 24715.0)]
FIVE = {
    33: (24585, 24590, 24581, 24586), 34: (24586, 24592, 24580, 24590), 35: (24590, 24600, 24588, 24595), 36: (24595, 24615, 24592, 24612), 37: (24612, 24635, 24608, 24630),
    38: (24630, 24660, 24625, 24655), 39: (24655, 24680, 24650, 24676), 40: (24676, 24715, 24672, 24705), 41: (24705, 24708, 24690, 24700), 42: (24700, 24704, 24680, 24684),
    43: (24684, 24688, 24668, 24672), 44: (24672, 24676, 24662, 24668), 45: (24668, 24680, 24666, 24678), 46: (24678, 24682, 24672, 24676), 47: (24676, 24680, 24668, 24672),
    48: (24672, 24678, 24664, 24670), 49: (24670, 24674, 24662, 24666), 50: (24666, 24672, 24662, 24668), 51: (24668, 24692, 24666, 24690),
}  # fmt: skip
NO_TURN = [("H", t15(2), 24760.0), ("L", t15(4), 24600.0), ("H", t15(6), 24650.0), ("L", t15(9), 24520.0), ("H", t15(11), 24600.0), ("L", t15(13), 24500.0)]
# lower highs and lower lows all the way: the down structure simply continues


def facts(weak="bearish", ext="none"):
    """Regime facts as the regime layer would give them, with only the states this strategy reads set (everything else "none")."""
    f = {n: {"state": "none", "checks": [], "summary": ""} for n in ("strong_trend", "compression", "volatility_expansion", "pause")}
    f["weakening_trend"] = {"state": weak, "checks": [], "summary": "ADX falling, no new extreme" if weak != "none" else "no check holds"}
    f["extended"] = {"state": ext, "checks": [], "summary": "3 ATR15 from the EMA20" if ext != "none" else "not extended"}
    return {"available": True, "why": None, "facts": f, "active": []}


def ter_ctx(through, rows=None, swings=None, c15_rows=None, weak="bearish", ext="none", trend=None, alignment="pausing", **over):
    """The market up to the close of 5M slot `through`. Default: 1H bearish and 15M sideways (so a call is against the 1H: the permission is needed)."""
    table = {**FIVE, **(rows or {})}
    bars = [c5(i, *table[i]) for i in range(33, through + 1)]
    cs15 = [c15(j, *r) for j, r in enumerate(c15_rows or C15)]
    cs15 = [x for x in cs15 if x.end <= bars[-1].end]  # only the 15M candles that have completed
    d = dict(
        c5=bars, c15=cs15, now=bars[-1].end + 3, price=bars[-1].close, regime=facts(weak, ext),
        trend=trend or {"1h": "bearish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment=alignment,
        pattern={"1h": "LH_LL", "15m": "HH_HL", "5m": "mixed", "1m": "mixed"}, adx={"1h": 30.0, "15m": 18.0, "5m": 14.0, "1m": 10.0},
        swings={"15m": SWINGS if swings is None else swings}, rsi={"1h": 30.0, "15m": 40.0, "5m": 50.0, "1m": 50.0},
        ema={"1h": (24800.0, 24850.0), "15m": (24640.0, 24660.0), "5m": (24660.0, 24650.0), "1m": (24680.0, 24678.0)},
        ema_slope={"15m": {"ema20": 2.0, "ema50": -1.0, "stack_bars": -25}, "5m": {"ema20": 1.0, "ema50": 0.0, "stack_bars": -5}},
        levels=[Level(24800.0, "swing level x2", 2)],
    )  # fmt: skip
    d.update(over)
    return bull(**d)


def live_ter(book, side):
    return live(book, TER, side)


def straight(a=33, b=48):
    """Every candle's high above the one before (no swing high forms): a rally with no pullback."""
    return {i: (24585 + 8 * (i - 33), 24595 + 8 * (i - 33), 24583 + 8 * (i - 33), 24593 + 8 * (i - 33)) for i in range(a, b)}


# ====================================================================================================================
# The lifecycle: DETECTED -> WAITING -> CONFIRMED, and how it ends
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_confirmed_15m_reversal_then_a_5m_break_is_a_counter_trend_setup_and_confirms(side):
    book, steps = play(ter_ctx, range(47, 52), side)
    scans = [r["scans"][(TER, side)] for _, r in steps]
    for sc in scans:
        assert sc.info["granted"] and sc.info["needed"]  # the 1H is against the call: needed; the structure turned: granted
    assert steps[0][1]["live_now"][(TER, side)] == (WAITING, pytest.approx(at(side, 24715)))  # slot 47: the only confirmed 5M swing high so far is the 15M higher high
    s = live_ter(book, side)
    assert (s.status, s.direction, s.strategy) == (CONFIRMED, SIDE_NAME[side], TER)
    assert s.trigger == pytest.approx(at(side, 24682)) and s.invalidation == pytest.approx(at(side, 24659.5)) and s.zone == pytest.approx(box(side, 24682, 24692))
    assert s.counter_trend["granted"] and s.counter_trend["needed"] and s.counter_trend["structure"]["exhaustion_extreme"] == pytest.approx(at(side, 24520))
    assert s.plan["risk_points"] == pytest.approx(22.5) and s.blockers == [] and s.candle_confirmed


@pytest.mark.parametrize("side", SIDES)
def test_the_reversal_is_detected_when_there_is_no_5m_swing_to_break_yet_then_waits_on_the_trigger_then_confirms(side):
    book, steps = play(ter_ctx, [47], side, rows=straight())
    s = live_ter(book, side)
    assert s.status == DETECTED and s.trigger is None and s.counter_trend["granted"] and not s.candle_confirmed  # the permission exists, the 5M has no swing high to break
    assert "a 5M swing" in s.confirm_text
    book, steps = play(ter_ctx, range(47, 52), side)
    assert [r["live_now"][(TER, side)][0] for _, r in steps] == [WAITING, WAITING, WAITING, WAITING, CONFIRMED]
    assert [e["status"] for e in reversed(book.events) if e["strategy"] == TER] == [WAITING, CONFIRMED]  # announced once each, in order
    assert steps[1][1]["live_now"][(TER, side)][1] == pytest.approx(at(side, 24715)) and steps[2][1]["live_now"][(TER, side)][1] == pytest.approx(at(side, 24682))  # the trigger follows the latest confirmed 5M swing high


@pytest.mark.parametrize("side", SIDES)
def test_a_15m_reversal_without_a_5m_break_is_waiting_and_never_actionable(side):
    book, steps = play(ter_ctx, range(47, 51), side)
    s = live_ter(book, side)
    assert s.status == WAITING and not s.candle_confirmed and s.trigger == pytest.approx(at(side, 24682))
    d = tr.setup_dict(s, steps[-1][0].price, True, True)
    assert not d["actionable"] and d["stage"] == "WAITING_FOR_CONFIRMATION"
    assert decision_after(ter_ctx, range(47, 51), side)["decision"]["action"] == "WAIT"


@pytest.mark.parametrize("side", SIDES)
def test_the_confirmed_setup_carries_its_evidence_and_is_explained(side):
    book, _ = play(ter_ctx, range(47, 52), side)
    s = live_ter(book, side)
    text = " ".join(s.reasons)
    assert "counter-trend permission granted" in text and "structure turned" in text and "the 1H trend is " + ("bearish" if side == "bull" else "bullish") in text
    assert "5M break: the 13:30 candle closed 8.0 pts" in text and f"{at(side, 24682):,.2f}" in text
    assert s.counter_trend["structure"]["higher_low"] == pytest.approx(at(side, 24580)) and s.counter_trend["structure"]["higher_high"] == pytest.approx(at(side, 24715))
    assert "counter-trend:" in " ".join(s.against)  # the setup says it runs against a higher timeframe
    want = ["15M higher high", "15M prior high"] if side == "bull" else ["15M lower low", "15M prior low"]  # (a put's targets are swing LOWS below the trigger: they are named as such)
    assert [t["label"] for t in s.plan["targets"]][:2] == want and s.plan["targets"][0]["price"] == pytest.approx(at(side, 24715))
    assert s.confirm_text.count("invalidation is the 5M pullback " + ("low" if side == "bull" else "high")) == 1 and s.level_label == ("exhaustion low" if side == "bull" else "exhaustion high")
    d = decision_after(ter_ctx, range(47, 52), side)
    assert any("counter-trend permission" in x for x in d["explain"]["now"]) and d["counter_trend"][0]["granted"]


@pytest.mark.parametrize("side", SIDES)
def test_the_end_of_a_reversal_setup_is_explained_and_nothing_revives_it(side):
    def ended(row, last=52):
        return play(ter_ctx, range(47, last + 1), side, rows={last: row})[0]

    book = ended((24690, 24690, 24655, 24658))  # closes 24,658: under the invalidation 24,659.5 (the 5M pullback low 24,662 less 2.5)
    assert live_ter(book, side) is None and book.recent[0].status == INVALIDATED and "the invalidation level" in book.recent[0].note
    book = ended((24690, 24690, 24668, 24678))  # 24,678: under the trigger by more than a quarter of ATR5 (24,679.5), above the invalidation
    assert book.recent[0].status == INVALIDATED and "failed follow-through" in book.recent[0].note
    book = ended((24690, 24698, 24689, 24696))  # 14 beyond the trigger: do not chase
    assert book.recent[0].status == EXPIRED and "the entry opportunity has passed" in book.recent[0].note
    assert not tr.setup_dict(book.recent[0], 24696.0, True, True)["actionable"] and tr.setup_dict(book.recent[0], 24696.0, True, True)["stage"] == "MISSED"
    held = {i: (24690, 24692, 24684, 24686) for i in range(52, 56)}  # the entry window of four candles after the confirmation passes (slots 52-55 hold)
    book, _ = play(ter_ctx, range(47, 56), side, rows=held)
    assert live_ter(book, side) is None and book.recent[0].status == EXPIRED and "entry window after the confirmation has passed" in book.recent[0].note
    key = book.recent[0].key
    more = {**held, **{i: (24686, 24690, 24680, 24684) for i in range(56, 64)}}
    book, _ = play(ter_ctx, range(47, 64), side, rows=more)  # ... and the same structural break gives no second setup while it stays in view
    assert [e["status"] for e in reversed(book.events) if e["strategy"] == TER] == [WAITING, CONFIRMED, EXPIRED] and key in book.terminal and live_ter(book, side) is None


@pytest.mark.parametrize("side", SIDES)
def test_a_candle_that_completed_before_the_15m_change_was_confirmed_cannot_confirm_the_break(side):
    early = {47: (24676, 24730, 24674, 24728)}  # slot 47 closes 13 above the 15M higher high and the swing high it would break, a strong candle, but it completed as the 15M change was confirmed
    book, _ = play(ter_ctx, [47], side, rows=early)
    s = live_ter(book, side)
    assert s.status == WAITING and not s.candle_confirmed and s.trigger == pytest.approx(at(side, 24715))  # it only waits: the break must be a candle completed after the confirmation
    quiet = {i: (24728, 24730, 24722, 24726) for i in range(48, 51)}
    book, _ = play(ter_ctx, range(47, 51), side, rows={**early, **quiet})
    assert live_ter(book, side).status == WAITING and not live_ter(book, side).candle_confirmed  # and a later look does not count it either


@pytest.mark.parametrize("side", SIDES)
def test_a_break_candle_with_no_body_does_not_confirm(side):
    weak = {51: (24686, 24690, 24684, 24688)}  # closes 6 above the trigger 24,682, but its body is 2 (the confirming candle needs 4: 0.4x ATR5)
    book, _ = play(ter_ctx, range(47, 52), side, rows=weak)
    s = live_ter(book, side)
    assert s.status == WAITING and not s.candle_confirmed and s.trigger == pytest.approx(at(side, 24682))
    strong, _ = play(ter_ctx, range(47, 52), side, rows={51: (24684, 24690, 24683, 24689)})  # a body of 5 closing at 86% of its range is enough: the rule is the body, not the close
    assert live_ter(strong, side).status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
def test_losing_the_15m_higher_low_or_the_exhaustion_low_ends_it_at_any_stage(side):
    book, _ = play(ter_ctx, range(47, 50), side, rows={49: (24670, 24671, 24565, 24570)})  # a 5M close at 24,570: under the 15M higher low 24,580 less 2.5, while still waiting
    assert live_ter(book, side) is None and "the structure the reversal rests on is lost" in book.recent[0].note
    book, _ = play(ter_ctx, range(47, 50), side, rows={49: (24670, 24671, 24500, 24510)})  # ... and under the exhaustion low 24,520
    assert "the reversal failed" in book.recent[0].note and "exhaustion" in book.recent[0].note


@pytest.mark.parametrize("side", SIDES)
def test_in_stricter_confirmation_the_next_candle_must_hold_the_break(side):
    kw = dict(strict=True, strict_why="volatility is high")
    book, _ = play(ter_ctx, range(47, 52), side, **kw)
    s = live_ter(book, side)
    assert s.status == WAITING and not s.candle_confirmed and "must also close beyond it" in s.confirm_text
    book, _ = play(ter_ctx, range(47, 53), side, rows={52: (24690, 24692, 24686, 24688)}, **kw)
    s = live_ter(book, side)
    assert s.status == CONFIRMED and s.break_candle["close"] == pytest.approx(at(side, 24690)) and s.confirm_candle["close"] == pytest.approx(at(side, 24688))
    book, _ = play(ter_ctx, range(47, 53), side, rows={52: (24690, 24690, 24670, 24672)}, **kw)  # the next candle closes back under the trigger
    assert live_ter(book, side).status == WAITING and not live_ter(book, side).candle_confirmed


@pytest.mark.parametrize("side", SIDES)
def test_the_unconfirmed_setup_expires_when_the_5m_never_breaks(side):
    quiet = {i: (24672, 24676, 24668, 24671) for i in range(51, 91)}  # (no break candle at slot 51 this time: nothing ever closes through the trigger)
    book, _ = play(ter_ctx, range(47, 58), side, rows=quiet, p=replace(P, ter_max_wait_bars=6))  # a limit of 6 candles: slots 48-54 are 7 candles after the 15M change was confirmed
    assert live_ter(book, side) is None and "no 5M break confirmed within 6 candles of the 15M structural change" in book.recent[0].note
    book, _ = play(ter_ctx, range(47, 70), side, rows=quiet)  # with the real limit of 24 candles (two hours) the day's 14:45 cut-off comes first, by the existing late-session rule
    assert live_ter(book, side) is None and "late session" in book.recent[0].note
    fresh = scan_one(TER, ter_ctx(90, rows=quiet), side)  # a NEW look this late no longer finds the change recent
    assert fresh.setup is None and "confirmed within the last 12 15M candles" in fresh.note and not fresh.info["granted"]


# ====================================================================================================================
# The permission: what grants it, and what never does
# ====================================================================================================================
def denied(side, **kw):
    sc = scan_one(TER, ter_ctx(kw.pop("through", 51), **kw), side)
    assert sc.setup is None and sc.info is not None and sc.info["granted"] is False
    return sc


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("rsi", [8.0, 25.0, 92.0], ids=["very_oversold", "oversold", "overbought"])
def test_an_extreme_rsi_without_a_structural_reversal_never_grants_permission(side, rsi):
    sc = denied(side, swings=NO_TURN, rsi={"1h": 20.0, "15m": rsi, "5m": 50.0, "1m": 50.0}, ext="bearish")  # RSI extreme, trend extended and weakening, but no higher low and no higher high
    assert "structure turned" in sc.note and "counter-trend permission denied" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_a_structural_reversal_does_not_depend_on_rsi_either_way(side):
    for rsi in (5.0, 40.0, 95.0):  # with the structure turned and the trend weakening, the RSI neither grants nor takes away anything
        s = scan_one(TER, ter_ctx(51, rsi={"1h": 20.0, "15m": rsi, "5m": 50.0, "1m": 50.0}), side).setup
        assert s is not None and s.counter_trend["granted"] and s.status == CONFIRMED


@pytest.mark.parametrize("side", SIDES)
def test_a_falling_adx_and_weakening_trend_without_a_structural_reversal_is_rejected(side):
    sc = denied(side, swings=NO_TURN, weak="bearish")
    assert "structure turned" in sc.note
    book, _ = play(ter_ctx, range(47, 52), side, swings=NO_TURN)
    assert live_ter(book, side) is None and not any(e["strategy"] == TER for e in book.events)  # nothing is ever announced


@pytest.mark.parametrize("side", SIDES)
def test_a_5m_break_without_a_valid_15m_structure_is_rejected(side):
    book, steps = play(ter_ctx, range(47, 52), side, swings=NO_TURN)  # the break candle at slot 51 closes through the 5M swing high, but the 15M never turned
    assert live_ter(book, side) is None and "counter-trend permission denied" in steps[-1][1]["scans"][(TER, side)].note
    assert decision_after(ter_ctx, range(47, 52), side, swings=NO_TURN)["decision"]["action"] != ("BUY CE" if side == "bull" else "BUY PE")


@pytest.mark.parametrize("side", SIDES)
def test_a_structural_reversal_without_weakening_evidence_is_rejected_too(side):
    assert "weakening continuation" in denied(side, weak="none").note


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("change,named", [
    ({"swings": [("H", t15(2), 24760.0), ("L", t15(4), 24600.0), ("H", t15(6), 24780.0)] + SWINGS[3:]}, "down structure before"),  # the "lower high" is higher than the high before it
    ({"atr": {"1h": 60.0, "15m": 130.0, "5m": 10.0, "1m": 4.0}}, "the exhausted leg was at least 2 ATR15"),  # the leg of 240 pts is under 2x a 130 point ATR15
    ({"c15_rows": C15[:14] + [(24700, 24705, 24510, 24670)] + C15[15:]}, "the exhaustion extreme still holds"),  # a 15M candle traded under the exhaustion low 24,520 since
    ({"swings": SWINGS[:5]}, "15M swing structure"),  # only two swing highs are confirmed: the change cannot be judged
    ({"swings": SWINGS[:5] + [("H", t15(13), 24640.0)]}, "structure turned"),  # a higher low but no higher high (24,640 is under the lower high 24,650)
    ({"swings": SWINGS[:4] + [("L", t15(11), 24500.0), ("H", t15(13), 24715.0)]}, "structure turned"),  # a higher high but a LOWER low (24,500 is under the exhaustion low)
])  # fmt: skip
def test_each_part_of_the_structural_requirement_can_deny_the_permission_on_its_own(side, change, named):
    named = named.replace("down structure before", "down structure before" if side == "bull" else "up structure before")  # (a put follows an UP structure that turned down)
    sc = denied(side, **change)
    assert named in sc.note and named in [x["name"] for x in sc.info["checks"] if x["ok"] is not True]


@pytest.mark.parametrize("side", SIDES)
def test_without_an_established_trend_there_is_nothing_to_reverse_and_the_strategy_is_inactive(side):
    sc = scan_one(TER, ter_ctx(51, trend={"1h": "sideways", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, ext="none", ema_slope={"15m": {"ema20": 1.0, "ema50": 0.0, "stack_bars": -2}}), side)
    assert not sc.applicable and sc.setup is None and "no established" in sc.note and sc.info is None  # (no permission question exists: no decision to record)
    sc = scan_one(TER, ter_ctx(51, regime={"available": False, "why": "the market data is not reliable right now (5M candles missing)", "facts": {}}), side)
    assert not sc.applicable and "regime facts are not available" in sc.note and "5M candles missing" in sc.note  # missing data is never read as a market condition
    sc = scan_one(TER, ter_ctx(51, trend={"1h": "unknown", "15m": "unknown", "5m": "unknown", "1m": "unknown"}), side)
    assert not sc.applicable and "not enough completed" in sc.note


@pytest.mark.parametrize("side", SIDES)
def test_an_extended_market_or_a_stacked_leg_also_establishes_the_trend_when_the_1h_does_not(side):
    sideways = {"1h": "sideways", "15m": "bullish", "5m": "sideways", "1m": "sideways"}  # the 1H is neutral, the 15M already up: a call is not opposed
    s = scan_one(TER, ter_ctx(51, trend=sideways, alignment="developing", ext="bearish", ema_slope={"15m": {"ema20": 2.0, "ema50": -1.0, "stack_bars": -2}}), side).setup
    assert s is not None and s.counter_trend["granted"] and not s.counter_trend["needed"]  # established by the extension; no permission NEEDED: nothing runs against this side
    s = scan_one(TER, ter_ctx(51, trend=sideways, alignment="developing", ext="none"), side).setup  # the stacked EMAs (-25 candles) after a 9.6x ATR15 leg do it too
    assert s is not None and "15M EMAs stacked" in " ".join(s.reasons)
    sc = scan_one(TER, ter_ctx(51, trend=sideways, alignment="developing", ext="none", ema_slope={"15m": {"ema20": 2.0, "ema50": -1.0, "stack_bars": -3}}), side)
    assert not sc.applicable  # neither: nothing to reverse
