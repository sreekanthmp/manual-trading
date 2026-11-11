"""Stage 3: the counter-trend exception in the decision engine, and the integration of the regime facts and Trend Exhaustion Reversal with the journal, the REST and WebSocket
payloads and the options layer.

The rule under test: a setup that runs against the 1H or 15M trend is eligible only if its strategy is registered counter-trend AND its setup holds a granted permission record;
in a "conflicting" market (1H and 15M disagree, normally NO TRADE) the decision proceeds only when such a setup exists, and in an "unclear" market never. It is held in the decision
as well as in each scanner, so these tests build the setups by hand, and with a deliberately badly behaved strategy, to show that nothing but the exception can get round it."""

import asyncio
import json

import pytest
from helpers import FRI, ist
from scenarios import M, P, mirror
from test_exhaustion import TER, ter_ctx
from test_phase3_integration import TODAY, World, create_app, route
from test_trend_family import decision_after, live, play

from app import registry
from app import strategy as st
from app import trading as tr
from app.journal import Journal, read, timeline
from app.registry import WITH_TREND, StrategyDef
from app.strategy import CONFIRMED, WAITING, Level, Scan, Setup

GRANTED = {"needed": True, "granted": True, "reason": "granted: the bearish trend is weakening and the 15M structure has turned", "checks": [], "structure": {}}
DENIED = {**GRANTED, "granted": False, "reason": "denied: structure turned"}


@pytest.fixture
def clean_registry():
    saved = list(registry.REGISTRY)
    yield
    registry.REGISTRY[:] = saved
    registry._refresh()


def sd(name, direction, status=CONFIRMED, counter=None, actionable=True, price_state="in_entry_area", blockers=(), zone=(24590.0, 24600.0)):
    """A setup as the dashboard receives it, built by hand."""
    d = {"id": f"{name}:{direction}", "strategy": name, "strategy_name": registry.NAMES[name], "direction": direction, "side": "bull" if direction == "CE" else "bear", "status": status,
         "zone": list(zone), "trigger": zone[0], "invalidation": 24559.5, "confirm_text": "", "reasons": ["r"], "against": [], "blockers": list(blockers), "price_state": price_state,
         "actionable": actionable, "confirmed_bar_end": None, "stage": "BLOCKED" if blockers else None}  # fmt: skip
    if counter is not None:
        d["counter_trend"] = counter
    return d


def market(regime="trend_down", h="bearish", m="sideways", trends=True):
    out = {"direction": "bearish", "regime": regime, "text": f"the market is {regime}", "volatility": "normal", "alignment": "x", "vol_ratio": 1.0}
    return {**out, "trends": {"1h": h, "15m": m}} if trends else out


def env(phase_at=(11, 0), **over):
    base = {"session": tr.session_info(ist(*phase_at, day=FRI), P), "data": {"ok": True, "issues": []}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None}
    return {**base, **over}


MONS = [{"id": n, "name": n, "active": True, "why": "", "state": "NO SETUP", "note": "", "setups": []} for n in registry.NAMES]


def decide(setups, mkt=None, e=None):
    return tr.decide(mkt or market(), e or env(), setups, MONS, 24595.0, True, 10.0)


# ====================================================================================================================
# The decision engine
# ====================================================================================================================
def test_a_conflicting_market_is_no_trade_unless_a_counter_trend_setup_holds_a_granted_permission():
    conflicting = market("conflicting", "bearish", "bullish")
    d = decide([sd("trend_pullback", "CE")], conflicting)
    assert d["action"] == "NO TRADE" and d["headline"] == "NO TRADE: the market is conflicting"  # exactly as before: nothing changed for everybody else
    d = decide([sd(TER, "CE", counter=GRANTED)], conflicting)
    assert (d["action"], d["strategy"]) == ("BUY CE", TER)  # the exception: a granted permission
    assert decide([sd(TER, "CE", counter=DENIED)], conflicting)["action"] == "NO TRADE"  # a denied one is nothing
    assert decide([sd(TER, "CE")], conflicting)["action"] == "NO TRADE"  # and so is no record at all
    claimed = decide([sd("trend_pullback", "CE", counter=GRANTED)], conflicting)  # another strategy cannot claim the permission, whatever its setup says
    assert claimed["action"] == "NO TRADE"
    for name in ("breakout_retest", "momentum_continuation", "opening_range_breakout", "failed_breakout", "range_bounce"):
        assert decide([sd(name, "CE", counter=GRANTED)], conflicting)["action"] == "NO TRADE", name


