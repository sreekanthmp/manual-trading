"""Stage 5.3: REST and WebSocket consistency, proved on the wire.

The earlier API tests called the route functions directly and re-encoded the result themselves. These drive the real ASGI application, the way a server would:
a GET of every route (FastAPI's own serialisation), and a WebSocket client that receives the first message and then a push from the ticker. So what is compared is
what a client really receives.

What has to hold, for every state below:
  * every body, and every WebSocket message, is standard JSON (no NaN or Infinity, which a browser's JSON.parse refuses);
  * `/api/state`, the first WebSocket message and a pushed one carry one and the same snapshot, and `/api/trading`, `/api/options`, `/api/analysis`, `/api/health`
    and `/api/readiness` carry exactly their section of it (the clock is pinned, so "the same" means equal);
  * the strategy states, the final decision, the originating strategy and setup ID, the agreeing strategies, the regime facts, the counter-trend permission and the
    options selection are the same in all of them;
  * inside a snapshot the options payload echoes the trading payload's decision, and no contract is selected unless that decision is the matching BUY;
  * unknown stays unknown (null, "no data", "unknown"), never a zero, wherever it is shown;
  * `/api/journal` names the same strategy, setup and agreeing strategies as the live decision of the same moment (nothing is written by reading it);
  * nothing that existed was removed or renamed, and no second API was added."""

import asyncio
import inspect
import json
from dataclasses import replace

import pytest
from helpers import CFG, Clock, ist
from test_exhaustion import TER, ter_ctx
from test_options import engine_with_window, live_engine
from test_options_integration import FLOWS, KIND, NAMES, NOW, SIDES, flow
from test_phase3_integration import TODAY, World
from test_trend_family import decision_after

from app import registry
from app.engine import MarketData
from app.journal import Journal
from app.main import create_app
from app.strategy import Params

PATHS = ["/api/state", "/api/analysis", "/api/trading", "/api/options", "/api/health", "/api/readiness", "/api/journal"]


# ====================================================================================================================
# The transport: the real application, called as an ASGI server calls it
# ====================================================================================================================
def scope(kind, path):
    base = {"type": kind, "asgi": {"version": "3.0"}, "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [], "root_path": "",
            "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000)}  # fmt: skip
    return {**base, "scheme": "http", "method": "GET", "http_version": "1.1"} if kind == "http" else {**base, "scheme": "ws", "subprotocols": []}


async def http_get(app, path):
    out, sent = {"status": None, "type": None, "body": b""}, False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["type"] = dict((k.decode().lower(), v.decode()) for k, v in m["headers"]).get("content-type")
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    await app(scope("http", path), receive, send)
    return out


async def until(cond, seconds=8.0):
    for _ in range(int(seconds / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for the server")


async def on_the_wire(md, pushes=1):
    """Everything a client can read at one pinned moment: a GET of each route, and the WebSocket's first message and `pushes` more from the ticker."""
    app = create_app(md, feed=None)
    app.state.md = md
    inbox, got = asyncio.Queue(), []
    await inbox.put({"type": "websocket.connect"})

    async def send(m):
        if m["type"] == "websocket.send":
            got.append(m["text"])

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope("websocket", "/ws"), inbox.get, send))
        await until(lambda: len(got) >= 1)
        early = await http_get(app, "/api/state")  # at the moment of connecting, before the ticker has run its watchdog
        for k in range(pushes):
            md.on_change()  # what a tick does: wake the ticker, which runs the watchdog and pushes the snapshot
            await until(lambda k=k: len(got) >= 2 + k)
        rest = {p: await http_get(app, p) for p in PATHS}
        await inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 5)
    return {**rest, "early": early}, got


def strict(raw):
    def refuse(constant):
        raise AssertionError(f"not standard JSON: {constant}")

    return json.loads(raw, parse_constant=refuse)


def wire(md, pushes=1):
    """(the routes' bodies, with `early` = GET /api/state made as the WebSocket connected), (the WebSocket's first message, then each push)."""
    rest, got = asyncio.run(on_the_wire(md, pushes))
    for p, r in rest.items():
        assert r["status"] == 200 and r["type"].startswith("application/json"), (p, r["status"])
    return {p: strict(r["body"]) for p, r in rest.items()}, [strict(t) for t in got]


