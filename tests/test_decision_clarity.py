"""Phase 2 context and Phase 3 clarity: the extension facts, the unchanged thresholds, the setup stage, the targets from real levels, the
wording of a blocked setup, the explanation and the journal. Scenario prices are hand-made (tests/scenarios.py): none is a market price."""

import json
import re
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import scenarios as S
from test_phase3_integration import bull  # noqa: F401

from app import analysis as an
from app import candles
from app import trading as tr
from app.candles import Candle
from app.health import TimeGuard
from app.journal import Journal, read, timeline
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Level, Params, Setup, clock_seconds

P = Params()
ROOT = Path(__file__).resolve().parent.parent


def decide_for(ctx, data_ok=True, issues=()):
    """The real lifecycle and decision for one hand-made context."""
    now = ctx.now
    sess = tr.session_info(now, P)
    env = {"session": sess, "data": {"ok": data_ok, "issues": list(issues)}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None, "gap": None}
    core = tr.SetupBook(P).update(ctx, env, now)
    t = ctx.trend
    opposite = {"bullish": "bearish", "bearish": "bullish"}
    trend = t["1h"] if t["1h"] in opposite and t["15m"] != opposite[t["1h"]] else "sideways"
    a = {"price": ctx.price, "condition": {"trend": trend, "volatility": ctx.volatility, "alignment": {"label": ctx.alignment, "text": "x"}},
         "timeframes": {tf: {"trend": t[tf], "adx": ctx.adx.get(tf), "atr14_intraday": ctx.atr.get(tf), "atr14": ctx.atr.get(tf)} for tf in t}}
    return tr.present(core, a, env, now, P, 1.0)


# -- nothing was tuned: every threshold is exactly where it was -------------------------------------------------------------------------
ORIGINAL_THRESHOLDS = {
    "open_time": "09:15", "observe_until": "09:30", "midday_start": "12:00", "midday_end": "13:30", "last_entry": "14:45", "close_time": "15:30",
    "opening_range_levels": True, "max_price_age": 20.0, "completeness_bars_5m": 12, "completeness_bars_15m": 8, "vol_extreme": 2.0,
    "level_tol_atr15": 0.25, "inv_buffer_atr5": 0.25, "entry_chase_atr5": 1.0, "min_risk_atr5": 0.75, "max_risk_atr15": 1.5, "min_room_risk": 1.0,
    "rsi_caution": 70.0, "rejection_wick": 0.4, "rejection_window": 3, "confirm_body_atr5": 0.4, "momentum_body_atr5": 0.7, "confirm_close_loc": 0.6,
    "confirm_valid_bars": 4, "failed_follow_atr5": 0.25, "max_bars_after_touch": 18, "detected_max_minutes": 120, "min_adx_1h": 20.0,
    "min_pullback_atr15": 0.5, "break_margin_atr15": 0.15, "break_body_atr15": 0.5, "break_close_loc": 0.6, "break_vs_recent": 1.0,
    "breakout_lookback_15m": 8, "retest_max_bars": 24, "missed_atr15": 1.0, "range_max_adx": 25.0, "min_range_atr15": 1.5, "max_range_atr15": 6.0,
    "range_edge_fraction": 0.25, "approach_zone_atr15": 1.0, "approach_gap_bars": 2, "approach_stale_bars": 10, "fail_depth_atr15": 1.0,
}  # fmt: skip
ADDED_THRESHOLDS = {  # Stage 2.1: the thresholds of the trend-continuation strategies. Initial assumptions like all the others, pinned here so a change is deliberate
    "mc_min_adx_15m": 25.0, "mc_min_leg_atr15": 1.0, "mc_min_pause_bars": 2, "mc_max_bars": 12,
    "etc_min_adx_15m": 20.0, "etc_min_stack_bars": 8, "etc_min_leg_atr15": 0.75, "etc_max_bars": 12,
    "ve_box_bars": 12, "ve_box_max_atr5": 3.5, "ve_range_atr5": 1.5, "ve_max_bars": 6,
    # Stage 2.2: the opening range breakout and the failed breakout / trap reversal. No threshold for the range's width: the existing risk limits judge it
    "orb_end": "11:00", "orb_probe_atr5": 0.25, "fbtr_window_bars": 12, "fbtr_max_bars": 6,
    # Stage 3: the thresholds of the NAMED checks behind the regime facts (app/regime.py) and the three of Trend Exhaustion Reversal. Initial assumptions like all the others
    "rg_strong_adx15": 25.0, "rg_adx_fall": 2.0, "rg_stack_bars": 8, "rg_persist_bars": 12, "rg_persist_share": 0.75, "rg_window": 6, "rg_body_fade": 0.5,
    "rg_stretch_growth_atr15": 0.5, "rg_wick_share": 0.5, "rg_weak_min_checks": 2, "rg_compress_atr_ratio": 0.8, "rg_compress_narrow": 0.75, "rg_compress_adx": 20.0,
    "rg_compress_min_checks": 3, "rg_expand_atr_ratio": 1.3, "rg_expand_body_atr15": 1.0, "rg_expand_close_loc": 0.7, "rg_expand_box_back": 4, "rg_ext_atr20": 2.0,
    "rg_ext_atr50": 3.0, "rg_ext_run": 5, "rg_ext_pullback_atr15": 1.0, "rg_ext_pullback_bars": 12,
    "ter_min_leg_atr15": 2.0, "ter_reversal_max_bars_15m": 12, "ter_max_wait_bars": 24,
}  # fmt: skip


