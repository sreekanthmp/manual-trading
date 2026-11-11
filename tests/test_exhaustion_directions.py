"""Stage 3 verification: Trend Exhaustion Reversal, each direction on its own, in REAL prices.

test_exhaustion.py runs every scenario for both sides, the put through a reflection of the call's candles. That proves the two sides agree with each other; it cannot show that the put
is right on its own. So here both directions are written out by hand, as a trader would see them, and every expectation is worked out from the rules, not read off the code:

  CALL  (BUY CE) after a BEARISH exhaustion: the 1H is bearish, its weakening is named, the 15M structure turned from lower highs and lower lows to a higher low and a higher high, and a
        completed 5M candle closes ABOVE the latest confirmed 5M swing HIGH. Entry zone above the trigger, invalidation below it. The fixture is test_exhaustion.py's (NIFTY ~24,600).
  PUT   (BUY PE) after a BULLISH exhaustion: the 1H is bullish, its weakening is named, the 15M structure turned from higher highs and higher lows to a lower high and a lower low, and a
        completed 5M candle closes BELOW the latest confirmed 5M swing LOW. Entry zone below the trigger, invalidation above it. Its own fixture below (NIFTY ~24,100).

The PUT's market (real prices; ATR5 = 10, ATR15 = 25; the chase limit 10, the invalidation buffer 2.5, the risk limits 7.5 and 37.5):
    15M swings, in time order:   L_a 24,040 (j=2)  H_a 24,190 (j=4)  L_b 24,140 (j=6)  H_b 24,300 (j=9)  H_c 24,250 (j=11)  L_c 24,080 (j=13)
        higher lows and higher highs (L_b over L_a, H_b over H_a), then a LOWER HIGH H_c under H_b and a LOWER LOW L_c under L_b. The exhaustion extreme is H_b = 24,300: the leg up from
        L_a was 260 pts = 10.4x ATR15. The change is confirmed when the later pivot (L_c, j=13) has two candles after it: the end of j=15, i.e. 5M slot 48.
    5M candles from the lower high's 15M candle (slot 33):
        slots 33-40  a decline from 24,250 to the 5M swing low 24,080 (slot 40, the 15M lower low)
        slots 41-45  a bounce to 24,136 and back
        slot 46      the 5M swing low 24,104: the TRIGGER (confirmed once slots 47 and 48 are in)
        slots 47-50  a drift up to the 5M pullback high 24,118
        slot 51      24,110 -> 24,098, high 24,112, low 24,092: body 12, closes at 70% of its range from the top, 6 BELOW the trigger: the BREAK.
    invalidation = the pullback high 24,118 plus 2.5 = 24,120.5 (risk from the trigger: 16.5); entry zone 24,094-24,104; the next level down is the 15M lower low 24,080: 24 away (1.45x risk)."""

import pytest
from scenarios import P, bull, c5, c15, t15
from test_counter_trend_decision import MONS, decide, market
from test_exhaustion import SWINGS, TER, facts, ter_ctx
from test_regime import facts_of, fading, rising, tfd
from test_trend_family import decision_after, live, play

from app import strategy as st
from app import trading as tr
from app.strategy import CONFIRMED, EXPIRED, INVALIDATED, WAITING, Level