# ====================================================================================================================
# The states
# ====================================================================================================================
def world_engine(*slots, params=None, journal=None):
    w = World()
    md, clock = w.engine(params)
    if journal is not None:
        md.journal = Journal(journal)
    for slot in slots:
        w.step(md, clock, slot)
    return md


def options_engine(payload, at=NOW):
    md, clock = live_engine()
    clock.t = at
    md.safe_trading = lambda: payload  # the engine serves the final decision it is given, exactly as trading() would
    return md


def with_stub(payload, at=NOW):
    return lambda: options_engine(payload, at)


STATES = {
    "cold": (lambda: MarketData(CFG, Clock(ist(10, 0))), "NO TRADE", False),
    "real engine, a setup waiting": (lambda: world_engine(16, 17), "WAIT", False),
    "real engine, BUY CE": (lambda: world_engine(16, 17, 18, 19), "BUY CE", False),  # (this engine watches no options window: nothing to select from)
    "real engine, entries closed": (lambda: world_engine(16, 17, 18, 19, params=replace(Params(), close_time="10:50", last_entry="10:40")), "NO TRADE", False),
    "options, WAIT": (with_stub(flow("trend_pullback", "bull", tick_age=60.0)), "WAIT", False),
    "options, NO TRADE": (with_stub(flow("trend_pullback", "bull", data_ok=False, issues=["historical data is stale"])), "NO TRADE", False),
    "options, reversal denied": (with_stub(decision_after(ter_ctx, range(47, 52), "bull", weak="none")), "WAIT", False),
    "options, market closed": (with_stub(flow("trend_pullback", "bull"), ist(18, 0)), "BUY CE", False),
    "options, spot stale": (with_stub(flow("trend_pullback", "bull"), ist(10, 1, 30)), "BUY CE", False),
}
for _name in NAMES:
    for _side in SIDES:
        STATES[f"options, {_name} {_side}"] = (with_stub(flow(_name, _side)), f"BUY {KIND[_side]}", True)


def identity(trading, options):
    """The facts the spec names, picked out of a trading payload and an options payload, in a form that compares."""
    d, m = trading["decision"], trading["market"]
    sel = (options or {}).get("selection") or {}
    return {
        "decision": {k: d.get(k) for k in ("action", "headline", "strategy", "strategy_name", "setup_id", "also", "reasons", "against")},
        "setups": [(s["id"], s["strategy"], s["direction"], s["status"], s["actionable"], s.get("counter_trend")) for s in trading["setups"]],
        "strategies": [(x["id"], x["state"], x["active"], x["setups"], [(y["side"], y["state"], y["applicable"], y["setup_id"]) for y in x["sides"]]) for x in trading["strategies"]],
        "counter_trend": [(c["strategy"], c["side"], c["needed"], c["granted"], c["reason"], c["setup_id"]) for c in trading["counter_trend"]],
        "regime": None if m is None else (m["regime"], m.get("regime_facts")),
        "options": {"available": (options or {}).get("available"), "echo": (options or {}).get("decision"), "actionable": sel.get("actionable"), "kind": sel.get("kind"),
                    "contract": (sel.get("contract") or {}).get("token"), "alternatives": [a["token"] for a in sel.get("alternatives") or []], "notice": sel.get("notice")},
    }  # fmt: skip


def consistent(trading, options):
    """Inside one snapshot: the options payload is about the trading payload's decision, and a contract is only ever selected for the matching BUY."""
    d = trading["decision"]
    ids = [s["id"] for s in trading["setups"]]
    assert all(i in ids for x in trading["strategies"] for i in x["setups"])  # a strategy only lists setups that exist
    if d["action"].startswith("BUY"):
        assert d["setup_id"] in ids and d["strategy"] in registry.NAMES and d["strategy_name"] == registry.NAMES[d["strategy"]]
        mine = next(x for x in trading["strategies"] if x["id"] == d["strategy"])
        assert d["setup_id"] in mine["setups"] and mine["state"] == "CONFIRMED"  # the strategy that made the decision shows the same setup, confirmed
        assert d["setup_id"].startswith(f"{d['strategy']}:")
    if not options or not options.get("available"):
        return
    assert options["decision"] == {**{k: d.get(k) for k in ("action", "headline", "strategy", "strategy_name", "setup_id")}, "also": d.get("also") or []}  # the echo is the decision
    sel = options["selection"]
    if sel["actionable"]:
        assert d["action"] in ("BUY CE", "BUY PE") and sel["kind"] == d["action"][-2:] and sel["contract"]["kind"] == sel["kind"]
    else:
        assert sel["contract"] is None and sel["alternatives"] == []  # nothing is selected unless the final decision calls for it


