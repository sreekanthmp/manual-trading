"""History for the analysis: recent candles read from Prism DB (strictly read-only), the days Prism DB
lacks fetched from Angel One into memory only (nothing is ever written to Prism DB), and a refresh at
the start of each trading session."""

import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta

from .auth import AuthError, is_transient
from .candles import IST, OPEN_S, TIMEFRAMES, Candle, bucket, day_start, in_session
from .gaps import API_INTERVAL, describe_span
from .ratelimit import is_refusal
from .readiness import describe_gaps, decision_window_gaps
from .series import missing_starts

log = logging.getLogger(__name__)

SYMBOL = "NIFTY"
DB_TIMEFRAME = {"1m": "1min", "5m": "5min", "15m": "15min", "1d": "1day"}
# Just what the indicators need, not the table: 1H is built from 15M, and the analysis uses at most
# 500 candles per timeframe (150 are needed to warm up the slowest indicator).
LOOKBACK_DAYS = {"1m": 5, "5m": 14, "15m": 70, "1d": 45}
MAX_ROWS = 5000  # hard cap per query; ORDER BY ... DESC keeps the newest rows if it is ever reached
SECONDS = {**TIMEFRAMES, "1d": 86400}
INTERVAL = {**API_INTERVAL, "1d": "ONE_DAY"}
MAX_BACKFILL_DAYS = 28
RETRY_SECONDS = 300  # between refresh attempts after a failure of the database or an unexpected error
RETRY_FAST = (20.0, 40.0, 80.0, 160.0)  # between refresh attempts after a TEMPORARY failure of the candle requests (refused for their rate, unreachable, a window
# that came back incomplete): the progress already made is kept, so a retry only asks for what is still missing, and the shared limiter makes it wait out a cool-down
AUTH_RETRY = 900.0  # a login that Angel One rejected is not hammered
RETRIES = 2  # more tries of one refused candle request, RETRY_PAUSE seconds apart
RETRY_PAUSE = 3.0
MAX_ATTEMPTS = 28  # refresh attempts per day (the first one included): over two hours of retries; giving up sooner leaves the whole day without a decision
POLL_SECONDS = 10  # how often the refresh loop checks the clock (no database access)
SQL = (
    "SELECT datetime, open, high, low, close, volume FROM ohlcv "
    "WHERE symbol = %s AND timeframe = %s AND datetime >= %s ORDER BY datetime DESC LIMIT %s"
)


def ist_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, IST).date()


def session_started(ts: float) -> bool:
    """True from 09:15 IST on a weekday (a holiday is not known; refreshing on one is harmless)."""
    dt = datetime.fromtimestamp(ts, IST)
    return dt.weekday() < 5 and dt.hour * 3600 + dt.minute * 60 + dt.second >= OPEN_S


# -- Prism DB (read-only) ---------------------------------------------------------------------------------
def _to_candles(rows, tf: str) -> list[Candle]:
    """DB rows (tz-aware datetimes in any zone) -> candles, oldest first, one per start time. Intraday
    candles outside the 09:15-15:30 IST session are dropped."""
    out: dict[float, Candle] = {}
    for dt, o, h, low, c, v in rows:
        start = dt.timestamp()
        if tf == "1d":
            end = start + 86400
        elif in_session(start):
            end = bucket(start, SECONDS[tf])[1]
        else:
            continue
        out.setdefault(start, Candle(start, end, float(o), float(h), float(low), float(c), int(v or 0), source="db"))
    return sorted(out.values(), key=lambda c: c.start)


def load_db(cfg, now: float, connect=None) -> dict[str, list[Candle]]:
    """Recent NIFTY candles from Prism DB. The session is read-only twice over (server setting and
    session flag), only SELECT is ever issued, and every query is bounded by date and row count."""
    if not cfg.pg_password:
        raise RuntimeError("PG_PASSWORD is not set; cannot read Prism DB")
    if connect is None:
        import psycopg2

        connect = psycopg2.connect
    conn = connect(host=cfg.pg_host, port=cfg.pg_port, user=cfg.pg_user, password=cfg.pg_password,
                   dbname=cfg.pg_dbname, connect_timeout=10,
                   options="-c default_transaction_read_only=on -c statement_timeout=15000")  # fmt: skip
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        out = {}
        for tf, db_tf in DB_TIMEFRAME.items():
            since = datetime.fromtimestamp(now, IST) - timedelta(days=LOOKBACK_DAYS[tf])
            cur.execute(SQL, (SYMBOL, db_tf, since, MAX_ROWS))
            out[tf] = _to_candles(cur.fetchall(), tf)
        return out
    finally:
        conn.close()


