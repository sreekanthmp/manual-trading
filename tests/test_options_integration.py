"""Stage 4: the ten strategies and the existing Phase 4 options pipeline, decision to contract.

The flow under test (read from the code, not from the documentation):

    strategies (registry, 10) -> SetupBook -> trading.decide() = THE FINAL DECISION {action, strategy, strategy_name, setup_id, also, ...}
        -> MarketData.options_view(trading, now): op.analyse(watched contracts, latest quotes, spot ...) + op.select(rows, decision["action"])
        -> snapshot() (the WebSocket push and /api/state) and /api/options -> the dashboard's Options tab.

`op.select` receives the decision's ACTION and nothing else: "BUY CE" chooses among calls, "BUY PE" among puts, anything else chooses nothing. The strategy, the setup and the other strategies
that agree are carried beside the selection for the reader; they never reach it. These tests hold that, for every strategy, on the decisions the real pipeline produces (the hand-built
candles of the strategy tests, through the real SetupBook and trading.present), against the Phase 4 test engine: a real option window with live mocked quotes. Nothing here relaxes a
threshold: the defaults are pinned, and every suitability finding keeps its severity."""

import asyncio
import inspect
import json
from dataclasses import replace
from itertools import combinations, permutations
from datetime import date
from pathlib import Path

import pytest
import stage2_snapshot as S
from helpers import CFG, Clock, ist
from test_counter_trend_decision import GRANTED, decide, env, market, sd
from test_exhaustion import TER, ter_ctx
from test_exhaustion_permission_lifecycle import look
from test_options import analyse, codes, contract, live_engine, quote
from test_phase3_integration import create_app, route
from test_trend_family import decision_after, play

from app import options as op
from app import registry
from app.engine import MarketData
from app.feed import token_groups
from app.strategy import EXPIRED, INVALIDATED

ROOT = Path(__file__).resolve().parent.parent
NOW = ist(10, 0, 22)  # two seconds after the Phase 4 test engine's last quotes
SIDES = ["bull", "bear"]
ACTION = {"bull": "BUY CE", "bear": "BUY PE"}
KIND = {"bull": "CE", "bear": "PE"}
ALL = {**S.SCENARIOS, **S.BUILTIN}
FLOWS = {  # the ten strategies: (build, the steps, the scenario's own arguments) -> a confirmed, actionable setup of that strategy on its own
    "trend_pullback": (*ALL["tp_pullback"][:2], ALL["tp_pullback"][2]),
    "breakout_retest": (*ALL["br_breakout"][:2], ALL["br_breakout"][2]),
    "range_reversal": (ALL["rr_range"][0], [33, 34, 35], {}),
    "momentum_continuation": (*ALL["mc_flow"][:2], ALL["mc_flow"][2]),
    "ema_trend_continuation": (*ALL["etc_flow"][:2], ALL["etc_flow"][2]),
    "volatility_expansion": (*ALL["ve_flow"][:2], ALL["ve_flow"][2]),
    "opening_range_breakout": (*ALL["orb_flow"][:2], ALL["orb_flow"][2]),
    "failed_breakout": (*ALL["fb_flow"][:2], ALL["fb_flow"][2]),
    "range_bounce": (*ALL["rb_flow"][:2], ALL["rb_flow"][2]),
    TER: (ter_ctx, list(range(47, 52)), {}),
}
FLOWS = {k: (b, list(t), kw) for k, (b, t, kw) in FLOWS.items()}
NAMES = list(FLOWS)


def flow(name, side, **flags):
    """The final decision payload (trading.present) of that strategy's own scenario."""
    build, throughs, kw = FLOWS[name]
    return decision_after(build, throughs, side, **flags, **kw)


def mutate(md, kind, fn):
    """Change the last quote of every watched contract of one side."""
    for c in md.contracts:
        if c.kind == kind and c.token in md.latest:
            md.latest[c.token] = fn(md.latest[c.token])


def selection(md, payload, now=NOW):
    return md.options_view(payload, now)["selection"]


def none_selected(sel):
    return sel["actionable"] is False and sel["contract"] is None and sel["alternatives"] == [] and sel["why"] == []


# ====================================================================================================================
# 1. All ten strategies reach the existing pipeline, and the same one
# ====================================================================================================================
def test_the_ten_strategies_are_exactly_the_registered_ones():
    assert len(NAMES) == 10 and set(NAMES) == set(registry.NAMES)  # a strategy added to the registry without a flow here fails this on purpose


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_each_strategys_buy_selects_a_contract_of_its_own_side_and_keeps_its_name_and_setup_id(name, side):
    md, _ = live_engine()
    out = flow(name, side)
    d = out["decision"]
    assert d["action"] == ACTION[side] and d["strategy"] == name and d["setup_id"].startswith(f"{name}:{side}:") and d["strategy_name"] == registry.NAMES[name]
    v = md.options_view(out, NOW)
    kind, sel = KIND[side], v["selection"]
    assert sel["actionable"] and sel["kind"] == kind and sel["contract"]["kind"] == kind and not sel["contract"]["blocking"] and sel["contract"]["freshness"] == "fresh"
    assert all(a["kind"] == kind and not a["blocking"] for a in sel["alternatives"]) and len(sel["alternatives"]) <= 2  # never the other side, never a blocked one
    tokens = [sel["contract"]["token"]] + [a["token"] for a in sel["alternatives"]]
    assert len(set(tokens)) == len(tokens) and set(tokens) <= {c.token for c in md.contracts if c.kind == kind}  # one selection, no duplicate, only from the watched contracts of that side
    assert {k: v["decision"][k] for k in ("action", "headline", "strategy", "strategy_name", "setup_id")} == {k: d[k] for k in ("action", "headline", "strategy", "strategy_name", "setup_id")}
    assert v["decision"]["also"] == d["also"]  # the decision is carried exactly as it is: who made it, which setup, who agrees


