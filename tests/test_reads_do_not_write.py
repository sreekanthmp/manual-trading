"""Stage 6.1: a request READS the decision; only the ticker EVALUATES it.

Until now `GET /api/trading`, `/api/state`, `/api/options` (and the WebSocket's first message) evaluated the decision when they were the first to see a new analysis: they advanced the
setup lifecycle and appended a journal record stamped with the moment of the request (found in the Stage 6.0 audit). Now every way of reading the decision runs inside
`app.engine.reading()`, where `trading()` presents what the last evaluation settled and nothing more. What is held here:

  * no GET route, and no WebSocket connection, advances the lifecycle or writes a byte of the journal (before any evaluation, and while a newer candle waits to be evaluated)
  * what a read returns: the evaluation's own answer, refreshed with the live price and age of now; the last answer while a newer analysis is pending; and "no decision has been
    evaluated yet" before the first evaluation (never a guess)
  * the ticker is what evaluates and journals, from the moment it starts, whether or not anyone is reading
  * `reading()` is per thread, ends with its block (an error included), and cannot be reached around: the only journal writer is `MarketData.trading()`, and every route that reads the
    decision does so inside it"""

import ast
import asyncio
import inspect
import json
import textwrap
import threading
from pathlib import Path

import market as MK
import pytest
from test_phase3_integration import DAY, World
from test_rest_websocket_consistency import http_get, scope, strict, until

from app import main as main_mod
from app.engine import MarketData, reading
from app.journal import Journal, read
from app.main import create_app

ROUTES = ["/api/state", "/api/trading", "/api/options", "/api/analysis", "/api/health", "/api/readiness", "/api/journal"]


def arrive(world, md, clock, slot):
    """Time moves on and the slot's candles have arrived: what the history refresher does. Nothing is evaluated."""
    now = world.clock_after(slot)
    clock.t = now
    md.last_spot_recv = now - 1
    md.set_history(MK.to_history([c for c in world.m1 if c.end <= now]), {"state": "ready", "error": None}, current_date=DAY, loaded_at=now)
    return now


def engine(tmp_path, evaluated=(), arrived=None):
    """The Phase 3 world with a journal: evaluated at each slot of `evaluated`, then the candles of slot `arrived` have come in but are not evaluated."""
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    for slot in evaluated:
        arrive(w, md, clock, slot)
        md.trading()
    if arrived is not None:
        arrive(w, md, clock, arrived)
    return w, md, clock


def fingerprint(md):
    """Everything an evaluation changes: the journal's bytes, the lifecycle's events and live setups, the last settled core and the last answer."""
    path = md.journal.path(md.clock())
    return (path.read_bytes() if path.exists() else None, len(md.book.events), md.book.seq, {k: s.status for k, s in md.book.active.items()}, sorted(md.book.terminal),
            md._trade.get("key"), id(md._last_out))  # fmt: skip


def served(md):
    app = create_app(md, feed=None)
    app.state.md = md
    return app


async def first_message(app):
    """The WebSocket's first message, with no ticker running (no lifespan): the connection handler alone."""
    inbox, got = asyncio.Queue(), []
    await inbox.put({"type": "websocket.connect"})

    async def send(m):
        if m["type"] == "websocket.send":
            got.append(m["text"])

    task = asyncio.create_task(app(scope("websocket", "/ws"), inbox.get, send))
    await until(lambda: got)
    await inbox.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 5)
    return strict(got[0])


def get(app, path):
    r = asyncio.run(http_get(app, path))
    assert r["status"] == 200, (path, r["status"])
    return strict(r["body"])


# ====================================================================================================================
# 1. A read changes nothing
# ====================================================================================================================
@pytest.mark.parametrize("situation", ["nothing evaluated yet", "a newer candle is waiting"])
@pytest.mark.parametrize("path", ROUTES)
def test_no_get_route_advances_the_lifecycle_or_writes_the_journal(tmp_path, situation, path):
    w, md, clock = engine(tmp_path, evaluated=() if situation == "nothing evaluated yet" else (16, 17), arrived=17 if situation == "nothing evaluated yet" else 19)
    app = served(md)
    before = fingerprint(md)
    for _ in range(3):  # (a dashboard-less script polling)
        get(app, path)
    assert fingerprint(md) == before


