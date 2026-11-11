"""MarketData: the in-memory state shared by the feed thread (writes) and the API (reads)."""

import logging
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace

from . import analysis as an
from . import history as hist_mod
from . import options as op
from . import readiness as ready_mod
from . import registry
from . import trading as tr
from .candles import TIMEFRAMES, CandleBuilder, in_session
from .gaps import GapTracker
from .health import TimeGuard
from .ratelimit import CandleLimiter
from .series import EXPECT_GRACE, completed_series
from .strategy import Params
from .ticks import TickValidator, decode

log = logging.getLogger(__name__)
QUIET_REASONS = {"duplicate", "out_of_order"}  # expected now and then: debug level only
UNHEALTHY_AFTER = 60.0  # seconds without a live connection, during the session, before the feed counts as unhealthy
LEGACY_ROLES = ("SPOT", "ATM_CE", "ATM_PE", "ITM_CE", "ITM_PE")  # the instruments whose candles the dashboard still lists (the rest of the option window has none shown)
_READ_ONLY: ContextVar[bool] = ContextVar("prism_read_only", default=False)


@contextmanager
def reading():
    """Inside this block a decision is only READ. trading() presents what the last evaluation settled (with the live price and age of now): it does not advance the setup
    lifecycle and it writes nothing to the journal. Every way a request reads the decision (GET /api/state, /api/trading, /api/options, the WebSocket) runs inside it; the
    ticker, once a second, is what evaluates (and so what journals), and so is a direct call of trading() outside the block. It is per thread / per request: a reader never
    stops the ticker, and the ticker never turns a reader into a writer."""
    token = _READ_ONLY.set(True)
    try:
        yield
    finally:
        _READ_ONLY.reset(token)