def test_every_strategy_gets_exactly_the_same_selection_for_the_same_action_there_is_one_implementation():
    md, _ = live_engine()
    for side in SIDES:
        picked = {name: selection(md, flow(name, side)) for name in NAMES}
        first = picked[NAMES[0]]
        assert all(sel == first for sel in picked.values())  # whichever strategy made it, the selection is identical
        assert first == op.select(md.options_view({"decision": {"action": ACTION[side]}}, NOW)["contracts"], ACTION[side])
    src = (ROOT / "app" / "options.py").read_text(encoding="utf-8")
    assert not [n for n in NAMES if n in src]  # options.py has never heard of a strategy
    body = inspect.getsource(MarketData.options_view)
    assert '"setups"' not in body and '"strategies"' not in body and "scans" not in body and "counter_trend" not in body  # and the engine's options view reads the decision only


def test_the_option_window_watches_both_sides_so_either_decision_can_be_served():
    md, _ = live_engine()
    kinds = {}
    for c in md.contracts:
        if c.kind:
            kinds.setdefault((c.expiry, c.strike), set()).add(c.kind)
    assert kinds and all(k == {"CE", "PE"} for k in kinds.values())  # every watched strike and expiry has a call and a put
    subscribed = {t for g in token_groups([c for c in md.contracts if c.kind]) for t in g["tokens"]}
    assert subscribed == {c.token for c in md.contracts if c.kind}  # and the feed subscribes all of them
    for side in SIDES:
        sel = selection(md, flow("trend_pullback", side))
        assert sel["contract"]["token"] in subscribed and all(a["token"] in subscribed for a in sel["alternatives"])


# ====================================================================================================================
# 2. The final decision, not a scanner, controls the selection
# ====================================================================================================================
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("what,flags,action", [
    ("untrusted data", {"data_ok": False, "issues": ["historical data is stale: today's refresh has not completed"]}, "NO TRADE"),
    ("a stale price tick", {"tick_age": 60.0}, "WAIT"),
    ("unusually high volatility", {"vol_extreme": True}, "NO TRADE"),
])  # fmt: skip
def test_a_stale_or_untrusted_market_gives_no_actionable_contract_for_any_strategy(name, side, what, flags, action):
    md, _ = live_engine()
    out = flow(name, side, **flags)
    assert out["decision"]["action"] == action, what
    sel = selection(md, out)
    assert none_selected(sel) and f"Phase 3 says {action}" in sel["notice"] and "information only" in sel["notice"] and sel["kind"] is None
    v = md.options_view(out, NOW)
    assert v["available"] and any(r["status"] == "fresh" for r in v["contracts"])  # the table is still there to read: informational only


@pytest.mark.parametrize("hour,minute,action", [(8, 0, "NO TRADE"), (9, 20, "WAIT"), (14, 50, "NO TRADE"), (16, 0, "NO TRADE")])
@pytest.mark.parametrize("name", NAMES)
def test_outside_the_entry_session_no_strategy_can_drive_a_contract(name, hour, minute, action):
    md, _ = live_engine()
    d = decide([sd(name, "CE")], market("trend_up", "bullish", "bullish"), env((hour, minute)))
    assert d["action"] == action and d["setup_id"] is None
    assert none_selected(selection(md, {"decision": d}))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_an_invalidated_or_expired_setup_cannot_drive_a_selection(name, side):
    md, _ = live_engine()
    build, throughs, kw = FLOWS[name]
    for status in (INVALIDATED, EXPIRED):
        book, steps = play(build, throughs, side, **kw)
        ctx, core = steps[-1]
        assert look(ctx, core)["decision"]["action"] == ACTION[side]  # confirmed and actionable...
        for s in list(book.active.values()):
            book._finish(s, status, "ended for the test", ctx.now)  # ... until the book ends it: every live setup
        after = look(ctx, {"setups": list(book.active.values()), "recent": list(book.recent), "events": list(book.events), "scans": core["scans"]})
        assert not after["decision"]["action"].startswith("BUY") and after["decision"]["setup_id"] is None, status
        assert none_selected(selection(md, after))
        assert not any(x["actionable"] for x in after["setups"]) and {x["status"] for x in after["recent"]} <= {INVALIDATED, EXPIRED}