def test_an_unclear_market_never_has_an_exception():
    unclear = market("unclear", "unknown", "unknown")
    d = decide([sd(TER, "CE", counter=GRANTED)], unclear)
    assert d["action"] == "NO TRADE" and d["headline"] == "NO TRADE: the market is unclear"


@pytest.mark.parametrize("direction,regime,h,m", [("CE", "trend_down", "bearish", "sideways"), ("PE", "trend_up", "bullish", "sideways"), ("CE", "developing", "sideways", "bearish"), ("PE", "developing", "sideways", "bullish")])
def test_in_any_regime_a_setup_against_the_1h_or_15m_counts_only_with_a_granted_permission_held_by_a_counter_trend_strategy(direction, regime, h, m):
    mkt = market(regime, h, m)
    d = decide([sd("trend_pullback", direction)], mkt)  # a strategy whose own gate failed to stop it: the decision does
    assert d["action"] == "WAIT" and d["headline"] == "WAIT: the market is tradable but there is no setup yet"
    assert any("ignored Trend Pullback" in x and "holds no counter-trend permission" in x for x in d["against"])
    d = decide([sd(TER, direction, counter=GRANTED)], mkt)
    assert d["action"] == f"BUY {direction}" and d["strategy"] == TER and not any("ignored" in x for x in d["against"])
    assert decide([sd(TER, direction, counter=DENIED)], mkt)["action"] == "WAIT"


def test_the_ignored_setup_is_explained_on_every_wait_path_not_only_the_last():
    mkt = market()
    d = decide([sd("trend_pullback", "CE"), sd(TER, "CE", status=WAITING, counter=GRANTED, actionable=False, price_state=None)], mkt)  # one ignored, one genuinely developing
    assert d["action"] == "WAIT" and "developing" in d["headline"] and any("ignored Trend Pullback" in x for x in d["against"])


def test_with_nothing_against_the_direction_every_decision_is_exactly_what_it_was():
    with_trends = decide([sd("trend_pullback", "CE")], market("trend_up", "bullish", "bullish"))
    without = decide([sd("trend_pullback", "CE")], market("trend_up", trends=False))  # an older caller that sends no trends at all
    assert with_trends == without and with_trends["action"] == "BUY CE"
    sideways = decide([sd("range_reversal", "CE"), sd("range_reversal", "PE", status=WAITING, actionable=False, price_state=None)], market("range", "sideways", "sideways"))
    assert sideways["action"] == "WAIT" and "disagree" in sideways["headline"]  # CE and PE together are still WAIT, by the existing rule


def test_a_permitted_setup_still_has_to_pass_every_ordinary_decision_rule():
    ok = sd(TER, "CE", counter=GRANTED)
    assert decide([ok], e=env(vol_extreme=True, vol_ratio=2.4))["action"] == "NO TRADE"  # extreme volatility
    assert decide([ok], e=env(data={"ok": False, "issues": ["5M candles missing"]}))["action"] == "NO TRADE"  # data not ready
    assert decide([ok], e=env((14, 50)))["action"] == "NO TRADE" and "late session" in decide([ok], e=env((14, 50)))["headline"]  # late session
    assert decide([ok], e=env((9, 20)))["action"] == "WAIT"  # the opening range is still forming
    assert decide([ok], e=env((15, 45)))["action"] == "NO TRADE"  # closed
    extended = decide([sd(TER, "CE", counter=GRANTED, actionable=False, price_state="extended")])
    assert extended["action"] == "WAIT" and "confirmed but not actionable" in extended["headline"]  # price ran away: do not chase
    blocked = decide([sd(TER, "CE", status=WAITING, counter=GRANTED, actionable=False, price_state=None, blockers=["only 8.0 pts to the next level 24,690.00"])])  # (as the engine holds it back: WAITING, stage BLOCKED)
    assert blocked["action"] == "WAIT" and "confirmed but blocked" in blocked["headline"]  # a room blocker
    d = tr.decide(market(), env(), [sd(TER, "CE", counter=GRANTED)], MONS, 24595.0, False, 10.0)
    assert d["action"] == "BUY CE"  # (a confirmed, actionable setup is decided on its own flag: the tick age is what makes setup_dict clear `actionable`, tested in the pipeline below)