# ====================================================================================================================
# 1. Every state: the same snapshot on every route
# ====================================================================================================================
@pytest.mark.parametrize("state", list(STATES))
def test_rest_state_and_the_websocket_carry_one_snapshot_and_the_routes_carry_its_sections(state):
    build, action, selects = STATES[state]
    md = build()
    rest, (first, push) = wire(md)
    snap = rest["/api/state"]
    assert first == rest["early"]  # a client connecting gets exactly what GET /api/state says at that instant...
    assert push == snap  # ...and what the ticker pushes is what GET /api/state says afterwards (the watchdog, which the ticker runs first, may close a candle in between)
    for section, route in (("trading", "/api/trading"), ("options", "/api/options"), ("analysis", "/api/analysis"), ("health", "/api/health")):
        assert snap[section] == rest[route], section
    assert snap["health"]["readiness"] == rest["/api/readiness"]
    ours = identity(rest["/api/trading"], rest["/api/options"])
    for other in (identity(snap["trading"], snap["options"]), identity(first["trading"], first["options"]), identity(rest["early"]["trading"], rest["early"]["options"]),
                  identity(push["trading"], push["options"])):  # fmt: skip
        assert other == ours  # the decision and everything it rests on do not depend on which way, or when, it is read
    consistent(snap["trading"], snap["options"])
    assert ours["decision"]["action"] == action  # (the comparison above is not about an empty payload)
    assert (ours["options"]["contract"] is not None) == selects


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_each_strategys_decision_reaches_a_client_with_its_strategy_setup_and_agreeing_strategies(name, side):
    payload = flow(name, side)
    rest, (first, push) = wire(options_engine(payload))
    d = payload["decision"]
    for trading, options in ((rest["/api/trading"], rest["/api/options"]), (first["trading"], first["options"]), (push["trading"], push["options"])):
        assert {k: trading["decision"][k] for k in ("action", "strategy", "strategy_name", "setup_id", "also")} == {k: d[k] for k in ("action", "strategy", "strategy_name", "setup_id", "also")}
        assert trading["decision"]["strategy"] == name and trading["decision"]["setup_id"] == d["setup_id"] and options["decision"]["setup_id"] == d["setup_id"]
        assert options["decision"]["also"] == d["also"] and options["selection"]["actionable"] and options["selection"]["kind"] == KIND[side]


def test_the_agreeing_strategies_and_a_granted_permission_are_the_same_on_every_route():
    payload = flow("trend_pullback", "bull")
    payload["decision"]["also"] = ["Range Bounce (same CE opportunity)", "Momentum Continuation (same CE opportunity)"]  # (a decision two other strategies agree with)
    rest, (first, push) = wire(options_engine(payload))
    for trading, options in ((rest["/api/trading"], rest["/api/options"]), (rest["/api/state"]["trading"], rest["/api/state"]["options"]), (first["trading"], first["options"]),
                             (push["trading"], push["options"])):
        assert trading["decision"]["also"] == options["decision"]["also"] == payload["decision"]["also"]
    granted = flow(TER, "bull")  # the reversal strategy's own BUY: its permission was granted
    rest, (first, push) = wire(options_engine(granted))
    assert granted["counter_trend"] and granted["counter_trend"][0]["granted"]
    for trading in (rest["/api/trading"], rest["/api/state"]["trading"], first["trading"], push["trading"]):
        assert trading["counter_trend"] == granted["counter_trend"] and trading["setups"][0]["counter_trend"] == granted["setups"][0]["counter_trend"]


