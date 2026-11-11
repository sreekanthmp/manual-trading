"""Tick -> 1m/5m/15m/1h candles, anchored to the 09:15 IST session open.

A candle only exists if at least one tick fell in it. A candle is flagged `partial` when we cannot
be sure it saw every tick (app started mid-candle, or a data gap overlapped it)."""

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
OPEN_S = 9 * 3600 + 15 * 60  # 09:15
CLOSE_S = 15 * 3600 + 30 * 60  # 15:30
CLOSE_GRACE = 3.0  # seconds to wait for late ticks before closing a candle on the clock
MAX_RECOVERY_ATTEMPTS = 3


def _seconds_into_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, IST)
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def day_start(ts: float) -> float:
    """Midnight IST (epoch seconds) of the day containing ts."""
    return ts - _seconds_into_day(ts)


def in_session(ts: float) -> bool:
    return datetime.fromtimestamp(ts, IST).weekday() < 5 and OPEN_S <= _seconds_into_day(ts) < CLOSE_S


def bucket(ts: float, secs: int) -> tuple[float, float] | None:
    """(start, end) of the candle containing ts, or None outside the session. The last 1h candle is
    cut at 15:30."""
    if not in_session(ts):
        return None
    day = day_start(ts)
    start = day + OPEN_S + ((ts - day - OPEN_S) // secs) * secs
    return start, min(start + secs, day + CLOSE_S)


@dataclass
class Candle:
    start: float
    end: float
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    ticks: int = 1
    partial: bool = False
    source: str = "live"  # "live" or "history" (recovered from the Angel One candle API)
    attempts: int = 0  # recovery attempts

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("attempts")
        return d


class CandleBuilder:
    def __init__(self, tokens, volume_tokens=(), keep: int = 500):
        self.tokens = list(tokens)
        self.volume_tokens = set(volume_tokens)
        self.current: dict[tuple[str, str], Candle] = {}
        self.closed: dict[tuple[str, str], deque[Candle]] = defaultdict(lambda: deque(maxlen=keep))
        self._last_closed_start: dict[tuple[str, str], float] = {}
        self._cum_volume: dict[str, int] = {}
        self._live_since: float | None = None
        self._keep = keep
        self.version = 0  # bumped whenever the closed candles change, so readers can cache cheaply

    def add_token(self, token: str, has_volume: bool) -> None:
        """Start building candles for one more instrument; the others are not touched."""
        if token not in self.tokens:
            self.tokens.append(token)
        if has_volume:
            self.volume_tokens.add(token)

    def forget(self, token: str) -> None:
        """Stop building candles for an instrument that is no longer watched and drop what was built for it. Nothing else changes, and `version` does
        not move: the candles other instruments' readers cache are the same."""
        self.tokens = [t for t in self.tokens if t != token]
        self.volume_tokens.discard(token)
        self._cum_volume.pop(token, None)
        for table in (self.current, self.closed, self._last_closed_start):
            for key in [k for k in table if k[0] == token]:
                del table[key]

    def on_tick(self, t) -> None:
        if self._live_since is None:
            self._live_since = t.recv_ts
        vol = self._volume_delta(t)
        for tf, secs in TIMEFRAMES.items():
            b = bucket(t.exch_ts, secs)
            if b is None:
                continue  # outside the trading session
            key = (t.token, tf)
            cur = self.current.get(key)
            if b[0] <= self._last_closed_start.get(key, -1.0) or (cur and b[0] < cur.start):
                continue  # belongs to a candle we already closed: never reopen it
            if cur and cur.start == b[0]:
                cur.high, cur.low, cur.close = max(cur.high, t.ltp), min(cur.low, t.ltp), t.ltp
                cur.ticks += 1
                if cur.volume is not None:
                    cur.volume += vol
                continue
            if cur:
                self._close(key)
            self.current[key] = Candle(
                b[0], b[1], t.ltp, t.ltp, t.ltp, t.ltp,
                volume=vol if t.token in self.volume_tokens else None,
                partial=b[0] < self._live_since - 1,  # we were not watching when it began
            )  # fmt: skip

    def _volume_delta(self, t) -> int:
        """Volume traded since the previous tick (the feed sends a running day total)."""
        if t.token not in self.volume_tokens or t.volume is None:
            return 0
        prev, self._cum_volume[t.token] = self._cum_volume.get(t.token), t.volume
        return 0 if prev is None or t.volume < prev else t.volume - prev

    def _close(self, key) -> None:
        c = self.current.pop(key)
        self.closed[key].append(c)
        self._last_closed_start[key] = c.start
        self.version += 1

    def close_expired(self, now: float) -> None:
        """Finalize candles whose interval has ended, even if no further tick arrives."""
        for key, c in list(self.current.items()):
            if now >= c.end + CLOSE_GRACE:
                self._close(key)

    def mark_gap(self, start: float, end: float) -> None:
        """Ticks may be missing between start and end: flag every candle that overlaps."""
        for c in self.current.values():
            if c.start < end:
                c.partial = True
        for dq in self.closed.values():
            for c in dq:
                if c.end > start and c.start < end:
                    c.partial, c.attempts = True, 0
        self._cum_volume.clear()  # the volume jump across the gap is not attributable to one tick
        self.version += 1

    # -- recovery from the historical-candle API -------------------------------------------------
    def recoverable(self, tokens=None) -> list[tuple[str, str, list[float]]]:
        """Closed partial live candles worth re-fetching: (token, timeframe, [candle starts]). `tokens`: only these instruments."""
        jobs = []
        for (token, tf), dq in self.closed.items():
            if tokens is not None and token not in tokens:
                continue
            starts = [c.start for c in dq if c.partial and c.source == "live" and c.attempts < MAX_RECOVERY_ATTEMPTS]
            if starts:
                for c in dq:
                    if c.start in starts:
                        c.attempts += 1
                jobs.append((token, tf, starts))
        return jobs

    def refund(self, token: str, tf: str, starts) -> None:
        """Give back the recovery attempt of these candles. Used when the API did not answer at all (refused for its rate, unreachable): that says nothing
        about whether the candle can be had, so it must not use up the candle's few tries."""
        want = set(starts)
        for c in self.closed.get((token, tf), ()):
            if c.start in want and c.attempts > 0:
                c.attempts -= 1

    def missing_in(self, start: float, end: float, now: float, tokens=None) -> dict[tuple[str, str], list[float]]:
        """Candles that should be finished by now and fall inside [start, end] but do not exist. `tokens`: only these instruments."""
        out = {}
        for tf, secs in TIMEFRAMES.items():
            starts = set()
            x = start
            while x <= end:
                b = bucket(x, secs)
                if b and now >= b[1] + CLOSE_GRACE:
                    starts.add(b[0])
                x += 60
            for token in (self.tokens if tokens is None else [t for t in self.tokens if t in tokens]):
                have = {c.start for c in self.closed.get((token, tf), ())}
                cur = self.current.get((token, tf))
                miss = sorted(s for s in starts if s not in have and not (cur and s >= cur.start))
                if miss:
                    out[(token, tf)] = miss
        return out

    def fill(self, token: str, tf: str, candles: list[Candle]) -> int:
        """Replace partial/missing closed candles with candles fetched from the API."""
        key = (token, tf)
        by_start = {c.start: c for c in self.closed[key]}
        cur = self.current.get(key)
        n = 0
        for c in candles:
            if cur and c.start >= cur.start:
                continue
            old = by_start.get(c.start)
            if old is None or old.partial:
                if token not in self.volume_tokens:
                    c.volume = None
                by_start[c.start] = c
                n += 1
        self.closed[key] = deque(sorted(by_start.values(), key=lambda c: c.start), maxlen=self._keep)
        self.version += 1
        return n

    def view(self, token: str, tf: str) -> tuple[dict | None, dict | None]:
        cur = self.current.get((token, tf))
        dq = self.closed.get((token, tf))
        return (cur.to_dict() if cur else None), (dq[-1].to_dict() if dq else None)