def test_a_counter_trend_call_and_an_ordinary_put_disagree_and_the_existing_rule_says_wait():
    mkt = market()  # 1H bearish, 15M sideways: a put is WITH the trend, a call needs the permission
    d = decide([sd(TER, "CE", counter=GRANTED), sd("trend_pullback", "PE", status=WAITING, actionable=False, price_state=None)], mkt)
    assert d["action"] == "WAIT" and d["headline"] == "WAIT: bullish and bearish setups disagree"


def test_several_strategies_agreeing_are_shown_as_one_opportunity_with_the_others_listed():
    mkt = market("developing", "sideways", "bullish")  # nothing runs against a call here: no permission is needed, and the originals still win the tie
    d = decide([sd(TER, "CE", counter={**GRANTED, "needed": False}, zone=(24592.0, 24602.0)), sd("trend_pullback", "CE")], mkt)
    assert (d["action"], d["strategy"]) == ("BUY CE", "trend_pullback") and d["also"] == ["Trend Exhaustion Reversal (same CE opportunity)"]
    assert registry.PRIORITY[-1] == TER  # the tie-break order is the registry's, and the reversal strategy is last


# ====================================================================================================================
# The whole pipeline
# ====================================================================================================================
@pytest.mark.parametrize("side", ["bull", "bear"])
def test_a_counter_trend_reversal_qualifies_even_in_a_conflicting_market(side):
    conflicting = {"trend": {"1h": "bearish", "15m": "bullish", "5m": "sideways", "1m": "sideways"}, "alignment": "conflicting"}
    out = decision_after(ter_ctx, range(47, 52), side, **conflicting)
    d = out["decision"]
    assert out["market"]["regime"] == "conflicting" and d["action"] == ("BUY CE" if side == "bull" else "BUY PE") and d["strategy"] == TER
    assert out["counter_trend"][0]["granted"] and out["counter_trend"][0]["needed"] and out["setups"][0]["counter_trend"]["granted"]
    earlier = decision_after(ter_ctx, range(47, 51), side, **conflicting)["decision"]  # before the 5M break the same market is just a developing setup, never a BUY
    assert earlier["action"] == "WAIT"
    no = decision_after(ter_ctx, range(47, 52), side, weak="none", **conflicting)["decision"]  # no permission: back to the unchanged NO TRADE
    assert no["action"] == "NO TRADE" and decision_after(ter_ctx, range(47, 52), side, weak="none", **conflicting)["market"]["regime"] == "conflicting"


@pytest.mark.parametrize("side", ["bull", "bear"])
@pytest.mark.parametrize("change", [
    {"levels": [Level(24690.0, "swing level x2", 2)]},  # 8 pts of room for 22.5 at risk
    {"rows": {49: (24670, 24674, 24640, 24666), 50: (24666, 24672, 24640, 24668)}},  # a deep 5M pullback: 44.5 pts of risk, over 1.5x ATR15 (37.5)
    {"can_confirm": False, "blockers": ["historical data is stale: today's refresh has not completed"]},
])  # fmt: skip
def test_a_counter_trend_setup_that_fails_a_common_safety_gate_is_blocked_not_bought(side, change):
    out = decision_after(ter_ctx, range(47, 52), side, **change)
    s = next(x for x in out["setups"] if x["strategy"] == TER)  # (an added level can make another strategy's legitimate setup too: the reversal's own is what is tested)
    assert out["decision"]["action"] == "WAIT" and s["stage"] == "BLOCKED" and s["blockers"] and not s["actionable"]
    assert any(k in " ".join(s["blockers"]) for k in ("next level", "invalidation", "stale"))


@pytest.mark.parametrize("side", ["bull", "bear"])
def test_the_ordinary_safety_checks_hold_for_the_reversal_as_for_every_strategy(side):
    kind = "BUY CE" if side == "bull" else "BUY PE"
    assert decision_after(ter_ctx, range(47, 52), side)["decision"]["action"] == kind
    bad = decision_after(ter_ctx, range(47, 52), side, data_ok=False, issues=["historical data is stale: today's refresh has not completed"])["decision"]
    assert bad["action"] == "NO TRADE" and "not reliable" in bad["headline"]
    assert decision_after(ter_ctx, range(47, 52), side, vol_extreme=True)["decision"]["action"] == "NO TRADE"
    old = decision_after(ter_ctx, range(47, 52), side, tick_age=60.0)  # the live price stopped
    assert old["decision"]["action"] == "WAIT" and not any(s["actionable"] for s in old["setups"])


