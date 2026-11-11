"""Data-gap detection and candle recovery from Angel One's historical candle API."""

import logging
import time
from collections import deque
from datetime import datetime

from .auth import AuthError, is_transient
from .candles import IST, TIMEFRAMES, Candle, in_session
from .ratelimit import is_refusal

log = logging.getLogger(__name__)

MAX_TRIES = 3
API_INTERVAL = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE", "1h": "ONE_HOUR"}


class GapTracker:
    """A gap is a stretch of the trading session with no ticks at all (the spot index normally ticks
    about once a second). Times are local receipt times."""

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.last_recv: float | None = None
        self.open: dict | None = None
        self.gaps: deque[dict] = deque(maxlen=50)

    def _can_gap(self, now: float) -> bool:
        """Both moments inside the trading session of the same day."""
        if self.last_recv is None or not (in_session(self.last_recv) and in_session(now)):
            return False
        return datetime.fromtimestamp(self.last_recv, IST).date() == datetime.fromtimestamp(now, IST).date()

    def start(self, now: float, reason: str) -> None:
        """Open a gap beginning at the last tick (used by the watchdog and on disconnects)."""
        if self.open is None and self._can_gap(now):
            self.open = {"start": self.last_recv, "end": None, "reason": reason}

    def check(self, now: float) -> None:
        """Called every second."""
        if self.open is None and self._can_gap(now) and now - self.last_recv > self.threshold:
            self.start(now, "no_ticks")
        elif self.open is not None and not in_session(now):
            self.open["ended_by"] = "session_close"  # market closed before data resumed
            self.gaps.append(self.open)
            self.open = None

    def on_tick(self, now: float) -> dict | None:
        """Returns the gap that this tick just ended, if any."""
        gap = self.open
        if gap is None and self._can_gap(now) and now - self.last_recv > self.threshold:
            gap = {"start": self.last_recv, "reason": "late_ticks"}
        self.open = None
        self.last_recv = now
        if gap:
            gap["end"] = now
            self.gaps.append(gap)
            log.warning("data gap %.0fs (%s)", now - gap["start"], gap["reason"])
        return gap


def describe_span(starts) -> str:
    """`11-11 09:15..11-11 11:20`: the first and last of these candle start times (IST), or `none`. What a candle request really answered, for the log: the answer is otherwise invisible."""
    if not starts:
        return "none"
    f = lambda t: datetime.fromtimestamp(t, IST).strftime("%m-%d %H:%M")  # noqa: E731
    return f"{f(min(starts))}..{f(max(starts))}"


def _parse_history(rows: list, tf: str) -> list[Candle]:
    out = []
    for ts, o, h, low, c, v in rows:
        start = datetime.fromisoformat(ts).timestamp()
        out.append(Candle(start, start + TIMEFRAMES[tf], o, h, low, c, volume=int(v), source="history"))
    return out