@pytest.mark.parametrize("situation", ["nothing evaluated yet", "a newer candle is waiting"])
def test_the_websocket_connection_advances_nothing_and_writes_nothing(tmp_path, situation):
    w, md, clock = engine(tmp_path, evaluated=() if situation == "nothing evaluated yet" else (16, 17), arrived=17 if situation == "nothing evaluated yet" else 19)
    before = fingerprint(md)
    msg = asyncio.run(first_message(served(md)))
    assert msg["trading"]["decision"]["action"] in ("NO TRADE", "WAIT") and fingerprint(md) == before


def test_the_same_reads_used_to_evaluate_and_a_direct_evaluation_still_does(tmp_path):
    """(The contrast: outside `reading()` the engine evaluates, as it always did: the ticker, the tests and the scripts rely on it.)"""
    w, md, clock = engine(tmp_path, arrived=17)
    before = fingerprint(md)
    out = md.trading()
    after = fingerprint(md)
    assert before[0] is None and after[0] is not None and after[1] > before[1]  # a journal line and lifecycle events appeared
    assert out["setups"] and read(md.journal.path(md.clock()))[0][-1]["events"]


# ====================================================================================================================
# 2. What a read returns
# ====================================================================================================================
def test_before_the_first_evaluation_a_read_says_there_is_no_decision_yet_and_never_guesses(tmp_path):
    w, md, clock = engine(tmp_path, arrived=19)  # the candles of the BUY are in, but nothing has been evaluated
    app = served(md)
    for t in (get(app, "/api/trading"), get(app, "/api/state")["trading"], asyncio.run(first_message(app))["trading"]):
        d = t["decision"]
        assert t["available"] is False and d["action"] == "NO TRADE" and d["headline"] == "NO TRADE: no decision has been evaluated yet" and d["setup_id"] is None and t["setups"] == []
    assert get(app, "/api/options")["available"] is False  # (this engine watches no option window: there is nothing to select from, with or without a decision)


def test_after_an_evaluation_a_read_gives_what_the_evaluation_gave_with_the_live_clock(tmp_path):
    w, md, clock = engine(tmp_path, evaluated=(16, 17, 18, 19))
    evaluated = json.loads(json.dumps(md.trading()))
    app = served(md)
    clock.t += 2  # two seconds on, still inside the same minute and the same analysis: nothing new to evaluate
    before = fingerprint(md)
    for read_ in (get(app, "/api/trading"), get(app, "/api/state")["trading"], asyncio.run(first_message(app))["trading"]):
        assert read_["decision"] == evaluated["decision"] and read_["decision"]["action"] == "BUY CE"
        assert [(s["id"], s["status"], s["trigger"], s["zone"], s["invalidation"]) for s in read_["setups"]] == [(s["id"], s["status"], s["trigger"], s["zone"], s["invalidation"]) for s in evaluated["setups"]]
        assert read_["events"] == evaluated["events"] and read_["as_of"] == clock.t  # presented again as of now
    assert fingerprint(md) == before


def test_while_a_newer_candle_waits_to_be_evaluated_a_read_gives_the_last_answer_unchanged(tmp_path):
    w, md, clock = engine(tmp_path, evaluated=(16, 17))
    last = json.loads(json.dumps(md.trading()))
    assert last["decision"]["action"] == "WAIT" and [s["status"] for s in last["setups"]] == ["WAITING_FOR_CONFIRMATION"]
    arrive(w, md, clock, 19)  # the candle that confirms the setup has completed; the ticker has not evaluated it yet
    app = served(md)
    before = fingerprint(md)
    now = get(app, "/api/trading")
    assert now == last  # the slot-17 answer, unchanged (not presented again over a newer analysis it was never evaluated against)
    assert [s["status"] for s in now["setups"]] == ["WAITING_FOR_CONFIRMATION"] and now["events"] == last["events"]
    assert fingerprint(md) == before
    out = md.trading()  # the evaluation (the ticker's job)
    assert out["decision"]["action"] == "BUY CE" and len(md.book.events) == len(last["events"]) + 1
    assert get(app, "/api/trading")["decision"]["action"] == "BUY CE"  # and from then on a read gives that


# ====================================================================================================================
# 3. The ticker evaluates
# ====================================================================================================================
async def ticker_session(md):
    """The application with its ticker running (the lifespan), and a WebSocket client: the first message, then the GETs."""
    app = served(md)
    inbox, got = asyncio.Queue(), []
    await inbox.put({"type": "websocket.connect"})

    async def send(m):
        if m["type"] == "websocket.send":
            got.append(strict(m["text"]))

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope("websocket", "/ws"), inbox.get, send))
        await until(lambda: got)
        first = got[0]
        path = md.journal.path(md.clock())
        await until(lambda: path.exists() and any(r["decision"]["action"] == "BUY CE" for r in read(path)[0]), seconds=20)  # the ticker journalled the decision
        settled = path.read_bytes()
        reads = {p: [strict((await http_get(app, p))["body"]) for _ in range(4)] for p in ROUTES}  # a burst of GETs
        await asyncio.sleep(0.3)
        after = path.read_bytes()
        await inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 5)
    return first, settled, after, reads