def test_a_badly_behaved_strategy_cannot_borrow_the_exception_even_with_the_best_priority(clean_registry):
    def rogue(v, p, prior):  # ignores the direction gate completely and always has a confirmed, actionable call at the break
        if v.side != "bull":
            return Scan(False, "bull only")
        return Scan(True, "", Setup("rogue", "bull", "rogue:bull:1793945100", CONFIRMED, 1793945100.0, trigger=24682.0, zone=(24682.0, 24692.0), invalidation=24659.5,
                                    candle_confirmed=True, confirmed_bar_end=1793945100.0, counter_trend=dict(GRANTED)))  # it even claims a granted permission

    registry.register(StrategyDef("rogue", "Rogue", rogue, WITH_TREND, priority=-5))
    assert "rogue" not in registry.COUNTER_TREND
    out = decision_after(ter_ctx, range(47, 52), "bull")
    assert [s["strategy"] for s in out["setups"]] == ["rogue", TER] or {s["strategy"] for s in out["setups"]} == {"rogue", TER}
    d = out["decision"]
    assert (d["action"], d["strategy"]) == ("BUY CE", TER) and not any("Rogue" in x for x in d["also"])  # the one with the permission is bought; the other is not even "also"
    assert any("ignored Rogue" in x for x in d["against"])
    out = decision_after(ter_ctx, range(47, 51), "bull")  # before the reversal confirms, the rogue strategy alone is not enough
    assert out["decision"]["action"] == "WAIT" and any("ignored Rogue" in x for x in out["decision"]["against"])
    out = decision_after(ter_ctx, range(47, 52), "bull", trend={"1h": "bearish", "15m": "bullish", "5m": "sideways", "1m": "sideways"}, alignment="conflicting", weak="none")
    assert out["decision"]["action"] == "NO TRADE"  # and in a conflicting market without a permission there is no way through at all


def test_the_exception_belongs_to_one_strategy_and_the_existing_gates_of_all_the_others_are_untouched():
    assert registry.COUNTER_TREND == {TER}
    ctx = ter_ctx(51)
    for (name, side), scan in st.scan_all(ctx, P, {}).items():
        if name != TER and scan.setup is not None:  # no other strategy produces a setup against the 1H bearish trend on this market
            assert side != "bull", f"{name} produced a call against a bearish 1H"
    for name in registry.NAMES:
        if name != TER:
            assert not registry.definition(name).counter_trend


# ====================================================================================================================
# The journal
# ====================================================================================================================
def observe_all(tmp_path, side="bull", throughs=range(47, 52), **kw):
    j = Journal(tmp_path)
    a = {"timeframes": {}, "condition": {}, "warnings": []}
    out = None
    for n in throughs:
        out = decision_after(ter_ctx, range(47, n + 1), side, **kw)
        out["market"] = {**out["market"], "regime_facts": ter_ctx(n).regime}  # (the helper builds no regime facts: the same shape the engine puts there)
        j.observe(out, a, [], ter_ctx(n).now)
    return out, next(tmp_path.glob("decisions-*.jsonl"))


def test_the_journal_records_the_regime_facts_the_permission_and_the_lifecycle(tmp_path):
    _, path = observe_all(tmp_path)
    records, bad = read(path)
    assert bad == [] and len(records) >= 3
    last = records[-1]
    assert last["counter_trend"][0]["granted"] and last["counter_trend"][0]["checks"] and last["counter_trend"][0]["structure"]["exhaustion_extreme"] == 24520.0
    assert last["market"]["regime_facts"]["facts"]["weakening_trend"]["state"] == "bearish" and last["market"]["trends"] == {"1h": "bearish", "15m": "sideways"}  # the facts are in the record
    assert last["setups"][0]["counter_trend"]["granted"] and last["decision"]["strategy"] == TER
    text = "\n".join(timeline(path, detail=True))
    assert "counter-trend CE Trend Exhaustion Reversal: permission granted" in text and "✓ weakening continuation" in text and "regime facts:" in text
    assert "BUY CE: Trend Exhaustion Reversal setup confirmed" in "\n".join(timeline(path))