def test_a_denied_permission_and_the_regime_facts_are_the_same_on_every_route():
    denied = decision_after(ter_ctx, range(47, 52), "bull", weak="none")
    assert denied["counter_trend"] and denied["counter_trend"][0]["granted"] is False and denied["decision"]["action"] == "WAIT"
    rest, (first, push) = wire(options_engine(denied))
    for trading in (rest["/api/trading"], rest["/api/state"]["trading"], first["trading"], push["trading"]):
        assert trading["counter_trend"] == denied["counter_trend"] and trading["market"] == denied["market"]
    md = world_engine(16, 17, 18, 19)  # the real engine's own regime facts
    rest, (first, push) = wire(md)
    facts = rest["/api/trading"]["market"]["regime_facts"]
    assert facts["available"] and set(facts["facts"]) == {"strong_trend", "weakening_trend", "compression", "volatility_expansion", "extended", "pause"}
    for trading in (rest["/api/state"]["trading"], first["trading"], push["trading"]):
        assert trading["market"]["regime_facts"] == facts and trading["market"]["regime"] == rest["/api/trading"]["market"]["regime"]


def test_several_pushes_in_a_row_never_disagree_with_a_read():
    rest, got = wire(world_engine(16, 17, 18, 19), pushes=4)
    assert len(got) == 5 and all(g == rest["/api/state"] for g in got)  # (the dashboard is pushed the same snapshot every time nothing has changed)
    assert len({json.dumps(g["trading"]["decision"], sort_keys=True) for g in got}) == 1


# ====================================================================================================================
# 2. Unknown stays unknown
# ====================================================================================================================
def test_an_option_that_has_not_quoted_is_unknown_never_zero_in_every_place_it_is_shown():
    rest, (first, push) = wire(options_engine(flow("trend_pullback", "bull")))
    unquoted = [r for r in rest["/api/options"]["contracts"] if r["ltp"] is None]
    assert len(unquoted) == 16 and len(rest["/api/options"]["contracts"]) == 28  # (sixteen of the 28 watched contracts have no quote in this engine)
    numbers = ("ltp", "bid", "ask", "bid_qty", "ask_qty", "mid", "spread", "spread_pct", "volume", "oi", "prev_close", "change", "change_pct", "exchange_time", "received_time",
               "last_trade_time", "age", "receipt_age", "exchange_age", "last_trade_age", "ref_price")  # fmt: skip
    for r in unquoted:
        assert all(r[k] is None for k in numbers), r["symbol"]  # no number that could be mistaken for a measurement
        assert r["status"] == r["freshness"] == "no data" and r["exchange_status"] == "unknown"
    quoted = next(r for r in rest["/api/options"]["contracts"] if r["ltp"] is not None)
    assert quoted["ltp"] > 0 and quoted["age"] is not None and quoted["exchange_status"] in ("fresh", "stale")
    tokens = {r["token"] for r in unquoted}
    for snap in (rest["/api/state"], first, push):  # the same contracts, unquoted, in the instruments table and the health rows
        rows = [i for i in snap["instruments"] if i["token"] in tokens]
        assert len(rows) == 16 and all(i["ltp"] is None and i["age"] is None and i["exchange_time"] is None and i["status"] == "no data" for i in rows)
        health = [i for i in snap["health"]["instruments"] if i["token"] in tokens]
        assert len(health) == 16 and all(i["age"] is None and i["receipt_age"] is None and i["exchange_age"] is None and i["exchange_status"] == "unknown" for i in health)
        assert next(r for r in snap["options"]["contracts"] if r["token"] in tokens)["ltp"] is None


def test_before_any_tick_every_instrument_is_unknown_and_nothing_pretends_otherwise():
    md, _, _ = engine_with_window()
    rest, (first, push) = wire(md)
    for snap in (rest["/api/state"], first, push):
        assert snap["instruments"] and all(i["ltp"] is None and i["volume"] is None and i["age"] is None and i["exchange_time"] is None and i["received_time"] is None for i in snap["instruments"])
        spot = next(i for i in snap["health"]["instruments"] if i["role"] == "SPOT")
        assert spot["age"] is None and spot["receipt_age"] is None and spot["exchange_age"] is None and spot["exchange_status"] == "unknown" and "no quote" in spot["exchange_reason"]
        assert snap["connection"]["spot_age"] is None and snap["connection"]["last_tick_received"] is None
    assert rest["/api/health"]["trusted"] is False  # unknown is never trusted


