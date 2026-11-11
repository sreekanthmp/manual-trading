"""Pure helpers over lists of candles: merge history with live candles, build 1H from 15M, and find
missing intervals. Nothing here ever invents a candle.

Trading days are inferred from the data itself: a day that has no candle at all (weekend, market
holiday) is never examined, and nothing outside 09:15-15:30 IST is ever expected."""

import math
from datetime import datetime

from .candles import CLOSE_S, IST, OPEN_S, Candle, bucket, day_start

EXPECT_GRACE = 10.0  # seconds after an interval ends before its candle is expected to exist
MAX_RUNS = 5  # how many of the most recent gaps are reported


def slots(day: float, secs: int) -> list[float]:
    """Interval starts of one trading day: 09:15 + n * secs, until 15:30 (the last 1h slot is 15:15)."""
    return [day + OPEN_S + i * secs for i in range(math.ceil((CLOSE_S - OPEN_S) / secs))]


# -- building the series the analysis uses -------------------------------------------------------------
def aggregate(candles, secs: int, base_secs: int) -> list[Candle]:
    """Build `secs` candles (09:15 anchored) from `base_secs` candles. A candle is produced only when
    every constituent exists, so a missing 15M candle means a missing 1H candle, never a wrong one."""
    groups: dict[float, dict[float, Candle]] = {}
    for c in candles:
        b = bucket(c.start, secs)
        if b:
            groups.setdefault(b[0], {})[c.start] = c
    out = []
    for start in sorted(groups):
        end = bucket(start, secs)[1]
        want = [start + i * base_secs for i in range(math.ceil((end - start) / base_secs))]
        if any(s not in groups[start] for s in want):
            continue
        cs = [groups[start][s] for s in want]
        out.append(Candle(start, end, cs[0].open, max(c.high for c in cs), min(c.low for c in cs), cs[-1].close,
                          volume=sum(c.volume or 0 for c in cs), source="aggregated"))  # fmt: skip
    return out


def _merge(history, live) -> list[Candle]:
    """One candle per start time, oldest first. History wins a tie; live candles only fill starts the
    history lacks, and partial live candles (not trustworthy) are never used."""
    by_start = {c.start: c for c in history}
    for c in live:
        if not c.partial and c.start not in by_start:
            by_start[c.start] = c
    return sorted(by_start.values(), key=lambda c: c.start)


def completed_series(hist: dict, builder, spot_token: str | None, tf: str) -> list[Candle]:
    """Completed candles for one timeframe from Prism DB / Angel history and the live builder. The 1H
    series is always built from the merged 15M series, so an hour that straddles history and live data
    is handled the same way as any other."""

    def live(t):
        return builder.closed.get((spot_token, t), ()) if builder is not None and spot_token else ()

    if tf == "1h":
        return aggregate(_merge(hist.get("15m", ()), live("15m")), 3600, 900)
    return _merge(hist.get(tf, ()), live(tf))


# -- missing intervals -------------------------------------------------------------------------------------
def expected_starts(candles, secs: int, now: float = math.inf) -> list[float]:
    """Interval starts that should exist from the first candle on, for the trading days present in
    `candles`, and that should already be complete (plus a short grace period) at `now`."""
    if not candles:
        return []
    first = min(c.start for c in candles)
    out = []
    for day in sorted({day_start(c.start) for c in candles}):
        for s in slots(day, secs):
            if s >= first and min(s + secs, day + CLOSE_S) + EXPECT_GRACE <= now:
                out.append(s)
    return out


def missing_starts(have, secs: int, now: float, since: float) -> list[float]:
    """The interval starts of today's session (from `since`, normally the open) that should already be complete at `now` and are not in `have`. Pure: it
    only reports what is absent, it never creates a candle."""
    day = day_start(now)
    return [s for s in slots(day, secs) if s >= since and min(s + secs, day + CLOSE_S) + EXPECT_GRACE <= now and s not in have]


def missing_intervals(candles, secs: int, now: float) -> dict:
    """Which expected intervals have no candle. `latest_session_*` covers the most recent trading day in
    the data (the session the analysis currently describes); `runs` are the latest consecutive gaps."""
    exp = expected_starts(candles, secs, now)
    have = {c.start for c in candles}
    miss = [s for s in exp if s not in have]
    runs: list[dict] = []
    for s in miss:
        end = min(s + secs, day_start(s) + CLOSE_S)
        if runs and runs[-1]["end"] == s and day_start(runs[-1]["start"]) == day_start(s):
            runs[-1]["end"], runs[-1]["count"] = end, runs[-1]["count"] + 1
        else:
            runs.append({"start": s, "end": end, "count": 1})
    latest = day_start(max(have)) if have else None
    return {
        "expected": len(exp),
        "missing": len(miss),
        "latest_session": datetime.fromtimestamp(latest, IST).date().isoformat() if latest is not None else None,
        "latest_session_expected": sum(1 for s in exp if day_start(s) == latest),
        "latest_session_missing": sum(1 for s in miss if day_start(s) == latest),
        "runs": runs[-MAX_RUNS:],
    }


def contiguity(candles, secs: int) -> list[bool]:
    """flags[i] is True when candle i directly follows candle i-1: same session and exactly one interval
    later, or the first candle of a later trading day (the overnight gap is a real price gap, not a
    missing interval). False marks a hole inside a session, so nothing should be inferred across it."""
    flags = [True] * len(candles)
    for i in range(1, len(candles)):
        a, b = candles[i - 1].start, candles[i].start
        flags[i] = day_start(a) != day_start(b) or b - a == secs
    return flags