@pytest.mark.parametrize("key,status", [
    ("mc_invalidated", INVALIDATED), ("ve_end", INVALIDATED), ("orb_invalidated", INVALIDATED), ("fb_end", INVALIDATED), ("mc_chase", EXPIRED), ("orb_chase", EXPIRED),
])  # fmt: skip
@pytest.mark.parametrize("side", SIDES)
def test_a_setup_that_really_ends_in_its_scenario_gives_no_contract(key, status, side):
    md, _ = live_engine()
    build, throughs, kw = ALL[key]
    book, steps = play(build, list(throughs), side, **kw)
    out = look(*steps[-1])
    assert any(s.status == status for s in book.recent)  # the scenario's own ending
    assert not out["decision"]["action"].startswith("BUY") and none_selected(selection(md, out))


@pytest.mark.parametrize("side", SIDES)
def test_the_reversal_ending_by_its_own_rules_gives_no_contract(side):
    md, _ = live_engine()
    rows = {52: (24690, 24690, 24655, 24658)}  # closes under the invalidation 24,659.5
    out = decision_after(ter_ctx, range(47, 53), side, rows=rows)
    assert not out["decision"]["action"].startswith("BUY") and none_selected(selection(md, out))
    out = decision_after(ter_ctx, range(47, 56), side, rows={i: (24690, 24692, 24684, 24686) for i in range(52, 56)})  # the entry window passes
    assert not out["decision"]["action"].startswith("BUY") and none_selected(selection(md, out))


def test_a_scanners_confirmed_setups_are_not_a_decision_the_final_decision_is():
    md, _ = live_engine()
    ce, pe = sd("trend_pullback", "CE"), sd("range_bounce", "PE")  # both confirmed and actionable on their own, in a market where both sides are allowed
    d = decide([ce, pe], market("range", "sideways", "sideways"))
    assert d["action"] == "WAIT" and "disagree" in d["headline"]
    assert none_selected(selection(md, {"decision": d, "setups": [ce, pe]}))  # the options view is given the setups too, and takes no notice
    only = decide([ce], market("range", "sideways", "sideways"))
    assert only["action"] == "BUY CE" and selection(md, {"decision": only, "setups": [ce, pe]})["kind"] == "CE"  # the same setups, a different decision, a different answer


@pytest.mark.parametrize("a,b", list(permutations(NAMES, 2)))  # (a strategy holds one setup per side, and the same strategy on both sides is the strategies' own business)
def test_conflicting_calls_and_puts_from_any_two_strategies_give_wait_and_no_contract(a, b):
    md, _ = live_engine()
    d = decide([sd(a, "CE"), sd(b, "PE")], market("range", "sideways", "sideways"))
    assert d["action"] == "WAIT" and "disagree" in d["headline"] and d["strategy"] is None
    assert none_selected(selection(md, {"decision": d}))


# ====================================================================================================================
# 3. Counter-trend permission, through the final decision
# ====================================================================================================================
@pytest.mark.parametrize("name", [n for n in NAMES if n != TER])
@pytest.mark.parametrize("direction,trend", [("CE", ("trend_down", "bearish", "sideways")), ("PE", ("trend_up", "bullish", "sideways"))])
def test_a_setup_against_the_higher_timeframes_gives_no_contract_unless_it_holds_the_permission(name, direction, trend):
    md, _ = live_engine()
    d = decide([sd(name, direction)], market(*trend))
    assert not d["action"].startswith("BUY") and any("ignored" in x for x in d["against"]) and none_selected(selection(md, {"decision": d}))
    claim = decide([sd(name, direction, counter=GRANTED)], market(*trend))  # a strategy that is not the registered one cannot hold it, whatever its setup claims
    assert not claim["action"].startswith("BUY") and none_selected(selection(md, {"decision": claim}))


@pytest.mark.parametrize("direction,trend,kind", [("CE", ("trend_down", "bearish", "sideways"), "CE"), ("PE", ("trend_up", "bullish", "sideways"), "PE")])
def test_the_reversal_with_a_granted_permission_selects_a_contract_of_its_own_side(direction, trend, kind):
    md, _ = live_engine()
    right = {**GRANTED, "exhausted_trend": "bearish" if direction == "CE" else "bullish"}
    d = decide([sd(TER, direction, counter=right)], market(*trend))
    assert d["action"] == f"BUY {kind}" and d["strategy"] == TER
    sel = selection(md, {"decision": d})
    assert sel["actionable"] and sel["contract"]["kind"] == kind
    denied = decide([sd(TER, direction, counter={**right, "granted": False})], market(*trend))
    assert not denied["action"].startswith("BUY") and none_selected(selection(md, {"decision": denied}))
    wrong = decide([sd(TER, direction, counter={**right, "exhausted_trend": "bullish" if direction == "CE" else "bearish"})], market(*trend))  # a record made for the other direction
    assert not wrong["action"].startswith("BUY") and none_selected(selection(md, {"decision": wrong}))