def test_every_strategy_and_analysis_threshold_is_unchanged():
    current = asdict(P)
    assert {k: current[k] for k in ORIGINAL_THRESHOLDS} == ORIGINAL_THRESHOLDS  # every original threshold has exactly the value it had
    assert {k: current[k] for k in ADDED_THRESHOLDS} == ADDED_THRESHOLDS
    assert set(current) == set(ORIGINAL_THRESHOLDS) | set(ADDED_THRESHOLDS)  # and nothing else exists: no threshold was added without being listed here
    assert (an.ADX_TREND, an.VOL_HIGH, an.VOL_LOW, an.CONSOLIDATION_BARS, an.CONSOLIDATION_ATR, an.WARMUP_BARS, an.WINDOW, an.SWING_K, an.ATR_AVG_BARS) == (
        20.0, 1.3, 0.7, 12, 3.0, 150, 500, 2, 100)  # fmt: skip
    assert (an.LEVEL_CLUSTER_ATR, an.MAX_LEVELS_PER_SIDE, an.RSI_STRETCHED) == (0.5, 3, (70.0, 30.0))


def test_the_constants_that_exist_twice_agree_so_they_cannot_drift_apart():
    assert P.rsi_caution == an.RSI_STRETCHED[0]  # the strategy caution and the analysis text use the same 70
    assert clock_seconds(P.open_time) == candles.OPEN_S and clock_seconds(P.close_time) == candles.CLOSE_S  # the session the candles are anchored to


def test_the_rsi_caution_follows_the_parameter_and_still_never_blocks():
    c = S.bull_pullback(19, rsi={"1h": 60.0, "15m": 66.0, "5m": 50.0, "1m": 50.0})
    assert not any("RSI" in x for s in decide_for(c)["setups"] for x in s["against"])  # 66 is under the default 70
    now = tr.SetupBook(replace(P, rsi_caution=65.0)).update(c, {"session": tr.session_info(c.now, P), "data": {"ok": True, "issues": []}, "vol_extreme": False,
                                                              "vol_ratio": 1.0, "opening_range": None, "gap": None}, c.now)  # fmt: skip
    caution = [x for s in now["setups"] for x in s.against if "RSI" in x]
    assert caution and "stretched" in caution[0] and now["setups"][0].status == CONFIRMED  # a caution is added, the setup is not stopped


# -- trend extension: facts, never a verdict ------------------------------------------------------------------------------------------
def candles_from(closes):
    return [Candle(1000.0 + 300 * i, 1300.0 + 300 * i, x, x + 1, x - 1, x) for i, x in enumerate(closes)]