def test_a_state_that_is_not_available_says_so_and_says_why_in_the_same_way_on_every_route():
    rest, (first, push) = wire(MarketData(CFG, Clock(ist(10, 0))))
    for snap in (rest["/api/state"], first, push):
        a, t, o = snap["analysis"], snap["trading"], snap["options"]
        assert a["available"] is False and a["reliable"] is False and a["warnings"][0]  # the analysis: its warnings say why
        assert t["available"] is False and t["decision"]["action"] == "NO TRADE" and t["decision"]["reasons"][0] and t["data"]["ok"] is False  # the decision: a NO TRADE that says why
        assert t["market"] is None and t["session"] is None and t["setups"] == [] and t["strategies"] == [] and t["counter_trend"] == []  # nothing invented to fill the gaps
        assert o["available"] is False and o["reason"]  # the options: a reason
    j = rest["/api/journal"]
    assert j["available"] is False and j["reason"] and j["decisions"] == [] and j["events"] == [] and j["file"] is None  # the journal: a reason, and empty lists rather than none


# ====================================================================================================================
# 3. The journal endpoint names what the live decision names
# ====================================================================================================================
CONDENSED = ("action", "headline", "strategy_name", "reasons", "against")


def test_the_journal_names_the_same_strategy_setup_and_agreeing_strategies_as_the_live_decision(tmp_path):
    md = world_engine(16, 17, 18, 19, journal=tmp_path)
    md.safe_trading()
    rest, (first, push) = wire(md)
    live, j = rest["/api/trading"], rest["/api/journal"]
    assert j["available"] and live["decision"]["action"] == "BUY CE"
    row = j["decisions"][0]  # the newest decision change
    for k in ("action", "headline", "strategy", "strategy_name", "setup_id", "also", "reasons", "against"):
        assert row[k] == live["decision"][k], k
    assert row["setup_id"] and row["strategy"] == "trend_pullback" and row["regime"] == live["market"]["regime"]
    assert row["regime_active"] == live["market"]["regime_facts"]["active"] and row["direction"] == live["market"]["direction"]
    assert row["counter_trend"] == [{k: c[k] for k in ("strategy_name", "side", "needed", "granted", "reason")} for c in live["counter_trend"]]
    assert [(e["id"], e["seq"], e["status"], e["strategy"], e["direction"]) for e in j["events"]] == [(e["id"], e["seq"], e["status"], e["strategy"], e["direction"]) for e in live["events"]]
    assert row["setup_id"] in {s["id"] for s in first["trading"]["setups"]} and row["setup_id"] == push["trading"]["decision"]["setup_id"]  # and the WebSocket says the same


def test_every_decision_row_of_the_journal_can_be_traced_to_a_setup_of_the_lifecycle(tmp_path):
    md = world_engine(16, 17, 18, 19, journal=tmp_path)
    j = wire(md)[0]["/api/journal"]
    ids = {e["id"] for e in j["events"]}
    assert ids and [r["action"] for r in j["decisions"]][0] == "BUY CE"
    assert all(r["setup_id"] in ids for r in j["decisions"] if r["action"].startswith("BUY"))  # a BUY row names a setup the lifecycle events know
    assert all(r["strategy"] in registry.NAMES and r["strategy_name"] == registry.NAMES[r["strategy"]] for r in j["decisions"] if r["strategy"])


def test_a_journal_record_from_before_the_identity_was_condensed_still_reads_with_unknowns_not_errors(tmp_path):
    md = world_engine(journal=tmp_path)
    path = md.journal.path(md.clock())
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {"time": "2026-11-06T10:00:00+05:30", "t": md.clock(), "reason": "start", "price": 1.0, "decision": {"action": "WAIT", "headline": "WAIT: x", "reasons": ["x"], "against": []}}
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    before = path.read_bytes()
    app = create_app(md, feed=None)
    app.state.md = md
    got = asyncio.run(http_get(app, "/api/journal"))  # (no ticker here: the ticker, like any read of the decision, may append a line of its own)
    row = strict(got["body"])["decisions"][0]
    assert got["status"] == 200 and row["strategy"] is None and row["setup_id"] is None and row["also"] == [] and row["action"] == "WAIT"
    assert path.read_bytes() == before  # and reading the journal, over the wire, wrote nothing


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_the_journal_names_the_contract_the_options_route_selects(tmp_path, name, side):
    payload = flow(name, side)
    md = options_engine(payload)
    md.journal = Journal(tmp_path)
    md.journal.observe(payload, {"timeframes": {}, "condition": {}, "warnings": []}, [], NOW, None, md.options_brief(payload, NOW))  # (what trading() hands the journal for a BUY)
    rest, _ = wire(md)
    row, sel = rest["/api/journal"]["decisions"][0], rest["/api/options"]["selection"]
    assert row["options"] == sel["contract"]["symbol"] and row["setup_id"] == payload["decision"]["setup_id"] == rest["/api/options"]["decision"]["setup_id"]
    assert row["strategy"] == name and row["also"] == rest["/api/options"]["decision"]["also"]