@pytest.mark.parametrize("side", SIDES)
def test_the_reversal_through_the_whole_pipeline_gives_a_contract_only_while_its_permission_holds(side):
    md, _ = live_engine()
    with_permission = flow(TER, side)
    assert with_permission["decision"]["strategy"] == TER and selection(md, with_permission)["contract"]["kind"] == KIND[side]
    build, throughs, kw = FLOWS[TER]
    no_weakening = decision_after(build, throughs, side, **{**kw, "weak": "none"})  # the facts do not name a weakening trend: no permission, no setup, no contract
    assert not no_weakening["decision"]["action"].startswith("BUY") and none_selected(selection(md, no_weakening))
    no_structure = decision_after(build, throughs, side, **{**kw, "swings": [("H", 1.0, 2.0)]})
    assert not no_structure["decision"]["action"].startswith("BUY") and none_selected(selection(md, no_structure))


# ====================================================================================================================
# 4. Several strategies agreeing
# ====================================================================================================================
@pytest.mark.parametrize("direction,trend", [("CE", ("trend_up", "bullish", "bullish")), ("PE", ("trend_down", "bearish", "bearish"))])
@pytest.mark.parametrize("a,b", list(combinations(NAMES, 2)))  # each unordered pair once
def test_two_strategies_that_agree_give_one_selection_for_the_one_the_decision_picked(a, b, direction, trend):
    md, _ = live_engine()
    both = [sd(a, direction), sd(b, direction)]
    d = decide(both, market(*trend))
    winner, other = sorted((a, b), key=registry.PRIORITY.index)  # the existing tie-break, untouched
    assert d["action"] == f"BUY {direction}" and d["strategy"] == winner and d["setup_id"] == f"{winner}:{direction}"
    assert any(registry.NAMES[other] in x and f"same {direction} opportunity" in x for x in d["also"])  # the other is kept as supporting information
    v = md.options_view({"decision": d}, NOW)
    alone = md.options_view({"decision": decide([sd(winner, direction)], market(*trend))}, NOW)
    assert v["selection"] == alone["selection"] and v["selection"]["contract"]["kind"] == direction  # one selection, exactly what the winner alone would get
    assert (v["decision"]["strategy"], v["decision"]["setup_id"], v["decision"]["also"]) == (winner, f"{winner}:{direction}", d["also"])


def test_the_tie_break_order_is_the_registrys_and_the_reversal_comes_last():
    assert registry.PRIORITY[:3] == ["breakout_retest", "trend_pullback", "range_reversal"] and registry.PRIORITY[-1] == TER and len(registry.PRIORITY) == 10


def test_three_agreeing_strategies_are_one_selection_and_the_others_are_listed_once_each():
    md, _ = live_engine()
    trio = ["range_bounce", TER, "trend_pullback"]
    d = decide([sd(n, "CE", counter=({**GRANTED, "exhausted_trend": "bearish"} if n == TER else None)) for n in trio], market("trend_up", "bullish", "bullish"))
    assert d["strategy"] == "trend_pullback" and len(d["also"]) == 2 and {registry.NAMES[n] for n in ("range_bounce", TER)} == {x.split(" (")[0] for x in d["also"]}
    v = md.options_view({"decision": d}, NOW)
    assert v["decision"]["also"] == d["also"] and v["selection"]["actionable"] and isinstance(v["selection"]["contract"], dict)


# ====================================================================================================================
# 5. The suitability checks: every Avoid blocks, every Warn does not, nothing is relaxed
# ====================================================================================================================
def test_the_option_thresholds_are_the_documented_defaults_nothing_was_relaxed():
    assert op.OptionParams() == op.OptionParams(
        strikes_each_side=3, expiries=2, recentre_hysteresis=0.75, quote_stale_seconds=30.0, exchange_stale_seconds=30.0, warn_spread_pct=1.0, max_spread_pct=3.0, min_volume=100_000,
        min_open_interest=100_000, min_top_quantity_lots=1.0, min_relative_volume=0.10, far_from_atm_steps=2, decay_warning_days=1, intrinsic_tolerance=1.0)  # fmt: skip


def stamp(c, **kw):
    return replace(quote(c, 150.0), **kw)


def single(c, t, **kw):
    rows = analyse([(c, t)], **kw)["contracts"]
    return rows[0], op.select(rows, "BUY CE")


C = contract(24500)
ITM = contract(24400)
AVOID = {  # code -> (the contract and its quote, what analyse() is given besides)
    "no_quote": (C, None, {}),
    "stale_quote": (C, quote(C, 150.0, age=60.0), {}),
    "exchange_time_stale": (C, stamp(C, exch_ts=ist(10, 0, 30) - 120), {}),
    "exchange_time_unknown": (C, stamp(C, exch_ts=None, exch_note="the exchange timestamp was not provided"), {}),
    "no_bid_ask": (C, quote(C, 150.0, bid=None, ask=None), {}),
    "crossed_book": (C, quote(C, 150.0, bid=151.0, ask=150.0), {}),
    "wide_spread": (C, quote(C, 150.0, bid=145.0, ask=155.0), {}),
    "below_intrinsic": (ITM, quote(ITM, 50.0), {}),  # a 24,400 call is worth at least 110 with the spot at 24,510
    "no_liquidity_info": (C, quote(C, 150.0, vol=None, oi=None, bq=None, aq=None), {}),
    "low_volume": (C, quote(C, 150.0, vol=50_000), {}),
    "market_closed": (C, quote(C, 150.0), {"now": ist(16, 0)}),
    "spot_stale": (C, quote(C, 150.0), {"spot_age": 90.0}),
}  # fmt: skip