def test_extension_facts_are_exact_and_carry_no_verdict():
    cs = candles_from([10, 11, 12, 13])
    e = an.extension(cs, ema20=10.0, ema50=8.0, atr_value=2.0, adx_s=[10, 12, 14, 16, 18, 20, 22], trend="bullish", brk="breakdown")
    assert e["ema20_atr"] == pytest.approx((13 - 10) / 2) and e["ema50_atr"] == pytest.approx((13 - 8) / 2)  # in ATRs
    assert e["run"] == 3  # three higher closes in a row
    assert e["adx_slope"] == pytest.approx(22 - 12) and e["break_against"] == "breakdown"  # ADX rose 10 over 5 candles; a breakdown against a bullish trend
    assert an.extension(candles_from([13, 12, 11]), 10.0, 8.0, 2.0, [], "bullish", None)["run"] == -2
    assert an.extension(candles_from([10, 11, 11, 12]), 10.0, 8.0, 2.0, [], "bullish", None)["run"] == 1  # an unchanged close ends the run
    assert an.extension(cs, 10.0, 8.0, 2.0, [], "bullish", "breakout")["break_against"] is None  # a breakout WITH the trend is not against it
    assert an.extension(cs, 10.0, 8.0, 2.0, [], "sideways", "breakdown")["break_against"] is None
    empty = an.extension(cs, None, None, None, [1, 2], "bullish", None)
    assert (empty["ema20_atr"], empty["ema50_atr"], empty["adx_slope"]) == (None, None, None)  # nothing is guessed when an input is missing
    text = an.extension_text("15M", e)
    assert "1.5 ATR above its EMA20" in text and "2.5 ATR above its EMA50" in text and "3 consecutive higher closes" in text
    assert "ADX rose by 10.0" in text and "breakdown" in text and "against the trend" in text
    assert not any(w in text.lower() for w in ("exhaust", "overbought", "extended", "reversal", "sell", "buy", "too "))  # facts, with no verdict attached


def test_the_extension_reaches_the_analysis_and_the_condition_text_and_only_the_regime_layer_reads_it(bull):  # noqa: F811
    md, clock = bull.engine()
    bull.step(md, clock, 19)
    a = md.analysis()
    for tf in ("1h", "15m", "5m", "1m"):
        ext = a["timeframes"][tf]["extension"]
        assert set(ext) == {"ema20_atr", "ema50_atr", "run", "adx_slope", "break_against"}
    last = a["timeframes"]["15m"]["last_completed"]["close"]
    assert a["timeframes"]["15m"]["extension"]["ema20_atr"] == pytest.approx((last - a["timeframes"]["15m"]["ema20"]) / a["timeframes"]["15m"]["atr14_intraday"])
    # Stage 3 relaxed the Stage 1 guard ("no rule reads the extension facts") on purpose: the regime layer reads them, with named thresholds of its own, and the strategies read the regime
    # facts, never the raw extension. The wording that said "context only, no rule uses it" was changed with it.
    assert any(r.startswith("Extension (a fact: the regime layer reads it, and no number is a signal by itself):") for r in a["condition"]["reasons"])
    assert not any("no rule uses it" in r for r in a["condition"]["reasons"])
    assert set(a["condition"]["extension"]) == {"1h", "15m"}
    import re

    readers = {p.name for p in [*(ROOT / "app").glob("*.py"), *(ROOT / "app" / "strategies").glob("*.py")] if re.search(r"""["']extension["']""", p.read_text(encoding="utf-8"))}
    assert readers == {"analysis.py", "regime.py", "journal.py"}, readers  # produced by the analysis, read by the regime layer, recorded by the journal: no scanner and no decision code touches it
    for source in ("strategy.py", "trading.py"):
        assert not re.search(r"""["']extension["']""", (ROOT / "app" / source).read_text(encoding="utf-8"))


# -- the setup stage: the lifecycle is untouched, the name is accurate ----------------------------------------------------------------------
def setup(status, **kw):
    return Setup("trend_pullback", "bull", "trend_pullback:bull:1", status, 1.0, **kw)