def test_a_change_of_permission_is_worth_a_line_of_its_own(tmp_path):
    j = Journal(tmp_path)
    a = {"timeframes": {}, "condition": {}, "warnings": []}
    out = decision_after(ter_ctx, range(47, 51), "bull")
    j.observe(out, a, [], 1.0)
    j.observe(out, a, [], 2.0)  # nothing changed: no new line
    denied = {**out, "counter_trend": [{**out["counter_trend"][0], "granted": False, "reason": "denied: structure turned"}]}
    j.observe(denied, a, [], 3.0)
    records, _ = read(next(tmp_path.glob("decisions-*.jsonl")))
    assert [r["reason"] for r in records] == ["start", "changed"] and records[-1]["counter_trend"][0]["granted"] is False


def test_an_older_journal_record_without_any_of_the_new_keys_still_reads_and_resumes(tmp_path):
    out = decision_after(ter_ctx, range(47, 52), "bull")
    old = {k: v for k, v in out.items() if k not in ("counter_trend",)}
    old["market"] = {k: v for k, v in out["market"].items() if k not in ("regime_facts", "trends")}
    old["setups"] = [{k: v for k, v in s.items() if k != "counter_trend"} for s in out["setups"]]
    j = Journal(tmp_path)
    j.observe(old, {"timeframes": {}, "condition": {}, "warnings": []}, [], 1.0)
    path = next(tmp_path.glob("decisions-*.jsonl"))
    records, bad = read(path)
    assert bad == [] and records[0]["counter_trend"] == [] and "\n".join(timeline(path, detail=True))  # recorded with no permission, printed without error
    s = tr.setup_from_dict({**old["setups"][0], "detected_at": 1.0, "status_since": 1.0})
    assert s.counter_trend is None and s.resumed and s.strategy == TER  # and an older setup restores with none


@pytest.mark.parametrize("side", ["bull", "bear"])
def test_a_restart_resumes_a_waiting_reversal_with_its_permission_and_announces_nothing_twice(side):
    book, steps = play(ter_ctx, range(47, 50), side)
    s, ctx = live(book, TER, side), steps[-1][0]
    assert s.status == WAITING and s.counter_trend["granted"]
    record = {"setups": [tr.setup_dict(s, ctx.price, True, True)], "events": list(reversed(book.events)), "market": {"regime": "trend_down"}}
    fresh = tr.SetupBook(P)
    assert fresh.restore([json.loads(json.dumps(record))], ctx.now) == 1  # through JSON, as the journal stores it
    r = fresh.active[(TER, side)]
    assert r.resumed and r.counter_trend == s.counter_trend and r.anchor_time == s.anchor_time
    before = len(fresh.events)
    for n in (50, 51):  # the next completed candles arrive
        c = ter_ctx(n) if side == "bull" else mirror(ter_ctx(n))
        fresh.update(c, {"session": tr.session_info(c.now, P)}, c.now)
    assert fresh.active[(TER, side)].status == CONFIRMED and fresh.active[(TER, side)].counter_trend == s.counter_trend  # it carries on from its own recorded permission
    assert [e["status"] for e in list(fresh.events)[: len(fresh.events) - before]] == [CONFIRMED]


# ====================================================================================================================
# REST, WebSocket, the options layer
# ====================================================================================================================
@pytest.fixture(scope="module")
def world():
    return World()


def patched(world, side="bull"):
    """The real engine and app, with the trading payload of the reversal scenario (the same payload shape as any other decision)."""
    md, clock = world.engine()
    for slot in (16, 17, 18, 19):
        world.step(md, clock, slot)
    payload = decision_after(ter_ctx, range(47, 52), side)
    md.safe_trading = lambda: payload
    return md, clock, payload


