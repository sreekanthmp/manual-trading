"""Stage 2.0: one registry for every strategy, and the extra facts the later strategies will read. No strategy is added here and the three original ones are
unchanged (test_existing_strategies_golden.py proves that); this proves the mechanism that the next stages rely on:

* a strategy registered once is scanned, ranked, shown by the monitor, restorable from the journal and decided on, without any other edit;
* the safety checks cannot be bypassed by a strategy: this loops over EVERY registered scanner, so it covers each strategy added later automatically;
* the new context fields are filled from the analysis and are mirrored correctly for the bearish side."""

import pytest
from helpers import FRI, ist
from scenarios import M, P, bull_breakout, bull_pullback, bull_range, mirror
from test_phase3_integration import DAY, World

from app import analysis as an
from app import registry
from app import strategy as st
from app import trading as tr
from app.candles import Candle
from app.registry import REVERSAL, WITH_TREND, StrategyDef
from app.strategy import CONFIRMED, WAITING, Context, Scan, Setup

ORIGINAL = ["trend_pullback", "breakout_retest", "range_reversal"]
LATER = ["momentum_continuation", "ema_trend_continuation", "volatility_expansion", "opening_range_breakout", "failed_breakout", "range_bounce", "trend_exhaustion_reversal"]  # in scan order: Stage 2.1, 2.2, 2.3, 3
TIE_BREAK = ["breakout_retest", "trend_pullback", "range_reversal", "ema_trend_continuation", "momentum_continuation", "volatility_expansion", "opening_range_breakout", "failed_breakout",
             "range_bounce", "trend_exhaustion_reversal"]  # fmt: skip


@pytest.fixture
def clean_registry():
    """Whatever a test registers is removed again: the registry is process-wide."""
    saved = list(registry.REGISTRY)
    yield
    registry.REGISTRY[:] = saved
    registry._refresh()


def quiet(v, p, prior):
    return Scan(False, "a test strategy that never applies")


# -- the registry itself ------------------------------------------------------------------------------------------------------
def test_the_registry_keeps_the_three_original_strategies_first_in_their_original_order_and_priority():
    assert [d.name for d in registry.REGISTRY][:3] == ORIGINAL  # scanned and shown in this order, as before; the later strategies follow
    assert list(registry.NAMES.items())[:3] == [("trend_pullback", "Trend Pullback"), ("breakout_retest", "Breakout and Retest"), ("range_reversal", "Range Reversal")]
    assert [n for n in registry.PRIORITY if n in ORIGINAL] == ["breakout_retest", "trend_pullback", "range_reversal"]  # the tie-break the originals always had, relative to each other
    assert {n: registry.FAMILY[n] for n in ORIGINAL} == {"trend_pullback": WITH_TREND, "breakout_retest": WITH_TREND, "range_reversal": registry.RANGE}
    assert all(d.needs == (registry.SPOT_CANDLES,) for d in registry.REGISTRY)
    assert [name for name, _ in registry.scanners()] == [d.name for d in registry.REGISTRY]


def test_the_later_strategies_are_registered_active_and_after_the_originals():
    assert [d.name for d in registry.REGISTRY] == ORIGINAL + LATER  # no feature flag: registered means scanned, on both sides, on every update
    assert registry.PRIORITY == TIE_BREAK  # the originals keep winning ties against everything added after them
    assert all(registry.FAMILY[n] == WITH_TREND for n in LATER if n not in ("opening_range_breakout", "range_bounce", "trend_exhaustion_reversal"))
    assert registry.FAMILY["opening_range_breakout"] == registry.SESSION  # anchored to the trading session, but the side rule is the same original one
    assert registry.FAMILY["range_bounce"] == registry.RANGE  # trades the edge of a box (in the 1H trend's direction only)


def test_only_trend_exhaustion_reversal_is_counter_trend_and_only_the_reversal_family_can_be(clean_registry):
    assert registry.COUNTER_TREND == {"trend_exhaustion_reversal"} and [d.name for d in registry.REGISTRY if d.counter_trend] == ["trend_exhaustion_reversal"]
    assert registry.FAMILY["trend_exhaustion_reversal"] == registry.REVERSAL
    for family in (WITH_TREND, registry.RANGE, registry.SESSION):  # the exception to the direction gate cannot be attached to any other kind of strategy
        with pytest.raises(ValueError, match="only the reversal family may be counter-trend"):
            registry.register(StrategyDef("sneaky", "Sneaky", quiet, family, priority=90, counter_trend=True))
    assert registry.COUNTER_TREND == {"trend_exhaustion_reversal"} and "sneaky" not in registry.NAMES  # and a refused registration leaves nothing behind
    same = registry.COUNTER_TREND
    registry.register(StrategyDef("scratch_reversal", "Scratch reversal", quiet, REVERSAL, priority=91, counter_trend=True))  # the reversal family may (this is what the flag is for) ...
    assert registry.COUNTER_TREND is same and registry.COUNTER_TREND == {"trend_exhaustion_reversal", "scratch_reversal"}  # ... and the set every module reads is the same object
    assert tr.COUNTER_TREND is registry.COUNTER_TREND


