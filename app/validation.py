"""Do the candles Prism builds from sampled ticks agree with Angel One's official candles?

After the session, the completed NIFTY candles built live (1M, 5M, 15M) are compared with the official candles of the same day from
Angel One's historical API, open / high / low / close separately. Nothing is ever overwritten: live candles stay as they are, and the
comparison only reports. The result goes to `logs/candle-validation-<date>.json` and to the dashboard.

How a candle is classified (no numeric tolerance is invented; this one rule is a fact about trading):
  match       all four prices equal the official candle's (to the paisa)
  sampling    they differ, but every live price lies inside the official candle's low-high range. This is what sampled ticks cause: the
              index prints about once a second, so a live candle can miss the true extreme or the first / last print of the minute
  material    a live price lies OUTSIDE the official range: a tick that does not belong to that candle (a timestamp / bucketing problem)
  missing_live / missing_official   a candle exists on one side only
  unverified  the live candle is known to be partial (a gap overlapped it), so it was not compared, or it came from the official history
              in the first place (comparing it would prove nothing)
How large a sampling difference is acceptable is NOT decided here: that needs these daily reports to calibrate. Until then the numbers are
shown as they are.

Limits: the official candles come from the same Angel One historical API Prism already uses, so this checks the tick-building and the
candle timing, not Angel One itself; and an official candle can still be revised. If the API cannot be read, the report says so instead
of inventing a result."""

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

from .candles import CLOSE_S, IST, OPEN_S, TIMEFRAMES, day_start
from .gaps import API_INTERVAL, _parse_history

log = logging.getLogger(__name__)

COMPARE = ("1m", "5m", "15m")
FIELDS = ("open", "high", "low", "close")
SAME = 0.005  # prices are in paise: a smaller difference is rounding
VALIDATE_AFTER = 15 * 3600 + 40 * 60  # 15:40 IST: the day's candles are final
MAX_ATTEMPTS = 3
RETRY_SECONDS = 300
EXAMPLES = 10
LIMITATIONS = [
    "The official candles come from the same Angel One historical API as Prism's history: this checks how Prism builds candles from ticks, not Angel One's own data.",
    "'sampling' differences are expected from tick-built candles. How large one may be before it matters is not decided yet: it needs these daily reports to calibrate.",
    "'material' is an objective test (a live price outside the official candle's range), not a size threshold.",
]


def compare(live: list, official: list, tf: str) -> dict:
    """One timeframe: the live candles of one day against the official ones of that day."""
    secs = TIMEFRAMES[tf]
    off = {c.start: c for c in official}
    out = {"tf": tf, "live_candles": len(live), "official_candles": len(official), "compared": 0, "match": 0, "sampling": 0, "material": 0,
           "missing_live": 0, "missing_official": 0, "unverified_partial": 0, "recovered_from_official": 0, "before_live_start": 0,
           "possible_timestamp_shift": False, "examples": {"material": [], "sampling": [], "missing_live": [], "missing_official": []}}  # fmt: skip
    if not live:
        out["unverified"] = "no live candles were built for this timeframe"
        return out
    first_live = min(c.start for c in live)
    diffs = {f: [] for f in FIELDS}
    pcts = {f: [] for f in FIELDS}
    seen, no_official = set(), []
    for c in sorted(live, key=lambda c: c.start):
        seen.add(c.start)
        if c.source != "live":
            out["recovered_from_official"] += 1  # it IS the official candle: comparing it with itself would prove nothing
            continue
        if c.partial:
            out["unverified_partial"] += 1
            continue
        o = off.get(c.start)
        if o is None:
            out["missing_official"] += 1
            no_official.append(c.start)
            _example(out, "missing_official", c.start)
            continue
        out["compared"] += 1
        d = {f: getattr(c, f) - getattr(o, f) for f in FIELDS}
        for f in FIELDS:
            diffs[f].append(abs(d[f]))
            pcts[f].append(100 * abs(d[f]) / o.close if o.close else 0.0)
        inside = all(o.low - SAME <= getattr(c, f) <= o.high + SAME for f in FIELDS)
        kind = "match" if all(abs(x) <= SAME for x in d.values()) else "sampling" if inside else "material"
        out[kind] += 1
        if kind != "match":
            _example(out, kind, c.start, {f: round(d[f], 2) for f in FIELDS if abs(d[f]) > SAME}, {f: getattr(o, f) for f in ("low", "high")})
    for start in sorted(off):
        if start in seen:
            continue
        if start < first_live:
            out["before_live_start"] += 1  # Prism was not watching yet (it started later): not a missing candle
        else:
            out["missing_live"] += 1
            _example(out, "missing_live", start)
    # live candles with no official twin, each sitting exactly one interval from an official candle: the timestamps may be offset
    out["possible_timestamp_shift"] = len(no_official) >= 3 and all(s + secs in off or s - secs in off for s in no_official)
    out["fields"] = {f: {"mean_abs": sum(v) / len(v), "max_abs": max(v), "mean_pct": sum(pcts[f]) / len(v), "max_pct": max(pcts[f])} for f, v in diffs.items() if v}
    return out


