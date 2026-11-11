"""FastAPI app: REST snapshot, WebSocket push for the dashboard, and the dashboard page itself.

Run:  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

import asyncio
import importlib.util
import json
import logging
import logging.handlers
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from . import instance, risk
from .auth import AngelSession
from .candles import TIMEFRAMES
from .config import ConfigError, load_config
from .engine import MarketData, reading
from .feed import Feed
from .history import run as run_history
from .validation import run as run_validation
from .journal import Journal
from .trades import TradeLog

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PUSH_INTERVAL = 0.25  # seconds; ticks arrive faster than the dashboard needs
JOURNAL_DEFAULT = Path(__file__).resolve().parent.parent / "logs"  # beside the app, whichever folder it is started from (it used to be "logs" of the folder it was started in)


def websocket_library() -> str | None:
    """The library uvicorn serves the dashboard's /ws with (uvicorn has none of its own: it uses `websockets`, or `wsproto`), or None when neither is installed."""
    return next((name for name in ("websockets", "wsproto") if importlib.util.find_spec(name) is not None), None)


def journal_dir() -> Path:
    """Where the decision journal (and the one-copy lock) live: JOURNAL_DIR (a relative one means relative to where Prism was started), or `logs` beside the app. Always absolute."""
    env = os.getenv("JOURNAL_DIR")
    return Path(env).resolve() if env else JOURNAL_DEFAULT


def start_once(name: str, target, args=()) -> bool:
    """Start a background task unless one with this name is already alive, so a repeated start-up can never leave two history refreshers or two validators
    running. Returns whether it was started."""
    if any(t.name == name and t.is_alive() for t in threading.enumerate()):
        log.warning("background task %r is already running: not starting a second one", name)
        return False
    threading.Thread(target=target, args=args, name=name, daemon=True).start()
    return True


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    for h in (logging.StreamHandler(), file_handler):
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)


def same_origin(request: Request) -> None:
    """A write comes from Prism's own page on this computer and from nowhere else. A page on another site can make a browser send a request to 127.0.0.1, so a write is refused when it says it
    comes from another site (`Origin` that is not this host, or `Sec-Fetch-Site: cross-site`) or when it was addressed to a name that is not this computer (a DNS-rebinding page is served from
    its own name): the host must be a loopback address, unless PRISM_ALLOW_REMOTE says the owner opened Prism to the network. A script with no `Origin` (curl) is allowed: it is on this computer too."""
    host = (request.headers.get("host") or "").strip()
    name = host.rsplit(":", 1)[0].strip("[]") if not host.startswith("[") else host[1:].split("]")[0]
    origin = request.headers.get("origin")
    if origin is not None and origin.lower() != f"{request.url.scheme}://{host}".lower():  # (the page's own origin: this scheme, this host and port)
        raise HTTPException(403, "this request comes from another site: refused")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "this request comes from another site: refused")
    if not (instance.is_loopback(name) or instance.remote_allowed()):
        raise HTTPException(403, f"this request was addressed to {name!r}, not to this computer: refused")


