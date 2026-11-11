"""Opt-in checks against the REAL Prism DB and the REAL Angel One account. They are skipped unless asked for:

    PRISM_INTEGRATION=1 PG_PASSWORD=...  python -m pytest tests/test_real_services.py        (Prism DB, read-only)
    ANGEL_INTEGRATION=1 (plus the ANGEL_* variables)  python -m pytest tests/test_real_services.py   (Angel One, read-only)

Nothing here writes anywhere. Nothing here needs the market to be open."""

import collections
import os
import time
from datetime import datetime, timedelta

import pytest

from app import analysis as an
from app import history, series
from app.candles import IST, in_session
from app.config import Config, load_config

needs_db = pytest.mark.skipif(
    os.getenv("PRISM_INTEGRATION") != "1" or not os.getenv("PG_PASSWORD"),
    reason="set PRISM_INTEGRATION=1 and PG_PASSWORD to run against the real Prism DB",
)
needs_angel = pytest.mark.skipif(os.getenv("ANGEL_INTEGRATION") != "1", reason="set ANGEL_INTEGRATION=1 to run against Angel One")


def db_config():
    return Config("k", "c", "p", "t", pg_host=os.getenv("PG_HOST", "localhost"), pg_port=int(os.getenv("PG_PORT", "5432")),
                  pg_user=os.getenv("PG_USER", "postgres"), pg_password=os.environ["PG_PASSWORD"], pg_dbname=os.getenv("PG_DBNAME", "prism"))  # fmt: skip


def connect_read_only():
    import psycopg2

    cfg = db_config()
    conn = psycopg2.connect(host=cfg.pg_host, port=cfg.pg_port, user=cfg.pg_user, password=cfg.pg_password, dbname=cfg.pg_dbname,
                            options="-c default_transaction_read_only=on")  # fmt: skip
    conn.set_session(readonly=True, autocommit=True)
    return conn


@needs_db
def test_real_load_db_returns_recent_nifty_candles_in_ist():
    now = time.time()
    hist = history.load_db(db_config(), now)
    for tf, candles in hist.items():
        assert candles, f"no {tf} candles in the lookback window"
        starts = [c.start for c in candles]
        assert starts == sorted(set(starts)) and starts[-1] < now
        assert datetime.fromtimestamp(starts[0], IST) >= datetime.fromtimestamp(now, IST) - timedelta(days=history.LOOKBACK_DAYS[tf] + 1)
        if tf != "1d":
            assert all(in_session(s) for s in starts)  # 09:15-15:29 IST on weekdays only
        assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) for c in candles)
    first_of_day = {tf: min(c.start for c in cs if datetime.fromtimestamp(c.start, IST).date() == datetime.fromtimestamp(cs[-1].start, IST).date())
                    for tf, cs in hist.items() if tf != "1d"}  # fmt: skip
    for tf, start in first_of_day.items():
        assert datetime.fromtimestamp(start, IST).strftime("%H:%M") == "09:15"  # candles are labelled by their START time
    sessions_15m = {datetime.fromtimestamp(c.start, IST).date() for c in hist["15m"]}
    daily_dates = {datetime.fromtimestamp(c.start, IST).date() for c in hist["1d"]}
    assert daily_dates <= sessions_15m | {datetime.fromtimestamp(now, IST).date()}  # a daily candle for every session, none invented


@needs_db
def test_real_prism_db_cannot_be_written_to_through_our_connection_settings():
    import psycopg2

    conn = connect_read_only()
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        conn.cursor().execute("CREATE TEMP TABLE _must_not_exist (i int)")
    conn.close()
    # the server-side setting alone (without the session flag) is enough as well
    cfg = db_config()
    bare = psycopg2.connect(host=cfg.pg_host, port=cfg.pg_port, user=cfg.pg_user, password=cfg.pg_password, dbname=cfg.pg_dbname,
                            options="-c default_transaction_read_only=on")  # fmt: skip
    bare.autocommit = True
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        bare.cursor().execute("CREATE TEMP TABLE _must_not_exist (i int)")
    bare.close()


def real_candles(tf, days=90):
    conn = connect_read_only()
    cur = conn.cursor()
    cur.execute(history.SQL, ("NIFTY", {"1m": "1min", "5m": "5min", "15m": "15min"}[tf], datetime.now(IST) - timedelta(days=days), 20000))
    out = history._to_candles(cur.fetchall(), tf)
    conn.close()
    return out