def _example(out: dict, kind: str, start: float, diff=None, official=None) -> None:
    if len(out["examples"][kind]) < EXAMPLES:
        out["examples"][kind].append(start if diff is None else {"start": start, "diff": diff, "official_range": official})


def fetch_official(session, token: str, day: date, tf: str, sleep=time.sleep, retries: int = 2) -> list:
    """The day's official candles for one timeframe. Raises if the API cannot be read (the caller reports that, it does not guess)."""
    params = {"exchange": "NSE", "symboltoken": token, "interval": API_INTERVAL[tf],
              "fromdate": f"{day.isoformat()} 09:15", "todate": f"{day.isoformat()} 15:30"}  # fmt: skip
    last = None
    for attempt in range(retries + 1):
        try:
            resp = session.call("getCandleData", params)
            if not resp or not resp.get("status"):
                raise RuntimeError((resp or {}).get("message", "no response"))
            candles = _parse_history(resp.get("data") or [], tf)
            return [c for c in candles if OPEN_S <= c.start - day_start(c.start) < CLOSE_S]
        except Exception as e:  # the shared limiter already waits out a rate-limit cool-down before the next request
            last = e
            sleep(3.0 if attempt < retries else 0.0)
    raise RuntimeError(f"official {tf} candles could not be read: {last}")


def validate_day(session, md, day: date, sleep=time.sleep, directory="logs") -> dict:
    """Compare today's completed live NIFTY candles with the official ones, write the report, remember it for the dashboard."""
    with md.lock:
        spot = md.spot_token
        live = {tf: [c for c in (md.builder.closed.get((spot, tf), ()) if md.builder else ()) if datetime.fromtimestamp(c.start, IST).date() == day] for tf in COMPARE}
    result = {"date": day.isoformat(), "generated": time.time(), "timeframes": {}, "limitations": LIMITATIONS}
    for tf in COMPARE:
        try:
            result["timeframes"][tf] = compare(live[tf], fetch_official(session, spot, day, tf, sleep), tf)
        except Exception as e:
            result["timeframes"][tf] = {"tf": tf, "unverified": str(e)[:200]}
        sleep(0.4)
    done = [t for t in result["timeframes"].values() if "unverified" not in t]
    result["status"] = "complete" if len(done) == len(COMPARE) else "partial" if done else "unavailable"
    result["lines"] = summary_lines(result)
    try:
        path = Path(directory) / f"candle-validation-{day.isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        result["file"] = str(path)
    except OSError as e:
        log.warning("the candle validation report could not be written: %s", e)
    with md.lock:
        md.validation = {k: result[k] for k in ("date", "status", "lines", "generated") if k in result} | {"file": result.get("file")}
    return result


def summary_lines(result: dict) -> list[str]:
    out = []
    for tf, t in result["timeframes"].items():
        label = tf.upper()
        if "unverified" in t:
            out.append(f"{label}: not verified ({t['unverified']})")
            continue
        out.append(f"{label}: {t['compared']} compared: {t['match']} exact, {t['sampling']} within sampling, {t['material']} MATERIAL; "
                   f"{t['missing_live']} missing live, {t['missing_official']} missing official, {t['unverified_partial']} partial (not compared)"
                   + ("; POSSIBLE TIMESTAMP SHIFT" if t["possible_timestamp_shift"] else ""))  # fmt: skip
    return out


def run(cfg, md, session, stop, now=time.time, wait=None, sleep=time.sleep, directory="logs") -> None:
    """Background thread: once a day, after the session, when the live candles exist. Retries a failed attempt a few times, then reports it."""
    wait = wait or stop.wait
    attempts, last_try, day = 0, None, None
    while not stop.is_set():
        t = now()
        dt = datetime.fromtimestamp(t, IST)
        if dt.date() != day:
            day, attempts, last_try = dt.date(), 0, None
        due = (dt.weekday() < 5 and dt.hour * 3600 + dt.minute * 60 + dt.second >= VALIDATE_AFTER and attempts < MAX_ATTEMPTS and md.spot_token
               and (md.validation or {}).get("date") != day.isoformat() and (last_try is None or t - last_try >= RETRY_SECONDS))  # fmt: skip
        if due and md.builder is not None and any(md.builder.closed.get((md.spot_token, "1m"), ())):
            attempts, last_try = attempts + 1, t
            try:
                result = validate_day(session, md, day, sleep, directory)
                if result["status"] != "complete" and attempts >= MAX_ATTEMPTS:
                    log.warning("candle validation incomplete after %d attempts: %s", attempts, result["lines"])
                elif result["status"] != "complete":
                    with md.lock:
                        md.validation = None  # try again later; the failed attempt is not "the day's result"
            except Exception as e:  # this thread must never die
                log.warning("candle validation failed: %s", e)
        wait(30)
