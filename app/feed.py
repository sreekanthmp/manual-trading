"""Angel One WebSocket V2 feed. One supervisor thread owns the connection, so there is never more
than one live connection and never more than one reconnect loop."""

import logging
import threading
import time
from datetime import datetime

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from . import instruments
from .auth import AuthError, NetworkError, is_transient
from .candles import IST
from .gaps import recover

log = logging.getLogger(__name__)

QUOTE_MODE = 2  # the NIFTY spot: last price, last quantity and cumulative day volume
SNAP_QUOTE_MODE = 3  # the options: the same plus previous close, open interest and the best bid / ask with their quantities
BACKOFF = [1, 2, 4, 8, 16, 30, 60]  # seconds between reconnect attempts; it stays at 60 s and never stops retrying a network outage
MAX_AUTH_FAILURES = 5  # stop retrying a login that Angel One REJECTED (avoids locking the account). A network failure is never counted here
RECOVERY_EVERY = 60.0  # seconds between candle-recovery sweeps
ROLLOVER_RETRY = 60.0  # seconds before a new trading day's reconnect is asked for again (a connection stays "connected" until its close has been processed, which can take longer than the one-second watchdog)
FAST_RECOVERY_EVERY = 15.0  # ... while candles are known to be missing (the shared limiter still spaces every request and waits out a cool-down)


class AngelWS(SmartWebSocketV2):
    """SmartWebSocketV2 without its built-in retry logic and shared-state quirks. Reconnection is
    handled by Feed, not by the SDK."""

    def __init__(self, auth_token, api_key, client_code, feed_token):
        super().__init__(auth_token, api_key, client_code, feed_token)
        self.input_request_dict = {}  # the SDK declares this on the class, shared by every instance
        from logzero import logger as sdk_logger

        sdk_logger.setLevel(logging.WARNING)  # the SDK logs every raw message at INFO

    def connect(self):
        import websocket

        headers = {
            "Authorization": self.auth_token,
            "x-api-key": self.api_key,
            "x-client-code": self.client_code,
            "x-feed-token": self.feed_token,
        }
        self.wsapp = websocket.WebSocketApp(
            self.ROOT_URI,
            header=headers,
            on_open=self._on_open,
            on_error=self._on_error,
            on_close=self._on_close,
            on_data=self._on_data,
        )
        self.wsapp.run_forever(ping_interval=self.HEART_BEAT_INTERVAL, ping_timeout=5)

    def _on_error(self, wsapp, error):
        self.on_error(wsapp, error)

    def _on_close(self, wsapp, *args):
        self.on_close(wsapp)

    def _on_data(self, wsapp, data, data_type, continue_flag):
        if data_type == 2:  # binary tick
            try:
                parsed = self._parse_binary_data(data)
            except Exception as e:
                self.on_error(wsapp, f"undecodable message: {e}")
                return
            self.on_data(wsapp, parsed)
        elif data != "pong":  # text frames are heartbeats or server error messages
            self.on_message(wsapp, data)


def default_ws_factory(cfg):
    return lambda session: AngelWS(session.jwt, cfg.api_key, cfg.client_id, session.feed_token)


def default_selector(cfg, params):
    """Pick today's contracts from the instrument master and the current NIFTY spot price: the spot and a window of strikes around ATM of the first
    expiries (`params`: OptionParams). Also returns the parsed chain, so the strikes can follow the spot later without reading the master again."""

    def select(session):
        rows = instruments.load_master(cfg.cache_dir)
        chain = instruments.Chain(rows)
        spot = chain.spot_contract
        resp = session.call("ltpData", spot.exch_seg, spot.symbol, spot.token)
        if not resp or not resp.get("status"):
            raise RuntimeError(f"could not read NIFTY spot price: {(resp or {}).get('message')}")
        price = float(resp["data"]["ltp"])
        return chain.window(price, params.strikes_each_side, params.expiries).contracts, price, chain

    return select


def token_groups(contracts) -> list:
    """The `tokenList` of a subscribe / unsubscribe request: one group per exchange type, each with a fresh list (the SDK keeps and extends it)."""
    groups: dict = {}
    for c in contracts:
        groups.setdefault(c.exch_type, []).append(c.token)
    return [{"exchangeType": t, "tokens": list(k)} for t, k in groups.items()]