def test_the_ticker_evaluates_and_journals_from_the_start_and_a_burst_of_reads_adds_nothing(tmp_path):
    w, md, clock = engine(tmp_path, arrived=19)  # the BUY's candles are in; nobody has evaluated them; no request has been made
    first, settled, after, reads = asyncio.run(ticker_session(md))
    assert first["trading"]["decision"]["action"] == "BUY CE"  # evaluated before the first client could ask
    recs = read(md.journal.path(md.clock()))[0]
    assert [e["status"] for r in recs for e in r["events"]] == ["CONFIRMED"] and md.book.seq == 1  # the lifecycle advanced, once, by the ticker
    assert after == settled  # and twenty-eight reads did not write one more byte
    assert all(r["trading"]["decision"]["action"] == "BUY CE" for r in reads["/api/state"]) and all(r["decision"]["action"] == "BUY CE" for r in reads["/api/trading"])


# ====================================================================================================================
# 4. `reading()` is per request and cannot be left on
# ====================================================================================================================
def test_reading_ends_with_its_block_even_after_an_error(tmp_path):
    w, md, clock = engine(tmp_path, arrived=17)
    with pytest.raises(RuntimeError):
        with reading():
            md.trading()
            raise RuntimeError("a request failed")
    assert not md.book.events  # (nothing was evaluated inside)
    md.trading()  # outside the block it evaluates again
    assert md.book.events


def test_reading_is_per_thread_a_reader_never_stops_the_evaluator(tmp_path):
    w, md, clock = engine(tmp_path, arrived=17)
    inside, release, seen = threading.Event(), threading.Event(), {}

    def reader():
        with reading():
            inside.set()
            release.wait(10)
            seen["events_while_reading"] = len(md.book.events)
            seen["answer"] = md.trading()["decision"]["headline"]

    t = threading.Thread(target=reader)
    t.start()
    assert inside.wait(10)
    md.trading()  # the evaluator, in another thread, while the reader is inside its block
    release.set()
    t.join(10)
    assert md.book.events and seen["events_while_reading"] == len(md.book.events)  # the evaluation went ahead, whatever the reader was doing
    assert "no decision" not in seen["answer"]  # (and the reader saw the answer that evaluation left)


# ====================================================================================================================
# 5. It cannot be reached around
# ====================================================================================================================
def test_the_journal_has_one_writer_and_it_is_the_evaluation():
    sources = {p.name: p.read_text(encoding="utf-8") for p in Path(main_mod.__file__).parent.glob("*.py")}
    callers = {name: src.count(".observe(") for name, src in sources.items() if ".observe(" in src}
    assert callers == {"engine.py": 2}  # the unavailable branch and the normal branch of trading(), nowhere else
    body = inspect.getsource(MarketData.trading)
    assert body.index("_READ_ONLY.get()") < body.index(".observe(")  # and a read leaves trading() before it can reach either
    assert ".observe(" not in inspect.getsource(MarketData._trading_view) and ".update(" not in inspect.getsource(MarketData._trading_view)  # the reading view neither writes nor advances


WATCHED = {"snapshot", "safe_trading", "trading", "safe_options"}


def test_every_route_that_reads_the_decision_does_so_inside_reading():
    tree = ast.parse(textwrap.dedent(inspect.getsource(main_mod.create_app)))
    offences, seen = [], []

    def inside(node, ancestors):
        return any(isinstance(a, ast.With) and any(isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "id", None) == "reading" for i in a.items) for a in ancestors)

    def walk(node, ancestors, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in WATCHED:
            seen.append((func, node.func.attr))
            if not inside(node, ancestors) and (func, node.func.attr) != ("_ticker", "safe_trading"):  # the ticker's own evaluation is the one allowed exception
                offences.append((func, node.func.attr, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, ancestors + [node], func)

    walk(tree, [], None)
    assert not offences, offences
    assert {"state", "trading", "options", "ws_endpoint", "_ticker"} <= {f for f, _ in seen} and ("_ticker", "safe_trading") in seen  # (the check found the routes it is about)
