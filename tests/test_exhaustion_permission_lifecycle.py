"""Stage 3 verification: the life of the counter-trend permission, from the moment it is granted to the moment it is stored, restored, used and ended.

The rule these tests hold: the permission is a RECORD of a decision made once, when the reversal was detected (who may have it, which direction, the structure it rests on). It lets one
strategy's setup past the higher-timeframe direction gate and nothing else. Every other safety requirement is decided afresh, from the data of NOW, on every look:

  * who:         only the registered counter-trend strategy, only for the direction it was made for; nothing else carries it, restores it or shows it
  * data health: a stored permission never makes a setup actionable while the data, the session, the feed or the price are not trusted; a setup that the data lost cannot be revived
  * structure:   the structure the permission rests on is checked against the candles of now: losing it ends the setup, whatever any indicator says
  * time:        the entry window after a confirmation is kept by the clock as well as by the candles, so a restart or a hole in the data cannot stretch it
  * irrelevant: a later change of the regime facts (the weakening fades, the extension goes, the RSI swings) neither revokes the permission nor grants one

Every scenario runs for both directions (the call, and the put through the independent reflection of scenarios.mirror)."""

import json
from dataclasses import replace

import pytest
from scenarios import P, t5
from test_exhaustion import TER, ter_ctx
from test_trend_family import oriented

from app import registry
from app import trading as tr
from app.journal import Journal, read
from app.registry import WITH_TREND, StrategyDef
from app.strategy import CONFIRMED, EXPIRED, INVALIDATED, WAITING, Scan, Setup

SIDES = ["bull", "bear"]
OPTION = {"bull": "CE", "bear": "PE"}
STALE = ["historical data is stale: today's refresh has not completed"]
HOLDS = {i: (24690, 24692, 24684, 24686) for i in range(52, 70)}  # candles that stay inside the entry zone after the break (slot 51)
OPEN_NEWS = {"regime": {"available": False, "why": "the market data is not reliable right now (5M candles missing)", "facts": {}}}


@pytest.fixture
def clean_registry():
    saved = list(registry.REGISTRY)
    yield
    registry.REGISTRY[:] = saved
    registry._refresh()


def context(side, through, drop=(), **kw):
    """The market up to the close of 5M slot `through`, in the orientation of `side`, optionally with some 5M candles missing from the data."""
    ctx = ter_ctx(through, **kw)
    if drop:
        ctx = replace(ctx, c5=[b for b in ctx.c5 if b.start not in {t5(i) for i in drop}])
    return oriented(ctx, side)


def run(side, specs, book=None, p=P):
    """Step the real SetupBook candle by candle. `specs`: (slot, kwargs of that moment) in order. Returns (book, [(ctx, result)])."""
    book, out = book or tr.SetupBook(p), []
    for n, kw in specs:
        ctx = context(side, n, **kw)
        res = book.update(ctx, {"session": tr.session_info(ctx.now, p)}, ctx.now)
        out.append((ctx, res))
    return book, out


def look(ctx, core, data_ok=True, issues=(), tick_age=1.0):
    """The dashboard payload (and the decision) for the book's state at that moment: the tail of test_trend_family.decision_after."""
    t = ctx.trend
    opposite = {"bullish": "bearish", "bearish": "bullish"}
    trend = t["1h"] if t["1h"] in opposite and t["15m"] != opposite[t["1h"]] else "sideways"
    a = {"price": ctx.price, "condition": {"trend": trend, "volatility": ctx.volatility, "alignment": {"label": ctx.alignment, "text": "x"}},
         "timeframes": {tf: {"trend": t[tf], "adx": ctx.adx.get(tf), "atr14_intraday": ctx.atr.get(tf), "atr14": ctx.atr.get(tf)} for tf in t}}  # fmt: skip
    env = {"session": tr.session_info(ctx.now, P), "data": {"ok": data_ok, "issues": list(issues)}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None, "gap": None}
    return tr.present(core, a, env, ctx.now, P, tick_age)


def specs(upto, **kw):
    return [(n, {"rows": HOLDS, **kw}) for n in range(47, upto + 1)]