class Feed:
    def __init__(self, cfg, session, md, ws_factory=None, selector=None, sleep=time.sleep):
        self.cfg, self.session, self.md = cfg, session, md
        self.ws_factory = ws_factory or default_ws_factory(cfg)
        self.selector = selector or default_selector(cfg, md.option_params)
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread = None
        self.ws = None
        self._last_recover = time.time()
        self._recovering = False
        self._rollover_at = None  # when the reconnect for a new trading day was last asked for

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return  # already running: never start a second supervisor
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.reconnect()  # closes the socket so connect() returns

    def reconnect(self) -> None:
        ws = self.ws
        if ws:
            try:
                ws.close_connection()
            except Exception as e:
                log.debug("close failed: %s", e)

    # -- supervisor -----------------------------------------------------------------------------
    def run(self) -> None:
        """Keep one connection alive for the whole session. A network failure (no network, DNS, a timeout, a gateway error) is retried
        for ever with a bounded back-off; only credentials that Angel One REJECTED stop it, after MAX_AUTH_FAILURES tries, and then the
        state says so. Every disconnect, its reason and the attempt number are recorded (MarketData.feed_events)."""
        failures = auth_failures = 0
        while not self._stop.is_set():
            self.md.set_state("connecting")
            started, accepted_before = time.time(), self.md.accepted
            network_down, reason = False, None

            def attempt() -> int:  # a connection that carried ticks before it ended starts the count again at 1
                return 1 if self.md.accepted > accepted_before or time.time() - started > 30 else failures + 1

            try:
                self._prepare()
                auth_failures = 0
                reason = self._run_connection()
            except NetworkError as e:  # Angel One could not be reached: the account is not at fault
                network_down, reason = True, str(e)
                self.md.set_state("disconnected", reason)
                self.md.note_feed_event("network_error", reason, attempt())
            except AuthError as e:
                auth_failures += 1
                self.session.invalidate()
                if auth_failures >= MAX_AUTH_FAILURES:
                    message = f"giving up after {auth_failures} rejected logins: {e}"
                    self.md.set_state("auth_failed", message)
                    self.md.note_feed_event("gave_up", message, auth_failures)
                    return
                self.md.set_state("disconnected", str(e))
                self.md.note_feed_event("auth_error", str(e), auth_failures)
                self._sleep(min(60 * 2 ** (auth_failures - 1), 600))
                continue
            except Exception as e:
                network_down, reason = is_transient(e), f"{type(e).__name__}: {e}"
                self.md.set_state("disconnected", reason)
                self.md.note_feed_event("network_error" if network_down else "error", reason, attempt())
            else:
                self.md.set_state("disconnected")
                self.md.note_feed_event("disconnected", reason or "the connection closed", attempt())
            finally:
                self.ws = None
            if self._stop.is_set():
                break
            healthy = self.md.accepted > accepted_before or time.time() - started > 30
            failures = 0 if healthy else failures + 1
            if failures >= 3 and not network_down:  # repeated failures that are not the network: assume the session is bad and log in again
                self.session.invalidate()
            self.md.feed_attempt = failures
            self._sleep(BACKOFF[min(failures, len(BACKOFF) - 1)])

    def _prepare(self) -> None:
        """Log in if needed and (once per day) choose the contracts."""
        self.session.ensure()
        today = datetime.fromtimestamp(self.md.clock(), IST).date()  # the engine's clock (the wall clock, except in a test that sets its own): rollover_due reads the same one, so the two always agree
        if self.md.contracts and self.md.selected_on == today:
            return
        contracts, spot_price, *extra = self.selector(self.session)  # a selector may also hand back the parsed option chain
        self.md.set_contracts(contracts, today, spot_price)
        if extra:
            self.md.set_chain(extra[0], spot_price, contracts)
        log.info("contracts: %s", ", ".join(f"{c.role}={c.symbol}" for c in contracts))

    def _run_connection(self) -> str | None:
        """One connection, until it ends. Returns the last error it reported (why it ended), if any."""
        ws = self.ws_factory(self.session)
        self.ws = ws
        errors = []

        def on_open(_):
            was_down = self.md.feed_attempt > 0
            self.md.set_state("connected")
            if was_down:
                self.md.note_feed_event("reconnected", "connected again and subscribing")
            spot = [c for c in self.md.contracts if c.kind is None]
            options = [c for c in self.md.contracts if c.kind is not None]
            try:
                ws.subscribe("prism0001", QUOTE_MODE, token_groups(spot))
                if options:  # snap-quote carries the bid / ask and the open interest that the quote mode does not
                    ws.subscribe("prism0002", SNAP_QUOTE_MODE, token_groups(options))
            except Exception as e:
                self.md.note_error(f"subscribe failed: {e}")
                ws.close_connection()

        def on_error(_, err):
            errors.append(str(err))
            self.md.note_error(f"websocket error: {err}")

        ws.on_open = on_open
        ws.on_data = lambda _, msg: self.md.on_message(msg)
        ws.on_message = lambda _, msg: self.md.note_error(f"server message: {str(msg)[:200]}")
        ws.on_error = on_error
        ws.on_close = lambda _: log.info("websocket closed")
        ws.connect()  # blocks until the connection ends
        if any(k in e for e in errors for k in ("401", "403", "nauthorized", "orbidden", "oken")):
            self.session.invalidate()  # expired/invalid session: the next attempt logs in again
        return errors[-1] if errors else None

    # -- called once a second by the API layer ---------------------------------------------------
    def _apply_subscription(self, change) -> None:
        """The watched strikes were re-centred: subscribe the new options and unsubscribe the old ones on the live connection. If that cannot be
        done the connection is reset, and the reconnect subscribes the whole current window from scratch."""
        added, dropped = change
        ws = self.ws
        if ws is None or self.md.state != "connected":
            return  # not connected: the next connection subscribes the current window anyway
        try:
            if added:
                ws.subscribe("prism0003", SNAP_QUOTE_MODE, token_groups(added))
            if dropped:
                ws.unsubscribe("prism0004", SNAP_QUOTE_MODE, token_groups(dropped))
        except Exception as e:
            self.md.note_error(f"changing the option subscription failed ({type(e).__name__}: {e}): reconnecting")
            self.reconnect()

    def rollover_due(self) -> bool:
        """A new IST day has begun and the live connection still carries the contracts chosen on an earlier day. The contracts are chosen when a connection is
        made, so one that never drops (Angel One does not always close it at midnight) would keep an expired expiry's options for ever. Only a connected
        feed with contracts qualifies: a feed that is down chooses afresh on its own reconnect."""
        md = self.md
        if md.state != "connected" or not md.contracts or md.selected_on is None:
            return False
        return md.selected_on < datetime.fromtimestamp(md.clock(), IST).date()

    def watchdog(self) -> None:
        if self.rollover_due() and (self._rollover_at is None or self.md.clock() - self._rollover_at >= ROLLOVER_RETRY):  # the reconnect chooses today's contracts (_prepare) and subscribes them from scratch: the proven path, not a second way to swap them
            message = f"a new trading day: the contracts were chosen on {self.md.selected_on.isoformat()}; reconnecting to choose today's"
            log.info(message)
            self.md.note_feed_event("rollover", message)
            self._rollover_at = self.md.clock()
            self.reconnect()
        change = self.md.take_subscription_change()
        if change is not None:
            self._apply_subscription(change)
        if self.md.watchdog():
            self.md.note_error(f"{self.md.watchdog_reason or 'no ticks during market hours'}: reconnecting")
            self.reconnect()
        now = time.time()
        every = FAST_RECOVERY_EVERY if self.md.needs_recovery(self.md.clock()) else RECOVERY_EVERY
        if not self._recovering and now - self._last_recover >= every and self.md.state == "connected":
            self._last_recover, self._recovering = now, True
            threading.Thread(target=self._recover, name="recover", daemon=True).start()

    def _recover(self) -> None:
        try:
            recover(self.session, self.md)
        except Exception as e:
            self.md.note_error(f"recovery failed: {e}")
        finally:
            self._recovering = False