def test_every_module_reads_the_same_objects_so_a_registration_is_seen_everywhere():
    assert st.NAMES is registry.NAMES and tr.NAMES is registry.NAMES and tr.PRIORITY is registry.PRIORITY


def test_a_bad_registration_is_refused_and_leaves_the_registry_as_it_was(clean_registry):
    ok = StrategyDef("scratch", "Scratch", quiet, WITH_TREND, priority=50)
    for bad, why in ((StrategyDef("trend_pullback", "X", quiet, WITH_TREND, priority=60), "already registered"),
                     (StrategyDef("other", "X", quiet, WITH_TREND, priority=0), "priority 0 is already taken"),
                     (StrategyDef("other", "X", quiet, "momentum", priority=60), "unknown family"),
                     (StrategyDef("bad:id", "X", quiet, WITH_TREND, priority=60), "part of every setup key"),
                     (StrategyDef("bad id", "X", quiet, WITH_TREND, priority=60), "part of every setup key")):  # fmt: skip
        with pytest.raises(ValueError, match=why):
            registry.register(bad)
    before, order = [d.name for d in registry.REGISTRY], list(registry.PRIORITY)
    assert before == ORIGINAL + LATER and order == TIE_BREAK  # the bad ones changed nothing
    registry.register(ok)
    assert registry.NAMES["scratch"] == "Scratch" and registry.PRIORITY == order + ["scratch"] and registry.PRIORITY[-1] == "scratch"  # priority 50 is the last


def test_a_new_strategy_is_scanned_shown_ranked_and_restorable_with_no_other_edit(clean_registry):
    registry.register(StrategyDef("scratch", "Scratch", quiet, REVERSAL, priority=5))
    scans = st.scan_all(bull_pullback(19), P, {})
    assert list(scans)[-2:] == [("scratch", "bull"), ("scratch", "bear")] and not scans[("scratch", "bull")].applicable  # scanned, on both sides, after the others
    rows = tr.monitor(scans, [], {"text": "m"}, tr.session_info(bull_pullback(19).now, P))
    assert [r["id"] for r in rows][-1] == "scratch" and len(rows) == len(ORIGINAL + LATER) + 1 and rows[-1]["name"] == "Scratch" and rows[-1]["state"] == "INACTIVE"  # the monitor lists it
    assert registry.PRIORITY == TIE_BREAK[:3] + ["scratch"] + TIE_BREAK[3:]  # priority 5 ranks it after the originals
    setup = Setup("scratch", "bull", "scratch:bull:1793939100", WAITING, 1793939100.0)
    d = tr.setup_dict(setup, 24595.0, True, True)
    assert d["strategy_name"] == "Scratch" and d["direction"] == "CE" and d["stage"] == "WAITING_FOR_CONFIRMATION"  # presentable
    back = tr.setup_from_dict({**d, "detected_at": 1.0, "status_since": 1.0})
    assert (back.strategy, back.side, back.key, back.resumed) == ("scratch", "bull", "scratch:bull:1793939100", True)  # the journal can restore it
    with pytest.raises(ValueError):
        tr.setup_from_dict({**d, "strategy": "unregistered", "id": "unregistered:bull:1", "detected_at": 1.0, "status_since": 1.0})  # and refuses what is not registered


def test_ready_setups_in_the_same_direction_are_ordered_by_the_registry_priority_including_a_new_strategy(clean_registry):
    registry.register(StrategyDef("scratch", "Scratch", quiet, WITH_TREND, priority=-1))  # ahead of every other strategy
    registry.register(StrategyDef("later", "Later", quiet, WITH_TREND, priority=9))

    def ready(name):
        return {"id": f"{name}:CE", "strategy": name, "strategy_name": registry.NAMES[name], "direction": "CE", "side": "bull", "status": CONFIRMED, "zone": [24590.0, 24600.0],
                "trigger": 24590.0, "invalidation": 24559.5, "confirm_text": "", "reasons": ["r"], "against": [], "blockers": [], "price_state": "in_entry_area", "actionable": True}  # fmt: skip

    market = {"direction": "bullish", "regime": "trend_up", "text": "Bullish trend", "volatility": "normal", "alignment": "aligned_bullish", "vol_ratio": 1.0}
    env = {"session": tr.session_info(ist(11, 0, day=FRI), P), "data": {"ok": True, "issues": []}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None}
    mons = [{"id": n, "name": n, "active": True, "why": "", "state": "NO SETUP", "note": "", "setups": []} for n in registry.NAMES]
    d = tr.decide(market, env, [ready("later"), ready("trend_pullback"), ready("scratch")], mons, 24595.0, True, 10.0)
    assert (d["action"], d["strategy"]) == ("BUY CE", "scratch") and len(d["also"]) == 2  # it wins the tie by its priority, and the others are listed as matching too
    d = tr.decide(market, env, [ready("later"), ready("trend_pullback")], mons, 24595.0, True, 10.0)
    assert d["strategy"] == "trend_pullback"  # without it, the order of the originals is what it always was