# -- Angel One: what Prism DB lacks (memory only) --------------------------------------------------------
def _align(dt: datetime, daily: bool) -> datetime:
    """Move a resume point onto trading hours: intraday starts at 09:15 at the earliest, and at the next
    day's open once the session has ended; daily starts at midnight."""
    if daily:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    opening = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    if dt < opening:
        return opening
    if dt >= dt.replace(hour=15, minute=30, second=0, microsecond=0):
        return opening + timedelta(days=1)
    return dt


def classify(e) -> str:
    """Why a candle request failed, so the right thing is done about it: `auth` (Angel One rejected the login: retrying only risks the account), `rate_limit`
    (it refused the request for going too fast: wait out the cool-down), `network` (it could not be reached) or `other`."""
    if isinstance(e, AuthError):
        return "auth"
    if is_refusal(e):
        return "rate_limit"
    if is_transient(e):
        return "network"
    return "other"


def fill_gap(session, token: str, hist: dict, now: float, sleep=time.sleep, errors: list | None = None, kinds: set | None = None) -> int:
    """Fetch the candles that are missing, up to now, into `hist`: from each timeframe's last candle on (resuming at the very next interval, so nothing already
    held is downloaded again) and, on a trading day, from the earliest interval of today's session that is absent, so a hole in the middle of the day is
    fetched too. Only completed candles are kept; nothing is ever made up. Returns the number added. Failed requests are appended to `errors` and, if given,
    their `classify` kinds to `kinds`; a rejected login stops the whole fill at once."""
    now_dt = datetime.fromtimestamp(now, IST)
    today_midnight = day_start(now)
    added = 0
    for tf in DB_TIMEFRAME:
        daily = tf == "1d"
        have = {c.start for c in hist.setdefault(tf, [])}
        last = max(have, default=None)
        if last is not None:
            cursor = datetime.fromtimestamp(last + SECONDS[tf], IST)
        else:  # nothing held at all: the last few days, from the open
            cursor = datetime.fromtimestamp(today_midnight, IST) - timedelta(days=5)
        if not daily and now_dt.weekday() < 5 and last is not None:
            holes = missing_starts(have, SECONDS[tf], now, today_midnight + OPEN_S)
            if holes and datetime.fromtimestamp(holes[0], IST) < cursor:
                cursor = datetime.fromtimestamp(holes[0], IST)  # an absent interval inside today's session: ask for it, and only from there
        cursor = _align(max(cursor, datetime.fromtimestamp(today_midnight, IST) - timedelta(days=MAX_BACKFILL_DAYS)), daily)
        while cursor < now_dt:
            if daily and cursor.date() >= now_dt.date():
                break  # today's daily candle is still forming and would be thrown away: not worth a request that can fail
            end = min(datetime.combine(cursor.date() + timedelta(days=4), dtime(15, 30), tzinfo=IST), now_dt)
            frm, cursor = cursor, _align(datetime.combine(cursor.date() + timedelta(days=5), dtime(0, 0), tzinfo=IST), daily)
            if frm >= end:
                continue
            fmt = "%Y-%m-%d %H:%M"  # small windows keep each response complete
            params = {"exchange": "NSE", "symboltoken": token, "interval": INTERVAL[tf],
                      "fromdate": frm.strftime(fmt), "todate": end.strftime(fmt)}  # fmt: skip
            rows, failure, kind, tries = None, None, "other", 0
            for attempt in range(RETRIES + 1):  # the candle API refuses bursts ("exceeding access rate"): one refusal must not mark the day stale
                tries = attempt + 1
                try:
                    resp = session.call("getCandleData", params)
                    if not resp or not resp.get("status"):
                        raise RuntimeError((resp or {}).get("message", "no response"))
                    rows = [(datetime.fromisoformat(r[0]), *r[1:6]) for r in resp.get("data") or []]
                    break
                except Exception as e:
                    failure, kind = e, classify(e)
                    if kind == "auth":
                        break  # Angel One rejected the login: asking again only risks the account
                    sleep(RETRY_PAUSE if attempt < RETRIES else 0.4)
            if rows is None:
                log.warning("backfill %s %s..%s failed (%s): %s", tf, params["fromdate"], params["todate"], kind, failure)
                if errors is not None:
                    errors.append(f"{tf} {params['fromdate']}: {failure}")
                if kinds is not None:
                    kinds.add(kind)
                if kind == "auth":
                    return added
                continue
            usable, kept = _to_candles(rows, tf), 0  # (those inside the session)
            for c in usable:
                complete = c.start < today_midnight if daily else c.end <= now  # never the forming candle
                if complete and c.start not in have:
                    c.source = "angel"
                    hist[tf].append(c)
                    have.add(c.start)
                    added += 1
                    kept += 1
            # what Angel One really answered: an answer with no candles, or candles outside the session, looks like success from the outside
            log.log(logging.INFO if rows else logging.WARNING, "backfill %s %s..%s: answered on try %d with %d candle(s) (%s), %d inside the session, %d new kept",
                    tf, params["fromdate"], params["todate"], tries, len(rows), describe_span([r[0].timestamp() for r in rows]), len(usable), kept)
            sleep(0.4)  # the candle API allows about 3 requests per second
        hist[tf].sort(key=lambda c: c.start)
    return added


