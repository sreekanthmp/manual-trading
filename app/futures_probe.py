"""Stage 2.4: a READ-ONLY probe of the NIFTY index futures data, run before any futures pipeline is built.

Why: NIFTY spot has no volume, so a VWAP can only come from the futures contract. Before building a futures feed, history, recovery and freshness path, this checks the
data that path would depend on, against criteria written down here BEFORE any data was looked at:

    R1  master      the front-month NIFTY futures contract is identifiable from the instrument master (token, expiry, lot size)
    R2  volume      historical 1M candles carry volume: at least MIN_VOLUME_SHARE of in-session minutes have volume > 0
    R3  complete    historical 1M candles are complete: at least MIN_COMPLETE of the 375 minutes of each session are present, labelled by their START time
    R4  consistent  the 1M candles agree with the exchange's own 5M candles (volume sums identical in at least BUCKET_MATCH of the 5M buckets)
    R5  stable      fetching the same finished session again returns identical candles (so recovery by re-fetching is trustworthy)
    R6  vwap        a VWAP computed from the 1M candles is within VWAP_TOLERANCE of the exchange's own average traded price (the quote's avgPrice)
    R7  day volume  the sum of the candle volumes (in lots, times the lot size) is within DAY_VOLUME_TOLERANCE of the exchange's own day volume (the quote's tradeVolume)
    R8  aligned     every minute the spot history has, the futures have a candle for on the same label (at least ALIGNED of them)
    R9  basis       the futures-minus-spot basis moves smoothly: the 99th percentile of its minute-to-minute change is at most BASIS_STEP_P99 points, which is under
                    the strategies' touch tolerance (0.25 x ATR15, about 6 points), so a futures VWAP translated to the spot price scale adds no meaningful noise
    R10 live ticks  the live feed delivers futures ticks with a cumulative day volume that never goes down        (needs the market OPEN)
    R11 freshness   the exchange timestamps of live ticks lag our clock by a median of at most LAG_MEDIAN s and a 95th percentile of at most LAG_P95 s  (market OPEN)
    R12 live volume the volume per minute built from live ticks (day volume in UNITS) matches the exchange's finished 1M candles (in LOTS, times the lot size): median
                    difference at most LIVE_MEDIAN, 90th percentile at most LIVE_P90                                 (market OPEN)
    R13 recovery    after a disconnect the feed resumes within RECOVERY_SECONDS with a day volume and exchange time that have not gone backwards, and the minutes
                    that were missed are in the finished candles                                                    (market OPEN)
    R14 live basis  at tick level (each futures tick against the latest spot tick, on a one-second grid) the basis stays within BASIS_TICK_P99 points of its median over
                    the previous BASIS_TICK_WINDOW seconds, 99% of the time: the candle-based R9 samples the two instruments at different instants within a minute, so it
                    can only suggest this; a futures VWAP is only usable on the spot price scale if this holds                                      (market OPEN)

Three of the criteria were corrected after the FIRST real run (2026-11-07, a Saturday), because that run's failures of R6, R7 and R8 came from how the probe measured, not from the
data, and they are recorded here so the change cannot pass unnoticed: (1) the probe asked the candle API only for candles up to 15:30, but the futures trade in a closing session to
about 15:40 and the exchange's day volume and average price include it, so it now asks to 15:45; (2) the candle volume is in lots and the exchange's volume in units, so R7 multiplies
by the lot size; (3) R8 said "every futures minute must be a spot minute", which the spot history's own 14-minute hole (15:15-15:28) made impossible whatever the futures did,
so it now says "every spot minute has a futures candle". R9 failed on that run for a real reason (the basis drifts and its minute-to-minute change is large) and its criterion
is unchanged; a smoothed-basis error is reported beside it for information only. The first run's findings file is kept as it was written.

Each is PASS, FAIL or NOT VERIFIED (with the reason). Nothing is guessed: a requirement that cannot be checked right now (the live ones, when the exchange is closed) is
NOT VERIFIED, and the overall verdict is CONFIRMED only when every one passes.

It only READS. The calls it makes are `getCandleData`, `getMarketData` and a WebSocket subscription to the one futures token (plus the public instrument master); there
is no order, position or account call anywhere in this module (a test scans for that), and it never prints or writes a credential. It logs in once, through the same
`AngelSession` the app uses, so the candle rate limiter applies.

    python -m app.futures_probe                 # last 3 sessions + (market open: a 3 minute live listen, a reconnect, a comparison with the finished candles)
    python -m app.futures_probe --no-live       # history and quotes only
    python -m app.futures_probe --days 5 --live-seconds 300 --out logs/futures-probe.json
    python -m app.futures_probe --merge logs/a.json logs/b.json     # no broker call: the verdict of two runs together

One run cannot verify everything. While the exchange is open its quote describes a session that is still running, so R6 and R7 (comparisons with finished candles) need a
run on a finished session; while it is closed there is nothing live to listen to, so R10-R13 need a run during the session. `--merge` combines them: any FAIL wins, otherwise
any PASS, otherwise NOT VERIFIED.
"""

import argparse
import json
import logging
import math
import statistics
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import requests

from . import ticks as tk
from .auth import AngelSession
from .candles import CLOSE_S, IST, OPEN_S
from .instruments import MASTER_URL
from .ratelimit import CandleLimiter

log = logging.getLogger(__name__)

