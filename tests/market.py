"""Synthetic but internally consistent markets for the Phase 3 integration tests.

A market is a 1-minute close path over many sessions. From it we build 1M candles (open = previous
close, small deterministic wicks), aggregate to 5M / 15M with the same `aggregate` Phase 2 uses, and
hand the result to MarketData as history. The previous sessions create the higher-timeframe context
(a staircase uptrend or a range); today's session is defined minute by minute so each scenario is
exactly the price action it claims to be."""

import math
from datetime import date, timedelta

from helpers import ist

from app import series
from app.candles import Candle

OPEN_MIN = 0  # minute 0 of a session = 09:15


def sessions_before(end_day, n):
    """The n weekdays before end_day, oldest first, as (y, m, d)."""
    days, d = [], date(*end_day)
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append((d.year, d.month, d.day))
    return list(reversed(days))


def wiggle(i):
    """A small deterministic wick size, so candles are not all identical."""
    return 0.25 + 0.15 * ((i * 7) % 3)


def minute_candles(day, closes, prev_close):
    """1M candles for one session day from its close path (index 0 = the 09:15 minute)."""
    out, prev = [], prev_close
    for i, c in enumerate(closes):
        s = ist(9, 15, day=day) + 60 * i
        w = wiggle(i)
        out.append(Candle(s, s + 60, prev, max(prev, c) + w, min(prev, c) - w, c, 0, source="db"))
        prev = c
    return out


def staircase(n_minutes, start, up=100, up_slope=0.35, down=40, down_slope=-0.2):
    """A rising staircase: `up` minutes of up_slope then `down` minutes of down_slope, repeating."""
    closes, px = [], start
    for i in range(n_minutes):
        px += up_slope if (i % (up + down)) < up else down_slope
        closes.append(round(px, 2))
    return closes


def oscillation(n_minutes, centre, amplitude, period=120):
    """A sideways range: a triangle wave around `centre` (a swing every period/2 minutes)."""
    out = []
    for i in range(n_minutes):
        phase = (i % period) / period
        out.append(round(centre + amplitude * (2 * abs(2 * phase - 1) - 1), 2))
    return out


def chain(days, closes):
    """Split one long close path into consecutive 375-minute sessions and build every 1M candle."""
    out, prev = [], closes[0]
    for k, day in enumerate(days):
        out += minute_candles(day, closes[k * 375 : (k + 1) * 375], prev)
        prev = closes[(k + 1) * 375 - 1]
    return out


def legs(start, plan):
    """Minute closes from a plan of (minutes, end_price) legs: straight lines between the given prices."""
    closes, px = [], start
    for minutes, end in plan:
        for k in range(1, minutes + 1):
            closes.append(round(px + (end - px) * k / minutes, 2))
        px = end
    return closes


def to_history(m1, daily_days_ohlc=None):
    """1M candles -> the {1m, 5m, 15m, 1d} history MarketData expects (5M and 15M aggregated from the 1M)."""
    m5 = series.aggregate(m1, 300, 60)
    m15 = series.aggregate(m5, 900, 300)
    days = sorted({int(c.start // 86400) for c in m1})
    daily = []
    for dd in days:
        cs = [c for c in m1 if int(c.start // 86400) == dd]
        if not cs:
            continue
        mid = cs[0].start - ((cs[0].start + 19800) % 86400)  # IST midnight of that day
        daily.append(Candle(mid, mid + 86400, cs[0].open, max(c.high for c in cs), min(c.low for c in cs), cs[-1].close, 0, source="db"))
    return {"1m": m1, "5m": m5, "15m": m15, "1d": daily}


def mirror_path(closes, pivot=50000.0):
    """The bearish twin of a close path (a fresh minute path, so wicks are rebuilt consistently)."""
    return [round(pivot - c, 2) for c in closes]


def session_clock(day, h, m, s=0):
    return ist(h, m, s, day=day)


def hist_until(m1_all, t):
    """History as it would be known at time t: only completed 1M candles."""
    return to_history([c for c in m1_all if c.end <= t])


def approx_eq(a, b, tol=1e-6):
    return math.isclose(a, b, abs_tol=tol)


def split5(day, slot, o, h, low, c, prev_close):
    """Five 1M candles whose aggregate is exactly the 5M candle (o, h, low, c). The minute closes visit the
    first extreme, then the second, then settle at c, so no minute's range exceeds the 5M candle's."""
    first, second = (low, h) if (o - low) >= (h - o) else (h, low)  # the bigger wick is visited first
    path = [first, second, second + (c - second) / 3, second + 2 * (c - second) / 3, c]
    out, prev = [], o
    s0 = ist(9, 15, day=day) + 300 * slot
    for k, close in enumerate(path):
        s = s0 + 60 * k
        out.append(Candle(s, s + 60, prev, max(prev, close), min(prev, close), close, 0, source="db"))
        prev = close
    return out


def session_from_5m(day, specs, start_price):
    """1M candles for the first len(specs) five-minute slots of `day`; specs = [(o, h, l, c), ...]."""
    out, prev = [], start_price
    for slot, (o, h, low, c) in enumerate(specs):
        out += split5(day, slot, o, h, low, c, prev)
        prev = c
    return out