@pytest.mark.parametrize("code", AVOID)
def test_each_avoid_finding_keeps_a_contract_from_being_selected_and_a_clean_neighbour_is_chosen_instead(code):
    c, t, kw = AVOID[code]
    r, sel = single(c, t, **kw)
    assert code in codes(r, "avoid") and r["blocking"], codes(r)
    assert none_selected(sel) and sel["rejected"] and sel["rejected"][0]["reasons"], sel
    if code in ("market_closed", "spot_stale"):
        return  # these make every contract unusable: there is no neighbour to fall back on
    good = contract(24550)
    rows = analyse([(c, t), (good, quote(good, 100.0))], **kw)["contracts"]
    chosen = op.select(rows, "BUY CE")
    assert chosen["contract"]["strike"] == 24550.0 and chosen["alternatives"] == []  # the avoided contract is not even an alternative
    assert all(not r["blocking"] for r in [chosen["contract"], *chosen["alternatives"]])


WARN = {  # code -> (the contracts and quotes to analyse, the one that carries the finding)
    "spread_high": ([(C, quote(C, 150.0, bid=149.2, ask=150.8))], C),
    "no_volume": ([(C, quote(C, 150.0, vol=None))], C),
    "no_oi": ([(C, quote(C, 150.0, oi=None))], C),
    "low_oi": ([(C, quote(C, 150.0, oi=50_000))], C),
    "thin_top_quantity": ([(C, quote(C, 150.0, bq=10, aq=10))], C),
    "expiry_day": ([(contract(24500, expiry=date(2026, 11, 6)), None)], None),
    "near_expiry": ([(contract(24500, expiry=date(2026, 11, 7)), None)], None),
}  # fmt: skip


@pytest.mark.parametrize("code", ["spread_high", "no_volume", "no_oi", "low_oi", "thin_top_quantity", "expiry_day", "near_expiry"])
def test_a_warning_is_shown_but_never_blocks_a_contract(code):
    pairs, _ = WARN[code]
    if code in ("expiry_day", "near_expiry"):
        c = pairs[0][0]
        pairs = [(c, quote(c, 150.0))]
    r, sel = single(*pairs[0])
    assert code in codes(r, "warn") and not codes(r, "avoid") and not r["blocking"], codes(r)
    assert sel["actionable"] and sel["contract"]["token"] == r["token"] and r["warnings"] >= 1


def test_the_warnings_that_depend_on_the_neighbours_do_not_block_either():
    busy, thin = contract(24500), contract(24550)
    rows = analyse([(busy, quote(busy, 150.0, vol=3_000_000)), (thin, quote(thin, 100.0, vol=200_000))])["contracts"]
    r = next(x for x in rows if x["strike"] == 24550)
    assert "thin_relative_volume" in codes(r, "warn") and not r["blocking"] and op.select(rows, "BUY CE")["actionable"]
    lo, hi = contract(24450), contract(24500)
    rows = analyse([(lo, quote(lo, 70.0)), (hi, quote(hi, 90.0))])["contracts"]  # a call that costs more at the higher strike (both above their intrinsic value: only the neighbours disagree)
    assert all("premium_inconsistent" in codes(x, "warn") and not x["blocking"] for x in rows)
    strikes = [24200, 24300, 24400, 24500, 24600]
    rows = analyse([(contract(k), quote(contract(k), 320.0 - (k - 24200) * 0.7)) for k in strikes])["contracts"]
    far = next(x for x in rows if x["strike"] == 24200)
    assert "far_from_atm" in codes(far, "warn") and not far["blocking"]  # three strikes from the money: a warning, not a block


def test_an_avoid_contract_is_never_selected_or_offered_as_an_alternative_whatever_else_is_in_the_chain():
    good, also, bad = contract(24550), contract(24450), contract(24500)
    for code in AVOID:
        c, t, kw = AVOID[code]
        pairs = [(good, quote(good, 100.0)), (also, quote(also, 180.0)), (bad, t) if c.strike == 24500 else (contract(24500), quote(contract(24500), 150.0))]
        rows = analyse(pairs, **kw)["contracts"]
        for action in ("BUY CE", "BUY PE"):
            sel = op.select(rows, action)
            for x in ([sel["contract"]] if sel["contract"] else []) + sel["alternatives"]:
                assert not x["blocking"] and x["kind"] == action[-2:]