def held_today(hist: dict, now: float) -> str:
    """`1m 120 (11-11 09:15..11-11 11:16), 5m 0 (none)`: how many of today's candles the history holds per timeframe, and from when to when. For the log only."""
    day = ist_date(now)
    out = []
    for tf in ("1m", "5m", "15m"):
        starts = [c.start for c in hist.get(tf, ()) if ist_date(c.start) == day]
        out.append(f"{tf} {len(starts)} ({describe_span(starts)})")
    return ", ".join(out)


def _carry_over(new: dict, old: dict, now: float) -> None:
    """Keep Angel candles fetched earlier that Prism DB still lacks, so they are not downloaded again."""
    for tf, candles in old.items():
        cutoff = day_start(now) - LOOKBACK_DAYS[tf] * 86400
        have = {c.start for c in new.setdefault(tf, [])}
        new[tf] += [c for c in candles if c.source == "angel" and c.start not in have and c.start >= cutoff]
        new[tf].sort(key=lambda c: c.start)


def coverage(hist: dict) -> dict:
    return {tf: {"bars": len(cs), "first": cs[0].start if cs else None, "last": cs[-1].start if cs else None}
            for tf, cs in hist.items()}  # fmt: skip


# -- refresh --------------------------------------------------------------------------------------------------
def _spot_token(md, sleep) -> str | None:
    for _ in range(150):  # the feed picks the contracts shortly after start
        token = next((c.token for c in md.contracts if c.role == "SPOT"), None)
        if token:
            return token
        sleep(2)
    return None


def refresh(cfg, md, session, now=time.time, sleep=time.sleep, loader=load_db) -> bool:
    """Load history and make it current for today's session. Never raises. Returns True when complete.

    On failure the previous history (if any) stays in use but is reported as stale, never as current. While it runs, `md.history_busy`
    tells the gap recovery to stand aside, so the two never compete for the candle API."""
    if not md.refresh_lock.acquire(blocking=False):  # one refresh at a time, whoever asks: two would fetch the same candles twice and fight over the API
        log.info("history refresh already running: not starting a second one")
        return False
    md.history_busy = True
    try:
        return _refresh(cfg, md, session, now, sleep, loader)
    finally:
        md.history_busy = False
        md.refresh_lock.release()


def _retry_delay(md, kind: str) -> float:
    """Seconds before a refresh that failed for `kind` is tried again."""
    if kind == "auth":
        return AUTH_RETRY
    delay = RETRY_FAST[md.history_failures - 1] if 0 < md.history_failures <= len(RETRY_FAST) else RETRY_SECONDS
    if kind == "rate_limit":
        delay = max(delay, md.limiter.status()["cooldown_seconds"] + 5.0)  # not before the cool-down the limiter is already enforcing
    return delay