@needs_db
def test_real_data_aggregation_reproduces_the_databases_own_higher_timeframes():
    """Independent check of series.aggregate (and the 09:15 anchoring) against real data: building 15M from the
    stored 5M, and 5M from the stored 1M, must give the stored 15M and 5M candles."""
    for small, big, secs, base in (("5m", "15m", 900, 300), ("1m", "5m", 300, 60)):
        stored = {c.start: c for c in real_candles(big)}
        built = series.aggregate(real_candles(small), secs, base)
        assert len(built) > 200
        compared = collections.Counter()
        for c in built:
            ref = stored.get(c.start)
            if ref is None:
                compared["not stored"] += 1
                continue
            same = all(abs(getattr(c, f) - getattr(ref, f)) < 1e-6 for f in ("open", "high", "low", "close"))
            compared["match" if same else "differ"] += 1
        assert compared["differ"] == 0 and compared["match"] > 200, (small, big, dict(compared))


@needs_db
def test_real_15m_indicators_agree_with_pandas():
    pd = pytest.importorskip("pandas")
    cs = series.completed_series({"15m": real_candles("15m")}, None, None, "15m")
    assert len(cs) > 500
    df = pd.DataFrame({"h": [c.high for c in cs], "l": [c.low for c in cs], "c": [c.close for c in cs]})
    n = 14
    wilder = lambda x: x.ewm(alpha=1 / n, adjust=False).mean()  # noqa: E731
    delta = df.c.diff()
    tr = pd.concat([df.h - df.l, (df.h - df.c.shift()).abs(), (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    assert an.ema([c.close for c in cs], 20)[-1] == pytest.approx(df.c.ewm(span=20, adjust=False).mean().iloc[-1], rel=1e-6)
    assert an.rsi([c.close for c in cs])[-1] == pytest.approx((100 - 100 / (1 + wilder(delta.clip(lower=0)) / wilder(-delta.clip(upper=0)))).iloc[-1], rel=1e-6)
    assert an.atr(cs)[-1] == pytest.approx(wilder(tr).iloc[-1], rel=1e-6)


@needs_db
def test_real_intraday_atr_agrees_with_pandas_once_the_overnight_gap_is_taken_out():
    """The first candle of each session measured by its own range, everything else as in the textbook ATR."""
    pd = pytest.importorskip("pandas")
    cs = series.completed_series({"15m": real_candles("15m")}, None, None, "15m")
    assert len(cs) > 500
    df = pd.DataFrame({"h": [c.high for c in cs], "l": [c.low for c in cs], "c": [c.close for c in cs], "day": [datetime.fromtimestamp(c.start, IST).date() for c in cs]})
    n = 14
    tr = pd.concat([df.h - df.l, (df.h - df.c.shift()).abs(), (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    tr_intraday = tr.where(df.day == df.day.shift(), df.h - df.l)  # the first candle of a session: no previous close to measure against
    wilder = lambda x: x.ewm(alpha=1 / n, adjust=False).mean()  # noqa: E731
    assert an.atr(cs, intraday=True)[-1] == pytest.approx(wilder(tr_intraday).iloc[-1], rel=1e-6)
    assert an.atr(cs)[-1] == pytest.approx(wilder(tr).iloc[-1], rel=1e-6)  # the textbook form is untouched
    assert (tr - tr_intraday).max() > 100  # and the real history does contain overnight gaps worth removing


@needs_angel
def test_real_hourly_candles_built_from_15m_match_angel_ones_own_hourly_candles():
    """Angel One's ONE_HOUR candles are the reference for the 15M -> 1H aggregation (09:15, 10:15, ... 15:15)."""
    from app.auth import AngelSession
    from app.instruments import find_spot, load_master

    cfg = load_config()
    sess, spot = AngelSession(cfg), find_spot(load_master(cfg.cache_dir))
    m15 = real_candles("15m", days=20)
    built = {c.start: c for c in series.aggregate(m15, 3600, 900)}
    last_day = max(datetime.fromtimestamp(c.start, IST).date() for c in m15 if datetime.fromtimestamp(c.start, IST).date() < datetime.now(IST).date())
    first_day = last_day - timedelta(days=4)
    resp = sess.call("getCandleData", {"exchange": "NSE", "symboltoken": spot.token, "interval": "ONE_HOUR",
                                       "fromdate": f"{first_day} 09:15", "todate": f"{last_day} 15:30"})  # fmt: skip
    assert resp["status"] and resp["data"]
    checked = 0
    for ts, o, h, low, c, _ in resp["data"]:
        start = datetime.fromisoformat(ts).timestamp()
        if start in built:
            checked += 1
            b = built[start]
            assert (b.open, b.high, b.low, b.close) == pytest.approx((o, h, low, c), abs=1e-6), ts
    assert checked >= 20  # several sessions of seven hourly candles each