CE, PE = "bull", "bear"  # the strategy's slot for a call / a put
PE_C15 = [
    (24090, 24110, 24085, 24105), (24105, 24125, 24100, 24120), (24120, 24125, 24040, 24050), (24050, 24150, 24048, 24145), (24145, 24190, 24140, 24185),
    (24185, 24188, 24150, 24160), (24160, 24165, 24140, 24160), (24160, 24215, 24158, 24210), (24210, 24265, 24205, 24260), (24260, 24300, 24255, 24290),
    (24290, 24295, 24240, 24245), (24235, 24250, 24206, 24210), (24210, 24214, 24112, 24118), (24118, 24122, 24080, 24100), (24100, 24136, 24098, 24124),
    (24124, 24128, 24104, 24114), (24114, 24118, 24106, 24116),
]  # fmt: skip
PE_SWINGS = [("L", t15(2), 24040.0), ("H", t15(4), 24190.0), ("L", t15(6), 24140.0), ("H", t15(9), 24300.0), ("H", t15(11), 24250.0), ("L", t15(13), 24080.0)]
PE_FIVE = {
    33: (24235, 24250, 24228, 24232), 34: (24232, 24236, 24218, 24222), 35: (24222, 24226, 24206, 24210), 36: (24210, 24214, 24176, 24182), 37: (24182, 24186, 24146, 24152),
    38: (24152, 24156, 24112, 24118), 39: (24118, 24122, 24086, 24090), 40: (24090, 24094, 24080, 24086), 41: (24086, 24104, 24084, 24100), 42: (24100, 24122, 24098, 24118),
    43: (24118, 24134, 24114, 24130), 44: (24130, 24136, 24120, 24124), 45: (24124, 24128, 24110, 24114), 46: (24114, 24118, 24104, 24108), 47: (24108, 24116, 24106, 24114),
    48: (24114, 24118, 24106, 24112), 49: (24112, 24116, 24108, 24114), 50: (24114, 24118, 24110, 24116), 51: (24110, 24112, 24092, 24098),
}  # fmt: skip