def test_the_journal_names_the_agreeing_strategies_of_a_decision_that_has_some(tmp_path):
    payload = flow("trend_pullback", "bull")
    payload["decision"]["also"] = ["Range Bounce (same CE opportunity)", "Momentum Continuation (same CE opportunity)"]
    md = options_engine(payload)
    md.journal = Journal(tmp_path)
    md.journal.observe(payload, {"timeframes": {}, "condition": {}, "warnings": []}, [], NOW, None, md.options_brief(payload, NOW))
    rest, (first, _) = wire(md)
    row = rest["/api/journal"]["decisions"][0]
    assert row["also"] == payload["decision"]["also"] == rest["/api/trading"]["decision"]["also"] == rest["/api/options"]["decision"]["also"] == first["options"]["decision"]["also"]
    assert row["strategy"] == "trend_pullback" and row["strategy_name"] == "Trend Pullback" and row["setup_id"] == payload["decision"]["setup_id"]


# ====================================================================================================================
# 4. Nothing that existed was removed, and no second API appeared
# ====================================================================================================================
def test_the_routes_are_exactly_the_existing_ones():
    app = create_app(MarketData(CFG, Clock(ist(10, 0))), feed=None)
    ours = sorted(r.path for r in app.routes if r.path == "/" or r.path.startswith("/api") or r.path == "/ws")
    existing = ["/", "/api/analysis", "/api/candles/{token}/{timeframe}", "/api/health", "/api/journal", "/api/options", "/api/readiness", "/api/state", "/api/trading", "/ws"]
    stage8 = ["/api/risk", "/api/trades", "/api/trades", "/api/trades/{trade_id}/void"]  # Stage 8: the notebook of your own trades (GET + POST on /api/trades, a void) and the size calculator
    assert ours == sorted(existing + stage8)
    src = inspect.getsource(create_app)
    assert src.count("md.snapshot()") == 3  # GET /api/state, the WebSocket's first message and the ticker's push all send the engine's one snapshot: no snapshot is assembled anywhere else


def test_every_key_of_the_payloads_that_existed_before_this_stage_is_still_there(tmp_path):
    md = world_engine(16, 17, 18, 19, journal=tmp_path)
    rest, (first, _) = wire(md)
    assert set(first) == {"server_time", "connection", "health", "contracts_selected_on", "spot_at_selection", "instruments", "gaps", "errors", "analysis", "trading", "options"}
    assert {"available", "as_of", "price", "decision", "explain", "market", "session", "counter_trend", "strategies", "setups", "recent", "events", "data"} <= set(rest["/api/trading"])
    assert {"action", "headline", "reasons", "against", "setup_id", "strategy", "strategy_name", "also"} <= set(rest["/api/trading"]["decision"])
    assert {"alignment", "atr", "change", "direction", "gap", "regime", "regime_facts", "text", "trends", "vol_ratio", "volatility"} <= set(rest["/api/trading"]["market"])
    assert {"live", "trusted", "issues", "warnings", "time", "instruments", "recovery", "feed", "validation", "readiness", "diagnostics"} <= set(rest["/api/health"])
    assert {"state", "ready", "decisions_permitted", "summary", "pre_market", "stages", "history", "candles", "rate_limit"} <= set(rest["/api/readiness"])
    assert {"available", "file", "decisions", "events", "records"} <= set(rest["/api/journal"])
    assert {"time", "reason", "price", "action", "headline", "strategy_name", "reasons", "against", "direction", "regime", "options", "counter_trend", "regime_active"} <= set(rest["/api/journal"]["decisions"][0])
    opts = wire(options_engine(flow("trend_pullback", "bull")))[0]["/api/options"]
    assert {"available", "decision", "selection", "window", "settings", "contracts"} <= set(opts)
    assert {"action", "headline", "strategy", "strategy_name", "setup_id", "also"} == set(opts["decision"])
    assert TODAY  # (the engine above is the one the earlier stages' tests use)