# -- no strategy can bypass the safety checks ----------------------------------------------------------------------------------
SCENARIOS = [("pullback", bull_pullback, 19), ("breakout", bull_breakout, 28), ("range", bull_range, 35)]


@pytest.mark.parametrize("name,build,through", SCENARIOS)
@pytest.mark.parametrize("orient", ["bull", "bear"])
def test_no_registered_strategy_can_confirm_or_become_actionable_while_the_data_or_session_blocks_entry(name, build, through, orient):
    """Loops over every registered scanner (the three today, every later one automatically). A setup built while `can_confirm` is false must keep its blockers,
    cannot be CONFIRMED, and cannot be actionable however good the setup is."""
    ctx = build(through)
    ctx = ctx if orient == "bull" else mirror(ctx)
    blocked = Context(**{**ctx.__dict__, "can_confirm": False, "blockers": ["data is not trusted right now"]})
    seen = 0
    for (strategy, side), scan in st.scan_all(blocked, P, {}).items():
        if scan.setup is None or scan.setup.status not in (WAITING, CONFIRMED, st.DETECTED):
            continue
        seen += 1
        s = scan.setup
        assert s.status != CONFIRMED, f"{strategy}/{side} confirmed while entry was blocked"
        d = tr.setup_dict(s, blocked.price, True, True)
        assert not d["actionable"], f"{strategy}/{side} is actionable while entry was blocked"
        if s.candle_confirmed:
            assert "data is not trusted right now" in s.blockers, f"{strategy}/{side} lost the blocker that held it back"
    assert seen >= 1  # the scenario really produced a setup to check


@pytest.mark.parametrize("name,build,through", SCENARIOS)
def test_every_scan_has_the_shape_the_lifecycle_the_journal_and_the_restore_rely_on(name, build, through):
    for orient in ("bull", "bear"):
        ctx = build(through) if orient == "bull" else mirror(build(through))
        for (strategy, side), scan in st.scan_all(ctx, P, {}).items():
            assert isinstance(scan, Scan) and strategy in registry.NAMES
            if scan.setup is None:
                continue
            s = scan.setup
            assert s.strategy == strategy and s.side == side and s.key.startswith(f"{strategy}:{side}:")
            float(s.key.rsplit(":", 1)[1])  # the key ends with the anchor candle's start time (setup_from_dict reads it back)
            assert s.direction == ("CE" if side == "bull" else "PE")


# -- the new context facts --------------------------------------------------------------------------------------------------
def test_the_new_context_fields_default_to_nothing_so_nothing_that_builds_a_context_by_hand_changes():
    c = Context()
    assert c.opening_range is None and c.ema_slope == {} and c.atr_ratio == {} and c.regime == {}  # (Stage 3: the regime facts; empty = not computed)
    assert not hasattr(c, "extension")  # the raw extension facts are still not exposed to the strategies: they read the regime facts built from them (Stage 3 relaxed this on purpose, no further)


def test_the_context_is_filled_from_the_analysis_and_the_session():
    w = World()
    md, clock = w.engine()
    w.step(md, clock, 19)
    a, series = md.analysis(), md._cache["series"]
    ctx, env = tr.build_context(a, series, clock.t, P)
    assert ctx.opening_range is env["opening_range"] and ctx.opening_range["complete"] is True  # the opening range, from the same 5M candles as before
    assert set(ctx.ema_slope) == {"1h", "15m", "5m", "1m"} and set(ctx.atr_ratio) == {"1h", "15m", "5m", "1m"}
    for tf in ("1h", "15m", "5m"):
        facts = ctx.ema_slope[tf]
        assert set(facts) == {"ema20", "ema50", "stack_bars"} and facts["ema20"] > 0 and facts["stack_bars"] > 0  # the staircase market: rising, EMA20 above EMA50
        assert facts["ema20"] == pytest.approx(a["timeframes"][tf]["ema20_slope"])
    assert ctx.atr_ratio["15m"] == ctx.vol_ratio  # the 15M ratio is the one the volatility gate already uses