# -- the criteria (fixed before any data was looked at) ---------------------------------------------------------------------------------------
MIN_VOLUME_SHARE = 0.99
MIN_COMPLETE = 0.99
BUCKET_MATCH = 0.99
VWAP_TOLERANCE = 0.0002  # 0.02% of the price: about 5 points at 24,600
DAY_VOLUME_TOLERANCE = 0.005
ALIGNED = 0.99
BASIS_STEP_P99 = 5.0  # points
BASIS_EDGE_MINUTES = 3  # the first and last minutes of a session are left out of the basis steps (auction-like, thin)
SMOOTH_WINDOW = 10  # minutes of basis a smoothed translation would take the median of (informational: it does not decide R9)
FUT_TO = "15:45"  # the futures' closing session runs to about 15:40: the day's volume and average price include it
LIVE_MIN_TICKS, LIVE_MIN_SECONDS = 20, 60
LAG_MEDIAN, LAG_P95 = 3.0, 10.0  # seconds
LIVE_MEDIAN, LIVE_P90 = 0.01, 0.05  # relative difference of the volume of a minute
RECOVERY_SECONDS = 10.0
BASIS_TICK_P99, BASIS_TICK_WINDOW, BASIS_TICK_MIN_SECONDS, BASIS_TICK_MAX_AGE = 3.0, 30, 60, 2.0  # points (half the strategies' 6.25 point touch tolerance), seconds, seconds, seconds
SESSION_MINUTES = (CLOSE_S - OPEN_S) // 60  # 375
SPOT_EXCHANGE, SPOT_TOKEN, FUT_EXCHANGE = "NSE", "99926000", "NFO"  # NIFTY 50 index, and where its futures trade (the spot token is re-read from the master)
QUOTE_MODE = 2  # last price, last quantity and the cumulative day volume: what a futures feed would subscribe to
PASS, FAIL, NOT_VERIFIED = "PASS", "FAIL", "NOT VERIFIED"
READ_ONLY_CALLS = ("getCandleData", "getMarketData", "ltpData")  # the only broker calls this module may make


# -- the instrument master -------------------------------------------------------------------------------------------------------------------
def futures_contracts(rows: list[dict]) -> list[dict]:
    """NIFTY index futures (not the other indices' futures) in the instrument master, nearest expiry first."""
    out = []
    for r in rows:
        if r.get("name") != "NIFTY" or r.get("exch_seg") != "NFO" or r.get("instrumenttype") != "FUTIDX" or not r.get("token"):
            continue
        try:
            expiry = datetime.strptime(r["expiry"], "%d%b%Y").date()
        except (KeyError, ValueError):
            continue
        out.append({"token": str(r["token"]), "symbol": r.get("symbol"), "expiry": expiry, "lot_size": int(float(r.get("lotsize") or 0))})
    return sorted(out, key=lambda c: c["expiry"])


def front_month(contracts: list[dict], now: datetime) -> dict | None:
    """The nearest contract that can still be traded: on its expiry day it is only the front month until the close."""
    after_close = now.hour * 3600 + now.minute * 60 + now.second >= CLOSE_S
    return next((c for c in contracts if c["expiry"] > now.date() or (c["expiry"] == now.date() and not after_close)), None)


def load_futures_master(cache_dir: str, today: date, url: str = MASTER_URL) -> list[dict]:
    """The futures rows of the public instrument master, cached once a day (the file itself is large; the app's own master cache keeps only options and the spot)."""
    cache = Path(cache_dir) / f"nifty-futures-master-{today.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    rows = [r for r in resp.json() if r.get("name") == "NIFTY" and r.get("instrumenttype") in ("FUTIDX", "AMXIDX")]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows), encoding="utf-8")
    return rows


# -- candles -----------------------------------------------------------------------------------------------------------------------------------
def parse_candles(data) -> tuple[list[dict], int]:
    """Angel One candle rows `[iso time, open, high, low, close, volume]` into dicts (oldest first, one per start time), and the number of unusable rows."""
    out, bad = {}, 0
    for r in data or []:
        try:
            start = datetime.fromisoformat(r[0]).timestamp()
            o, h, low, c = (float(x) for x in r[1:5])
            v = None if r[5] is None else int(r[5])
        except (TypeError, ValueError, IndexError):
            bad += 1
            continue
        out.setdefault(start, {"start": start, "open": o, "high": h, "low": low, "close": c, "volume": v})
    return sorted(out.values(), key=lambda c: c["start"]), bad