def pe_ctx(through, rows=None, swings=None, c15_rows=None, weak="bullish", ext="none", trend=None, alignment="pausing", **over):
    """The put's market up to the close of 5M slot `through`, in real prices. 1H bullish and 15M sideways (so a put is against the 1H: the permission is needed); the regime facts name a
    BULLISH trend weakening (the regime layer speaks in real directions)."""
    table = {**PE_FIVE, **(rows or {})}
    bars = [c5(i, *table[i]) for i in range(33, through + 1)]
    cs15 = [x for x in (c15(j, *r) for j, r in enumerate(c15_rows or PE_C15)) if x.end <= bars[-1].end]
    d = dict(
        c5=bars, c15=cs15, now=bars[-1].end + 3, price=bars[-1].close, regime=facts(weak, ext),
        trend=trend or {"1h": "bullish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment=alignment,
        pattern={"1h": "HH_HL", "15m": "LH_LL", "5m": "mixed", "1m": "mixed"}, adx={"1h": 30.0, "15m": 18.0, "5m": 14.0, "1m": 10.0},
        swings={"15m": PE_SWINGS if swings is None else swings}, rsi={"1h": 70.0, "15m": 60.0, "5m": 50.0, "1m": 50.0},
        ema={"1h": (24200.0, 24150.0), "15m": (24160.0, 24140.0), "5m": (24120.0, 24130.0), "1m": (24100.0, 24104.0)},
        ema_slope={"15m": {"ema20": -2.0, "ema50": 1.0, "stack_bars": 25}, "5m": {"ema20": -1.0, "ema50": 0.0, "stack_bars": 5}},
        levels=[Level(24000.0, "swing level x2", 2)],
    )  # fmt: skip
    d.update(over)
    return bull(**d)


def real(build, throughs, **kw):
    """The scenario through the real SetupBook. The contexts are already in real prices: nothing is mirrored (the slot of a put is "bear" because it IS a put)."""
    return play(build, throughs, "bull", **kw)


def decided(build, throughs, **kw):
    return decision_after(build, throughs, "bull", **kw)


def the_setup(build, through, slot, **kw):
    book, _ = real(build, range(47, through + 1), **kw)
    return live(book, TER, slot)


def scan_real(ctx, slot):
    """The reversal's scan of one side on a context that is already in real prices."""
    return st.scan_all(ctx, P, {})[(TER, slot)]


# the two directions, side by side
CASES = {
    "CE": dict(slot=CE, build=ter_ctx, option="CE", exhausted="bearish", trigger=24682.0, zone=(24682.0, 24692.0), inv=24659.5, extreme=24520.0),
    "PE": dict(slot=PE, build=pe_ctx, option="PE", exhausted="bullish", trigger=24104.0, zone=(24094.0, 24104.0), inv=24120.5, extreme=24300.0),
}


@pytest.fixture(params=["CE", "PE"])
def case(request):
    return CASES[request.param]


# ====================================================================================================================
# 1. Each direction has the structural conditions it should, in real prices
# ====================================================================================================================
def test_each_direction_confirms_on_its_own_literal_candles_with_the_trigger_the_zone_and_the_invalidation_on_the_right_side(case):
    book, steps = real(case["build"], range(47, 52))
    s = live(book, TER, case["slot"])
    assert (s.status, s.direction, s.side) == (CONFIRMED, case["option"], case["slot"])
    assert s.trigger == pytest.approx(case["trigger"]) and s.zone == pytest.approx(case["zone"]) and s.invalidation == pytest.approx(case["inv"])
    lo, hi = s.zone
    if case["option"] == "CE":  # a call: the entry area is ABOVE the trigger, the invalidation BELOW everything
        assert lo == s.trigger < hi and s.invalidation < s.trigger
    else:  # a put: the entry area is BELOW the trigger, the invalidation ABOVE everything
        assert hi == s.trigger > lo and s.invalidation > s.trigger
    assert [r["live_now"][(TER, case["slot"])][0] for _, r in steps] == [WAITING, WAITING, WAITING, WAITING, CONFIRMED]  # the same lifecycle either way
    assert [e["status"] for e in reversed(book.events) if e["strategy"] == TER] == [WAITING, CONFIRMED]


def test_the_exact_structural_conditions_of_each_direction_in_real_prices(case):
    s = the_setup(case["build"], 51, case["slot"])
    perm = s.counter_trend
    st_ = perm["structure"]
    assert perm["granted"] and perm["needed"] and perm["exhausted_trend"] == case["exhausted"]
    assert st_["exhaustion_extreme"] == pytest.approx(case["extreme"])
    keys = ("prior_high", "lower_high", "exhaustion_extreme", "higher_low", "higher_high")  # (the record keeps the call's key names; the dashboard and the journal read them by side)
    if case["option"] == "CE":  # bearish exhaustion: lower highs and lower lows, then a higher low and a higher high
        assert st_["lower_high"] < st_["prior_high"] and st_["exhaustion_extreme"] < st_["higher_low"] < st_["higher_high"] and st_["lower_high"] < st_["higher_high"]
        assert tuple(st_[k] for k in keys) == (24760.0, 24650.0, 24520.0, 24580.0, 24715.0)
    else:  # bullish exhaustion: higher highs and higher lows, then a LOWER HIGH and a LOWER LOW
        assert st_["lower_high"] > st_["prior_high"] and st_["exhaustion_extreme"] > st_["higher_low"] > st_["higher_high"] and st_["lower_high"] > st_["higher_high"]
        assert tuple(st_[k] for k in keys) == (24040.0, 24140.0, 24300.0, 24250.0, 24080.0)
    names = {c["name"]: c for c in perm["checks"]}
    first = "down structure before" if case["option"] == "CE" else "up structure before"
    assert all(names[n]["ok"] is True for n in (first, "structure turned", "weakening continuation", "an established trend", "the exhaustion extreme still holds"))
    assert f"the 1H trend is {case['exhausted']}" in names["an established trend"]["text"]


def test_the_evidence_reads_in_real_words_for_each_direction():
    ce, pe = (the_setup(CASES[k]["build"], 51, CASES[k]["slot"]).counter_trend for k in ("CE", "PE"))

    def text(perm, name):
        return next(c["text"] for c in perm["checks"] if c["name"] == name)

    assert text(ce, "down structure before") == "15M lower high 24,650.00 under 24,760.00 and lower low 24,520.00 under 24,600.00"
    assert text(ce, "structure turned") == "then a higher low 24,580.00 over 24,520.00 and a higher high 24,715.00 over 24,650.00"
    assert text(pe, "up structure before") == "15M higher low 24,140.00 over 24,040.00 and higher high 24,300.00 over 24,190.00"
    assert text(pe, "structure turned") == "then a lower high 24,250.00 under 24,300.00 and a lower low 24,080.00 under 24,140.00"
    assert text(pe, "the exhaustion extreme still holds") == "no 15M candle has traded above 24,300.00 since it formed"
    assert text(ce, "the exhaustion extreme still holds") == "no 15M candle has traded below 24,520.00 since it formed"
    assert ce["reason"] == "granted: the bearish trend is weakening and the 15M structure has turned" and pe["reason"] == "granted: the bullish trend is weakening and the 15M structure has turned"


def test_the_setup_speaks_in_the_right_direction_for_each_side():
    ce, pe = (the_setup(CASES[k]["build"], 51, CASES[k]["slot"]) for k in ("CE", "PE"))
    assert ce.level_label == "exhaustion low" and pe.level_label == "exhaustion high" and ce.level == 24520.0 and pe.level == 24300.0
    assert "closes above 24,682.00" in ce.confirm_text and "a completed 5M bullish candle" in ce.confirm_text and "pullback low before the break, 24,659.50" in ce.confirm_text
    assert "closes below 24,104.00" in pe.confirm_text and "a completed 5M bearish candle" in pe.confirm_text and "pullback high before the break, 24,120.50" in pe.confirm_text
    assert "close below the 15M higher low 24,580.00" in ce.confirm_text and "close above the 15M lower high 24,250.00" in pe.confirm_text
    reasons = " ".join(pe.reasons)
    assert "the bullish leg ended at the exhaustion high 24,300.00 (10.4x ATR15 up from 24,040.00)" in reasons
    assert "5M break: the 13:30 candle closed 6.0 pts (0.6x ATR5) below the 5M swing low 24,104.00 (formed 13:05), body 1.2x ATR5, closed 70% of its range" in reasons
    assert "the bearish leg ended at the exhaustion low 24,520.00 (9.6x ATR15 down from 24,760.00)" in " ".join(ce.reasons)
    assert "5M break: the 13:30 candle closed 8.0 pts (0.8x ATR5) above the 5M swing high 24,682.00" in " ".join(ce.reasons)
    assert "the 1H trend is still bullish" in " ".join(pe.against) and "the 1H trend is still bearish" in " ".join(ce.against)
    assert [(t["label"], t["price"]) for t in pe.plan["targets"]] == [("15M lower low", 24080.0), ("15M prior low", 24040.0)]  # a put's targets are the swing LOWS below it
    assert [(t["label"], t["price"]) for t in ce.plan["targets"]] == [("15M higher high", 24715.0), ("15M prior high", 24760.0)]
    assert pe.plan["risk_points"] == pytest.approx(16.5) and pe.plan["targets"][0]["reward_risk"] == pytest.approx(24 / 16.5) and ce.plan["risk_points"] == pytest.approx(22.5)
    assert (pe.confirm_candle["close"], pe.confirm_candle["high"], pe.confirm_candle["low"]) == (24098.0, 24112.0, 24092.0)  # a candle in real prices, whichever side


def test_the_trigger_is_a_5m_swing_high_for_a_call_and_a_5m_swing_low_for_a_put():
    ce_bars, pe_bars = ter_ctx(51).c5, pe_ctx(51).c5

    def swing_highs(bars):
        return [b.high for i, b in enumerate(bars) if 2 <= i < len(bars) - 2 and all(b.high > x.high for x in bars[i - 2 : i]) and all(b.high >= x.high for x in bars[i + 1 : i + 3])]

    def swing_lows(bars):
        return [b.low for i, b in enumerate(bars) if 2 <= i < len(bars) - 2 and all(b.low < x.low for x in bars[i - 2 : i]) and all(b.low <= x.low for x in bars[i + 1 : i + 3])]

    assert the_setup(ter_ctx, 51, CE).trigger == swing_highs(ce_bars)[-1] == 24682.0  # the latest confirmed swing HIGH, from the highs
    assert the_setup(pe_ctx, 51, PE).trigger == swing_lows(pe_bars)[-1] == 24104.0  # the latest confirmed swing LOW, from the lows
    assert swing_lows(pe_bars) == [24080.0, 24104.0]  # (no swing high is anywhere in a put's trigger)
    # before the later swing is confirmed the trigger is the earlier one: the 15M higher high's 5M swing for a call, the 15M lower low's for a put
    first_ce = real(ter_ctx, [47])[1][0][1]["live_now"][(TER, CE)][1]
    first_pe = real(pe_ctx, [47])[1][0][1]["live_now"][(TER, PE)][1]
    assert (first_ce, first_pe) == (24715.0, 24080.0)


def test_the_final_decision_follows_the_structural_reversal(case):
    out = decided(case["build"], range(47, 52))
    d = out["decision"]
    s = next(x for x in out["setups"] if x["strategy"] == TER)
    assert d["action"] == f"BUY {case['option']}" and d["strategy"] == TER and d["setup_id"] == s["id"]
    assert s["id"].startswith(f"{TER}:{case['slot']}:") and s["direction"] == case["option"]
    ct = out["counter_trend"][0]
    assert ct["granted"] and ct["needed"] and ct["side"] == case["option"] and ct["exhausted_trend"] == case["exhausted"]
    assert {"bearish": "CE", "bullish": "PE"}[ct["exhausted_trend"]] == d["action"].split()[1]  # a bearish exhaustion is a call, a bullish one a put: never the other way round
    assert out["explain"]["actionable"] and s["price_state"] == "in_entry_area" and s["stage"] == "READY_TO_ACT"
    other = "PE" if case["option"] == "CE" else "CE"
    assert not any(x["direction"] == other for x in out["setups"] if x["strategy"] == TER)  # and the other side has nothing


# ====================================================================================================================
# 2. How each direction ends (entry, invalidation, expiry on the correct side)
# ====================================================================================================================
ENDS = {
    "CE": {"invalidated": ((24690, 24690, 24655, 24658), "the invalidation level"), "follow": ((24690, 24690, 24668, 24678), "failed follow-through"),
           "chase": ((24690, 24698, 24689, 24696), "the entry opportunity has passed"), "structure": ((24670, 24671, 24565, 24570), "closed below the 15M higher low 24,580.00"),
           "exhaustion": ((24670, 24671, 24500, 24510), "closed below the exhaustion low 24,520.00")},
    "PE": {"invalidated": ((24100, 24122, 24099, 24121), "the invalidation level 24,120.50"), "follow": ((24100, 24109, 24099, 24108), "failed follow-through"),
           "chase": ((24096, 24097, 24085, 24086), "the entry opportunity has passed"), "structure": ((24200, 24260, 24198, 24256), "closed above the 15M lower high 24,250.00"),
           "exhaustion": ((24200, 24320, 24198, 24310), "closed above the exhaustion high 24,300.00")},
}  # fmt: skip
HOLD = {"CE": (24690, 24692, 24684, 24686), "PE": (24100, 24102, 24095, 24097)}  # a candle that stays inside the entry zone


@pytest.mark.parametrize("kind,status", [("invalidated", INVALIDATED), ("follow", INVALIDATED), ("chase", EXPIRED)])
def test_the_end_of_a_setup_is_on_the_correct_side_for_each_direction(case, kind, status):
    row, words = ENDS[case["option"]][kind]
    book, _ = real(case["build"], range(47, 53), rows={52: row})
    assert live(book, TER, case["slot"]) is None
    ended = book.recent[0]
    assert ended.status == status and words in ended.note and ended.key in book.terminal
    assert not tr.setup_dict(ended, row[3], True, True)["actionable"]


@pytest.mark.parametrize("kind", ["structure", "exhaustion"])
def test_losing_the_structure_ends_it_on_the_correct_side_for_each_direction(case, kind):
    row, words = ENDS[case["option"]][kind]
    book, _ = real(case["build"], range(47, 50), rows={49: row})  # while still waiting for the break
    assert live(book, TER, case["slot"]) is None and book.recent[0].status == INVALIDATED and words in book.recent[0].note


def test_the_entry_window_passes_the_same_way_for_each_direction(case):
    book, _ = real(case["build"], range(47, 56), rows={i: HOLD[case["option"]] for i in range(52, 56)})
    assert live(book, TER, case["slot"]) is None and book.recent[0].status == EXPIRED and "entry window after the confirmation has passed" in book.recent[0].note


def test_a_put_is_actionable_only_inside_its_own_zone_below_the_trigger():
    book, steps = real(pe_ctx, range(47, 52))
    s, price = live(book, TER, PE), steps[-1][0].price
    assert tr.price_state(s, price) == "in_entry_area" and tr.price_state(s, 24106.0) == "above_trigger"  # back over the trigger: not in the area
    assert tr.price_state(s, 24090.0) == "extended" and tr.price_state(s, 24121.0) == "through_invalidation"  # past the zone: do not chase; over the invalidation: wrong
    ce = live(real(ter_ctx, range(47, 52))[0], TER, CE)
    assert tr.price_state(ce, 24685.0) == "in_entry_area" and tr.price_state(ce, 24680.0) == "below_trigger" and tr.price_state(ce, 24695.0) == "extended"
    assert tr.price_state(ce, 24659.0) == "through_invalidation"


# ====================================================================================================================
# 3. Invalid mirrored conditions: the other direction's evidence never makes this direction's trade
# ====================================================================================================================
def never_trades(build, slot, **kw):
    """No reversal setup is ever announced on this market, and the decision is not the strategy's."""
    book, _ = real(build, range(47, 52), **kw)
    assert live(book, TER, slot) is None and not [e for e in book.events if e["strategy"] == TER]
    assert decided(build, range(47, 52), **kw)["decision"]["strategy"] != TER


def test_a_put_is_never_made_from_the_calls_structure_and_a_call_never_from_the_puts():
    sc = scan_real(pe_ctx(51, swings=SWINGS), PE)  # a bullish 1H, and the swings of a BEARISH exhaustion that turned up: the put's up-structure-before is false
    assert sc.setup is None and sc.info["granted"] is False and "up structure before" in sc.note
    sc = scan_real(ter_ctx(51, swings=PE_SWINGS), CE)  # a bearish 1H, and the swings of a BULLISH exhaustion that turned down
    assert sc.setup is None and sc.info["granted"] is False and "down structure before" in sc.note
    never_trades(pe_ctx, PE, swings=SWINGS)
    never_trades(ter_ctx, CE, swings=PE_SWINGS)


def test_a_structure_that_cannot_be_judged_yet_counts_the_real_swing_highs_and_lows():
    ce = scan_real(ter_ctx(51, swings=SWINGS[:5]), CE)  # H L H L L: two swing highs, three swing lows
    pe = scan_real(pe_ctx(51, swings=PE_SWINGS[:5]), PE)  # L H L H H: three swing highs, two swing lows (a put's view swaps them; the words must not)
    assert "only 2 swing highs and 3 swing lows are confirmed" in ce.note
    assert "only 3 swing highs and 2 swing lows are confirmed" in pe.note
    assert not ce.info["granted"] and not pe.info["granted"] and ce.setup is None and pe.setup is None


def test_a_structure_that_turned_only_halfway_is_refused_in_each_direction():
    ce_half = SWINGS[:5] + [("H", t15(13), 24640.0)]  # a higher low, but the high (24,640) is under the lower high 24,650
    pe_half = PE_SWINGS[:5] + [("L", t15(13), 24150.0)]  # a lower high, but the low (24,150) is over L_b 24,140
    assert "structure turned" in scan_real(ter_ctx(51, swings=ce_half), CE).note
    assert "structure turned" in scan_real(pe_ctx(51, swings=pe_half), PE).note
    never_trades(ter_ctx, CE, swings=ce_half)
    never_trades(pe_ctx, PE, swings=pe_half)


def test_the_wrong_directions_weakening_never_grants_the_permission():
    sc = scan_real(pe_ctx(51, weak="bearish"), PE)  # a bullish trend is the one that must be weakening for a put; a weakening BEARISH trend is no reason
    assert sc.setup is None and not sc.info["granted"] and "weakening continuation" in sc.note
    sc = scan_real(ter_ctx(51, weak="bullish"), CE)
    assert sc.setup is None and not sc.info["granted"] and "weakening continuation" in sc.note
    never_trades(pe_ctx, PE, weak="bearish")
    never_trades(ter_ctx, CE, weak="bullish")


def test_the_wrong_direction_of_trend_establishes_nothing():
    down = {"1h": "bearish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}
    up = {"1h": "bullish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}
    sc = scan_real(pe_ctx(51, trend=down, ema_slope={"15m": {"ema20": -2.0, "ema50": 1.0, "stack_bars": -25}}), PE)  # the put's structure under a BEARISH 1H: nothing bullish was exhausted
    assert not sc.applicable and "no established bullish trend" in sc.note
    sc = scan_real(ter_ctx(51, trend=up, ema_slope={"15m": {"ema20": 2.0, "ema50": -1.0, "stack_bars": 25}}), CE)
    assert not sc.applicable and "no established bearish trend" in sc.note
    sideways = {"1h": "sideways", "15m": "sideways", "5m": "sideways", "1m": "sideways"}  # ... and an extension has to be on the exhausted side too
    flat = {"15m": {"ema20": 0.0, "ema50": 0.0, "stack_bars": 1}}
    assert not scan_real(pe_ctx(51, trend=sideways, ext="bearish", ema_slope=flat), PE).applicable  # extended DOWN is not a bullish exhaustion
    assert not scan_real(ter_ctx(51, trend=sideways, ext="bullish", ema_slope=flat), CE).applicable  # extended UP is not a bearish one
    assert scan_real(pe_ctx(51, trend=sideways, ext="bullish", ema_slope=flat), PE).applicable
    assert scan_real(ter_ctx(51, trend=sideways, ext="bearish", ema_slope=flat), CE).applicable


def test_a_break_the_wrong_way_never_confirms_either_direction():
    up_break = {51: (24104, 24132, 24102, 24130)}  # the put's market: a strong BULLISH candle closing over the pullback high 24,118: a bullish trigger, not a bearish one
    down_break = {51: (24676, 24677, 24648, 24650)}  # the call's market: a strong BEARISH candle under everything
    pe = the_setup(pe_ctx, 51, PE, rows=up_break)
    assert pe.status == WAITING and not pe.candle_confirmed and pe.trigger == pytest.approx(24104.0)  # still waiting on a close BELOW the swing low
    ce = the_setup(ter_ctx, 51, CE, rows=down_break)
    assert ce.status == WAITING and not ce.candle_confirmed and ce.trigger == pytest.approx(24682.0)  # still waiting on a close ABOVE the swing high
    for build, rows in ((pe_ctx, up_break), (ter_ctx, down_break)):
        out = decided(build, range(47, 52), rows=rows)
        assert not out["decision"]["action"].startswith("BUY") and not any(s["actionable"] for s in out["setups"] if s["strategy"] == TER)


def test_a_candle_that_stops_short_of_the_swing_is_not_a_trigger_in_either_direction():
    pe = the_setup(pe_ctx, 51, PE, rows={51: (24116, 24117, 24106, 24107)})  # body 9, closes at 91% of the way down, but still ABOVE the swing low 24,104
    assert pe.status == WAITING and not pe.candle_confirmed
    ce = the_setup(ter_ctx, 51, CE, rows={51: (24668, 24681, 24666, 24680)})  # body 12, closes at 93% of the way up, but still UNDER the swing high 24,682
    assert ce.status == WAITING and not ce.candle_confirmed


def test_inverting_the_permission_direction_grants_nothing_in_the_decision():
    pe = tr.setup_dict(the_setup(pe_ctx, 51, PE), 24098.0, True, True)
    ce = tr.setup_dict(the_setup(ter_ctx, 51, CE), 24686.0, True, True)
    assert tr.permitted(pe) and pe["counter_trend"]["exhausted_trend"] == "bullish" and tr.permitted(ce) and ce["counter_trend"]["exhausted_trend"] == "bearish"
    assert not tr.permitted({**pe, "counter_trend": {**pe["counter_trend"], "exhausted_trend": "bearish"}})  # a record made for a call cannot permit a put
    assert not tr.permitted({**ce, "counter_trend": {**ce["counter_trend"], "exhausted_trend": "bullish"}})  # ... nor a put's record a call
    assert not tr.permitted({**pe, "direction": "CE"}) and not tr.permitted({**ce, "direction": "PE"})  # the direction of the setup itself cannot be flipped under its record
    assert tr.permitted({**pe, "counter_trend": {"granted": True}})  # (a hand-built record without the field behaves as it always did)


def test_a_put_and_a_call_reversal_that_are_both_live_disagree_and_the_existing_rule_says_wait():
    ce, pe = tr.setup_dict(the_setup(ter_ctx, 51, CE), 24686.0, True, True), tr.setup_dict(the_setup(pe_ctx, 51, PE), 24098.0, True, True)
    d = decide([ce, pe], market("conflicting", "bearish", "bullish"))  # a market where each is against one of the higher timeframes
    assert d["action"] == "WAIT" and "disagree" in d["headline"]
    assert len(MONS) > 1


# ====================================================================================================================
# 4. The regime facts the permission reads, each direction on literal candles
# ====================================================================================================================
def falling(n, start, step=8.0, body=7.0):
    """A steady slide: each close `step` under the last, bodies of `body`, a little wick each side."""
    return [(start - step * j + body, start - step * j + body + 1.0, start - step * j - 2.0, start - step * j) for j in range(n)]


def stalling_down():
    """The bearish twin of test_regime.fading(), written out: a slide, six more at the same pace, then six that stall: tiny bodies, lows not under the earlier six's, and the latest
    low made by a candle that was bought back (a long lower wick, closing in its upper half)."""
    spec = falling(28, 25000.0)
    last = spec[-1][3]
    spec += [(last - 8 * k, last - 8 * k + 1, last - 8 * k - 9, last - 8 * k - 7) for k in range(6)]
    bottom = spec[-1][2]
    spec += [(bottom + 6, bottom + 8, bottom + 3, bottom + 5), (bottom + 5, bottom + 7, bottom + 2, bottom + 4), (bottom + 4, bottom + 6, bottom + 1, bottom + 3),
             (bottom + 16, bottom + 18, bottom, bottom + 15), (bottom + 2, bottom + 5, bottom + 1, bottom + 3), (bottom + 3, bottom + 6, bottom + 2, bottom + 4)]  # fmt: skip
    return spec


def bearish(**kw):
    return tfd("bearish", slope=-5.0, stack=-20, ema20=24700.0, ema50=24800.0, **kw)


def test_a_weakening_bullish_trend_and_a_weakening_bearish_trend_are_each_named_for_their_own_direction():
    up = facts_of(tfd(), tfd(adx=34.0, adx_slope=-4.0, pattern="mixed"), fading())["facts"]["weakening_trend"]
    down = facts_of(bearish(), bearish(adx=34.0, adx_slope=-4.0, pattern="mixed"), stalling_down(), "aligned_bearish")["facts"]["weakening_trend"]
    assert up["state"] == "bullish" and down["state"] == "bearish"
    for f, want in ((up, "HH_HL"), (down, "LH_LL")):
        held = {c["name"] for c in f["checks"] if c["ok"] is True}
        assert {"ADX falling", "directional candles fading", "no new extreme", "latest push rejected", f"15M structure no longer {want}"} <= held
    assert next(c["text"] for c in down["checks"] if c["name"] == "no new extreme").startswith("the last 6 candles' low ")  # a bearish trend's extreme is a LOW
    assert next(c["text"] for c in up["checks"] if c["name"] == "no new extreme").startswith("the last 6 candles' high ")  # a bullish trend's is a HIGH


def test_a_healthy_trend_is_not_weakening_whichever_way_it_runs():
    up = facts_of(tfd(), tfd(adx_slope=2.0), rising())["facts"]["weakening_trend"]
    down = facts_of(bearish(), bearish(adx_slope=2.0), falling(40, 25000.0), "aligned_bearish")["facts"]["weakening_trend"]
    assert up["state"] == "none" and down["state"] == "none"