class MarketData:
    def __init__(self, cfg, clock=time.time, guard=None):
        registry.require()  # an engine with no strategies would say NO TRADE for ever: fail here, loudly, instead
        self.cfg = cfg
        self.clock = clock
        self.lock = threading.RLock()
        self.on_change = lambda: None  # set by the API layer to wake dashboard pushes
        self.contracts = []
        self.selected_on = None  # date the contracts were chosen
        self.spot_at_selection = None
        self.validator = None
        self.builder = None
        self.latest = {}
        self.gaps = GapTracker(cfg.gap_seconds)
        self.state = "starting"  # starting | connecting | connected | disconnected | auth_failed | config_error
        self.state_since = clock()
        self.connected_at = None
        self.reconnects = 0
        self.accepted = 0
        self.rejected = Counter()
        self.last_tick_recv = None  # the last tick of ANY instrument: connection information only, never a freshness test of the spot
        self.spot_token = None
        self.last_spot_recv = None  # the NIFTY spot's own last tick: what the price age, the watchdog and the decision use
        self.last_spot_exch = None
        self.down_since = clock()  # when the connection was last lost (None while connected)
        self.feed_attempt = 0  # consecutive failed connection attempts
        self.feed_reason = None  # why the connection is down, as the feed saw it
        self.feed_events = deque(maxlen=30)
        self.watchdog_reason = ""
        # the PC clock is only checked against the real clock: a test that injects its own clock has no monotonic timer to compare with
        self.timeguard = guard if guard is not None else (TimeGuard() if clock is time.time else None)
        self.limiter = CandleLimiter()  # shared by the history refresh and the gap recovery
        self.history_busy = False  # the history refresh is running: gap recovery waits so the two do not compete for the API
        self.recovery = {"requests": 0, "recovered": 0, "failures": 0, "deferred": 0, "last_error": None, "last_run": None}
        self.validation = None  # the latest official-candle comparison (see validation.py)
        self.errors = deque(maxlen=30)
        self.history = None  # {"1m","5m","15m","1d": [Candle]} from Prism DB / Angel One
        self.history_status = {"state": "loading", "error": None}
        self.history_date = None  # the session date the history was last completely refreshed for
        self.history_loaded_at = None
        self.history_coverage = {}
        self.history_version = 0
        # how the history refresh is going (read by the readiness check and the diagnostics; written by history.py)
        self.refresh_lock = threading.Lock()  # only one refresh at a time, whoever asks
        self.history_attempts = 0  # refresh attempts made today
        self.history_max_attempts = hist_mod.MAX_ATTEMPTS
        self.history_last_try = None
        self.history_retry_at = None  # when a refresh that failed for a temporary reason (rate limit, network, an incomplete window) is tried again
        self.history_failures = 0  # failed refreshes in a row
        self.history_kind = None  # why the last refresh failed: rate_limit | network | auth | validation | db | other
        self.recovery_tries = {}  # (timeframe, candle start) -> tries a candle that is absent (not merely partial) has had from the candle API
        self.started_at = clock()
        self.instance = None  # what instance.py wrote about this process (pid, start time, port)
        self._ready = {"key": None, "value": None}
        self._ready_state = None
        self._gap_cache = {"key": None, "value": {}}
        self.contracts_version = 0
        self._cache = {"key": None}  # last analysis, reused until a candle completes or history changes
        try:  # Phase 3: strategy parameters (STRATEGY_* environment variables override the defaults)
            self.params = Params.from_env()
        except ValueError as e:
            log.error("%s: using the default strategy parameters", e)
            self.params = Params()
            self.errors.append({"time": clock(), "message": f"strategy settings ignored: {e}"})
        try:  # Phase 4: the option thresholds (OPTIONS_* environment variables override the defaults)
            self.option_params = op.OptionParams.from_env()
        except ValueError as e:
            log.error("%s: using the default option settings", e)
            self.option_params = op.OptionParams()
            self.errors.append({"time": clock(), "message": f"option settings ignored: {e}"})
        self.chain = None  # the option chain of the day (instruments.Chain), parsed once
        self.window = None  # the strikes currently watched around ATM (instruments.Window)
        self.window_events = deque(maxlen=20)  # when and why the watched strikes were re-centred
        self._subscription_change = None  # (added, dropped) contracts the feed has to subscribe / unsubscribe on the live connection
        self.book = tr.SetupBook(self.params)
        self._trade = {"key": None}  # last setup update, advanced once per analysis update
        self._last_out = None  # what the last evaluation (trading() outside reading()) answered: what a read gives while a newer analysis has not been evaluated yet
        self.journal = None  # a Journal, set by the application: every decision is written down for review after the session

    # -- writes ---------------------------------------------------------------------------------
    def set_contracts(self, contracts, selected_on, spot_price) -> None:
        with self.lock:
            self.contracts = list(contracts)
            self.spot_token = next((c.token for c in contracts if c.role == "SPOT"), None)
            self.last_spot_recv = self.last_spot_exch = None
            self.selected_on, self.spot_at_selection = selected_on, spot_price
            self.validator = TickValidator({c.token: c.exch_type for c in contracts})
            self.builder = CandleBuilder([c.token for c in contracts], [c.token for c in contracts if c.has_volume])
            self.latest = {}
            self.contracts_version += 1
        self.on_change()

    def set_chain(self, chain, spot_price: float, contracts: list) -> None:
        """The option chain of the day, so the watched strikes can follow the spot without reading the instrument master again."""
        with self.lock:
            self.chain, self.window = chain, chain.adopt(contracts, spot_price)

    def apply_window(self, window, now: float, reason: str) -> None:
        """Watch a different set of strikes. Only the option contracts change: the spot's ticks, candles and gap state are not touched, and nothing
        is reset. What the feed has to subscribe and unsubscribe is queued for it (take_subscription_change)."""
        with self.lock:
            old, new = {c.token: c for c in self.contracts}, {c.token: c for c in window.contracts}
            added = [c for t, c in new.items() if t not in old]
            dropped = [c for t, c in old.items() if t not in new]
            self.contracts, self.window = list(window.contracts), window
            if self.validator is not None:
                self.validator.tokens = {c.token: c.exch_type for c in self.contracts}
                for c in dropped:
                    self.validator._last.pop(c.token, None)
            for c in added:
                if self.builder is not None:
                    self.builder.add_token(c.token, c.has_volume)
            for c in dropped:
                self.latest.pop(c.token, None)
                if self.builder is not None:
                    self.builder.forget(c.token)
            self.window_events.append({"time": now, "reason": reason, "center": window.center, "spot": window.spot, "added": len(added), "dropped": len(dropped)})
            if added or dropped:
                pend = self._subscription_change or ([], [])
                self._subscription_change = (pend[0] + added, pend[1] + dropped)
        self.on_change()

    def take_subscription_change(self):
        """The contracts to subscribe and to unsubscribe since the last call (None if nothing changed). Called by the feed."""
        with self.lock:
            change, self._subscription_change = self._subscription_change, None
            return change

    def maybe_recentre(self, now: float) -> bool:
        """Re-centre the watched strikes when the spot has moved far enough from the strike they are centred on (a lookup in the chain already in
        memory). Needs a fresh spot and a trusted clock, so a stale or untrusted price never moves the window. Never raises."""
        try:
            with self.lock:
                if self.chain is None or self.window is None or not in_session(now) or self.spot_token is None:
                    return False
                tick = self.latest.get(self.spot_token)
                if tick is None or now - tick.recv_ts > self.params.max_price_age or (self.timeguard is not None and not self.timeguard.trusted):
                    return False
                p = self.option_params
                if not self.chain.needs_recentre(self.window, tick.ltp, p.recentre_hysteresis):
                    return False
                window = self.chain.window(tick.ltp, p.strikes_each_side, p.expiries)
                if window.center == self.window.center and window.primary == self.window.primary:
                    return False
                self.apply_window(window, now, f"the spot moved to {tick.ltp:.2f}: the strikes are now centred on {window.center:g}")
                return True
        except Exception:
            log.exception("re-centring the option strikes failed")
            return False

    def set_history(self, history, status: dict, current_date=None, loaded_at=None) -> None:
        """Replace the history. `current_date` marks it as completely refreshed for that session date;
        without it the history is used but still counts as needing a refresh."""
        with self.lock:
            self.history, self.history_status = history, status
            self.history_coverage = hist_mod.coverage(history) if history else {}
            self.history_version += 1
            if current_date is not None:
                self.history_date, self.history_loaded_at = current_date, loaded_at
        self.on_change()

    def history_failed(self, error: str) -> None:
        """A refresh failed: keep whatever history exists, but say so instead of presenting it as current."""
        with self.lock:
            self.history_status = {"state": "stale" if self.history is not None else "error", "error": error}
        self.note_error(error)
        self.on_change()

    def set_state(self, state: str, error: str | None = None) -> None:
        with self.lock:
            now = self.clock()
            if state == "connected":
                self.connected_at = now
                self.down_since, self.feed_attempt, self.feed_reason = None, 0, None
            elif self.down_since is None:
                self.down_since = now
            if state == "connecting" and self.state == "disconnected":
                self.reconnects += 1
            elif state == "disconnected" and self.state == "connected":
                self.gaps.start(now, "disconnect")
            self.state, self.state_since = state, now
        if error:
            self.note_error(error)
        self.on_change()

    def note_error(self, msg: str) -> None:
        log.warning(msg)
        with self.lock:
            self.errors.append({"time": self.clock(), "message": msg})

    def note_feed_event(self, kind: str, message: str, attempt: int | None = None) -> None:
        """Why the connection went down or came back, and which attempt this was (the dashboard and the journal show it)."""
        with self.lock:
            if attempt is not None:
                self.feed_attempt = attempt
            if kind != "reconnected":
                self.feed_reason = message
            self.feed_events.append({"time": self.clock(), "kind": kind, "message": message[:200], "attempt": attempt})

    def on_message(self, msg: dict) -> None:
        """Handle one decoded WebSocket message. Called from the feed thread for every tick."""
        now = self.clock()
        token = str(msg.get("token")) if isinstance(msg, dict) else None
        # the spot needs its exchange time (its candles and gap detection are built on it), so for the spot a missing time is an error, as it always was; for
        # an option the message is kept and an unusable exchange time becomes "unknown" (Tick.exch_ts None), which the options analysis refuses to trust
        strict = self.spot_token is None or token == self.spot_token
        try:
            tick = decode(msg, now, strict_time=strict)
        except ValueError as e:
            self._reject("malformed", f"{e}: {str(msg)[:120]}")
            return
        with self.lock:
            if self.validator is None:
                return
            if tick.token == self.spot_token and self.timeguard is not None and in_session(now):
                event = self.timeguard.note_skew(now, now - tick.exch_ts)
                if event:
                    self._time_event(event, now)
            reason = self.validator.check(tick)
            if reason:
                self._reject(reason, f"token={tick.token} ltp={tick.ltp} ts={tick.exch_ts}")
                return
            if tick.token == self.spot_token:  # a gap is a silence of the NIFTY spot: option ticks do not fill it
                gap = self.gaps.on_tick(now)
                if gap:
                    self.builder.mark_gap(gap["start"], gap["end"])
                self.last_spot_recv, self.last_spot_exch = now, tick.exch_ts
            self.latest[tick.token] = tick
            if tick.exch_ts is not None:  # a candle needs the exchange time to know which candle the tick belongs to
                self.builder.on_tick(tick)
            self.accepted += 1
            self.last_tick_recv = now
        self.on_change()

    def _reject(self, reason: str, detail: str) -> None:
        with self.lock:
            self.rejected[reason] += 1
        if reason in QUIET_REASONS:
            log.debug("tick rejected (%s): %s", reason, detail)
        else:
            self.note_error(f"tick rejected ({reason}): {detail}")

    def _time_event(self, event: dict, now: float) -> None:
        """The clock cannot be trusted (see health.TimeGuard). Candles that may span the unseen period are flagged partial, so the
        analysis ignores them, and the period is recorded as a gap, so the spot candles in it are re-fetched from the official history.
        Nothing is completed or invented for that period."""
        self.note_error(f"time integrity: {event['reason']}")
        start = event.get("from")
        if self.builder is not None:
            if start is not None and in_session(start):
                lo, hi = sorted((start, now))
                self.builder.mark_gap(lo, hi)
                self.gaps.gaps.append({"start": lo, "end": hi, "reason": event["kind"]})
                self.gaps.open = None
            for c in self.builder.current.values():
                c.partial = True

    def spot_age(self, now: float) -> float | None:
        """Seconds since the NIFTY spot's last tick (None if it has not ticked). Never taken from another instrument."""
        return None if self.last_spot_recv is None else now - self.last_spot_recv

    def watchdog(self) -> bool:
        """Run once a second. Returns True when the connection looks dead (no NIFTY spot tick during market
        hours, or the application was suspended) and the feed should reconnect."""
        now = self.clock()
        with self.lock:
            suspended = False
            if self.timeguard is not None:  # before the candles are closed on the clock: a candle must not be completed across a suspension
                event = self.timeguard.check(now, self.last_spot_recv)
                if event:
                    self._time_event(event, now)
                    suspended = event["kind"] in ("suspended", "clock_back", "clock_jump")
            if self.builder:
                self.builder.close_expired(now)
            self.gaps.check(now)
            self.maybe_recentre(now)
            if self.state != "connected" or not in_session(now):
                return False
            if suspended:
                self.watchdog_reason = "the application was suspended or the clock changed: the old connection cannot be trusted"
                return True
            spot_known = self.spot_token is not None or self.last_spot_recv is not None  # with no spot at all, nothing better than any tick exists
            last = max((self.last_spot_recv if spot_known else self.last_tick_recv) or 0, self.connected_at or 0)
            if now - last > self.cfg.stale_seconds:
                self.watchdog_reason = f"no NIFTY spot tick for {now - last:.0f} s during market hours"
                return True
            return False

    # -- is the data trustworthy right now? ---------------------------------------------------------------------
    def instrument_status(self, now: float) -> list[dict]:
        """Every subscribed instrument's own freshness. The spot is judged by its own ticks (limit: the strategy's max_price_age),
        an option by its own (OPTION_STALE_SECONDS, shown only)."""
        live, out = in_session(now), []
        spot_p = replace(self.option_params, exchange_stale_seconds=self.params.max_price_age)  # the spot's exchange time is held to the same limit as its receipt age
        for c in self.contracts:
            t = self.latest.get(c.token)
            limit = self.params.max_price_age if c.role == "SPOT" else getattr(self.cfg, "option_stale_seconds", self.option_params.quote_stale_seconds)
            age = None if t is None else now - t.recv_ts
            status = "market closed" if not live else "no data" if t is None else "stale" if age > limit else "fresh"
            xage, xstatus, xreason = op.exchange_freshness(t, now, spot_p if c.role == "SPOT" else self.option_params)  # the same function, and the same field names, as the option rows
            out.append({"role": c.role, "token": c.token, "symbol": c.symbol, "exchange_time": t.exch_ts if t else None, "received_time": t.recv_ts if t else None,
                        "age": age, "limit": limit, "status": status,
                        "receipt_age": age, "exchange_age": xage, "exchange_status": xstatus, "exchange_reason": xreason,
                        "exchange_limit": spot_p.exchange_stale_seconds if c.role == "SPOT" else self.option_params.exchange_stale_seconds})  # fmt: skip
        return out

    def health_issues(self, now: float) -> list[str]:
        """Reasons the market data must not produce a decision right now (only while the session is open). A stale spot price is
        handled by the decision itself (it makes every setup non-actionable); these are the reasons it can say nothing at all."""
        if not in_session(now):
            return []
        out = []
        if self.state in ("auth_failed", "config_error"):
            out.append(f"the live feed has stopped ({self.feed_reason or self.state}): restart Prism after fixing it")
        elif self.state not in ("connected", "starting") and self.down_since is not None and now - self.down_since > UNHEALTHY_AFTER:  # "starting": no feed has been tried yet
            out.append(f"no live connection for {now - self.down_since:.0f} s" + (f" ({self.feed_reason})" if self.feed_reason else ""))
        if self.timeguard is not None and not self.timeguard.trusted:
            out.append(f"the clock cannot be trusted: {self.timeguard.reason}")
        return out

    def recovery_status(self, now: float) -> dict:
        pending = 0
        if self.builder is not None and self.spot_token:
            pending = sum(1 for (tok, _), dq in self.builder.closed.items() if tok == self.spot_token for c in dq if c.partial and c.source == "live")
        return {**self.recovery, **self.limiter.status(), "pending_candles": pending, "history_refreshing": self.history_busy}

    # -- start-up readiness and recovery bookkeeping ---------------------------------------------------
    def history_next_try(self, now: float) -> float | None:
        """Seconds until the next refresh attempt, or None when none is waiting (the history is current for today, or one is running right now)."""
        if self.history is not None and self.history_date == hist_mod.ist_date(now):
            return None
        if self.history_busy:
            return None
        if self.history_retry_at is not None:
            return max(0.0, self.history_retry_at - now)
        if self.history_last_try is not None:
            return max(0.0, self.history_last_try + hist_mod.RETRY_SECONDS - now)
        return None

    def missing_spot_starts(self, now: float) -> dict:
        """Today's completed NIFTY candles (1M / 5M / 15M) that neither the history nor the live candles hold, by timeframe. Only once the history is current
        for today (until then the refresh is what fetches them) and never while it is running. Cached for a couple of seconds."""
        with self.lock:
            if self.history is None or self.history_busy or self.history_date != hist_mod.ist_date(now) or self.builder is None:
                return {}
            key = (int(now // 2), self.history_version, self.builder.version)
            if self._gap_cache["key"] != key:
                self._gap_cache = {"key": key, "value": ready_mod.today_gaps(self, self.history, now)}
            return self._gap_cache["value"]

    def needs_recovery(self, now: float) -> bool:
        return bool(self.missing_spot_starts(now)) or self.recovery_status(now)["pending_candles"] > 0

    def readiness(self, now: float | None = None) -> dict:
        """READY / NOT READY and why (see readiness.py). Computed at most once a second; a change of state is logged once."""
        with self.lock:
            now = self.clock() if now is None else now
            key = (int(now), self.history_version, self.builder.version if self.builder else 0, self.state, self.history_busy, self.history_attempts, self.history_status.get("error"),
                   len(self.contracts), self.connected_at, self.history_date, self.history_retry_at, self.history_kind, self.last_spot_recv, len(self.recovery_tries))  # fmt: skip
            if self._ready["key"] == key:
                return self._ready["value"]
            try:
                r = ready_mod.assess(self, now)
            except Exception as e:  # never let the readiness check break the live data
                log.exception("readiness check failed")
                r = {"state": ready_mod.DEGRADED, "ready": False, "decisions_permitted": False, "summary": f"the readiness check failed: {type(e).__name__}: {e}",
                     "pre_market": False, "stages": [], "history": {}, "candles": {"missing_today": {}, "missing_in_decision_window": {}, "pending_partial": 0,
                     "unavailable_after_retries": 0, "recovering": False}, "rate_limit": {}}  # fmt: skip
            if r["state"] != self._ready_state:
                log.info("readiness: %s -> %s: %s", self._ready_state or "start", r["state"], r["summary"])
                self._ready_state = r["state"]
            self._ready = {"key": key, "value": r}
            return r

    def diagnostics(self, now: float) -> dict:
        r = self.readiness(now)
        tasks = sorted({t.name for t in threading.enumerate() if t.is_alive() and t.name in ("feed", "history", "validation", "recover")})
        return {
            "uptime_seconds": max(0.0, now - self.started_at), "started_at": self.started_at, "instance": self.instance, "startup_state": r["state"], "startup_stage": r["summary"],
            "analysis_permitted": r["decisions_permitted"], "history": r["history"], "missing_candles": r["candles"]["missing_today"],
            "last_recovery_attempt": self.recovery["last_run"], "last_spot_tick_age": self.spot_age(now), "feed_state": self.state, "reconnects": self.reconnects,
            "background_tasks": tasks,
        }  # fmt: skip

    def health(self, now: float) -> dict:
        """Everything the dashboard needs to say whether the data can be trusted: the feed, each instrument, the clock, the recovery."""
        inst = self.instrument_status(now)
        issues, warnings, live = self.health_issues(now), [], in_session(now)
        spot = next((i for i in inst if i["role"] == "SPOT"), None)
        if live and spot is not None and spot["status"] != "fresh":
            warnings.append("NIFTY spot has no recent tick" if spot["age"] is None else f"NIFTY spot is stale: last tick {spot['age']:.0f} s ago")
        quiet = [i for i in inst if live and i["role"] != "SPOT" and i["status"] != "fresh"]
        if len(quiet) <= 4:
            for i in quiet:
                warnings.append(f"{i['role']} {i['symbol']} quote is stale" if i["age"] is not None else f"{i['role']} {i['symbol']} has no quote yet")
        else:  # a whole strike window: one line, the options table says which
            warnings.append(f"{len(quiet)} of {sum(1 for i in inst if i['role'] != 'SPOT')} option quotes are stale or missing (see Options Analysis)")
        rec = self.recovery_status(now)
        if rec["cooling_down"]:
            warnings.append(f"Angel One is rate-limiting candle requests: waiting {rec['cooldown_seconds']:.0f} s")
        jr = self.journal_status(now)
        if jr["configured"] and not jr["ok"]:  # a warning, never an issue: a journal that cannot be written does not stop the decision
            warnings.append(f"the decision journal cannot be written ({jr['last_error']}): {jr['pending']} record(s) are held in memory" + (f", {jr['lost']} lost" if jr["lost"] else ""))
        return {"live": live, "trusted": live and not issues and spot is not None and spot["status"] == "fresh", "issues": issues, "warnings": warnings,
                "time": self.timeguard.status() if self.timeguard is not None else None, "instruments": inst, "recovery": rec,
                "feed": {"state": self.state, "since": self.state_since, "down_since": self.down_since, "attempt": self.feed_attempt,
                         "reason": self.feed_reason, "events": list(self.feed_events)[-5:]}, "validation": self.validation,
                "readiness": self.readiness(now), "diagnostics": self.diagnostics(now), "journal": jr}  # fmt: skip

    def journal_status(self, now: float) -> dict:
        """Where the decision journal is, and whether it is being written (see Journal.status)."""
        return self.journal.status(now) if self.journal is not None else {"configured": False, "ok": True}

    # -- analysis -------------------------------------------------------------------------------
    def _history_info(self, now: float) -> dict:
        stale = (
            self.history is not None
            and self.history_date != hist_mod.ist_date(now)
            and hist_mod.session_started(now)
        )
        return {
            **self.history_status,
            "stale": stale,
            "loaded_at": self.history_loaded_at,
            "as_of_date": self.history_date.isoformat() if self.history_date else None,
            "coverage": self.history_coverage,
        }

    def analysis(self) -> dict:
        """Market analysis from completed candles (history + live). The heavy part is recomputed only
        when a candle completes, the history changes or a minute passes (missing intervals depend on the
        clock); the price, forming candles and support/resistance split are fresh on every call."""
        with self.lock:
            now = self.clock()
            info = self._history_info(now)
            if self.history is None:
                reason = info["error"] or f"Historical data is still loading ({ready_mod.loading_detail(self)})."  # say what the start-up is waiting for
                return {"available": False, "history": info, "warnings": [reason], "reliable": False}
            spot = next((c.token for c in self.contracts if c.role == "SPOT"), None)
            b = self.builder
            forming = {tf: b.current.get((spot, tf)) if b and spot else None for tf in an.SECS}
            key = (self.history_version, self.contracts_version, b.version if b else 0,
                   int((now - EXPECT_GRACE) // 60), forming["1m"].start if forming["1m"] else None)  # fmt: skip
            if self._cache["key"] != key:
                series = {tf: completed_series(self.history, b, spot, tf) for tf in an.SECS}
                self._cache = {
                    "key": key,
                    "core": an.analyze(series, self.history.get("1d", []), forming, now),
                    "last_close": series["1m"][-1].close if series["1m"] else None,
                    "series": series,
                }
            core = self._cache["core"]
            tick = self.latest.get(spot)
            price = tick.ltp if tick else self._cache["last_close"]
            warnings = list(core["warnings"])
            ref = dict(core["reference"])
            if info["stale"]:
                why = f"refresh failed: {info['error']}" if info["error"] else "refresh for today's session is pending"
                warnings.append(f"History was last refreshed for {info['as_of_date'] or 'no session'}, not today's session "
                                f"({why}); previous-day levels and older candles may be stale.")  # fmt: skip
                if ref.get("previous_day"):
                    ref["previous_day"] = dict(ref["previous_day"], stale=True)
            result = dict(core)  # includes level_clusters: every level, which Phase 3 reads
            result.update(
                reference=ref, levels=an.split_levels(core["level_clusters"], price), price=price, as_of=now,
                forming={tf: c.to_dict() if c else None for tf, c in forming.items()},
                history=info, warnings=warnings, reliable=not warnings,
            )  # fmt: skip
            return result

    # -- reads ----------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            now = self.clock()
            statuses_by_token = {i["token"]: i["status"] for i in self.instrument_status(now)}
            instruments = []
            for c in self.contracts:
                t = self.latest.get(c.token)
                candles = {}
                for tf in TIMEFRAMES if c.role in LEGACY_ROLES else ():  # the rest of the option window is analysed in options_view, not charted
                    cur, last = self.builder.view(c.token, tf)
                    candles[tf] = {"current": cur, "last_closed": last}
                instruments.append(
                    {
                        "role": c.role,
                        "token": c.token,
                        "symbol": c.symbol,
                        "expiry": c.expiry.isoformat() if c.expiry else None,
                        "strike": c.strike,
                        "lot_size": c.lot_size,
                        "ltp": t.ltp if t else None,
                        "volume": t.volume if t and c.has_volume else None,
                        "exchange_time": t.exch_ts if t else None,
                        "received_time": t.recv_ts if t else None,
                        "age": None if t is None else now - t.recv_ts,
                        "status": statuses_by_token.get(c.token),
                        "candles": candles,
                    }
                )
            gaps = list(self.gaps.gaps)[-10:] + ([self.gaps.open] if self.gaps.open else [])
            trading = self.safe_trading()
            return {
                "server_time": now,
                "connection": {
                    "state": self.state,
                    "since": self.state_since,
                    "reconnects": self.reconnects,
                    "last_tick_received": self.last_tick_recv,
                    "ticks_accepted": self.accepted,
                    "ticks_rejected": dict(self.rejected),
                    "spot_age": self.spot_age(now),
                    "attempt": self.feed_attempt,
                    "reason": self.feed_reason,
                },
                "health": self.health(now),
                "contracts_selected_on": self.selected_on.isoformat() if self.selected_on else None,
                "spot_at_selection": self.spot_at_selection,
                "instruments": instruments,
                "gaps": [{k: v for k, v in g.items() if k != "tries"} for g in gaps],
                "errors": list(self.errors)[-10:],
                "analysis": self.safe_analysis(),
                "trading": trading,
                "options": self.safe_options(trading, now),
            }

    # -- Phase 4: the option contracts ----------------------------------------------------------------------------
    def options_view(self, trading: dict | None, now: float) -> dict:
        """The watched option contracts analysed against their own quotes, and, only when Phase 3's decision is BUY CE / BUY PE, the contract of that
        side that could be considered. Read-only: nothing here sends anything to the broker."""
        with self.lock:
            if not any(c.kind for c in self.contracts):
                return {"available": False, "reason": "no option contracts are selected yet"}
            tick = self.latest.get(self.spot_token)
            a = op.analyse(self.contracts, self.latest, tick.ltp if tick else None, self.spot_age(now), self.params.max_price_age, now, self.option_params)
            d = (trading or {}).get("decision") or {}
            w = self.window
            return {"available": True, **a,
                    "decision": {**{k: d.get(k) for k in ("action", "headline", "strategy", "strategy_name", "setup_id")}, "also": list(d.get("also") or [])},  # the final decision as it is: who made it, which setup, who agrees
                    "selection": op.select(a["contracts"], d.get("action")),  # ...and only its action chooses the side: the strategy, the setup and the others never reach the selection
                    "window": None if w is None else {"primary": w.primary.isoformat(), "center": w.center, "spot_at_selection": w.spot, "contracts": sum(1 for c in self.contracts if c.kind),
                                                      "events": list(self.window_events)[-3:]},  # fmt: skip
                    "settings": asdict(self.option_params)}  # fmt: skip

    def safe_options(self, trading: dict | None, now: float) -> dict:
        try:
            return self.options_view(trading, now)
        except Exception as e:  # the options layer must never disturb the live data, the analysis or the decision
            log.exception("options analysis failed")
            return {"available": False, "reason": f"options analysis failed: {type(e).__name__}: {e}"}

    def options_brief(self, out: dict, now: float) -> dict | None:
        """What the journal keeps of the options selection: only for a BUY decision, only the chosen contract and why."""
        try:
            sel = self.options_view(out, now)["selection"]
        except Exception:
            return None
        c = sel["contract"]
        if c is None:
            return {"symbol": None, "notice": sel["notice"], "rejected": sel["rejected"][:3]}
        keep = ("symbol", "kind", "strike", "expiry", "days_to_expiry", "moneyness", "steps_from_atm", "ltp", "bid", "ask", "spread", "spread_pct", "volume", "oi", "age", "status",
                "exchange_age", "exchange_status", "last_trade_age", "freshness")
        return {**{k: c[k] for k in keep}, "flags": [{"severity": f["severity"], "code": f["code"]} for f in c["flags"]], "why": sel["why"][:3]}

    def safe_analysis(self) -> dict:
        try:
            return self.analysis()
        except Exception as e:  # analysis must never break the live data push
            log.exception("analysis failed")
            msg = f"analysis failed: {type(e).__name__}: {e}"
            return {"available": False, "history": {"state": "error", "error": msg}, "warnings": [msg], "reliable": False}

    # -- Phase 3: setups and the decision -------------------------------------------------------
    def feed_info(self, now: float) -> dict:
        """The connection as the journal records it (the dashboard shows the same facts live): state, reconnects,
        data gaps, history refresh state and the latest errors, so a feed failure can be reviewed after the session."""
        g = self.gaps

        def brief(gap):
            return None if not gap else {k: gap.get(k) for k in ("start", "end", "reason", "ended_by")}

        return {
            "state": self.state, "since": self.state_since, "reconnects": self.reconnects,
            "last_tick_age": None if self.last_tick_recv is None else now - self.last_tick_recv,
            "spot": {"age": self.spot_age(now), "exchange_time": self.last_spot_exch, "received_time": self.last_spot_recv,
                     "stale": in_session(now) and (self.spot_age(now) is None or self.spot_age(now) > self.params.max_price_age)},  # fmt: skip
            "health_issues": self.health_issues(now), "attempt": self.feed_attempt, "reason": self.feed_reason,
            "ticks_accepted": self.accepted, "ticks_rejected": dict(self.rejected),
            "open_gap": brief(g.open), "last_gap": brief(g.gaps[-1] if g.gaps else None),
            "history": {"state": self.history_status.get("state"), "error": self.history_status.get("error"),
                        "as_of_date": self.history_date.isoformat() if self.history_date else None, "loaded_at": self.history_loaded_at},
            "errors": [{"time": e["time"], "message": str(e["message"])[:200]} for e in list(self.errors)[-3:]],
        }  # fmt: skip

    def resume(self) -> int:
        """After a restart: pick today's setups up where the journal left them, so nothing is announced twice and the
        original detection and confirmation times survive (see SetupBook.restore). Never raises; returns the number
        of live setups picked up."""
        if self.journal is None:
            return 0
        try:
            with self.lock:
                now = self.clock()
                records = self.journal.load_day(now)
                n = self.book.restore(records, now)
                if records:  # the first record of this run says what was taken over
                    self.journal.resumed(self.book.seq, {"ok": True, "error": None, "records_read": len(records), "restored": sorted(s.id for s in self.book.active.values()),
                                                         "ended": sorted(self.book.terminal), "events_carried": len(self.book.events), "next_event": self.book.seq})  # fmt: skip
                    log.info("restart: picked up %d live setup(s) and %d earlier event(s) from today's journal", n, len(self.book.events))
                return n
        except Exception as e:
            log.exception("today's journal could not be used to resume: the day's setups start afresh")
            self.book = tr.SetupBook(self.params)
            after = 0
            try:  # ...but the events of this run are numbered after those already on record, so no number is used twice in the file
                after = self.journal.next_seq(self.clock())
                if after:
                    self.book.seq = after
            except Exception:
                log.warning("the events already in today's journal could not be counted: this run's events are numbered from 0")
            self.journal.resumed(after, {"ok": False, "error": f"{type(e).__name__}: {e}", "records_read": None, "restored": [], "ended": [], "events_carried": 0, "next_event": after})
            return 0

    def trading(self) -> dict:
        """The decision and the setups. The setup lifecycle advances once per analysis update (a completed
        candle or a new minute), never per tick; the live price only decides what is actionable now.

        This EVALUATES (it advances the lifecycle and writes the journal) except inside `reading()`, where it only reads (see `_trading_view`)."""
        if _READ_ONLY.get():
            return self._trading_view()
        with self.lock:
            a = self.analysis()
            now = self.clock()
            feed = self.feed_info(now)
            if not a["available"]:
                out = tr.unavailable((a["warnings"] or ["the market analysis is not available"])[0], now)
                if self.journal is not None:  # "no decision is possible" is a decision worth having on record
                    self.journal.observe(out, a, list(self.book.events), now, feed)
                self._last_out = out
                return out
            health = tuple(self.health_issues(now))
            if self._trade["key"] != self._cache["key"] or self._trade.get("health") != health:  # also when the feed or the clock goes wrong, or is right again
                ctx, env = tr.build_context(a, self._cache["series"], now, self.params, list(health))
                self._trade = {"key": self._cache["key"], "core": self.book.update(ctx, env, now), "env": env, "health": health}
            env = self._trade["env"]
            out = tr.present(self._trade["core"], a, env, now, self.params, self.spot_age(now))
            if self.journal is not None:
                brief = self.options_brief(out, now) if out["decision"]["action"].startswith("BUY") else None
                self.journal.observe(out, a, list(self.book.events), now, feed, brief)
            self._last_out = out
            return out

    def _trading_view(self) -> dict:
        """The decision as a READER gets it: the answer an evaluation would give, but nothing is advanced and nothing is written. When the last evaluation already covers the
        current analysis and data health, it is presented again with the live price and ages of now. When a newer analysis (a candle has just completed) has not been evaluated
        yet, the last evaluation's answer is returned unchanged until the ticker evaluates it (within about a second). Before the first evaluation there is no decision to read."""
        with self.lock:
            a = self.analysis()
            now = self.clock()
            if not a["available"]:
                return tr.unavailable((a["warnings"] or ["the market analysis is not available"])[0], now)
            t = self._trade
            if t.get("core") is not None and t["key"] == self._cache["key"] and t.get("health") == tuple(self.health_issues(now)):
                return tr.present(t["core"], a, t["env"], now, self.params, self.spot_age(now))
            return self._last_out if self._last_out is not None else tr.unavailable("no decision has been evaluated yet", now)

    def safe_trading(self) -> dict:
        try:
            return self.trading()
        except Exception as e:  # the trading layer must never disturb the live data or the analysis
            log.exception("trading failed")
            return tr.unavailable(f"trading engine error: {type(e).__name__}: {e}", self.clock())