def mine(book, side):
    return book.active.get((TER, side))


def events(book):
    return [e["status"] for e in reversed(book.events) if e["strategy"] == TER]


def record_of(book, ctx):
    """What the journal keeps of the book at that moment, as it comes back from the file."""
    return json.loads(json.dumps({"setups": [tr.setup_dict(s, ctx.price, True, True) for s in book.active.values()], "events": list(reversed(book.events)), "market": {"regime": "trend_down"}}))


def restart(side, upto, **kw):
    """Play to the close of slot `upto`, then 'restart the app': a new book restored from the journal's last record of the old one."""
    old, steps = run(side, specs(upto, **kw))
    rec = record_of(old, steps[-1][0])
    fresh = tr.SetupBook(P)
    assert fresh.restore([rec], steps[-1][0].now) == 1
    return old, fresh, rec


# ====================================================================================================================
# Who may hold the permission
# ====================================================================================================================
def test_only_the_registered_counter_trend_strategy_has_the_permission_and_only_the_reversal_family_can_be_registered_for_it(clean_registry):
    assert registry.COUNTER_TREND == {TER} and registry.definition(TER).counter_trend
    assert [n for n in registry.NAMES if registry.definition(n).counter_trend] == [TER]
    with pytest.raises(ValueError):
        registry.register(StrategyDef("sneaky", "Sneaky", lambda v, p, prior: Scan(False, "x"), WITH_TREND, priority=99, counter_trend=True))
    assert registry.COUNTER_TREND == {TER}


