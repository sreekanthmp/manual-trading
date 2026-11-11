"""
Daily EOD update: download yesterday's NIFTY OHLCV and insert into PostgreSQL.

Usage:
    python daily_update.py                                        # yesterday
    python daily_update.py --start 2026-01-01 --end 2026-01-10  # backfill
"""

import os
import json
import time
import argparse
from datetime import date, datetime, timedelta

import pyotp
import psycopg2
from psycopg2.extras import execute_values
from SmartApi import SmartConnect

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env(path):
    """Read KEY=VALUE lines of the repository's git-ignored .env (the file Prism itself reads); real environment variables win."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


_load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

# The database login is read from PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DBNAME (the same names Prism uses), never written in this file.
PG = dict(host=os.getenv("PG_HOST", "localhost"), port=int(os.getenv("PG_PORT", "5432")), user=os.getenv("PG_USER", "postgres"),
          password=os.getenv("PG_PASSWORD", ""), dbname=os.getenv("PG_DBNAME", "prism"))
if not PG["password"]:
    raise SystemExit("PG_PASSWORD is not set: put it in the git-ignored .env beside the app (or in the environment); it is no longer kept in this file.")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "config.json")

SYMBOL = "NIFTY"
TOKEN  = "99926000"

TIMEFRAMES = {
    "1min" : ("ONE_MINUTE",     True),
    "5min" : ("FIVE_MINUTE",    True),
    "15min": ("FIFTEEN_MINUTE", True),
    "1day" : ("ONE_DAY",        False),
}

DELAY = 0.4

# ── Angel One ─────────────────────────────────────────────────────────────────

def angel_login() -> SmartConnect:
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)["smartapi"]
    obj  = SmartConnect(api_key=cfg["api_key"])
    totp = pyotp.TOTP(cfg["totp_token"]).now()
    resp = obj.generateSession(cfg["client_id"], cfg["password"], totp)
    if not resp["status"]:
        raise RuntimeError(f"Login failed: {resp.get('message')}")
    print(f"  Logged in as {cfg['client_id']}")
    return obj


def fetch_bars(obj, interval, intraday, start, end):
    if intraday:
        from_dt = datetime(start.year, start.month, start.day,  9, 15)
        to_dt   = datetime(end.year,   end.month,   end.day,   15, 30)
    else:
        from_dt = datetime(start.year, start.month, start.day,  0,  0)
        to_dt   = datetime(end.year,   end.month,   end.day,   23, 59)

    resp = obj.getCandleData({
        "exchange": "NSE", "symboltoken": TOKEN, "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate":   to_dt.strftime("%Y-%m-%d %H:%M"),
    })
    if not resp or not resp.get("status"):
        return []
    return resp.get("data") or []


# ── PostgreSQL ────────────────────────────────────────────────────────────────

def insert_rows(conn, raw, timeframe):
    if not raw:
        return 0

    rows = []
    for ts, o, h, l, c, v in raw:
        dt = __import__("pandas").to_datetime(ts)
        if dt.tzinfo is None:
            dt = dt.tz_localize("Asia/Kolkata")
        dt = dt.tz_convert("UTC").to_pydatetime()
        rows.append((dt, SYMBOL, timeframe, float(o), float(h), float(l), float(c), int(v)))

    cur = conn.cursor()
    execute_values(
        cur,
        """
        INSERT INTO ohlcv (datetime, symbol, timeframe, open, high, low, close, volume)
        VALUES %s
        ON CONFLICT (datetime, symbol, timeframe) DO NOTHING
        """,
        rows,
        page_size=2000,
    )
    inserted = cur.rowcount
    conn.commit()
    cur.close()
    return inserted


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None)
    parser.add_argument("--end",   default=None)
    args = parser.parse_args()

    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = date.fromisoformat(args.start or yesterday)
    end   = date.fromisoformat(args.end   or yesterday)

    print(f"\n=== NIFTY Daily Update  |  {start} → {end} ===\n")

    obj  = angel_login()
    conn = psycopg2.connect(**PG)

    total = 0
    for tf, (interval, intraday) in TIMEFRAMES.items():
        raw = fetch_bars(obj, interval, intraday, start, end)
        n   = insert_rows(conn, raw, tf)
        total += n
        print(f"  {tf:<6}  {len(raw):>5} fetched  {n:>5} inserted")
        time.sleep(DELAY)

    conn.close()
    print(f"\nDone. Total rows inserted: {total:,}")


if __name__ == "__main__":
    main()