def test_rest_and_websocket_carry_the_regime_facts_and_the_counter_trend_decision_unchanged_in_shape(world):
    md, _, payload = patched(world)
    app = create_app(md, feed=None)
    app.state.md = md
    rest, state = route(app, "/api/trading").endpoint(), route(app, "/api/state").endpoint()
    for p in (rest, state["trading"]):
        json.dumps(p, allow_nan=False)  # strictly valid JSON
        assert p["decision"]["action"] == "BUY CE" and p["decision"]["strategy"] == TER and p["counter_trend"][0]["granted"]
        assert p["market"]["trends"] == {"1h": "bearish", "15m": "sideways"} and set(p) >= {"decision", "market", "session", "strategies", "setups", "recent", "events", "data", "counter_trend"}
        assert p["setups"][0]["counter_trend"]["structure"]["exhaustion_extreme"] == 24520.0 and next(m for m in p["strategies"] if m["id"] == TER)["state"] == "CONFIRMED"

    async def scenario():
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
        return sent

    first = asyncio.run(scenario())[0]["trading"]
    assert first["decision"]["strategy"] == TER and first["counter_trend"] == payload["counter_trend"] and first["market"]["regime_facts"] == payload["market"]["regime_facts"]


def test_a_real_engine_payload_has_the_regime_facts_and_an_empty_permission_report_when_nothing_is_reversing(world):
    md, clock = world.engine()
    for slot in (16, 17, 18, 19):
        out = world.step(md, clock, slot)
    rf = out["market"]["regime_facts"]
    assert rf["available"] and set(rf["facts"]) == {"strong_trend", "weakening_trend", "compression", "volatility_expansion", "extended", "pause"}
    assert all(f["state"] and f["checks"] for f in rf["facts"].values()) and out["market"]["trends"]["1h"] == "bullish"
    assert out["decision"]["action"] == "BUY CE" and out["decision"]["strategy"] == "trend_pullback"  # the existing strategies' decision on the real engine is what it was
    assert [(c["side"], c["granted"]) for c in out["counter_trend"]] in ([], [("PE", False)])  # the reversal strategy considered it and no permission was granted


def test_the_options_layer_is_unchanged_and_selects_by_the_decisions_action_alone():
    from test_options import live_engine

    md, _ = live_engine()  # the Phase 4 tests' own engine: a window of real option contracts with live quotes
    now = ist(10, 0, 22)
    for side, kind in (("bull", "CE"), ("bear", "PE")):
        payload = decision_after(ter_ctx, range(47, 52), side)
        assert payload["decision"]["action"] == f"BUY {kind}" and payload["decision"]["strategy"] == TER
        from_ter = md.options_view(payload, now)
        from_tp = md.options_view({"decision": {**payload["decision"], "strategy": "trend_pullback", "strategy_name": "Trend Pullback"}}, now)
        assert from_ter["selection"] == from_tp["selection"] and from_ter["selection"]["contract"]["kind"] == kind  # the same action, exactly the same selection
        assert from_ter["decision"]["strategy_name"] == "Trend Exhaustion Reversal"  # the decision is echoed as it is
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "app" / "options.py").read_text(encoding="utf-8")
    assert "counter_trend" not in src and "regime" not in src and "exhaustion" not in src  # Phase 4 knows nothing about any of this: it reads the decision's action
    assert M == 50000.0 and TODAY


def test_the_journal_endpoint_carries_the_permission_and_the_active_regime_facts_and_still_reads_an_older_record(world, tmp_path):
    import shutil

    out, path = observe_all(tmp_path / "new")
    old = {k: v for k, v in decision_after(ter_ctx, range(47, 52), "bull").items() if k != "counter_trend"}
    old["market"] = {k: v for k, v in old["market"].items() if k not in ("regime_facts", "trends")}
    md, _ = world.engine()
    md.journal = Journal(tmp_path / "day")
    target = md.journal.path(md.clock())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(path, target)
    app = create_app(md, feed=None)
    app.state.md = md
    endpoint = route(app, "/api/journal").endpoint
    last = endpoint()["decisions"][0]
    assert last["counter_trend"] == [{"strategy_name": "Trend Exhaustion Reversal", "side": "CE", "needed": True, "granted": True, "reason": out["counter_trend"][0]["reason"]}]
    assert last["regime_active"] == out["market"]["regime_facts"]["active"] and last["headline"].startswith("BUY CE")  # (the checks and the structure stay in the file: the Journal tab condenses)
    later = md.clock() + 5
    md.journal.observe(old, {"timeframes": {}, "condition": {}, "warnings": []}, [], later)  # an older-shaped record is appended to the same day's file
    old_row = next(d for d in endpoint()["decisions"] if d["time"] == later)
    assert old_row["counter_trend"] == [] and old_row["regime_active"] == []