def recover(session, md, sleep=time.sleep) -> int:
    """Re-fetch candles damaged by a gap from the candle API: partial ones and ones that are missing. Returns the number filled in.

    Only the NIFTY SPOT is recovered: the analysis and the decision read the spot's candles and nothing consumes the option candles,
    so asking the API for them only spent the request allowance the history refresh needs. The recovery also stands aside while the
    history refresh is running, never asks again for a candle that has had its tries, and stops for this sweep as soon as the API
    refuses a request for its rate (the shared limiter then makes every caller wait out a cool-down)."""
    with md.lock:
        if md.builder is None or md.spot_token is None:
            return 0
        if md.history_busy:
            md.recovery["deferred"] += 1
            return 0
        spot, now = [md.spot_token], md.clock()
        wanted: dict[tuple[str, str], set] = {(t, tf): set(s) for t, tf, s in md.builder.recoverable(spot)}
        refunds: dict[tuple[str, str], list] = {}  # how to give a try back if the API does not answer at all (refused, unreachable): that is not an answer about the candle
        for (token, tf), starts in wanted.items():
            refunds.setdefault((token, tf), []).append(lambda t=token, f=tf, s=tuple(starts): md.builder.refund(t, f, s))
        for gap in md.gaps.gaps:  # missing candles inside a gap we recorded
            if gap.get("end") is None:
                continue
            for (token, tf), starts in md.builder.missing_in(gap["start"], gap["end"], now, spot).items():
                tries = gap.setdefault("tries", {})
                if tries.get(tf, 0) < MAX_TRIES:
                    tries[tf] = tries.get(tf, 0) + 1
                    wanted.setdefault((token, tf), set()).update(starts)
                    refunds.setdefault((token, tf), []).append(lambda tr=tries, f=tf: tr.__setitem__(f, tr.get(f, 0) - 1))
        for tf, starts in md.missing_spot_starts(now).items():  # candles that are simply ABSENT today (a restart, a window the history never returned), recorded gap or not
            fresh = [s for s in starts if md.recovery_tries.get((tf, s), 0) < MAX_TRIES]
            if fresh:
                for s in fresh:
                    md.recovery_tries[(tf, s)] = md.recovery_tries.get((tf, s), 0) + 1
                wanted.setdefault((md.spot_token, tf), set()).update(fresh)
                refunds.setdefault((md.spot_token, tf), []).append(
                    lambda f=tf, ss=tuple(fresh): [md.recovery_tries.__setitem__((f, s), md.recovery_tries.get((f, s), 0) - 1) for s in ss])
        contracts = {c.token: c for c in md.contracts}
    if wanted:
        log.info("recovery sweep: asking for %s", ", ".join(f"{tf} x{len(s)}" for (_, tf), s in sorted(wanted.items(), key=lambda kv: TIMEFRAMES[kv[0][1]])))
    fixed = 0
    md.recovery["last_run"] = md.clock()
    for (token, tf), starts in sorted(wanted.items(), key=lambda kv: TIMEFRAMES[kv[0][1]]):  # the short timeframes first: the 1M and 5M feed the strategies
        if md.history_busy:  # the refresh started meanwhile: let it have the API
            md.recovery["deferred"] += 1
            break
        c = contracts.get(token)
        if c is None:
            continue
        starts = sorted(starts)
        fmt = "%Y-%m-%d %H:%M"
        params = {
            "exchange": c.exch_seg,
            "symboltoken": token,
            "interval": API_INTERVAL[tf],
            "fromdate": datetime.fromtimestamp(min(starts), IST).strftime(fmt),
            "todate": datetime.fromtimestamp(max(starts), IST).strftime(fmt),
        }
        md.recovery["requests"] += 1
        try:
            resp = session.call("getCandleData", params)
            if not resp or not resp.get("status"):
                raise RuntimeError((resp or {}).get("message", "no response"))
            answered = _parse_history(resp["data"] or [], tf)
            candles = [x for x in answered if x.start in starts]
            log.log(logging.INFO if answered else logging.WARNING, "recovery %s %s %s..%s: asked for %d candle(s), the answer held %d (%s), %d of them asked for",
                    c.symbol, tf, params["fromdate"], params["todate"], len(starts), len(answered), describe_span([x.start for x in answered]), len(candles))
        except Exception as e:
            transient = is_refusal(e) or is_transient(e) or isinstance(e, AuthError)
            log.warning("candle recovery failed for %s %s (%s): %s", c.symbol, tf, "temporary" if transient else "no answer", e)
            md.note_error(f"recovery {c.symbol} {tf}: {e}")
            md.recovery["failures"] += 1
            md.recovery["last_error"] = str(e)[:160]
            if transient:
                with md.lock:
                    for undo in refunds.get((token, tf), ()):
                        undo()  # the API did not answer: the candle keeps its tries for when it does
                break  # refused, unreachable or logged out: the other requests of this sweep would only fail too
            continue
        with md.lock:
            n = md.builder.fill(token, tf, candles)
        fixed += n
        md.recovery["recovered"] += n
        log.info("recovery %s %s: %d of %d candle(s) filled in", c.symbol, tf, n, len(starts))
        sleep(0.4)  # the candle API allows about 3 requests per second
    if fixed:
        log.info("recovered %d candle(s) from history", fixed)
        md.on_change()
    if wanted:
        left = md.missing_spot_starts(md.clock())
        count = sum(len(v) for v in left.values())
        if count != getattr(md, "recovery_left", None):  # said once per change, not every sweep
            md.recovery_left = count
            log.info("recovery: %s", "no candle missing now" if not count else f"{count} candle(s) still missing ({', '.join(f'{tf} x{len(v)}' for tf, v in left.items())})")
    return fixed