def _refresh(cfg, md, session, now, sleep, loader) -> bool:
    started = now()
    log.info("history refresh started (attempt %d today)", md.history_attempts)
    try:
        hist = loader(cfg, started)
    except Exception as e:
        md.history_kind = "db"
        md.history_retry_at = None  # the database is retried on the slower schedule
        md.history_failed(f"Prism DB: {type(e).__name__}: {e}")
        return False
    if md.history:
        _carry_over(hist, md.history, started)
    else:  # first load: show the analysis from the DB data while the Angel backfill runs
        md.set_history({tf: list(cs) for tf, cs in hist.items()}, {"state": "db_loaded", "error": None})
        log.info("history: %d candle(s) read from Prism DB; fetching today's candles from Angel One", sum(len(v) for v in hist.values()))
    token = _spot_token(md, sleep)
    errors: list[str] = []
    kinds: set[str] = set()
    if token:
        try:
            n = fill_gap(session, token, hist, now(), sleep, errors, kinds)
            log.info("history refreshed: %d candle(s) fetched from Angel One after Prism DB's last candle", n)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            kinds.add(classify(e))
    gaps = decision_window_gaps(md, hist, now()) if token and not errors else {}  # validated against the same candles the decision uses
    if gaps:
        log.info("history holds for today: %s", held_today(hist, now()))  # next to the missing candles, so the hole can be placed
    kind = None
    if not token:
        problem, kind = "no spot token yet; Angel One backfill skipped", "network"
    elif errors:
        problem = f"backfill incomplete ({len(errors)} failed: {errors[0]})"
        kind = "auth" if "auth" in kinds else "rate_limit" if "rate_limit" in kinds else "network" if "network" in kinds else "other"
    elif gaps:
        problem, kind = f"candle validation failed: {describe_gaps(gaps)}", "validation"
    else:
        problem = None
    if problem:
        md.history_failures += 1
        md.history_kind = kind
        md.history_retry_at = now() + _retry_delay(md, kind)
        log.warning("history not complete (%s): %s; trying again in %.0f s", kind, problem, md.history_retry_at - now())
        md.set_history(hist, {"state": "ready", "error": problem})  # usable, but not marked current: retried
        return False
    md.history_failures, md.history_kind, md.history_retry_at = 0, None, None
    md.set_history(hist, {"state": "ready", "error": None}, current_date=ist_date(started), loaded_at=now())
    log.info("history ready for %s: candles validated, no gaps in the decision window", ist_date(started))
    return True


def retry_due(md, now: float, attempts: int) -> bool:
    """A refresh that failed for a temporary reason (see _retry_delay) is tried again as soon as its short delay is over, not 5 minutes later."""
    at = getattr(md, "history_retry_at", None)
    return at is not None and now >= at and attempts < MAX_ATTEMPTS and md.history_date != ist_date(now)


def refresh_due(md, now: float, attempts: int, last_try: float | None) -> bool:
    """History is loaded at startup, and again once a trading session has begun on a day it has not
    been refreshed for. Failed attempts are retried every RETRY_SECONDS, at most MAX_ATTEMPTS a day."""
    if md.history_date == ist_date(now):
        return False
    if md.history is not None and not session_started(now):
        return False  # nothing new before the session opens
    if attempts >= MAX_ATTEMPTS:
        return False
    return last_try is None or now - last_try >= RETRY_SECONDS


def run(cfg, md, session, stop: threading.Event, now=time.time, wait=None, loader=load_db, sleep=time.sleep) -> None:
    """Background thread. The loop only reads the clock; the database is queried when a refresh is due."""
    wait = wait or stop.wait
    attempts, last_try, day = 0, None, None
    while not stop.is_set():
        t = now()
        if ist_date(t) != day:  # a new day gets a fresh retry budget
            day, attempts, last_try = ist_date(t), 0, None
            md.history_attempts, md.history_failures, md.history_retry_at = 0, 0, None
        if refresh_due(md, t, attempts, last_try) or retry_due(md, t, attempts):
            attempts, last_try = attempts + 1, t
            md.history_attempts, md.history_last_try = attempts, t
            try:
                refresh(cfg, md, session, now, sleep, loader)
            except Exception as e:  # refresh() should not raise, but this thread must never die
                md.history_failed(f"unexpected: {type(e).__name__}: {e}")
        wait(POLL_SECONDS)