# the engine, on every strategy's decision: the same findings hold on the live quotes
AVOID_ON_THE_SIDE = {
    "stale quote": (lambda t: replace(t, recv_ts=t.recv_ts - 60), "limit 30 s"),
    "exchange time stale": (lambda t: replace(t, exch_ts=t.exch_ts - 120), "exchange-time age"),
    "exchange time unknown": (lambda t: replace(t, exch_ts=None, exch_note="the exchange timestamp was not provided"), "cannot be known"),
    "no bid": (lambda t: replace(t, bid=None, bid_qty=None), "not provided"),
    "no ask": (lambda t: replace(t, ask=None, ask_qty=None), "not provided"),
    "crossed book": (lambda t: replace(t, bid=t.ask + 1.0, ask=t.bid), "bid is above the ask"),
    "wide spread": (lambda t: replace(t, bid=round(t.ltp * 0.9, 2), ask=round(t.ltp * 1.1, 2)), "over the maximum"),
    "no liquidity information": (lambda t: replace(t, volume=None, oi=None, bid_qty=None, ask_qty=None), "liquidity is unknown"),
    "low volume": (lambda t: replace(t, volume=50_000), "under the minimum"),
}  # fmt: skip


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("case", list(AVOID_ON_THE_SIDE))
def test_an_avoid_finding_on_every_contract_of_the_side_leaves_nothing_to_select_and_the_other_side_alone(name, side, case):
    md, _ = live_engine()
    out = flow(name, side)
    kind, other = KIND[side], KIND["bear" if side == "bull" else "bull"]
    fn, words = AVOID_ON_THE_SIDE[case]
    mutate(md, kind, fn)
    sel = selection(md, out)
    assert sel["actionable"] is False and sel["contract"] is None and sel["alternatives"] == [] and "no suitable" in sel["notice"]
    assert sel["rejected"] and all(any(words in x for x in r["reasons"]) for r in sel["rejected"])  # why, in the data's own words
    assert all(r["blocking"] for r in md.options_view(out, NOW)["contracts"] if r["kind"] == kind)
    flipped = {"decision": {**out["decision"], "action": f"BUY {other}"}}
    assert selection(md, flipped)["actionable"] and selection(md, flipped)["contract"]["kind"] == other  # the other side's quotes were not touched


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_the_selected_contract_moves_off_a_contract_that_goes_bad_and_never_onto_a_blocked_one(name, side):
    md, _ = live_engine()
    out = flow(name, side)
    first = selection(md, out)["contract"]
    md.latest[first["token"]] = replace(md.latest[first["token"]], bid=None, bid_qty=None)  # the selected contract loses its bid
    again = selection(md, out)
    assert again["actionable"] and again["contract"]["token"] != first["token"] and again["contract"]["kind"] == KIND[side]
    assert all(x["token"] != first["token"] for x in [again["contract"], *again["alternatives"]])


WARN_ON_THE_SIDE = {
    "no open interest": (lambda t: replace(t, oi=None), "no_oi"),
    "low open interest": (lambda t: replace(t, oi=50_000), "low_oi"),
    "no day volume": (lambda t: replace(t, volume=None), "no_volume"),
    "thin top quantity": (lambda t: replace(t, bid_qty=10, ask_qty=10), "thin_top_quantity"),
    "a wide-ish spread": (lambda t: replace(t, bid=round(t.ltp * 0.992, 2), ask=round(t.ltp * 1.008, 2)), "spread_high"),
}  # fmt: skip


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("case", list(WARN_ON_THE_SIDE))
def test_a_warning_on_every_contract_of_the_side_still_offers_one_with_the_warning_shown(name, side, case):
    md, _ = live_engine()
    fn, code = WARN_ON_THE_SIDE[case]
    mutate(md, KIND[side], fn)
    sel = selection(md, flow(name, side))
    assert sel["actionable"] and not sel["contract"]["blocking"]
    flags = {f["code"]: f["severity"] for f in sel["contract"]["flags"]}
    assert flags.get(code) == "warn" and "avoid" not in flags.values()  # the warning is on the contract it is shown for, as a warning


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_a_closed_market_or_a_stale_spot_leaves_no_actionable_contract_for_any_strategy(name, side):
    md, _ = live_engine()
    out = flow(name, side)
    closed = md.options_view(out, ist(18, 0))
    assert none_selected(closed["selection"]) and closed["live"] is False and all("market_closed" in codes(r, "avoid") for r in closed["contracts"])
    stale = md.options_view(out, ist(10, 1, 30))  # the spot last ticked at 10:00:20
    assert none_selected(stale["selection"]) and stale["spot_fresh"] is False and all(r["blocking"] for r in stale["contracts"])


# ====================================================================================================================
# 6. REST, WebSocket and the dashboard
# ====================================================================================================================
def served(name, side):
    md, clock = live_engine()
    clock.t = NOW
    payload = flow(name, side)
    md.safe_trading = lambda: payload  # the engine serves the final decision it is given, exactly as trading() would
    app = create_app(md, feed=None)
    app.state.md = md
    return md, app, payload


async def first_push(app):
    sent = []

    class Client:
        async def accept(self):
            pass

        async def send_text(self, text):
            sent.append(json.loads(text))

        async def receive_text(self):
            await asyncio.sleep(3600)

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(route(app, "/ws").endpoint(Client()))
        await asyncio.sleep(0.15)
        task.cancel()
    return sent[0]