def test_an_empty_timeframe_gives_unknown_facts_not_an_error():
    w = World()
    md, clock = w.engine()
    w.step(md, clock, 19)
    a = dict(md.analysis())
    a["timeframes"] = {**a["timeframes"], "1m": an.timeframe([], 60, clock.t)}
    ctx, _ = tr.build_context(a, md._cache["series"], clock.t, P)
    assert ctx.ema_slope["1m"] == {"ema20": None, "ema50": None, "stack_bars": None} and ctx.atr_ratio["1m"] is None


def test_the_bearish_twin_of_the_new_fields_is_the_reflection_of_the_bullish_ones():
    ctx = bull_pullback(19, opening_range={"high": 24650.0, "low": 24500.0, "complete": True},
                        ema_slope={"15m": {"ema20": 8.0, "ema50": 3.0, "stack_bars": 12}}, atr_ratio={"15m": 1.1})  # fmt: skip
    twin = mirror(ctx)
    assert twin.opening_range == {"high": M - 24500.0, "low": M - 24650.0, "complete": True}  # a high becomes a low, in reflected prices
    assert twin.ema_slope["15m"] == {"ema20": -8.0, "ema50": -3.0, "stack_bars": -12} and twin.atr_ratio == {"15m": 1.1}  # rising becomes falling; sizes do not change


# -- the indicator facts ----------------------------------------------------------------------------------------------------
def test_ema_slope_is_the_move_over_the_last_five_candles_and_never_guessed():
    series = [None, None, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 17.0]
    assert an.ema_slope(series) == 17.0 - 11.0  # the last value less the one five candles before it
    assert an.ema_slope(series, 2) == 17.0 - 14.0 and an.ema_slope([1.0, 2.0, 3.0]) is None  # too short: unknown
    assert an.ema_slope([None, None, None, None, None, 5.0, 6.0]) is None  # the value five candles back does not exist yet: unknown, not zero
    assert an.ema_slope([]) is None


def test_ema_stack_bars_counts_how_long_the_same_ordering_has_held():
    fast = [None, 5.0, 6.0, 7.0, 8.0, 9.0]
    slow = [None, 4.0, 5.0, 8.0, 7.0, 6.0]
    assert an.ema_stack_bars(fast, slow) == 2  # above on the last two candles (8 > 7, 9 > 6), below before that (7 < 8)
    assert an.ema_stack_bars(slow, fast) == -2  # the mirror: the slow one is below by two
    assert an.ema_stack_bars([1.0, 2.0, 3.0], [0.0, 1.0, 2.0]) == 3 and an.ema_stack_bars([0.0, 1.0, 2.0], [1.0, 2.0, 3.0]) == -3
    assert an.ema_stack_bars([1.0, 2.0, 2.0], [0.0, 1.0, 2.0]) == 0  # equal on the last candle: no ordering to speak of
    assert an.ema_stack_bars([None, None], [None, None]) is None and an.ema_stack_bars([], []) is None


def test_the_timeframe_carries_the_new_facts_and_its_existing_values_are_unchanged():
    candles = [Candle(ist(9, 15) + 300 * i, ist(9, 20) + 300 * i, 24000 + 3 * i, 24000 + 3 * i + 4, 24000 + 3 * i - 2, 24000 + 3 * i + 2, 0) for i in range(170)]
    info = an.timeframe(candles, 300, candles[-1].end + 30)
    closes = [c.close for c in candles]
    assert info["ema20"] == an.ema(closes, 20)[-1] and info["ema50"] == an.ema(closes, 50)[-1]  # as before, to the last digit
    assert info["ema20_slope"] > 0 and info["ema50_slope"] > 0 and info["ema_stack_bars"] > 0  # a steadily rising series
    assert info["ema20_slope"] == an.ema_slope(an.ema(closes, 20)) and info["ema_stack_bars"] == an.ema_stack_bars(an.ema(closes, 20), an.ema(closes, 50))
    assert "ema20_slope" not in an.timeframe([], 300, ist(10, 0))  # nothing is invented for a timeframe with no candles


def test_the_strategies_package_registers_its_modules_on_import():
    import app.strategies as pkg

    assert pkg.__doc__ and all(hasattr(pkg, m) for m in ("trend_family", "opening_range", "failed_breakout", "range_bounce", "exhaustion")) and len(registry.REGISTRY) == len(ORIGINAL + LATER) == 10
    assert DAY and FRI  # (shared fixtures imported on purpose)