@pytest.mark.parametrize("side", SIDES)
def test_a_permission_record_on_any_other_strategys_setup_is_neither_shown_restored_nor_honoured(side):
    book, steps = run(side, specs(51))
    ctx = steps[-1][0]
    granted = mine(book, side).counter_trend
    other = Setup("trend_pullback", side, f"trend_pullback:{side}:1793944800", CONFIRMED, 1793944800.0, trigger=24682.0, zone=(24682.0, 24692.0), invalidation=24659.5,
                  candle_confirmed=True, confirmed_bar_end=ctx.now - 10, counter_trend=dict(granted))  # fmt: skip
    d = tr.setup_dict(other, ctx.price, True, True)
    assert "counter_trend" not in d and not tr.permitted({**d, "counter_trend": granted})  # not shown for it, and not honoured even if it is put back
    own = tr.setup_dict(mine(book, side), ctx.price, True, True)
    assert own["counter_trend"] == granted
    stolen = {**own, "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "id": f"trend_pullback:{side}:1793944800"}
    back = tr.setup_from_dict({**stolen, "detected_at": 1.0, "status_since": 1.0})
    assert back.counter_trend is None and back.strategy == "trend_pullback"  # restored without it
    ours = tr.setup_from_dict({**own, "detected_at": 1.0, "status_since": 1.0})
    assert ours.counter_trend == granted and ours.resumed  # and the registered strategy's own is restored whole


@pytest.mark.parametrize("side", SIDES)
def test_the_permission_is_made_for_one_direction_and_the_decision_refuses_it_for_the_other(side):
    book, steps = run(side, specs(51))
    d = tr.setup_dict(mine(book, side), steps[-1][0].price, True, True)
    right = "bearish" if side == "bull" else "bullish"
    assert d["counter_trend"]["exhausted_trend"] == right and tr.permitted(d)
    wrong = {**d, "counter_trend": {**d["counter_trend"], "exhausted_trend": "bullish" if right == "bearish" else "bearish"}}
    assert not tr.permitted(wrong)
    unlisted = {**d, "strategy": "trend_pullback"}
    assert not tr.permitted(unlisted) and not tr.permitted({**d, "counter_trend": {**d["counter_trend"], "granted": False}})


# ====================================================================================================================
# Stored permission versus the data of now
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_stored_permission_never_makes_a_setup_actionable_while_the_data_is_not_trusted(side):
    option = OPTION[side]
    book, steps = run(side, specs(51))
    s = mine(book, side)
    assert s.status == CONFIRMED and look(*steps[-1])["decision"]["action"] == f"BUY {option}"
    seen = len(events(book))
    book, bad = run(side, [(52, {"rows": HOLDS, "can_confirm": False, "blockers": STALE})], book)  # the feed goes stale after the break
    ctx, core = bad[-1]
    assert mine(book, side) is s and s.status == CONFIRMED and s.blockers == STALE  # the stored setup is still there, with the reason it cannot be acted on
    out = look(ctx, core, data_ok=False, issues=STALE)
    assert out["decision"]["action"] == "NO TRADE" and not out["setups"][0]["actionable"]
    out = look(ctx, core)  # even with the data flag right, the blocker on the setup itself holds it
    assert out["decision"]["action"] == "WAIT" and out["setups"][0]["stage"] == "BLOCKED" and not out["setups"][0]["actionable"]
    book, ok = run(side, [(53, {"rows": HOLDS})], book)  # the data recovers while the entry window (4 candles) is still open: the SAME setup, no new announcement
    assert mine(book, side) is s and s.blockers == [] and len(events(book)) == seen
    assert look(*ok[-1])["decision"]["action"] == f"BUY {option}"


@pytest.mark.parametrize("side", SIDES)
def test_after_the_data_recovers_a_setup_whose_window_has_passed_does_not_come_back(side):
    book, _ = run(side, specs(51))
    book, steps = run(side, [(52, {"rows": HOLDS, "can_confirm": False, "blockers": STALE}), (53, {"rows": HOLDS, "can_confirm": False, "blockers": STALE}),
                             (54, {"rows": HOLDS, "can_confirm": False, "blockers": STALE}), (55, {"rows": HOLDS, "can_confirm": False, "blockers": STALE}),
                             (56, {"rows": HOLDS})], book)  # stale for four candles: the whole entry window  # fmt: skip
    assert mine(book, side) is None and book.recent[0].status == EXPIRED and "entry window after the confirmation has passed" in book.recent[0].note
    out = look(*steps[-1])
    assert not out["decision"]["action"].startswith("BUY") and not any(x["actionable"] for x in out["setups"] if x["strategy"] == TER)
    assert book.recent[0].key in book.terminal


@pytest.mark.parametrize("side", SIDES)
def test_a_trend_that_cannot_be_judged_ends_the_setup_for_good_it_does_not_come_back_with_the_data(side):
    unknown = {"trend": {"1h": "unknown", "15m": "unknown", "5m": "unknown", "1m": "unknown"}}
    book, steps = run(side, [(47, {}), (48, {}), (49, {}), (50, unknown), (51, {}), (52, {"rows": HOLDS})])
    assert mine(book, side) is None and book.recent[0].status == INVALIDATED and "not enough completed 1H / 15M candles" in book.recent[0].note
    assert events(book) == [WAITING, INVALIDATED]  # (the break of slot 51 finds nothing to confirm: its key has ended)
    assert not look(*steps[-1])["decision"]["action"].startswith("BUY")


@pytest.mark.parametrize("side", SIDES)
def test_without_the_regime_facts_nothing_new_is_granted_but_a_stored_permission_carries_on(side):
    sc = run(side, [(51, OPEN_NEWS)])[1][-1][1]["scans"][(TER, side)]
    assert not sc.applicable and "regime facts are not available" in sc.note and sc.setup is None  # a fresh look with the data in doubt: no permission is invented
    book, steps = run(side, [(47, {}), (48, {}), (49, OPEN_NEWS), (50, OPEN_NEWS), (51, OPEN_NEWS)])  # ... but one granted earlier is not taken away by it
    assert mine(book, side).status == CONFIRMED and events(book) == [WAITING, CONFIRMED]


# ====================================================================================================================
# The permission is not revoked by what does not concern it
# ====================================================================================================================
IRRELEVANT = {
    "the weakening fades": {"weak": "none"},
    "the extension goes": {"ext": "none"},
    "the RSI swings to an extreme": {"rsi": {"1h": 5.0, "15m": 97.0, "5m": 50.0, "1m": 50.0}},
    "the ADX rises again": {"adx": {"1h": 45.0, "15m": 40.0, "5m": 30.0, "1m": 20.0}},
    "the EMAs stop being stacked": {"ema_slope": {"15m": {"ema20": 0.0, "ema50": 0.0, "stack_bars": -1}, "5m": {"ema20": 0.0, "ema50": 0.0, "stack_bars": 0}}},
    "the 5M and 1M turn": {"trend": {"1h": "bearish", "15m": "sideways", "5m": "bullish", "1m": "bearish"}},
    "the 15M turns bullish with the reversal": {"trend": {"1h": "bearish", "15m": "bullish", "5m": "bullish", "1m": "bullish"}, "alignment": "conflicting"},
    "the 1H eases to sideways": {"trend": {"1h": "sideways", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, "alignment": "range_bound"},
}  # fmt: skip


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("what", list(IRRELEVANT))
def test_a_later_change_of_an_indicator_neither_revokes_nor_re_decides_the_permission(side, what):
    book, steps = run(side, [(47, {}), (48, {})] + [(n, IRRELEVANT[what]) for n in (49, 50, 51)])
    s = mine(book, side)
    assert s is not None and s.status == CONFIRMED and events(book) == [WAITING, CONFIRMED]  # the same setup, announced once each
    assert s.counter_trend["granted"] and s.counter_trend["exhausted_trend"] == ("bearish" if side == "bull" else "bullish")
    first = run(side, [(47, {}), (48, {})])[0]
    assert s.counter_trend == mine(first, side).counter_trend  # the record is the one made at detection, untouched by what came after


@pytest.mark.parametrize("side", SIDES)
def test_a_denied_look_leaves_nothing_behind_and_a_later_grant_starts_one_setup_announced_once(side):
    book, steps = run(side, [(47, {"weak": "none"}), (48, {"weak": "none"})])  # the weakening is not there yet: denied, and a denial is not a setup
    assert mine(book, side) is None and not events(book) and not steps[-1][1]["scans"][(TER, side)].info["granted"]
    book, _ = run(side, [(49, {}), (50, {}), (51, {})], book)  # the facts now grant it: ONE setup, decided at that look, announced once each step of its life
    assert mine(book, side).status == CONFIRMED and events(book) == [WAITING, CONFIRMED] and mine(book, side).counter_trend["granted"]


# ====================================================================================================================
# The structure the permission rests on is checked against the candles of now
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_losing_the_structure_ends_the_setup_whatever_the_indicators_say(side):
    quiet = {"weak": "none", "ext": "none", "rsi": {"1h": 50.0, "15m": 50.0, "5m": 50.0, "1m": 50.0}}
    lost = {49: (24670, 24671, 24565, 24570)}  # a 5M close under the 15M higher low 24,580 less 2.5
    book, steps = run(side, [(47, {}), (48, {}), (49, {**quiet, "rows": lost}), (50, {**quiet, "rows": lost})])
    assert mine(book, side) is None and book.recent[0].status == INVALIDATED and "the structure the reversal rests on is lost" in book.recent[0].note
    key = book.recent[0].key
    book, back = run(side, [(51, {"rows": lost})], book)  # price recovers and breaks out: the ended structure does not come back
    assert mine(book, side) is None and key in book.terminal and events(book) == [WAITING, INVALIDATED]
    assert not look(*back[-1])["decision"]["action"].startswith("BUY")


@pytest.mark.parametrize("side", SIDES)
def test_a_restart_does_not_hide_a_structure_that_failed_while_the_app_was_down(side):
    old, fresh, rec = restart(side, 49)  # restored while WAITING
    failing = {i: (24670, 24671, 24565, 24570) for i in (50,)}
    fresh_steps = run(side, [(51, {"rows": {**HOLDS, **failing}})], fresh)[1]  # the first look after the restart sees a close under the 15M higher low
    assert mine(fresh, side) is None and fresh.recent[0].status == INVALIDATED and not look(*fresh_steps[-1])["decision"]["action"].startswith("BUY")
    assert events(fresh)[-1] == INVALIDATED


@pytest.mark.parametrize("side", SIDES)
def test_an_expired_setup_never_becomes_actionable_again_even_with_price_back_in_the_zone(side):
    book, steps = run(side, specs(56))
    assert mine(book, side) is None and book.recent[0].status == EXPIRED
    key = book.recent[0].key
    book, later = run(side, [(57, {"rows": HOLDS}), (58, {"rows": HOLDS}), (59, {"rows": HOLDS})], book)  # price sits in the entry zone the whole time
    assert mine(book, side) is None and key in book.terminal
    out = look(*later[-1])
    assert not out["decision"]["action"].startswith("BUY") and not any(x["actionable"] for x in out["setups"] if x["strategy"] == TER)
    assert events(book) == [WAITING, CONFIRMED, EXPIRED]


# ====================================================================================================================
# Journal restoration: the permission and the state come back as they were, and nothing stale comes back as actionable
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_a_restart_inside_the_entry_window_resumes_the_confirmed_setup_with_its_permission(side):
    old, fresh, rec = restart(side, 51)
    r = mine(fresh, side)
    assert r.resumed and r.status == CONFIRMED and r.counter_trend == mine(old, side).counter_trend and r.confirm_candle == mine(old, side).confirm_candle
    before = len(fresh.events)
    steps = run(side, [(53, {"rows": HOLDS})], fresh)[1]  # two candles later, the data complete
    assert mine(fresh, side) is r and r.status == CONFIRMED and len(fresh.events) == before  # nothing is announced twice
    out = look(*steps[-1])
    assert out["decision"]["action"] == f"BUY {OPTION[side]}" and next(x for x in out["setups"] if x["strategy"] == TER)["counter_trend"]["granted"]


@pytest.mark.parametrize("side", SIDES)
def test_a_restart_after_the_entry_window_does_not_resume_it_as_actionable(side):
    old, fresh, rec = restart(side, 51)
    steps = run(side, [(57, {"rows": HOLDS})], fresh)[1]  # back six candles later, the data complete
    assert mine(fresh, side) is None and fresh.recent[0].status == EXPIRED
    out = look(*steps[-1])
    assert not out["decision"]["action"].startswith("BUY")


@pytest.mark.parametrize("side", SIDES)
def test_a_hole_in_the_data_cannot_stretch_the_entry_window_of_a_restored_setup(side):
    """The defect this stage found: the candles the confirmation was made from were missing after the restart (a hole in the data that had aged out of the completeness check). The
    book freezes a CONFIRMED setup, so the replay could no longer show the break, the setup stayed CONFIRMED, and with price back in the zone it was READY_TO_ACT ten candles on."""
    old, fresh, rec = restart(side, 51)
    steps = run(side, [(61, {"rows": HOLDS, "drop": range(49, 53)})], fresh)[1]
    assert mine(fresh, side) is None and fresh.recent[0].status == EXPIRED and "entry window after the confirmation has passed" in fresh.recent[0].note
    out = look(*steps[-1])
    assert not out["decision"]["action"].startswith("BUY") and not any(x["actionable"] for x in out["setups"] if x["strategy"] == TER)


@pytest.mark.parametrize("side", SIDES)
def test_the_clock_closes_the_entry_window_after_exactly_four_candles_even_when_the_data_has_a_hole(side):
    old, fresh, rec = restart(side, 51)
    run(side, [(54, {"rows": HOLDS, "drop": [51]})], fresh)  # three candles after the confirming one (which is missing from the data): 903 s on the clock, the window is open
    assert mine(fresh, side) is not None and mine(fresh, side).status == CONFIRMED
    run(side, [(55, {"rows": HOLDS, "drop": [51]})], fresh)  # four candles: 1203 s, the window is over
    assert mine(fresh, side) is None and fresh.recent[0].status == EXPIRED and "entry window after the confirmation has passed" in fresh.recent[0].note


@pytest.mark.parametrize("side", SIDES)
def test_a_restored_confirmed_setup_with_an_incomplete_record_of_its_confirmation_is_not_trusted(side):
    old, steps = run(side, specs(51))
    rec = record_of(old, steps[-1][0])
    rec["setups"][0]["confirm_candle"], rec["setups"][0]["confirmed_bar_end"] = None, None
    fresh = tr.SetupBook(P)
    assert fresh.restore([rec], steps[-1][0].now) == 1
    out = run(side, [(52, {"rows": HOLDS})], fresh)[1]
    assert mine(fresh, side) is None and fresh.recent[0].status == EXPIRED and "incomplete" in fresh.recent[0].note
    assert not look(*out[-1])["decision"]["action"].startswith("BUY")


@pytest.mark.parametrize("side", SIDES)
def test_a_hole_inside_the_structure_blocks_the_confirmation_until_the_data_is_whole(side):
    holed = {"drop": [42]}  # one 5M candle missing between the swings the setup is read from, long enough ago that the data check no longer flags it
    book, steps = run(side, [(n, {"rows": HOLDS, **holed}) for n in range(47, 52)])
    s = mine(book, side)
    assert s.status == WAITING and s.candle_confirmed and any("5M candles are missing between" in b for b in s.blockers)  # the break candle has completed, but the swings cannot be trusted
    out = look(*steps[-1])
    shown = next(x for x in out["setups"] if x["strategy"] == TER)
    assert out["decision"]["action"] == "WAIT" and shown["stage"] == "BLOCKED" and not shown["actionable"]
    book, whole = run(side, [(52, {"rows": HOLDS})], book)  # the candle is filled in: the same setup confirms, once
    assert mine(book, side).status == CONFIRMED and events(book) == [WAITING, CONFIRMED] and mine(book, side).blockers == []
    assert look(*whole[-1])["decision"]["action"] == f"BUY {OPTION[side]}"


@pytest.mark.parametrize("side", SIDES)
def test_a_hole_that_appears_under_a_confirmed_restored_setup_blocks_it_while_the_window_is_open(side):
    old, fresh, rec = restart(side, 51)
    steps = run(side, [(53, {"rows": HOLDS, "drop": [51]})], fresh)[1]  # restored two candles on, the confirming candle itself missing from the data
    s = mine(fresh, side)
    assert s is not None and s.status == CONFIRMED and any("5M candles are missing between" in b for b in s.blockers)
    out = look(*steps[-1])
    assert not next(x for x in out["setups"] if x["strategy"] == TER)["actionable"] and not out["decision"]["action"].startswith("BUY")


@pytest.mark.parametrize("side", SIDES)
def test_a_permission_record_that_cannot_be_trusted_is_decided_afresh_for_a_waiting_setup(side):
    right = "bearish" if side == "bull" else "bullish"

    def inverted(ct):  # every number present and the direction right, but the structure the wrong way round (the exhaustion extreme and the higher low swapped)
        st = ct["structure"]
        return {"structure": {**st, "exhaustion_extreme": st["higher_low"], "higher_low": st["exhaustion_extreme"]}}

    damages = [{"exhausted_trend": "bullish" if right == "bearish" else "bearish"}, {"granted": False}, {"structure": {"exhaustion_extreme": "x"}}, {"structure": None}, inverted]
    for damage in damages:
        old, steps = run(side, specs(49))
        original = mine(old, side).counter_trend
        rec = record_of(old, steps[-1][0])
        rec["setups"][0]["counter_trend"].update(damage(rec["setups"][0]["counter_trend"]) if callable(damage) else damage)
        fresh = tr.SetupBook(P)
        assert fresh.restore([rec], steps[-1][0].now) == 1
        out = run(side, [(50, {"rows": HOLDS}), (51, {"rows": HOLDS})], fresh)[1]
        s = mine(fresh, side)
        assert s.status == CONFIRMED and s.counter_trend["granted"] and s.counter_trend["exhausted_trend"] == right, damage  # the damaged record was not used: the facts of now decided it
        assert s.counter_trend["structure"] == original["structure"]  # ... and the structure in force is the one the candles show, not the one the record claimed
        assert look(*out[-1])["decision"]["action"] == f"BUY {OPTION[side]}"


@pytest.mark.parametrize("side", SIDES)
def test_a_damaged_permission_record_does_not_keep_a_setup_alive_when_the_facts_no_longer_grant_it(side):
    old, steps = run(side, specs(49))
    rec = record_of(old, steps[-1][0])
    rec["setups"][0]["counter_trend"]["granted"] = False
    fresh = tr.SetupBook(P)
    assert fresh.restore([rec], steps[-1][0].now) == 1
    run(side, [(50, {"rows": HOLDS, "weak": "none"})], fresh)
    assert mine(fresh, side) is None and fresh.recent[0].status == EXPIRED  # no trusted record and no permission now: it ends instead of carrying a permission nobody can vouch for


@pytest.mark.parametrize("side", SIDES)
def test_a_confirmed_setup_restored_with_a_permission_for_the_other_direction_is_refused_by_the_decision(side):
    old, steps = run(side, specs(51))
    rec = record_of(old, steps[-1][0])
    rec["setups"][0]["counter_trend"]["exhausted_trend"] = "bullish" if side == "bull" else "bearish"
    fresh = tr.SetupBook(P)
    assert fresh.restore([rec], steps[-1][0].now) == 1
    out = look(*run(side, [(52, {"rows": HOLDS})], fresh)[1][-1])
    assert not out["decision"]["action"].startswith("BUY") and any("ignored" in x for x in out["decision"]["against"])


@pytest.mark.parametrize("side", SIDES)
def test_an_ended_setup_is_not_restored_even_if_a_later_record_still_lists_it(side):
    old, steps = run(side, specs(56))  # confirmed at 51, expired at 55
    ended = [e for e in old.events if e["status"] == EXPIRED and e["strategy"] == TER]
    assert ended
    stale = run(side, specs(51))[0]
    rec = record_of(stale, context(side, 51))  # an older record that still shows it live
    rec["events"] = list(reversed(old.events))  # ... but the journal's events say it expired
    fresh = tr.SetupBook(P)
    assert fresh.restore([rec], steps[-1][0].now) == 0 and mine(fresh, side) is None and rec["setups"][0]["id"] in fresh.terminal


# ====================================================================================================================
# The journal keeps the permission history, and history decides nothing
# ====================================================================================================================
@pytest.mark.parametrize("side", SIDES)
def test_the_journal_keeps_the_permission_history_of_a_setup_that_ended_and_the_ended_record_decides_nothing(side, tmp_path):
    j = Journal(tmp_path)
    a = {"timeframes": {}, "condition": {}, "warnings": []}
    book = tr.SetupBook(P)
    rows = {**HOLDS, 52: (24690, 24690, 24655, 24658)}  # the break holds for a candle, then closes under the invalidation
    last = None
    for n in range(47, 54):
        ctx = context(side, n, rows=rows)
        core = book.update(ctx, {"session": tr.session_info(ctx.now, P)}, ctx.now)
        last = look(ctx, core)
        j.observe(last, a, core["events"], ctx.now)  # (the engine hands the journal the lifecycle events, newest first)
    records, bad = read(next(tmp_path.glob("decisions-*.jsonl")))
    assert bad == []
    confirmed = next(r for r in records if r["decision"]["action"] == f"BUY {OPTION[side]}")
    assert confirmed["counter_trend"][0]["granted"] and confirmed["counter_trend"][0]["checks"] and confirmed["counter_trend"][0]["structure"]["exhaustion_extreme"]  # the permission, whole
    assert confirmed["setups"][0]["counter_trend"]["granted"] and confirmed["setups"][0]["status"] == CONFIRMED
    assert any(e["status"] == INVALIDATED for r in records for e in r["events"])  # the lifecycle, to its end
    final = records[-1]
    assert not final["decision"]["action"].startswith("BUY") and final["setups"] == []  # and after it ended nothing of the history makes a trade
    assert final["decision"]["action"] == last["decision"]["action"]