def session_starts(day: date, step: int = 60) -> list[float]:
    open_ts = datetime.combine(day, dtime(9, 15), tzinfo=IST).timestamp()
    return [open_ts + i * step for i in range((CLOSE_S - OPEN_S) // step)]


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def _ohlc_ok(c: dict) -> bool:
    vals = (c["open"], c["high"], c["low"], c["close"])
    return all(math.isfinite(x) and x > 0 for x in vals) and c["low"] <= min(c["open"], c["close"]) and c["high"] >= max(c["open"], c["close"])


def check_day(candles: list[dict], day: date, step: int = 60) -> dict:
    """What one session of candles looks like against the full set of minutes it should have."""
    expected = session_starts(day, step)
    want = set(expected)
    have = {c["start"]: c for c in candles}
    present = [s for s in expected if s in have]
    missing = [s for s in expected if s not in have]
    inside = [have[s] for s in present]
    vols = [c["volume"] or 0 for c in inside]
    return {
        "day": day.isoformat(), "expected": len(expected), "present": len(present), "missing": len(missing), "missing_first": [hm(s) for s in missing[:5]],
        "outside_session": sum(1 for c in candles if c["start"] not in want), "zero_or_missing_volume": sum(1 for c in inside if not c["volume"]),
        "bad_ohlc": sum(1 for c in inside if not _ohlc_ok(c)), "volume_total": sum(vols), "volume_max": max(vols, default=0),
        "volume_median": statistics.median(vols) if vols else 0,
    }  # fmt: skip


def consistency_1m_5m(c1: list[dict], c5: list[dict], day: date) -> dict:
    """The 1M candles against the exchange's own 5M candles, bucket by bucket (a bucket missing any of its five minutes is left out and counted)."""
    by1 = {c["start"]: c for c in c1}
    buckets = equal_v = equal_ohlc = skipped = 0
    examples, valid = [], set(session_starts(day, 300))
    for b in c5:
        if b["start"] not in valid:
            continue
        minutes = [by1.get(b["start"] + 60 * i) for i in range(5)]
        if any(m is None for m in minutes):
            skipped += 1
            continue
        buckets += 1
        vol = sum(m["volume"] or 0 for m in minutes)
        same_v = vol == (b["volume"] or 0)
        same_p = (minutes[0]["open"], max(m["high"] for m in minutes), min(m["low"] for m in minutes), minutes[-1]["close"]) == (b["open"], b["high"], b["low"], b["close"])
        equal_v += same_v
        equal_ohlc += same_p
        if not (same_v and same_p) and len(examples) < 3:
            examples.append({"at": hm(b["start"]), "volume_1m_sum": vol, "volume_5m": b["volume"], "ohlc_equal": same_p})
    return {"buckets": buckets, "volume_equal": equal_v, "ohlc_equal": equal_ohlc, "skipped_incomplete": skipped, "examples": examples}


def session_vwap(candles: list[dict]) -> float | None:
    """The volume-weighted average of each minute's typical price (high + low + close) / 3."""
    num = sum((c["high"] + c["low"] + c["close"]) / 3 * (c["volume"] or 0) for c in candles)
    den = sum(c["volume"] or 0 for c in candles)
    return None if den <= 0 else num / den


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def basis_stats(fut: list[dict], spot: list[dict], day: date) -> dict:
    """futures close minus spot close, minute by minute: its level, and how much it changes from one minute to the next (the first and last few minutes left out)."""
    sp = {c["start"]: c for c in spot}
    starts = session_starts(day)
    edge = set(starts[:BASIS_EDGE_MINUTES] + starts[-BASIS_EDGE_MINUTES:])
    series = [(c["start"], c["close"] - sp[c["start"]]["close"]) for c in fut if c["start"] in sp]
    steps = [abs(b - a) for (s0, a), (s1, b) in zip(series, series[1:]) if s1 - s0 == 60 and s0 not in edge and s1 not in edge]
    levels = [b for _, b in series]
    smooth = []  # how far each minute's basis is from the median of the previous SMOOTH_WINDOW consecutive minutes: the error of translating with a smoothed basis (informational)
    for k in range(SMOOTH_WINDOW, len(series)):
        window = series[k - SMOOTH_WINDOW : k + 1]
        if window[-1][0] - window[0][0] == 60 * SMOOTH_WINDOW and series[k][0] not in edge:
            smooth.append(abs(series[k][1] - statistics.median(b for _, b in window[:-1])))
    return {
        "minutes": len(series), "mean": statistics.fmean(levels) if levels else None, "stdev": statistics.pstdev(levels) if len(levels) > 1 else None,
        "min": min(levels, default=None), "max": max(levels, default=None), "share_positive": (sum(1 for x in levels if x > 0) / len(levels)) if levels else None,
        "step_median": statistics.median(steps) if steps else None, "step_p99": percentile(steps, 0.99), "step_max": max(steps, default=None),
        "smoothed_error_median": statistics.median(smooth) if smooth else None, "smoothed_error_p99": percentile(smooth, 0.99),
    }  # fmt: skip


# -- quotes ------------------------------------------------------------------------------------------------------------------------------------
def quote_summary(resp, token: str) -> dict | None:
    """The fields of a FULL market-data quote that the checks use (raw strings kept for the times)."""
    try:
        row = next(r for r in resp["data"]["fetched"] if str(r.get("symbolToken")) == str(token))
    except (KeyError, TypeError, StopIteration):
        return None
    keep = ("ltp", "tradeVolume", "avgPrice", "opnInterest", "exchFeedTime", "exchTradeTime", "lastTradeQty", "open", "high", "low", "close", "tradingSymbol")
    return {k: row.get(k) for k in keep}


def quote_date(q: dict | None) -> date | None:
    for k in ("exchFeedTime", "exchTradeTime"):
        try:
            return datetime.strptime(q[k], "%d-%b-%Y %H:%M:%S").date()
        except (TypeError, KeyError, ValueError):
            continue
    return None


# -- live ticks --------------------------------------------------------------------------------------------------------------------------------
def analyse_ticks(ticks: list[dict]) -> dict:
    """What a listen to the live futures feed showed. A tick is {recv, exch (seconds, or None), ltp, volume (cumulative, or None), ltq, oi}."""
    n = len(ticks)
    if not n:
        return {"ticks": 0, "seconds": 0.0, "without_exchange_time": 0, "without_volume": 0, "volume_decreases": 0, "exchange_time_decreases": 0, "lag_median": None, "lag_p95": None,
                "lag_min": None, "lag_max": None, "volume_first": None, "volume_last": None}  # fmt: skip
    exch = [t["exch"] for t in ticks if t["exch"] is not None]
    vols = [t["volume"] for t in ticks if t["volume"] is not None]
    lags = [t["recv"] - t["exch"] for t in ticks if t["exch"] is not None]
    return {
        "ticks": n, "seconds": ticks[-1]["recv"] - ticks[0]["recv"], "without_exchange_time": n - len(exch), "without_volume": n - len(vols),
        "volume_decreases": sum(1 for a, b in zip(vols, vols[1:]) if b < a), "exchange_time_decreases": sum(1 for a, b in zip(exch, exch[1:]) if b < a),
        "lag_median": statistics.median(lags) if lags else None, "lag_p95": percentile(lags, 0.95), "lag_min": min(lags, default=None), "lag_max": max(lags, default=None),
        "volume_first": vols[0] if vols else None, "volume_last": vols[-1] if vols else None,
    }  # fmt: skip


def basis_from_ticks(fut: list[dict], spot: list[dict]) -> dict:
    """The basis at tick level: each futures tick against the latest spot tick that is not newer and at most BASIS_TICK_MAX_AGE seconds older (by exchange time, receipt time
    where a tick has none), then the last value in each second. `error` is how far each second's basis is from the median of the previous BASIS_TICK_WINDOW seconds: what a
    smoothed translation of a futures level to the spot scale would be off by."""

    def when(t):
        return t["exch"] if t["exch"] is not None else t["recv"]

    s = sorted((when(t), t["ltp"]) for t in spot)
    grid, j = {}, -1
    for t in sorted(fut, key=when):
        w = when(t)
        while j + 1 < len(s) and s[j + 1][0] <= w:
            j += 1
        if j >= 0 and w - s[j][0] <= BASIS_TICK_MAX_AGE:
            grid[int(w)] = t["ltp"] - s[j][1]
    secs = sorted(grid)
    errors = []
    for k, sec in enumerate(secs):
        prev = [grid[x] for x in secs[:k] if sec - x <= BASIS_TICK_WINDOW]
        if len(prev) >= 10:
            errors.append(abs(grid[sec] - statistics.median(prev)))
    steps = [abs(grid[b] - grid[a]) for a, b in zip(secs, secs[1:]) if b - a <= 5]  # change between neighbouring paired seconds (a gap of a few seconds is still neighbouring)
    levels = [grid[x] for x in secs]
    return {"futures_ticks": len(fut), "spot_ticks": len(spot), "seconds": len(secs), "mean": statistics.fmean(levels) if levels else None,
            "stdev": statistics.pstdev(levels) if len(levels) > 1 else None, "step_p99": percentile(steps, 0.99), "error_median": statistics.median(errors) if errors else None,
            "error_p99": percentile(errors, 0.99)}  # fmt: skip


def live_minute_volumes(ticks: list[dict]) -> dict[float, int]:
    """Volume traded in each COMPLETE minute that the listen saw end to end: the change of the cumulative day volume across the minute, taken from the last tick of
    the minute before to the last tick of the minute. The first minute heard (its start unseen) and the last (not finished) are left out."""
    last_in: dict[float, int] = {}
    for t in ticks:
        if t["exch"] is not None and t["volume"] is not None:
            minute = t["exch"] // 60 * 60
            last_in[minute] = max(last_in.get(minute, 0), t["volume"])  # the day volume only grows: the highest seen in a minute is its end, even if a tick arrived late
    minutes = sorted(last_in)
    return {m: last_in[m] - last_in[p] for p, m in zip(minutes, minutes[1:]) if m - p == 60 and m != minutes[-1]}


def compare_live_to_history(live: dict[float, int], c1: list[dict], lot_size: int = 1) -> dict:
    """The per-minute volume heard on the live feed against the finished 1M candles. The live day volume is in UNITS (the first real snapshot carried the exchange's own
    8,133,905 for a day whose candles add up to 124,806 lots of 65), the candle volume in LOTS, so the candles are multiplied by the lot size first."""
    by = {c["start"]: c for c in c1}
    diffs = [abs(v - (by[m]["volume"] or 0) * lot_size) / max((by[m]["volume"] or 0) * lot_size, 1) for m, v in live.items() if m in by]
    return {"minutes_compared": len(diffs), "median_rel_diff": statistics.median(diffs) if diffs else None, "p90_rel_diff": percentile(diffs, 0.9) if diffs else None,
            "max_rel_diff": max(diffs, default=None)}  # fmt: skip


# -- the verdict -------------------------------------------------------------------------------------------------------------------------------
def _req(rid: str, text: str, status: str, evidence: str) -> dict:
    return {"id": rid, "requirement": text, "status": status, "evidence": evidence}


def _all(days: list[dict], rid, text, ok, show, need: str) -> dict:
    """One requirement over every session checked: PASS only if every session passes; the evidence is the worst case."""
    scored = [(d["day"], ok(d), show(d)) for d in days if need(d) is not None]
    if not scored:
        return _req(rid, text, NOT_VERIFIED, "no session had the data for this check")
    bad = [f"{day}: {s}" for day, good, s in scored if not good]
    return _req(rid, text, FAIL if bad else PASS, "; ".join(bad) if bad else "; ".join(f"{day}: {s}" for day, _, s in scored))


def evaluate(f: dict) -> dict:
    """The requirements and the overall verdict, from what the probe found (`f`)."""
    r, days = [], f.get("days") or []
    c = f.get("contract")
    r.append(_req("R1", "the front-month NIFTY futures contract is identifiable from the master", PASS if c else FAIL,
                  f"{c['symbol']} token {c['token']} expires {c['expiry']} lot {c['lot_size']}" if c else "no tradable NIFTY futures contract in the instrument master"))  # fmt: skip
    if not days:
        for rid, text in (("R2", "volume"), ("R3", "complete"), ("R4", "consistent"), ("R8", "aligned"), ("R9", "basis")):
            r.append(_req(rid, text, NOT_VERIFIED, "no finished session could be fetched"))
    else:
        r.append(_all(days, "R2", "historical 1M candles carry volume", lambda d: 1 - d["fut"]["zero_or_missing_volume"] / max(d["fut"]["present"], 1) >= MIN_VOLUME_SHARE,
                      lambda d: f"{d['fut']['present'] - d['fut']['zero_or_missing_volume']}/{d['fut']['present']} minutes with volume", lambda d: d["fut"]))  # fmt: skip
        r.append(_all(days, "R3", "historical 1M candles are complete and OHLC-valid", lambda d: d["fut"]["present"] / d["fut"]["expected"] >= MIN_COMPLETE and d["fut"]["bad_ohlc"] == 0,
                      lambda d: f"{d['fut']['present']}/{d['fut']['expected']} minutes, {d['fut']['bad_ohlc']} invalid, first missing {d['fut']['missing_first']}", lambda d: d["fut"]))  # fmt: skip
        r.append(_all(days, "R4", "1M candles agree with the exchange's own 5M candles", lambda d: d["c1_c5"]["buckets"] > 0 and d["c1_c5"]["volume_equal"] / d["c1_c5"]["buckets"] >= BUCKET_MATCH,
                      lambda d: f"{d['c1_c5']['volume_equal']}/{d['c1_c5']['buckets']} buckets equal in volume ({d['c1_c5']['ohlc_equal']} in price), examples {d['c1_c5']['examples']}",
                      lambda d: d.get("c1_c5")))  # fmt: skip
        r.append(_all(days, "R8", "every spot minute has a futures candle on the same label", lambda d: d["aligned"] >= ALIGNED,
                      lambda d: f"{d['aligned']:.1%} of the spot minutes have a futures candle on the same label" + (f" (the spot history itself lacks {d['spot_gaps']['missing']} of the {d['fut']['expected']} session minutes, {d['spot_gaps']['first_missing']}-{d['spot_gaps']['last_missing']})" if d.get("spot_gaps") and d["spot_gaps"]["missing"] else ""),
                      lambda d: d.get("aligned")))  # fmt: skip
        r.append(_all(days, "R9", "the basis moves smoothly", lambda d: d["basis"]["step_p99"] is not None and d["basis"]["step_p99"] <= BASIS_STEP_P99,
                      lambda d: f"basis mean {d['basis']['mean']:.1f} pts (min {d['basis']['min']:.1f}, max {d['basis']['max']:.1f}), minute change p99 {d['basis']['step_p99']:.2f}, max {d['basis']['step_max']:.2f}; "
                                f"for information, a {SMOOTH_WINDOW}-minute median basis is off by a median of {d['basis']['smoothed_error_median']:.1f} and a p99 of {d['basis']['smoothed_error_p99']:.1f} pts",
                      lambda d: (d.get("basis") or {}).get("minutes") or None))  # fmt: skip
    st = f.get("stability")
    r.append(_req("R5", "a finished session fetched again is identical", NOT_VERIFIED if st is None else PASS if st["identical"] else FAIL,
                  "not run" if st is None else f"{st['day']}: {st['candles']} candles, {st['differences']} differences between two fetches"))  # fmt: skip
    q, latest = f.get("quote"), (days[-1] if days else None)
    comparable = bool(q and latest and quote_date(q) is not None and quote_date(q).isoformat() == latest["day"])
    why = "no quote" if not q else "the quote is not for the latest fetched session" if not comparable else ""
    avg = q.get("avgPrice") if q else None
    vw = latest.get("vwap") if latest else None
    if comparable and avg and vw:
        rel = abs(vw - float(avg)) / float(avg)
        r.append(_req("R6", "a VWAP from the 1M candles matches the exchange's average traded price", PASS if rel <= VWAP_TOLERANCE else FAIL,
                      f"{latest['day']}: candles (the whole day, closing session included) {vw:.2f}, exchange {float(avg):.2f}, difference {rel:.4%} (limit {VWAP_TOLERANCE:.2%}); "
                      f"the 09:15-15:30 session alone {latest['vwap_session']:.2f}"))  # fmt: skip
    else:
        r.append(_req("R6", "a VWAP from the 1M candles matches the exchange's average traded price", NOT_VERIFIED, why or "the quote had no average traded price"))
    tv, lot = (q.get("tradeVolume") if q else None), (c or {}).get("lot_size")
    if comparable and tv and lot:
        quantity = latest["lots_total"] * lot  # the candle volume is in lots (contracts); the exchange's day volume is in units
        rel = abs(quantity - int(tv)) / int(tv)
        r.append(_req("R7", "the candle volumes add up to the exchange's day volume", PASS if rel <= DAY_VOLUME_TOLERANCE else FAIL,
                      f"{latest['day']}: candles {latest['lots_total']} lots x lot size {lot} = {quantity} (of which {latest['post_close']['lots']} lots after 15:30), exchange {int(tv)}, difference {rel:.3%} (limit {DAY_VOLUME_TOLERANCE:.1%})"))  # fmt: skip
    else:
        r.append(_req("R7", "the candle volumes add up to the exchange's day volume", NOT_VERIFIED, why or "the quote had no day volume, or the lot size is unknown"))
    live = f.get("live")
    for rid, text in (("R10", "live futures ticks with a cumulative volume that never goes down"), ("R11", "live exchange timestamps are fresh"),
                      ("R12", "live per-minute volume matches the finished candles"), ("R13", "the feed recovers after a disconnect"),
                      ("R14", "the tick-level basis is smooth enough to put a futures level on the spot scale")):  # fmt: skip
        if not live or not live.get("verified"):
            r.append(_req(rid, text, NOT_VERIFIED, (live or {}).get("reason", "the live part was not run")))
            continue
        r.append(_live_req(rid, text, live))
    status = [x["status"] for x in r]
    verdict = "NOT RELIABLE" if FAIL in status else "CONFIRMED" if all(s == PASS for s in status) else "NOT CONFIRMED"
    return {"verdict": verdict, "requirements": r, "failed": [x["id"] for x in r if x["status"] == FAIL], "not_verified": [x["id"] for x in r if x["status"] == NOT_VERIFIED]}


def merge_evaluations(evaluations: list[dict], labels: list[str]) -> dict:
    """The verdict of several runs taken together. One run cannot verify everything: while the exchange is open its quote describes a session that is still running (so the
    comparisons with finished candles, R6 and R7, cannot be made), and while it is closed there is nothing live to listen to (R10-R13). A requirement takes its status from the runs
    that could check it: any FAIL wins, otherwise any PASS, otherwise it stays NOT VERIFIED. The evidence names the run it came from."""
    merged = []
    for rid in dict.fromkeys(x["id"] for ev in evaluations for x in ev["requirements"]):  # every requirement any run knew (an older findings file may lack the newest)
        seen = [(label, x) for ev, label in zip(evaluations, labels) for x in ev["requirements"] if x["id"] == rid]
        for wanted in (FAIL, PASS):
            hit = next(((label, x) for label, x in seen if x["status"] == wanted), None)
            if hit:
                merged.append({**hit[1], "evidence": f"[{hit[0]}] {hit[1]['evidence']}"})
                break
        else:
            merged.append({**seen[0][1], "evidence": "; ".join(f"[{label}] {x['evidence']}" for label, x in seen)})
    status = [x["status"] for x in merged]
    verdict = "NOT RELIABLE" if FAIL in status else "CONFIRMED" if all(s == PASS for s in status) else "NOT CONFIRMED"
    return {"verdict": verdict, "requirements": merged, "failed": [x["id"] for x in merged if x["status"] == FAIL],
            "not_verified": [x["id"] for x in merged if x["status"] == NOT_VERIFIED], "runs": labels}  # fmt: skip


def _live_req(rid: str, text: str, live: dict) -> dict:
    a = live["analysis"]
    if rid == "R14":
        b = live.get("basis_ticks") or {}
        counts = f"{b.get('futures_ticks', 0)} futures and {b.get('spot_ticks', 0)} spot ticks"
        if b.get("seconds", 0) < BASIS_TICK_MIN_SECONDS:
            return _req(rid, text, NOT_VERIFIED, f"only {b.get('seconds', 0)} seconds in which a futures tick and a spot tick could be paired (need {BASIS_TICK_MIN_SECONDS}): {counts}")
        if b.get("error_p99") is None:
            return _req(rid, text, NOT_VERIFIED, f"{b['seconds']} paired seconds, but too sparse to measure the distance from the {BASIS_TICK_WINDOW} s median (it needs 10 paired seconds inside {BASIS_TICK_WINDOW} s): {counts}")  # fmt: skip
        step = "n/a" if b["step_p99"] is None else f"{b['step_p99']:.2f}"
        return _req(rid, text, PASS if b["error_p99"] <= BASIS_TICK_P99 else FAIL,
                    f"{b['seconds']} paired seconds, basis mean {b['mean']:.1f} pts; change between neighbouring seconds p99 {step}; distance from its {BASIS_TICK_WINDOW} s median: median {b['error_median']:.2f}, p99 {b['error_p99']:.2f} (limit {BASIS_TICK_P99} pts)")  # fmt: skip
    if rid == "R10":
        enough = a["ticks"] >= LIVE_MIN_TICKS and a["seconds"] >= LIVE_MIN_SECONDS
        good = enough and a["volume_decreases"] == 0 and a["without_volume"] == 0
        return _req(rid, text, PASS if good else FAIL, f"{a['ticks']} ticks in {a['seconds']:.0f} s (need {LIVE_MIN_TICKS} in {LIVE_MIN_SECONDS}), {a['volume_decreases']} decreases of the day volume, {a['without_volume']} without volume")  # fmt: skip
    if rid == "R11":
        good = a.get("lag_median") is not None and a["without_exchange_time"] == 0 and a["exchange_time_decreases"] == 0 and a["lag_median"] <= LAG_MEDIAN and a["lag_p95"] <= LAG_P95
        return _req(rid, text, PASS if good else FAIL, f"lag median {a.get('lag_median')}, p95 {a.get('lag_p95')} s (limits {LAG_MEDIAN} / {LAG_P95}), {a['without_exchange_time']} ticks without an exchange time, {a['exchange_time_decreases']} going backwards")  # fmt: skip
    if rid == "R12":
        c = live.get("history_comparison") or {}
        if not c.get("minutes_compared"):
            return _req(rid, text, NOT_VERIFIED, "no complete minute could be compared with the finished candles")
        good = c["median_rel_diff"] <= LIVE_MEDIAN and c["p90_rel_diff"] <= LIVE_P90
        return _req(rid, text, PASS if good else FAIL, f"{c['minutes_compared']} minutes: median difference {c['median_rel_diff']:.3%}, p90 {c['p90_rel_diff']:.3%} (limits {LIVE_MEDIAN:.1%} / {LIVE_P90:.1%})")  # fmt: skip
    rec = live.get("recovery")
    if not rec:
        return _req(rid, text, NOT_VERIFIED, "no reconnect was tested (no ticks before or after it)")
    if "gap_minutes_in_history" not in rec:
        return _req(rid, text, NOT_VERIFIED, "the finished candles could not be fetched to check that the missed minutes are in them")
    good = rec["first_tick_delay"] is not None and rec["first_tick_delay"] <= RECOVERY_SECONDS and rec["volume_continuous"] and rec["time_continuous"] and rec["gap_minutes_in_history"]
    return _req(rid, text, PASS if good else FAIL, f"first tick {rec['first_tick_delay']} s after reconnecting (limit {RECOVERY_SECONDS} s), volume continuous {rec['volume_continuous']}, exchange time continuous {rec['time_continuous']}, {rec['missed_minutes']} missed minutes all in the finished candles: {rec['gap_minutes_in_history']}")  # fmt: skip


# -- talking to the broker (read-only) -------------------------------------------------------------------------------------------------------
def _call(session, method: str, *args):
    if method not in READ_ONLY_CALLS:
        raise ValueError(f"{method} is not a read-only call")
    return session.call(method, *args)


def fetch_candles(session, exchange: str, token: str, interval: str, day: date, to: str = "15:30") -> tuple[list[dict], int, str | None]:
    """One session of candles. Returns (candles, unusable rows, the broker's message if the call failed). The futures trade in a closing session after 15:30 (to about 15:40),
    and the exchange's own day volume and average price include it: ask `to` 15:45 to see it (the first run of this probe asked only to 15:30 and wrongly failed R6 and R7)."""
    params = {"exchange": exchange, "symboltoken": token, "interval": interval,
              "fromdate": f"{day.isoformat()} 09:15", "todate": f"{day.isoformat()} {to}"}  # fmt: skip
    try:
        resp = _call(session, "getCandleData", params)
    except Exception as e:
        return [], 0, f"{type(e).__name__}: {e}"
    if not resp or not resp.get("status"):
        return [], 0, str((resp or {}).get("message", "no response"))[:160]
    candles, bad = parse_candles(resp.get("data"))
    return candles, bad, None


def recent_session_days(now: datetime, wanted: int, extra: int = 4) -> list[date]:
    """Candidate days, newest first: weekdays whose session has finished (holidays are not known: a day with no candles is skipped by the caller)."""
    out, d = [], now.date()
    if now.hour * 3600 + now.minute * 60 + now.second < CLOSE_S + 120:
        d -= timedelta(days=1)  # today's session is not finished (or its last candle has not been published yet)
    while len(out) < wanted + extra:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN_S <= now.hour * 3600 + now.minute * 60 + now.second < CLOSE_S  # (an exchange holiday is not known)


def listen(ws_factory, session, token: str, seconds: float, clock=time.time, sleep=time.sleep, spot_token: str | None = None) -> dict:
    """One WebSocket connection to the futures token (and the spot index, for the tick-level basis), read-only. Returns the ticks heard (each says which token), the errors,
    and when the connection opened."""
    ws = ws_factory(session)
    ticks_, errors, state = [], [], {"opened": None}
    groups = [{"exchangeType": 2, "tokens": [token]}] + ([{"exchangeType": 1, "tokens": [spot_token]}] if spot_token else [])

    def on_open(_):
        state["opened"] = clock()
        try:
            ws.subscribe("probe0001", QUOTE_MODE, groups)
        except Exception as e:
            errors.append(f"subscribe failed: {e}")

    def on_data(_, msg):
        recv = clock()
        try:
            t = tk.decode(msg, recv, strict_time=False)
        except ValueError as e:
            errors.append(f"undecodable tick: {e}")
            return
        if t.token in (str(token), str(spot_token)):
            ticks_.append({"token": t.token, "recv": recv, "exch": t.exch_ts, "ltp": t.ltp, "volume": t.volume, "ltq": t.ltq, "oi": t.oi, "note": t.exch_note})

    ws.on_open, ws.on_data = on_open, on_data
    ws.on_message = lambda _, m: errors.append(f"server message: {str(m)[:160]}")
    ws.on_error = lambda _, e: errors.append(f"websocket error: {str(e)[:160]}")
    ws.on_close = lambda _: None
    thread = threading.Thread(target=ws.connect, daemon=True)  # connect() blocks until the connection ends
    started = clock()
    thread.start()
    sleep(seconds)
    try:
        ws.close_connection()
    except Exception as e:
        errors.append(f"close failed: {e}")
    thread.join(timeout=10)
    return {"ticks": ticks_, "errors": errors, "started": started, "opened": state["opened"]}


def live_phase(ws_factory, session, token: str, seconds: float, sleep=time.sleep, clock=time.time, settle: float = 90.0, reconnect_seconds: float = 45.0, day: date | None = None,
               spot_token: str | None = None, lot_size: int | None = None) -> dict:  # fmt: skip
    """The live checks: listen, reconnect, then compare what was heard with the finished candles. Only meaningful while the exchange is open."""
    first = listen(ws_factory, session, token, seconds, clock, sleep, spot_token)
    second = listen(ws_factory, session, token, reconnect_seconds, clock, sleep, spot_token)
    for part in (first, second):  # the futures ticks are what the volume, freshness and recovery checks read; the spot ticks are only for the basis
        part["spot"] = [t for t in part["ticks"] if t["token"] != str(token)]
        part["ticks"] = [t for t in part["ticks"] if t["token"] == str(token)]
    analysis = analyse_ticks(first["ticks"])
    out = {"verified": True, "seconds_requested": seconds, "analysis": analysis, "errors": first["errors"][:5], "snapshot_on_subscribe_delay": None,
           "basis_ticks": basis_from_ticks(first["ticks"] + second["ticks"], first["spot"] + second["spot"])}  # fmt: skip
    if first["opened"] is not None and first["ticks"]:
        out["snapshot_on_subscribe_delay"] = round(first["ticks"][0]["recv"] - first["opened"], 2)
    if first["ticks"] and second["ticks"]:
        a, b = first["ticks"][-1], second["ticks"][0]
        out["recovery"] = {"first_tick_delay": round(b["recv"] - second["opened"], 2) if second["opened"] else None, "disconnected_seconds": round(b["recv"] - a["recv"], 1),
                           "volume_continuous": b["volume"] is not None and a["volume"] is not None and b["volume"] >= a["volume"],
                           "time_continuous": b["exch"] is not None and a["exch"] is not None and b["exch"] >= a["exch"]}  # fmt: skip
    all_ticks = first["ticks"] + second["ticks"]
    out["analysis_all"] = analyse_ticks(all_ticks)
    sleep(settle)  # the finished minute candles are published a little after the minute ends
    day = day or datetime.fromtimestamp(clock(), IST).date()
    candles, _, err = fetch_candles(session, FUT_EXCHANGE, token, "ONE_MINUTE", day)
    if err or not candles:
        out["history_comparison"], out["history_error"] = None, err or "no candles"
        return out
    if not lot_size:
        out["history_comparison"], out["history_error"] = None, "the lot size is unknown, so the live volume (units) cannot be compared with the candle volume (lots)"
        return out
    out["history_comparison"] = compare_live_to_history(live_minute_volumes(all_ticks), candles, lot_size)
    if "recovery" in out:  # the minutes between the two connections must be in the finished candles
        gap = [m for m in range(int(first["ticks"][-1]["exch"] // 60 * 60) + 60, int(second["ticks"][0]["exch"] // 60 * 60), 60)] if first["ticks"][-1]["exch"] and second["ticks"][0]["exch"] else []
        have = {c["start"] for c in candles}
        out["recovery"]["missed_minutes"] = len(gap)
        out["recovery"]["gap_minutes_in_history"] = all(m in have for m in gap)
    return out


def run(cfg, days: int = 3, live_seconds: float | None = None, out: str | None = None, now: datetime | None = None, session=None, master_rows=None, ws_factory=None,
        sleep=time.sleep, clock=time.time, cache_dir: str = ".cache", settle: float = 90.0, quote_pause: float = 1.2) -> dict:
    """The whole probe. `session`, `master_rows`, `ws_factory` and the clock are injectable so it can be tested with fakes; nothing else differs."""
    now = now or datetime.now(IST)
    session = session or AngelSession(cfg, limiter=CandleLimiter())
    rows = master_rows if master_rows is not None else load_futures_master(cache_dir, now.date())
    contract = front_month(futures_contracts(rows), now)
    spots = [r for r in rows if r.get("name") == "NIFTY" and r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX"]
    spot_row = next((r for r in spots if str(r.get("symbol", "")).lower() == "nifty 50"), spots[0] if spots else None)  # chosen exactly as instruments.find_spot chooses
    spot_token = str(spot_row["token"]) if spot_row else SPOT_TOKEN
    found: dict = {"generated": now.isoformat(), "contract": contract and {**contract, "expiry": contract["expiry"].isoformat()}, "all_contracts": [
        {"symbol": c["symbol"], "token": c["token"], "expiry": c["expiry"].isoformat(), "lot_size": c["lot_size"]} for c in futures_contracts(rows)], "days": [], "notes": []}  # fmt: skip
    if contract is None:
        found["evaluation"] = evaluate({"contract": None})
        return _finish(found, out)
    token = contract["token"]
    for day in recent_session_days(now, days):
        if len(found["days"]) >= days:
            break
        c1, bad1, err = fetch_candles(session, FUT_EXCHANGE, token, "ONE_MINUTE", day, FUT_TO)
        if err or not c1:
            found["notes"].append(f"{day}: no futures 1M candles ({err or 'none returned: a holiday?'})")
            continue
        c5, _, err5 = fetch_candles(session, FUT_EXCHANGE, token, "FIVE_MINUTE", day)
        spot, _, errs = fetch_candles(session, SPOT_EXCHANGE, spot_token, "ONE_MINUTE", day)
        closed_at = session_starts(day)[-1] + 60  # 15:30
        inside = [c for c in c1 if c["start"] < closed_at]
        post = [c for c in c1 if c["start"] >= closed_at]
        entry = {"day": day.isoformat(), "fut": check_day(c1, day), "unusable_rows": bad1, "c1_c5": consistency_1m_5m(c1, c5, day) if c5 else None,
                 "post_close": {"candles": len(post), "lots": sum(c["volume"] or 0 for c in post), "last": hm(post[-1]["start"]) if post else None},
                 "lots_total": sum(c["volume"] or 0 for c in c1), "vwap": session_vwap(c1), "vwap_session": session_vwap(inside)}  # fmt: skip
        fu = {c["start"] for c in c1}
        entry["aligned"] = (sum(1 for s in spot if s["start"] in fu) / len(spot)) if spot else None  # every minute the SPOT has must have a futures candle on the same label
        in_spot = {s["start"] for s in spot}
        absent = [t for t in session_starts(day) if t not in in_spot]
        entry["spot_gaps"] = {"present": len(spot) - sum(1 for s in spot if s["start"] not in set(session_starts(day))), "missing": len(absent), "first_missing": hm(absent[0]) if absent else None,
                              "last_missing": hm(absent[-1]) if absent else None} if spot else None  # fmt: skip
        entry["basis"] = basis_stats(c1, spot, day) if spot else None
        entry["spot"] = check_day(spot, day) if spot else None
        for what, e in (("5M", err5), ("spot", errs)):
            if e:
                found["notes"].append(f"{day}: {what} candles failed ({e})")
        found["days"].append(entry)
        found.setdefault("_c1", {})[day.isoformat()] = c1
    found["days"].sort(key=lambda d: d["day"])
    if found["days"]:  # stability: the newest finished session, fetched again
        latest = found["days"][-1]["day"]
        again, _, e2 = fetch_candles(session, FUT_EXCHANGE, token, "ONE_MINUTE", date.fromisoformat(latest), FUT_TO)
        first = found.pop("_c1")[latest]
        found["stability"] = {"day": latest, "candles": len(first), "differences": (sum(1 for a, b in zip(first, again) if a != b) + abs(len(first) - len(again))) if not e2 else None,
                              "identical": (not e2) and first == again}  # fmt: skip
    found.pop("_c1", None)
    sleep(quote_pause)
    try:
        found["quote"] = quote_summary(_call(session, "getMarketData", "FULL", {FUT_EXCHANGE: [token]}), token)
    except Exception as e:
        found["quote"] = None
        found["notes"].append(f"quote failed: {type(e).__name__}: {e}")
    live_for = (180.0 if market_open(now) else 15.0) if live_seconds is None else live_seconds
    if live_seconds == 0:
        found["live"] = {"verified": False, "reason": "skipped (--no-live)"}
    elif not market_open(now):
        found["live"] = {"verified": False, "reason": f"the exchange is closed ({now.strftime('%A %H:%M')} IST): live volume, exchange-timestamp freshness and recovery can only be checked while it is open"}
        heard = listen(ws_factory or _default_ws_factory(cfg), session, token, live_for, clock, sleep, spot_token)  # informational: does the broker push a snapshot on subscribing when it is closed?
        found["live"]["closed_market_listen"] = {"seconds": live_for, "ticks": len(heard["ticks"]), "errors": heard["errors"][:3], "analysis": analyse_ticks([t for t in heard["ticks"] if t["token"] == token]),
                                                 "first_ticks": heard["ticks"][:3]}  # fmt: skip
    else:
        found["live"] = live_phase(ws_factory or _default_ws_factory(cfg), session, token, live_for, sleep, clock, settle, spot_token=spot_token, lot_size=contract["lot_size"])
    found["evaluation"] = evaluate(found)
    return _finish(found, out)


def _default_ws_factory(cfg):
    from .feed import default_ws_factory

    return default_ws_factory(cfg)


def _finish(found: dict, out: str | None) -> dict:
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found, indent=2, default=str), encoding="utf-8")
    return found


def summary(found: dict) -> str:
    ev = found["evaluation"]
    lines = [f"NIFTY futures probe: {ev['verdict']}"]
    lines += [f"  {x['id']:<4}{x['status']:<13}{x['requirement']}\n        {x['evidence']}" for x in ev["requirements"]]
    lines += [f"  note: {n}" for n in found.get("notes", [])]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only probe of the NIFTY index futures data (see the module docstring)")
    ap.add_argument("--days", type=int, default=3, help="finished sessions to check")
    ap.add_argument("--live-seconds", type=float, default=None, help="how long to listen to the live feed (default 180 s while the exchange is open, 15 s when closed)")
    ap.add_argument("--no-live", action="store_true", help="history and quotes only")
    ap.add_argument("--out", default=f"logs/futures-probe-{datetime.now(IST).date().isoformat()}.json", help="where to write the findings (JSON, no credentials)")
    ap.add_argument("--merge", nargs="+", metavar="FINDINGS", help="no broker call: combine the verdicts of earlier runs (a run while the exchange is open and one after it closed)")
    args = ap.parse_args(argv)
    if args.merge:
        evals = [json.loads(Path(p).read_text(encoding="utf-8"))["evaluation"] for p in args.merge]
        print(summary({"evaluation": merge_evaluations(evals, [Path(p).name for p in args.merge]), "notes": []}))
        return 0
    from .config import ConfigError, load_config

    try:  # the SDK logs raw messages at INFO: nothing of a login or a response should ever reach the console
        from logzero import logger as sdk_logger

        sdk_logger.setLevel(logging.WARNING)
    except ImportError:
        pass
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"cannot run: {e}")
        return 2
    found = run(cfg, args.days, 0 if args.no_live else args.live_seconds, args.out)
    print(summary(found))
    print(f"findings written to {args.out}")
    return 0 if found["evaluation"]["verdict"] != "NOT RELIABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