def test_every_stage_is_derived_from_the_lifecycle_state_and_what_holds_the_setup_back():
    stage = lambda s, actionable=False, allowed=True, tick=True: tr.setup_stage(s, actionable, allowed, tick)[0]  # noqa: E731
    assert stage(setup(DETECTED)) == "DETECTED" and stage(setup(WAITING)) == "WAITING_FOR_CONFIRMATION"
    assert stage(setup(CONFIRMED), actionable=True) == "READY_TO_ACT"
    assert stage(setup(CONFIRMED, blockers=["only 15.0 pts to the next level"])) == "BLOCKED"
    assert stage(setup(CONFIRMED), allowed=False) == "BLOCKED" and stage(setup(CONFIRMED), tick=False) == "BLOCKED"  # the session or a stale price holds it back
    assert stage(setup(CONFIRMED)) == "CONFIRMED"  # confirmed and nothing blocks it, but price is outside the entry area
    assert stage(setup(WAITING, candle_confirmed=True, blockers=["room"])) == "BLOCKED"  # the candle HAS confirmed: not waiting for one
    assert stage(setup(INVALIDATED, note="price closed below")) == "INVALIDATED"
    assert stage(setup(EXPIRED, note="price closed 12.0 pts beyond the trigger: the entry opportunity has passed")) == "MISSED"
    assert stage(setup(EXPIRED, note="missed: price ran more than 1x ATR15 beyond the breakout candle without retesting")) == "MISSED"
    assert stage(setup(EXPIRED, note="the entry window after the confirmation has passed")) == "MISSED"
    assert stage(setup(EXPIRED, note="no confirmation within 18 candles of the low")) == "EXPIRED"
    assert stage(setup(EXPIRED, note="no longer present: the trend made a new high")) == "EXPIRED"


def room_blocked():
    c = S.bull_pullback(19)
    return replace(c, levels=c.levels + [Level(24605.0, "swing level x2", 2)])  # a level 15 points above the 24,590 trigger, with 30.5 points at risk


def test_a_confirmed_setup_blocked_by_room_is_blocked_not_waiting_for_a_candle():
    out = decide_for(room_blocked())
    [s] = out["setups"]
    assert s["status"] == "WAITING_FOR_CONFIRMATION"  # the lifecycle state is exactly what it was: nothing in the state machine changed
    assert s["candle_confirmed"] is True and s["stage"] == "BLOCKED" and s["actionable"] is False
    room = "only 15.0 pts to the next level 24,605.00 (under 1x the 30.5 pts at risk)"
    assert s["blockers"] == [room]  # the room rule and its text are untouched
    d = out["decision"]
    assert d["action"] == "WAIT" and d["headline"] == "WAIT: Trend Pullback (CE) is confirmed but blocked"
    assert "confirmation is not what is missing" in d["reasons"][0] and room in d["reasons"]
    assert not any("waiting for confirmation" in r for r in d["reasons"])  # the old wording is gone
    assert [m["state"] for m in out["strategies"] if m["id"] == "trend_pullback"] == ["BLOCKED"]  # the strategy monitor says it too


def test_a_setup_that_has_not_been_confirmed_is_still_waiting_and_a_ready_one_is_ready():
    waiting = decide_for(S.bull_pullback(18))
    assert waiting["setups"][0]["stage"] == "WAITING_FOR_CONFIRMATION" and waiting["decision"]["headline"].endswith("is developing")
    ready = decide_for(S.bull_pullback(19))
    assert ready["setups"][0]["stage"] == "READY_TO_ACT" and ready["decision"]["action"] == "BUY CE"
    assert [m["state"] for m in ready["strategies"] if m["id"] == "trend_pullback"] == ["CONFIRMED"]  # the lifecycle word is unchanged in the monitor