def canonical(x):
    return json.loads(json.dumps(x, allow_nan=False))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_rest_websocket_and_state_carry_the_same_decision_and_the_same_contract(name, side):
    _, app, payload = served(name, side)
    rest = canonical(route(app, "/api/options").endpoint())
    state = canonical(route(app, "/api/state").endpoint())
    ws = asyncio.run(first_push(app))
    d = payload["decision"]
    for options in (rest, state["options"], ws["options"]):
        assert options["decision"] == {"action": d["action"], "headline": d["headline"], "strategy": name, "strategy_name": d["strategy_name"], "setup_id": d["setup_id"], "also": d["also"]}
        assert options["selection"]["actionable"] and options["selection"]["kind"] == KIND[side] and options["selection"]["contract"]["kind"] == KIND[side]
    assert rest == state["options"] == ws["options"]  # one state, three ways to read it
    assert state["trading"]["decision"]["setup_id"] == ws["trading"]["decision"]["setup_id"] == rest["decision"]["setup_id"]  # and it is the trading payload's decision
    tokens = lambda o: [o["selection"]["contract"]["token"]] + [a["token"] for a in o["selection"]["alternatives"]]  # noqa: E731
    assert tokens(rest) == tokens(state["options"]) == tokens(ws["options"])


@pytest.mark.parametrize("action", ["WAIT", "NO TRADE"])
def test_wait_and_no_trade_are_informational_in_every_way_the_api_serves_them(action):
    md, clock = live_engine()
    clock.t = NOW
    payload = decision_after(*FLOWS["trend_pullback"][:2], "bull", **FLOWS["trend_pullback"][2], **({"tick_age": 60.0} if action == "WAIT" else {"data_ok": False, "issues": ["x"]}))
    assert payload["decision"]["action"] == action
    md.safe_trading = lambda: payload
    app = create_app(md, feed=None)
    app.state.md = md
    rest, state = canonical(route(app, "/api/options").endpoint()), canonical(route(app, "/api/state").endpoint())
    ws = asyncio.run(first_push(app))
    for o in (rest, state["options"], ws["options"]):
        assert o["available"] and o["selection"]["actionable"] is False and o["selection"]["contract"] is None and o["selection"]["kind"] is None
        assert "no options selection" in o["selection"]["notice"] and "information only" in o["selection"]["notice"] and len(o["contracts"]) == 28  # the table is still served
        assert o["decision"]["action"] == action


def test_without_a_watched_option_window_a_buy_selects_nothing_and_says_so_everywhere():
    md = MarketData(CFG, Clock(ist(10, 0)))
    payload = flow("trend_pullback", "bull")
    md.safe_trading = lambda: payload
    assert md.options_view(payload, ist(10, 0)) == {"available": False, "reason": "no option contracts are selected yet"}
    assert md.options_brief(payload, ist(10, 0)) is None  # and the journal records no contract
    app = create_app(md, feed=None)
    app.state.md = md
    assert route(app, "/api/options").endpoint() == {"available": False, "reason": "no option contracts are selected yet"}
    assert md.snapshot()["options"] == {"available": False, "reason": "no option contracts are selected yet"}


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_the_journal_brief_is_the_selected_contract_of_the_decisions_side(name, side):
    md, _ = live_engine()
    brief = md.options_brief(flow(name, side), NOW)
    assert brief["kind"] == KIND[side] and brief["symbol"] and brief["status"] == "fresh" and all(f["severity"] in ("avoid", "warn") for f in brief["flags"])
    assert not any(f["severity"] == "avoid" for f in brief["flags"])


HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [, , htmlPath, snapPath, tab] = process.argv;
const html = fs.readFileSync(htmlPath, "utf8"), snap = JSON.parse(fs.readFileSync(snapPath, "utf8"));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {};
const el = id => els[id] || (els[id] = {id, innerHTML: "", textContent: "", className: "", hidden: false, style: {setProperty() {}}, dataset: {}, offsetHeight: 0, value: "",
  classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }}, setAttribute() {}, getAttribute() { return null; }, addEventListener() {}, focus() {},
  querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; }});