def create_app(md: MarketData | None = None, feed: Feed | None = None) -> FastAPI:
    """With no arguments, builds the real thing from environment variables. Tests pass their own."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal md, feed
        setup_logging()
        note = None
        if md is None and websocket_library() is None:  # (the dashboard would sit on "loading" for ever, and nothing else would say why)
            log.error("no WebSocket library is installed: uvicorn cannot serve the dashboard's live updates (/ws). Install the packages in requirements.txt (it lists `websockets`) and start Prism again")
        if md is None:  # the real thing (tests pass their own): one copy only, and before anything is started, written or logged in
            try:
                note = instance.guard(str(journal_dir()))
            except instance.InstanceError as e:
                log.error("start-up stopped: %s", e)
                raise
            log.info("startup 1/4: this is the only Prism running (process %s%s)", note["pid"], f", port {note['port']}" if note["port"] else "")
        history_stop.clear()
        if md is None:
            try:
                cfg = load_config()
                md = MarketData(cfg)
                md.journal = Journal(journal_dir(), sync=os.getenv("JOURNAL_FSYNC", "1") != "0")  # (every record but the heartbeat is synced to disk; JOURNAL_FSYNC=0 turns that off)
                log.info("decision journal: %s (fsync %s)", md.journal.dir, "on" if md.journal.sync else "off")
                md.resume()  # a restart picks today's setups up from the journal instead of announcing them again
                feed = Feed(cfg, AngelSession(cfg, limiter=md.limiter), md)
                log.info("startup 2/4: configuration loaded, credentials present")
            except ConfigError as e:
                md = MarketData(_NoConfig())
                md.set_state("config_error", str(e))
                log.error("startup 2/4: configuration problem: %s", e)
        md.instance = note
        app.state.md, app.state.feed = md, feed
        loop, wake = asyncio.get_running_loop(), asyncio.Event()
        md.on_change = lambda: loop.call_soon_threadsafe(wake.set)
        if feed:
            feed.start()
            start_once("history", run_history, (feed.cfg, md, feed.session, history_stop))
            start_once("validation", run_validation, (feed.cfg, md, feed.session, history_stop))
            log.info("startup 3/4: feed, history refresh and validation started; the dashboard shows each stage as it completes")
        task = asyncio.create_task(_ticker(app, wake))
        log.info("startup 4/4: dashboard is available")
        yield
        task.cancel()
        history_stop.set()
        if feed:
            feed.stop()
        if note is not None:
            instance.release()

    history_stop = threading.Event()
    app = FastAPI(title="Prism market data", lifespan=lifespan)
    clients: set[WebSocket] = set()

    async def _ticker(app: FastAPI, wake: asyncio.Event) -> None:
        """Runs the watchdog every second, advances the setup lifecycle, and pushes a snapshot to the dashboard when data changed. It starts with an evaluation, not a wait:
        nothing a client can ask for is evaluated by the client's request, so the first decision must not wait a second to exist."""
        while True:
            try:  # one bad iteration must not kill the watchdog and dashboard pushes for good
                (app.state.feed or app.state.md).watchdog()
                app.state.md.safe_trading()  # THE evaluation: the lifecycle advances and the decision journal is written here, and nowhere a request can reach (no dashboard need be open)
                if clients:
                    with reading():
                        text = json.dumps(app.state.md.snapshot())
                    for ws in list(clients):
                        try:
                            await ws.send_text(text)
                        except Exception:
                            clients.discard(ws)
            except Exception:
                logging.getLogger(__name__).exception("ticker iteration failed")
            await asyncio.sleep(PUSH_INTERVAL)
            try:
                await asyncio.wait_for(wake.wait(), timeout=1.0)  # (a tick, or a second, wakes it)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        with reading():  # a request reads the latest evaluation: it never advances the lifecycle or writes the journal
            return app.state.md.snapshot()

    @app.get("/api/analysis")
    def analysis():
        return app.state.md.safe_analysis()

    @app.get("/api/trading")
    def trading():
        with reading():
            return app.state.md.safe_trading()

    @app.get("/api/options")
    def options():
        """The watched option contracts analysed against their own quotes, and the contract to consider only when Phase 3 says BUY CE / BUY PE."""
        md = app.state.md
        with md.lock, reading():
            return md.safe_options(md.safe_trading(), md.clock())

    @app.get("/api/health")
    def health():
        """Is the data trustworthy right now: the feed, each instrument's own freshness, the clock, the recovery, the last validation."""
        md = app.state.md
        with md.lock:
            return md.health(md.clock())

    @app.get("/api/readiness")
    def readiness():
        """Is Prism ready: the state (INITIALIZING / HISTORICAL_LOADING / RECOVERING_GAPS / READY / DEGRADED / FAILED), the start-up stages, and whether decisions are
        permitted. Also part of /api/health."""
        md = app.state.md
        with md.lock:
            return md.readiness(md.clock())

    @app.get("/api/journal")
    def journal_today(limit: int = 300):
        """Today's decision journal, condensed for the Journal tab: the decision changes (newest first) and every setup lifecycle event. Read-only: it
        only reads the file the journal already writes; the once-per-candle heartbeat lines are left out."""
        md = app.state.md
        j = getattr(md, "journal", None)
        if j is None:
            return {"available": False, "reason": "no decision journal is configured", "decisions": [], "events": [], "file": None}
        try:
            records = j.load_day(md.clock())
            bad = list(getattr(j, "unreadable", []))  # (the lines of that read which were not records)
        except Exception as e:  # a journal that cannot be read must not take the dashboard down
            return {"available": False, "reason": f"the journal could not be read: {e}", "decisions": [], "events": [], "file": None}
        decisions, events, seen, unreadable = [], [], set(), 0
        for r in records:
            try:  # one odd record is skipped and counted: it must not take the whole journal down with it
                d = r.get("decision") or {}
                new_events = []
                for e in r.get("events") or []:
                    key = (e.get("seq"), e.get("id"), e.get("status")) if isinstance(e, dict) else None  # (a number alone is not an identity: a file can hold two runs that numbered from 0)
                    if key is not None and key not in seen:
                        new_events.append((key, e))
                row = None
                if r.get("reason") != "candle":
                    m = r.get("market") or {}
                    row = {"time": r.get("t"), "reason": r.get("reason"), "price": r.get("price"), "action": d.get("action"), "headline": d.get("headline"),
                           "strategy_name": d.get("strategy_name"), "reasons": d.get("reasons") or [], "against": d.get("against") or [],
                           "strategy": d.get("strategy"), "setup_id": d.get("setup_id"), "also": list(d.get("also") or []),  # who made it, which setup, who agrees: as /api/trading says (older records have none)
                           "also_setups": list(d.get("also_setups") or []), "decision_setup": r.get("decision_setup"), "restart": r.get("restart"), "v": r.get("v", 1),  # (version 2: the agreeing ones as ids; the setup the decision is about; the record's version)
                           "direction": m.get("direction"), "regime": m.get("regime"), "options": (r.get("options") or {}).get("symbol"),
                           "counter_trend": [{k: c.get(k) for k in ("strategy_name", "side", "needed", "granted", "reason")} for c in r.get("counter_trend") or []],  # (older records have none)
                           "regime_active": ((m.get("regime_facts") or {}).get("active") or [])}  # fmt: skip
            except Exception:
                unreadable += 1
                continue
            for key, e in new_events:
                seen.add(key)
                events.append(e)
            if row is not None:
                decisions.append(row)
        limit = max(1, min(limit, 1000))
        return {"available": True, "file": str(j.path(md.clock()).name), "decisions": decisions[::-1][:limit], "events": events[::-1][:limit], "records": len(records), "status": j.status(md.clock()),
                "bad_lines": len(bad), "bad_line_numbers": bad[:20], "unreadable_records": unreadable}  # (what could not be read is said, not hidden)

    # -- the trades you made yourself, and the size calculator (Stage 8) ---------------------------------------------------------------------------------------------
    # A notebook and a calculator: neither places an order nor reads an account. The two writes are guarded: the request must come from this computer's own page (`same_origin`).
    def trade_log() -> TradeLog:
        if getattr(app.state, "trades", None) is None:
            j = getattr(app.state.md, "journal", None)
            app.state.trades = TradeLog(j.dir if j is not None else journal_dir(), clock=app.state.md.clock)
        return app.state.trades

    def risk_limits() -> risk.Limits:
        try:
            return risk.limits_from_env()
        except ValueError as e:
            raise HTTPException(500, f"the risk limits in the environment are not usable: {e}")

    @app.get("/api/trades")
    def trades_today():
        """Today's trades (the ones you logged), what is still open, the realised profit and loss and where it stands against your daily loss limit."""
        return trade_log().summary(risk_limits())

    @app.post("/api/trades")
    def add_trade(request: Request, payload=Body(...)):
        """Log a trade you made. A BUY is stamped with what Prism's decision said at that moment."""
        same_origin(request)
        md = app.state.md
        with reading():
            seen = md.safe_trading() or {}
        d = seen.get("decision") or {}
        prism = {"action": d.get("action"), "setup_id": d.get("setup_id"), "strategy": d.get("strategy"), "price": seen.get("price")}
        try:
            rec = trade_log().add(payload, prism)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "trade": rec, "summary": trade_log().summary(risk_limits())}

    @app.post("/api/trades/{trade_id}/void")
    def void_trade(trade_id: str, request: Request):
        """Cancel a trade you logged by mistake (the file is only ever appended to)."""
        same_origin(request)
        try:
            trade_log().void(trade_id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "summary": trade_log().summary(risk_limits())}

    @app.get("/api/risk")
    def risk_size(entry: float, stop: float, lot: int, capital: float | None = None, risk_pct: float | None = None, max_loss: float | None = None):
        """How many lots keep the loss at your stop inside your limits (RISK_* in the environment; `capital`, `risk_pct` and `max_loss` override them for this answer). A calculator only."""
        env = risk_limits()
        try:
            over = risk.Limits(capital=capital if capital is not None else env.capital, per_trade_pct=risk_pct if risk_pct is not None else env.per_trade_pct,
                               max_daily_loss=max_loss if max_loss is not None else env.max_daily_loss)  # fmt: skip
            for name, v in (("capital", over.capital), ("risk_pct", over.per_trade_pct), ("max_loss", over.max_daily_loss)):
                if v is not None and not (v > 0):
                    raise ValueError(f"{name} must be a positive number")
            summary = trade_log().summary(env)
            return risk.size(entry, stop, lot, over, summary["lost_today"] if summary.get("available") else 0.0)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/candles/{token}/{timeframe}")
    def candles(token: str, timeframe: str, limit: int = 100):
        """Closed candles for one instrument (newest last)."""
        if timeframe not in TIMEFRAMES:
            raise HTTPException(404, f"timeframe must be one of {list(TIMEFRAMES)}")
        md = app.state.md
        with md.lock:
            if md.builder is None or token not in md.builder.tokens:
                raise HTTPException(404, "unknown token")
            closed = [c.to_dict() for c in list(md.builder.closed.get((token, timeframe), ()))[-limit:]]
            cur, _ = md.builder.view(token, timeframe)
        return {"token": token, "timeframe": timeframe, "closed": closed, "current": cur}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        try:
            with reading():
                text = json.dumps(app.state.md.snapshot())
            await ws.send_text(text)
            while True:
                await ws.receive_text()  # the dashboard never sends; this just notices disconnects
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    return app


class _NoConfig:
    """Stand-in config so the dashboard can still show 'config_error' when credentials are missing."""

    gap_seconds = 15.0
    stale_seconds = 45.0


app = create_app()