# -- targets: only real levels, in index points ---------------------------------------------------------------------------------------------
def test_targets_come_from_the_levels_that_were_detected_beyond_the_trigger_with_underlying_risk_and_reward():
    plan = decide_for(room_blocked())["setups"][0]["plan"]
    assert plan["risk_points"] == pytest.approx(30.5) and plan["entry_reference"] == 24590.0 and "not option premium" in plan["basis"]
    t1, t2 = plan["targets"]
    assert (t1["n"], t1["price"], t1["label"], t1["distance"]) == (1, 24605.0, "swing level x2", 15.0) and t1["reward_risk"] == pytest.approx(15.0 / 30.5)
    assert (t2["n"], t2["price"], t2["label"], t2["distance"]) == (2, 24650.0, "prior high (where this pullback started)", 60.0) and t2["reward_risk"] == pytest.approx(60 / 30.5)
    assert t1["distance"] == 24605.0 - 24590.0  # T1 is the very level the room rule measures: one source of truth
    assert plan["note"] == ""


def test_with_one_level_beyond_there_is_one_target_and_with_none_there_is_none():
    plan = decide_for(S.bull_pullback(19))["setups"][0]["plan"]
    assert [g["price"] for g in plan["targets"]] == [24650.0] and "no second target" in plan["note"]  # the prior high is the only level above the trigger
    blue_sky = S.bull_breakout(28, levels=[Level(S.LEVEL, "swing level x3", 3)])  # nothing above the broken level
    plan = decide_for(blue_sky)["setups"][0]["plan"]
    assert plan["targets"] == [] and "no level was detected beyond the trigger" in plan["note"] and plan["risk_points"] > 0  # no target is invented


def test_the_same_level_seen_twice_is_one_target_and_a_bearish_setup_points_down():
    c = S.bull_pullback(19)
    twice = replace(c, levels=c.levels + [Level(24650.5, "swing level x2", 2)])  # the prior high again, half a point higher
    assert [g["price"] for g in decide_for(twice)["setups"][0]["plan"]["targets"]] == [24650.0]
    bear = decide_for(S.mirror(c))["setups"][0]
    plan = bear["plan"]
    assert bear["direction"] == "PE" and plan["entry_reference"] == 25410.0 and plan["targets"][0]["price"] == 25350.0  # below the trigger
    assert plan["targets"][0]["distance"] == 60.0 and plan["targets"][0]["reward_risk"] == pytest.approx(60 / 30.5)  # distances are positive


def test_the_leg_high_is_named_for_what_it_is_and_still_counts_for_the_room_rule():
    c = S.bull_pullback(19)
    only_leg = decide_for(replace(c, levels=[Level(24565.0, "swing level x2", 2)]))["setups"][0]  # no other level above the trigger
    [t1] = only_leg["plan"]["targets"]
    assert t1["label"] == "prior high (where this pullback started)" and t1["price"] == 24650.0
    assert next(q for q in only_leg["quality"] if q["name"] == "Room")["text"] == "60.0 pts to 24,650.00 = 2.0x the risk"  # the rule measures to it, as before


# -- the timeframes: the higher ones decide the side, the lower ones are information ------------------------------------------------------------
def test_5m_and_1m_never_override_the_higher_timeframes_and_the_wording_says_so():
    c = S.bull_pullback(19)
    against = decide_for(replace(c, trend={"1h": "bullish", "15m": "bullish", "5m": "bearish", "1m": "bearish"}))
    s = against["setups"][0]
    assert against["decision"]["action"] == "BUY CE"  # a bearish 5M and 1M do not turn a bullish 1H/15M setup into anything else
    higher = next(q for q in s["quality"] if q["name"].startswith("Higher timeframes"))
    entry = next(q for q in s["quality"] if q["name"].startswith("Entry timeframes"))
    assert higher["name"] == "Higher timeframes (1H and 15M)" and not re.search(r"(?<![0-9])5M", higher["text"]) and higher["ok"] is True  # it no longer implies 5M agrees
    assert "5M bearish" in entry["text"] and "never block" in entry["text"] and entry["ok"] is False
    ex = against["explain"]
    assert ex["supports"] == ["1H bullish", "15M bullish"] and ex["disagrees"] == ["5M bearish", "1M bearish"] and "only the 1H and 15M decide" in ex["note"]
    conflicting = decide_for(replace(c, trend={"1h": "bullish", "15m": "bearish", "5m": "bearish", "1m": "bearish"}, alignment="conflicting"))
    assert conflicting["decision"]["action"] == "NO TRADE" and conflicting["setups"] == []  # a higher timeframe that disagrees does