const document = {getElementById: el, addEventListener() {}, querySelectorAll() { return []; }, querySelector() { return null; }, documentElement: el("html")};
class WebSocket { constructor() { this.readyState = 3; } }
const ctx = {document, WebSocket, location: {protocol: "http:", host: "x", hash: ""}, history: {replaceState() {}}, console: {log() {}, error() {}}, setTimeout() {}, clearTimeout() {},
  fetch: () => Promise.reject(new Error("no network")), addEventListener() {}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(script, ctx);
vm.runInContext(`TAB = ${JSON.stringify(tab)}; render(${JSON.stringify(snap)});`, ctx);
console.log(JSON.stringify({head: els["opt-head"].innerHTML, sel: els["opt-sel"].innerHTML, table: els["opt-table"].innerHTML, err: els["jserr"].textContent}));
"""


def rendered(tmp_path, md, payload):
    """The Options tab as the page's own script draws it from the snapshot the server would push (the page's script run under node with a stand-in for the DOM)."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the dashboard was not rendered")
    md.safe_trading = lambda: payload
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "snap.json").write_text(json.dumps(md.snapshot(), allow_nan=False), encoding="utf-8")
    done = subprocess.run([node, str(tmp_path / "harness.js"), str(ROOT / "app" / "static" / "index.html"), str(tmp_path / "snap.json"), "options"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[:600]
    out = json.loads(done.stdout)
    assert out["err"] == "", out["err"]  # the page's own error line is empty: nothing failed while drawing
    return out


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_the_options_tab_shows_the_side_the_originating_strategy_the_setup_and_a_consistent_contract(tmp_path, name, side):
    md, clock = live_engine()
    clock.t = NOW
    payload = flow(name, side)
    page = rendered(tmp_path, md, payload)
    d, kind = payload["decision"], KIND[side]
    sel = md.options_view(payload, NOW)["selection"]
    assert f"<b>{d['action']}</b>" in page["head"] and f"Option side <b>{kind}</b>" in page["head"]
    assert f"From <b>{d['strategy_name'].replace('&', '&amp;')}</b>" in page["head"] and f"setup {d['setup_id']}" in page["head"]  # who made the decision, and which setup
    chosen = sel["contract"]
    assert f"Selected contract" in page["sel"] and f"{kind} {chosen['strike']:g}</span>" in page["sel"] and chosen["symbol"] in page["sel"]
    assert ("big up" if kind == "CE" else "big down") in page["sel"] and ("No suitability warnings." in page["sel"] or "Suitability findings" in page["sel"])
    assert "Alternatives" in page["sel"] and all(f'<td class="l">{kind} {a["strike"]:g}</td>' in page["sel"] for a in sel["alternatives"]) and "No contract selected" not in page["sel"]
    rows = page["table"].count("<tr class=")
    assert rows == 14 and page["table"].count(f'<td class="l">{kind}</td>') == 14 and f'<td class="l">{"PE" if kind == "CE" else "CE"}</td>' not in page["table"]  # only the decision's side
    assert page["table"].count('<tr class="opt-sel">') == 1  # the selected contract is the one highlighted in the table


def test_the_options_tab_lists_the_strategies_that_agree_as_supporting_information_beside_the_one_selection(tmp_path):
    md, clock = live_engine()
    clock.t = NOW
    payload = flow("trend_pullback", "bull")
    payload["decision"]["also"] = ["Range Bounce (same CE opportunity)", "Momentum Continuation (same CE opportunity)"]
    page = rendered(tmp_path, md, payload)
    assert "also matches: Range Bounce (same CE opportunity), Momentum Continuation (same CE opportunity)" in page["head"] and page["sel"].count("Selected contract") == 1
    assert page["head"].count("From <b>") == 1 and "From <b>Trend Pullback</b>" in page["head"]  # the one that was picked, once
    quiet = rendered(tmp_path, md, flow("trend_pullback", "bull"))
    assert "also matches" not in quiet["head"]


@pytest.mark.parametrize("action,flags", [("WAIT", {"tick_age": 60.0}), ("NO TRADE", {"data_ok": False, "issues": ["historical data is stale: today's refresh has not completed"]})])
def test_wait_and_no_trade_are_plainly_informational_on_the_options_tab(tmp_path, action, flags):
    md, clock = live_engine()
    clock.t = NOW
    payload = flow("trend_pullback", "bull", **flags)
    page = rendered(tmp_path, md, payload)
    assert payload["decision"]["action"] == action and f"<b>{action}</b>" in page["head"] and "Option side <b>none</b>" in page["head"] and "From <b>" not in page["head"]
    if payload["decision"]["strategy_name"]:  # a WAIT about a setup (confirmed but not actionable) names it as a setup in view, never as the source of a contract
        assert "Setup in view (not a BUY, so no contract): <b>Trend Pullback</b>" in page["head"] and f"setup {payload['decision']['setup_id']}" in page["head"]
    assert "No contract selected" in page["sel"] and "informational: only a BUY CE / BUY PE decision selects a contract" in page["sel"]
    assert f"Phase 3 says {action}" in page["sel"] and "The table below is for information only" in page["sel"] and "Selected contract" not in page["sel"] and 'class="big' not in page["sel"]
    assert page["table"].count("<tr class=") == 28 and "opt-sel" not in page["table"]  # both sides are listed to read, none is picked


def test_when_no_contract_is_fit_the_options_tab_says_which_findings_ruled_each_one_out(tmp_path):
    md, clock = live_engine()
    clock.t = NOW
    mutate(md, "CE", lambda t: replace(t, bid=None, bid_qty=None))
    page = rendered(tmp_path, md, flow("trend_pullback", "bull"))
    assert "From <b>Trend Pullback</b>" in page["head"] and "Setup in view" not in page["head"]  # it is still a BUY made by that strategy: only the contract is missing
    assert "No contract selected" in page["sel"] and "no suitable CE contract is available" in page["sel"] and page["sel"].count("the bid or the ask is not provided") >= 3
    assert "opt-sel" not in page["table"] and page["table"].count("opt-avoid") == 14  # every call is marked as one that cannot be used