# -- the explanation: five things, built from the setup the decision is about --------------------------------------------------------------------------
def test_the_explanation_answers_what_happened_what_now_what_next_what_ends_it_and_what_blocks():
    ready = decide_for(S.bull_pullback(19))["explain"]
    assert ready["stage"] == "READY_TO_ACT" and ready["actionable"] is True and ready["side"] == "CE"
    assert any("confirming 5M candle completed 10:55" in x for x in ready["happened"])
    assert any("inside the entry area" in x for x in ready["now"]) and any("beyond the area it is chasing" in x for x in ready["next"])
    assert ready["invalidation"].startswith("a completed 5M candle closing below 24,559.50") and ready["blocking"] == []
    blocked = decide_for(room_blocked())["explain"]
    assert blocked["stage"] == "BLOCKED" and blocked["actionable"] is False
    assert blocked["blocking"] == ["only 15.0 pts to the next level 24,605.00 (under 1x the 30.5 pts at risk)"]
    assert any("not a missing candle" in x for x in blocked["next"]) and any("confirmation is not what is missing" in x for x in blocked["now"])
    waiting = decide_for(S.bull_pullback(18))["explain"]
    assert waiting["stage"] == "WAITING_FOR_CONFIRMATION" and "completed 5M bullish candle closes above" in waiting["next"][0]


def test_untrusted_data_means_no_trade_nothing_actionable_and_the_reason_in_the_explanation():
    out = decide_for(S.bull_pullback(19), data_ok=False, issues=["the clock cannot be trusted: nothing was seen for 400 s"])
    assert out["decision"]["action"] == "NO TRADE" and "not reliable" in out["decision"]["headline"]
    assert all(not s["actionable"] for s in out["setups"]) and out["explain"]["blocking"] == ["the clock cannot be trusted: nothing was seen for 400 s"]
    assert all(s["stage"] != "READY_TO_ACT" for s in out["setups"])


def test_a_stale_price_is_never_described_as_where_the_market_is():
    c = S.bull_pullback(19)
    sess = tr.session_info(c.now, P)
    env = {"session": sess, "data": {"ok": True, "issues": []}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None, "gap": None}
    core = tr.SetupBook(P).update(c, env, c.now)
    a = {"price": c.price, "condition": {"trend": "bullish", "volatility": "normal", "alignment": {"label": "aligned_bullish", "text": "x"}},
         "timeframes": {tf: {"trend": c.trend[tf], "adx": 20.0, "atr14_intraday": 10.0, "atr14": 10.0} for tf in c.trend}}
    stale = tr.present(core, a, env, c.now, P, 95.0)
    assert stale["decision"]["action"] == "WAIT" and not stale["setups"][0]["actionable"]  # a price 95 s old is not acted on
    text = " ".join(stale["explain"]["now"])
    assert "95 s old" in text and "not treated as live" in text and "inside the entry area" not in text
    assert "not available" in " ".join(tr.present(core, a, env, c.now, P, None)["explain"]["now"])


def test_with_no_setup_the_explanation_says_why_and_the_dashboard_says_a_decision_is_not_a_recommendation():
    none = decide_for(S.bull_pullback(11))  # the leg has just made its high: no pullback yet
    assert none["decision"]["action"] != "BUY CE" and none["explain"]["setup"] is None and none["explain"]["happened"]
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "is not a recommendation to trade" in html and "Data not trusted" in html and "STALE: last NIFTY tick" in html and "id=\"dh\"" in html


# -- the journal keeps what the decision was made from ---------------------------------------------------------------------------------------------------
def test_the_journal_records_the_spot_the_levels_the_plan_the_stage_and_the_extension(bull, tmp_path):  # noqa: F811
    md, clock = bull.engine()
    md.journal = Journal(tmp_path)
    for slot in (17, 18, 19):
        bull.step(md, clock, slot)
    records, bad = read(next(tmp_path.glob("decisions-*.jsonl")))
    buy = records[-1]
    assert bad == [] and buy["decision"]["action"] == "BUY CE"
    assert buy["spot"]["price"] == buy["price"] and buy["spot"]["age"] == pytest.approx(1.0)  # the price and how old it was
    assert set(buy["levels"]) == {"support", "resistance", "previous_day", "session_open", "session_avg_price"}
    assert all(len(x) == 2 for x in buy["levels"]["support"] + buy["levels"]["resistance"])  # [price, touches]
    s = buy["setups"][0]
    assert s["stage"] == "READY_TO_ACT" and s["candle_confirmed"] and s["plan"]["targets"] and s["plan"]["basis"].startswith("index points")
    assert s["trigger"] and s["invalidation"] and s["blockers"] == [] and buy["session"]["opening_range"] is not None
    assert set(buy["timeframes"]["15m"]["extension"]) == {"ema20_atr", "ema50_atr", "run", "adx_slope", "break_against"}
    assert buy["feed"]["spot"]["stale"] is False and buy["feed"]["health_issues"] == [] and buy["time"] and "T" in buy["time"]  # local time of the record
    assert len(json.dumps(buy)) < 25000  # still lightweight JSONL


def test_a_stale_spot_and_an_untrusted_clock_are_on_record_when_they_start(bull, tmp_path):  # noqa: F811
    md, clock = bull.engine()
    md.journal = Journal(tmp_path)
    md.set_state("connected")
    md.timeguard = TimeGuard(lambda: clock.t)
    for slot in (16, 17):
        bull.step(md, clock, slot)
    md.last_spot_recv = clock.t - 90  # the NIFTY spot went quiet
    md.trading()
    md.timeguard.trusted, md.timeguard.reason = False, "nothing was seen for 400 s: the application was suspended (the PC slept?) or the PC clock was set forward"
    out = md.trading()
    assert out["decision"]["action"] == "NO TRADE" and "clock cannot be trusted" in " ".join(out["data"]["issues"])
    path = next(tmp_path.glob("decisions-*.jsonl"))
    text = "\n".join(timeline(path))
    assert "NIFTY SPOT STALE (last tick 90 s ago)" in text and "DATA NOT TRUSTED: the clock cannot be trusted" in text
    last = read(path)[0][-1]
    assert last["feed"]["spot"]["stale"] is True and last["feed"]["health_issues"] and last["decision"]["action"] == "NO TRADE"


def test_a_buy_stops_when_the_application_was_suspended_and_only_returns_when_time_is_trusted_again(bull):  # noqa: F811
    md, clock = bull.engine()
    md.set_state("connected")
    for slot in (17, 18, 19):
        bull.step(md, clock, slot)
    md.timeguard = TimeGuard(lambda: clock.t)  # the fake clock stands in for the monotonic timer; it is watched from here, a second at a time
    md.watchdog()
    clock.t += 1
    md.last_spot_recv = clock.t - 1
    assert md.watchdog() is False and md.trading()["decision"]["action"] == "BUY CE" and md.health_issues(clock.t) == []
    clock.t += 400  # the PC slept for more than six minutes
    assert md.watchdog() is True  # and the feed is told to reconnect
    out = md.trading()
    assert out["decision"]["action"] == "NO TRADE" and out["data"]["ok"] is False
    assert any("clock cannot be trusted" in i and "suspended" in i for i in out["data"]["issues"])
    assert all(not s["actionable"] for s in out["setups"]) and any("clock cannot be trusted" in b for b in out["explain"]["blocking"])
    for _ in range(30):  # thirty normal seconds with fresh spot ticks are not yet enough
        clock.t += 1
        md.last_spot_recv = clock.t - 1
        md.watchdog()
    assert md.health_issues(clock.t) and md.trading()["decision"]["action"] == "NO TRADE"
    for _ in range(40):  # the calm period is over, and a spot tick has arrived after the event
        clock.t += 1
        md.last_spot_recv = clock.t - 1
        md.watchdog()
    assert md.health_issues(clock.t) == [] and not any("clock" in i for i in md.trading()["data"]["issues"])
