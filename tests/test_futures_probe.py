"""Stage 2.4: the read-only NIFTY futures probe (app/futures_probe.py), exercised offline with synthetic sessions, a fake broker session and a fake WebSocket. Nothing here
touches the network or the broker. What these prove is that the probe's verdict logic is right: every requirement passes on good data and fails, on its own, on bad data;
the live requirements are NOT VERIFIED (never passed) when the exchange is closed; only read-only broker calls are made; and no credential reaches the output."""

import copy
import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app import futures_probe as fp
from app.candles import IST

SAT = datetime(2026, 11, 7, 2, 30, tzinfo=IST)  # the exchange is closed
FRI_OPEN = datetime(2026, 11, 6, 10, 30, tzinfo=IST)  # a Friday, mid-morning: the exchange is open
FUT_TOKEN, SPOT_TOKEN = "61471", "99926000"
SECRETS = ("PASSWORD-SECRET-456", "JWT-SECRET-123", "FEED-SECRET-789", "TOTP-SECRET-000", "APIKEY-SECRET-111")


# -- synthetic data ---------------------------------------------------------------------------------------------------------------------------
def synth(day, base=24650.0, basis=lambda i: 55.0 + 0.2 * math.sin(i / 9), vol=lambda i: 1000 + (i * 37) % 400):
    """A whole session of futures and spot 1M candles (prices to two decimals so a 5M candle built from them is exactly equal)."""
    fut, spot = [], []
    for i, s in enumerate(fp.session_starts(day)):
        p = base + 6 * math.sin(i / 25) + 0.01 * i
        o, c = round(p, 2), round(p + 0.5 * math.sin(i), 2)
        h, low, b = round(max(o, c) + 1, 2), round(min(o, c) - 1, 2), basis(i)
        fut.append({"start": s, "open": o, "high": h, "low": low, "close": c, "volume": vol(i)})
        spot.append({"start": s, "open": round(o - b, 2), "high": round(h - b, 2), "low": round(low - b, 2), "close": round(c - b, 2), "volume": 0})
    return fut, spot


POST_CLOSE_LOTS = (9000, 7000, 5000)  # the futures' closing session after 15:30: about 4.7% of the day's volume, as in the real data


def post_close(day, last=24700.0):
    """Three minutes of the closing session (15:30, 15:31, 15:32): the exchange's day totals include them, the 09:15-15:29 session does not."""
    base = fp.session_starts(day)[-1] + 60
    return [{"start": base + 60 * i, "open": last, "high": last + 1, "low": last - 1, "close": last, "volume": v} for i, v in enumerate(POST_CLOSE_LOTS)]


def five_minute(c1, day):
    out = []
    for k, s in enumerate(fp.session_starts(day, 300)):
        m = c1[5 * k : 5 * k + 5]
        out.append({"start": s, "open": m[0]["open"], "high": max(x["high"] for x in m), "low": min(x["low"] for x in m), "close": m[-1]["close"], "volume": sum(x["volume"] for x in m)})
    return out


def rows(candles):
    return [[datetime.fromtimestamp(c["start"], IST).isoformat(), c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles]


def master(lot="75"):
    def fut(token, symbol, expiry, name="NIFTY", kind="FUTIDX", seg="NFO"):
        return {"token": token, "symbol": symbol, "name": name, "expiry": expiry, "lotsize": lot, "instrumenttype": kind, "exch_seg": seg}

    return [fut(FUT_TOKEN, "NIFTY23NOV26FUT", "23NOV2026"), fut("58875", "NIFTY29DEC26FUT", "29DEC2026"), fut("54749", "NIFTY25JAN27FUT", "25JAN2027"),
            fut("999", "FINNIFTY23NOV26FUT", "23NOV2026", name="FINNIFTY"), fut("998", "NIFTY23NOV2624600CE", "23NOV2026", kind="OPTIDX"),
            fut(SPOT_TOKEN, "Nifty 50", "", kind="AMXIDX", seg="NSE")]  # fmt: skip


class FakeSession:
    """A broker session that answers from synthetic sessions and records every call it is asked to make."""

    def __init__(self, days, today_candles=None, quote_for=None, fail_quote=False, refuse_minutes_for=()):
        self.days, self.calls, self.today = days, [], today_candles or {}
        self.quote_for, self.fail_quote, self.refuse = quote_for, fail_quote, set(refuse_minutes_for)
        self.jwt, self.feed_token = "JWT-SECRET-123", "FEED-SECRET-789"

    def call(self, method, *args):
        self.calls.append(method)
        if method == "getCandleData":
            p = args[0]
            day = date.fromisoformat(p["fromdate"][:10])
            if day in self.refuse and p["exchange"] == "NFO" and p["interval"] == "ONE_MINUTE":
                return {"status": False, "message": "no data available"}  # the broker refuses this one request
            if day in self.today and p["exchange"] == "NFO" and p["interval"] == "ONE_MINUTE":
                return {"status": True, "data": rows(self.today[day])}
            if day not in self.days:
                return {"status": True, "data": []}  # a holiday
            fut, spot = self.days[day]
            if p["exchange"] == "NSE":
                return {"status": True, "data": rows(spot)}
            if p["interval"] == "FIVE_MINUTE":
                return {"status": True, "data": rows(five_minute(fut, day))}
            return {"status": True, "data": rows(fut + (post_close(day) if p["todate"][-5:] > "15:30" else []))}  # the closing session is only returned if it is asked for
        if method == "getMarketData":
            if self.fail_quote:
                raise RuntimeError("quote service unavailable")
            day = self.quote_for or max(self.days)
            fut, _ = self.days[day]
            whole = fut + post_close(day)  # the exchange's figures are for the whole day, closing session included, and the volume is in UNITS (lots x the lot size)
            return {"status": True, "data": {"fetched": [{"symbolToken": FUT_TOKEN, "tradingSymbol": "NIFTY23NOV26FUT", "ltp": fut[-1]["close"], "tradeVolume": sum(c["volume"] for c in whole) * 75,
                                                          "avgPrice": round(fp.session_vwap(whole), 2), "opnInterest": 12345678, "exchFeedTime": f"{day.strftime('%d-%b-%Y')} 15:40:00",
                                                          "exchTradeTime": f"{day.strftime('%d-%b-%Y')} 15:39:59"}], "unfetched": []}}
        raise AssertionError(f"the probe made a call it must not make: {method}")


class FakeCfg:
    api_key, client_id, password, totp_secret = "APIKEY-SECRET-111", "CLIENT1", "PASSWORD-SECRET-456", "TOTP-SECRET-000"


def good_days(n=4, end=date(2026, 11, 6)):
    out, d = {}, end
    while len(out) < n:
        if d.weekday() < 5:
            out[d] = synth(d, base=24650.0 + 10 * len(out))
        d -= timedelta(days=1)
    return out


def probe_closed(days=None, **kw):
    days = days or good_days()
    session = FakeSession(days, **kw)
    out = fp.run(FakeCfg(), days=3, now=SAT, session=session, master_rows=master(), ws_factory=lambda s: FakeWS(Clock(0.0), []), sleep=lambda s: time.sleep(0.02))
    return out, session


# -- a fake WebSocket and clock -----------------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeWS:
    """Behaves like the SDK client the app uses: connect() blocks, calling on_open and then on_data for each scripted message, until close_connection()."""

    def __init__(self, clock, script, opened_at=None):
        self.clock, self.script, self.opened_at = clock, script, opened_at
        self.stop, self.subscribed = threading.Event(), []
        self.on_open = self.on_data = self.on_message = self.on_error = self.on_close = None

    def connect(self):
        if self.opened_at is not None:
            self.clock.t = self.opened_at
        self.on_open(self)
        for recv, msg in self.script:
            self.clock.t = recv
            self.on_data(self, msg)
        self.stop.wait(5)

    def subscribe(self, correlation_id, mode, token_list):
        self.subscribed.append((mode, token_list))

    def close_connection(self):
        self.stop.set()


def tick_msg(token, exch, volume, ltp=24700.0, exch_type=2):
    return {"token": token, "exchange_type": exch_type, "last_traded_price": int(round(ltp * 100)), "exchange_timestamp": int(exch * 1000), "volume_trade_for_the_day": volume,
            "last_traded_quantity": 75, "open_interest": 5000000}  # fmt: skip


def minute_ticks(first_minute, volumes, base_volume, lag=1.0, per_minute=30, spot_noise=lambda n: 0.3 * math.sin(n)):
    """Ticks through consecutive minutes: thirty a minute, the last one at the end-of-minute cumulative volume, each with a spot index tick 0.2 s before it at a basis of 55
    points plus `spot_noise`. Returns [(recv, message)] (both instruments, in arrival order) and the cumulative volume reached."""
    out, cum, n = [], base_volume, 0
    for k, v in enumerate(volumes):
        start = first_minute + 60 * k
        for j, off in enumerate(range(1, 60, 2)[:per_minute]):
            vol = cum + round(v * (j + 1) / per_minute)
            out.append((start + off - 0.2 + lag, tick_msg(SPOT_TOKEN, start + off - 0.2, 0, 24700.0 - 55.0 + spot_noise(n), exch_type=1)))
            out.append((start + off + lag, tick_msg(FUT_TOKEN, start + off, vol)))
            n += 1
        cum += v
    return out, cum


LOT = 75  # the lot size in the test master
MIN_VOLUMES = [1200, 900, 1500, 800, 1100, 1000, 1300, 700, 1250]  # lots in 10:20 .. 10:28


def open_market_probe(mutate=None, lag=1.0, second_open_delay=2.0, drop_history_minutes=(), with_history=True, spot_noise=lambda n: 0.3 * math.sin(n), lot="75"):
    days = good_days()
    today = FRI_OPEN.date()
    base = datetime(2026, 11, 6, 10, 20, tzinfo=IST).timestamp()
    units = [v * LOT for v in MIN_VOLUMES]  # the live day volume is in UNITS; the finished candles carry LOTS (the real broker does exactly this)
    first, cum = minute_ticks(base, units[:5], 50_000_000, lag, spot_noise=spot_noise)
    gap_cum = cum + units[5] + units[6]  # 10:25 and 10:26 are traded while the feed is down
    second, _ = minute_ticks(base + 60 * 7, units[7:], gap_cum, lag, spot_noise=spot_noise)
    fut_today, _ = synth(today)
    for k, v in enumerate(MIN_VOLUMES):  # the finished 1M candles for 10:20 .. 10:28 carry exactly the volumes that were traded
        idx = (10 * 3600 + 20 * 60 - 9 * 3600 - 15 * 60) // 60 + k
        fut_today[idx]["volume"] = v
    fut_today = [c for c in fut_today if c["start"] not in {base + 60 * m for m in drop_history_minutes}]
    clock = Clock(base - 5)
    scripts = iter([FakeWS(clock, first, base), FakeWS(clock, [(r + 0.0, m) for r, m in second], base + 60 * 7 + 0.0)])
    ws_list = []

    def factory(session):
        ws = next(scripts)
        if ws.opened_at and ws.script and ws.opened_at != base:
            ws.script = [(max(r, ws.opened_at + second_open_delay), m) for r, m in ws.script]
        ws_list.append(ws)
        return ws

    session = FakeSession(days, today_candles={today: fut_today}, refuse_minutes_for=() if with_history else (today,))  # (without history the broker refuses today's minute candles)
    found = fp.run(FakeCfg(), days=3, live_seconds=0.05, now=FRI_OPEN, session=session, master_rows=master(lot), ws_factory=factory, sleep=lambda s: time.sleep(0.05), clock=clock, settle=0)
    if mutate:
        mutate(found)
        found["evaluation"] = fp.evaluate(found)
    return found, session, ws_list


# ====================================================================================================================
# The master and the candles
# ====================================================================================================================
def test_the_front_month_is_the_nearest_nifty_futures_contract_and_only_until_its_expiry_close():
    contracts = fp.futures_contracts(master())
    assert [c["symbol"] for c in contracts] == ["NIFTY23NOV26FUT", "NIFTY29DEC26FUT", "NIFTY25JAN27FUT"]  # no FINNIFTY future, no option, no index row, sorted by expiry
    assert contracts[0] == {"token": FUT_TOKEN, "symbol": "NIFTY23NOV26FUT", "expiry": date(2026, 11, 23), "lot_size": 75}
    assert fp.front_month(contracts, SAT)["token"] == FUT_TOKEN
    expiry_day = datetime(2026, 11, 23, 11, 0, tzinfo=IST)
    assert fp.front_month(contracts, expiry_day)["token"] == FUT_TOKEN  # still tradable on its expiry day
    assert fp.front_month(contracts, expiry_day.replace(hour=15, minute=31))["symbol"] == "NIFTY29DEC26FUT"  # and rolled after the close
    assert fp.front_month(contracts, datetime(2027, 2, 1, 10, 0, tzinfo=IST)) is None
    bad = master() + [{"token": "1", "symbol": "X", "name": "NIFTY", "expiry": "garbage", "instrumenttype": "FUTIDX", "exch_seg": "NFO"}]
    assert len(fp.futures_contracts(bad)) == 3  # an unreadable expiry is skipped, not fatal


def test_candle_rows_are_parsed_and_unusable_ones_are_counted_not_guessed():
    good = ["2026-11-06T09:15:00+05:30", 100, 102, 99, 101, 500]
    parsed, bad = fp.parse_candles([good, good, ["x", 1, 2, 3, 4, 5], ["2026-11-06T09:16:00+05:30", 1, 2, 3], ["2026-11-06T09:17:00+05:30", 1, 2, 3, 4, None], None])
    assert [c["volume"] for c in parsed] == [500, None] and bad == 3  # the duplicate collapsed, a missing volume stays None (never 0), three rows were unusable
    assert parsed[0]["start"] == datetime(2026, 11, 6, 9, 15, tzinfo=IST).timestamp()


def test_a_whole_good_session_checks_out_and_each_defect_is_counted():
    day = date(2026, 11, 6)
    fut, _ = synth(day)
    ok = fp.check_day(fut, day)
    assert (ok["expected"], ok["present"], ok["missing"], ok["zero_or_missing_volume"], ok["bad_ohlc"], ok["outside_session"]) == (375, 375, 0, 0, 0, 0)
    broken = [dict(c) for c in fut if c["start"] != fut[10]["start"]]  # one minute gone
    broken[20]["volume"] = 0
    broken[30]["volume"] = None
    broken[40]["low"] = broken[40]["high"] + 1  # a low above the high
    broken.append({**fut[0], "start": fut[0]["start"] - 60})  # a candle before the open
    r = fp.check_day(broken, day)
    assert (r["present"], r["missing"], r["missing_first"], r["zero_or_missing_volume"], r["bad_ohlc"], r["outside_session"]) == (374, 1, ["09:25"], 2, 1, 1)


def test_one_minute_candles_are_compared_with_the_exchanges_own_five_minute_candles():
    day = date(2026, 11, 6)
    fut, _ = synth(day)
    five = five_minute(fut, day)
    same = fp.consistency_1m_5m(fut, five, day)
    assert (same["buckets"], same["volume_equal"], same["ohlc_equal"], same["skipped_incomplete"]) == (75, 75, 75, 0)
    five[3] = {**five[3], "volume": five[3]["volume"] + 1}
    five[4] = {**five[4], "high": five[4]["high"] + 0.05}
    diff = fp.consistency_1m_5m(fut, five, day)
    assert (diff["volume_equal"], diff["ohlc_equal"]) == (74, 74) and len(diff["examples"]) == 2
    partial = fp.consistency_1m_5m([c for c in fut if c["start"] != fut[7]["start"]], five_minute(fut, day), day)
    assert partial["buckets"] == 74 and partial["skipped_incomplete"] == 1  # a bucket missing a minute is left out, and said


def test_vwap_basis_and_percentiles_are_computed_as_defined():
    c = [{"start": 0, "open": 0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 100}, {"start": 60, "open": 0, "high": 112.0, "low": 108.0, "close": 110.0, "volume": 300}]
    assert fp.session_vwap(c) == pytest.approx((100 * 100 + 110 * 300) / 400)  # typical prices (high + low + close) / 3 are 100 and 110
    assert fp.session_vwap([{**c[0], "volume": 0}, {**c[1], "volume": None}]) is None
    assert fp.percentile([1, 2, 3, 4, 5], 0.5) == 3 and fp.percentile([1, 2, 3, 4, 5], 0.99) == 5 and fp.percentile([], 0.5) is None
    day = date(2026, 11, 6)
    fut, spot = synth(day)
    b = fp.basis_stats(fut, spot, day)
    assert b["minutes"] == 375 and 54.5 < b["mean"] < 55.5 and b["share_positive"] == 1.0 and b["step_p99"] < 0.5  # a smooth basis
    spot2 = [dict(s) for s in spot]
    spot2[100]["close"] -= 20  # one minute's basis jumps by 20 points, and back
    assert fp.basis_stats(fut, spot2, day)["step_max"] > 19
    spot3 = [dict(s) for s in spot]
    spot3[1]["close"] -= 20  # the same jump in the second minute of the session is an edge minute: left out
    assert fp.basis_stats(fut, spot3, day)["step_max"] < 1


def test_a_quote_is_read_for_the_fields_the_checks_use():
    resp = {"status": True, "data": {"fetched": [{"symbolToken": "1", "tradeVolume": 5}, {"symbolToken": FUT_TOKEN, "tradeVolume": 777, "avgPrice": 24650.5, "exchFeedTime": "06-Nov-2026 15:30:12"}]}}
    q = fp.quote_summary(resp, FUT_TOKEN)
    assert q["tradeVolume"] == 777 and q["avgPrice"] == 24650.5 and fp.quote_date(q) == date(2026, 11, 6)
    assert fp.quote_summary({"status": False}, FUT_TOKEN) is None and fp.quote_date(None) is None and fp.quote_date({"exchFeedTime": "yesterday"}) is None


# ====================================================================================================================
# Live ticks
# ====================================================================================================================
def tick(recv, exch, volume):
    return {"recv": recv, "exch": exch, "ltp": 1.0, "volume": volume, "ltq": 75, "oi": 1}


def test_a_tick_stream_is_summarised_for_volume_order_and_lag():
    a = fp.analyse_ticks([tick(10.0, 9.0, 100), tick(20.0, 18.0, 150), tick(30.0, 29.5, 140), tick(40.0, 28.0, None), tick(50.0, None, 200)])
    assert a["ticks"] == 5 and a["volume_decreases"] == 1 and a["without_volume"] == 1 and a["without_exchange_time"] == 1 and a["exchange_time_decreases"] == 1
    assert a["lag_median"] == pytest.approx(1.5) and a["lag_max"] == pytest.approx(12.0) and a["seconds"] == pytest.approx(40.0)  # lags 1, 2, 0.5 and 12 s: the median is (1 + 2) / 2
    empty = fp.analyse_ticks([])
    assert empty["ticks"] == 0 and empty["lag_median"] is None  # no ticks: every key is still there for the verdict


def stick(exch, ltp, recv=None):
    return {"recv": exch + 0.1 if recv is None else recv, "exch": exch, "ltp": ltp, "volume": None, "ltq": None, "oi": None}


def test_the_tick_level_basis_pairs_each_futures_tick_with_the_latest_spot_tick_that_is_not_newer_and_not_stale():
    spot = [stick(t, 24000.0) for t in range(0, 100)]
    fut = [stick(t + 0.5, 24055.0) for t in range(0, 100)]
    b = fp.basis_from_ticks(fut, spot)
    assert b["seconds"] == 100 and b["mean"] == pytest.approx(55.0) and b["error_p99"] == 0 and b["step_p99"] == 0
    stale = fp.basis_from_ticks([stick(50.0, 24055.0)], [stick(40.0, 24000.0)])  # the spot tick is 10 s older than the futures tick: too old to pair
    assert stale["seconds"] == 0
    newer = fp.basis_from_ticks([stick(10.0, 24055.0)], [stick(10.5, 24000.0)])  # a spot tick that arrived AFTER the futures tick is never used for it
    assert newer["seconds"] == 0
    jumpy = fp.basis_from_ticks([stick(t + 0.5, 24055.0 + (10 if t == 60 else 0)) for t in range(0, 100)], spot)  # one second where the futures jump 10 away from the spot
    assert jumpy["error_p99"] == pytest.approx(10.0) and jumpy["step_p99"] == pytest.approx(10.0)
    none = fp.basis_from_ticks([], [])
    assert none["seconds"] == 0 and none["error_p99"] is None


def test_the_tick_level_basis_requirement_passes_fails_and_says_when_it_cannot_be_measured():
    found, _, _ = open_market_probe()
    assert next(r for r in found["evaluation"]["requirements"] if r["id"] == "R14")["status"] == fp.PASS
    noisy, _, _ = open_market_probe(spot_noise=lambda n: 8.0 if n % 5 == 0 else 0.0)  # the spot jumps 8 points on every fifth tick pair
    r14 = next(r for r in noisy["evaluation"]["requirements"] if r["id"] == "R14")
    assert r14["status"] == fp.FAIL and "limit 3.0 pts" in r14["evidence"] and noisy["evaluation"]["failed"] == ["R14"]
    few, _, _ = open_market_probe(mutate=lambda f: f["live"]["basis_ticks"].update(seconds=20))
    assert next(r for r in few["evaluation"]["requirements"] if r["id"] == "R14")["status"] == fp.NOT_VERIFIED
    sparse, _, _ = open_market_probe(mutate=lambda f: f["live"]["basis_ticks"].update(error_p99=None, error_median=None))
    r = next(r for r in sparse["evaluation"]["requirements"] if r["id"] == "R14")
    assert r["status"] == fp.NOT_VERIFIED and "too sparse" in r["evidence"]  # the reason is what is missing, not a misleading count


def test_the_volume_of_a_minute_is_the_change_of_the_day_volume_across_it():
    base = 600.0
    t = [tick(0, base + 10, 1000), tick(0, base + 50, 1100), tick(0, base + 70, 1300), tick(0, base + 110, 1500), tick(0, base + 130, 1900), tick(0, base + 190, 2000)]
    assert fp.live_minute_volumes(t) == {base + 60: 400, base + 120: 400}  # the first minute (its start unseen) and the last (not finished) are left out
    late = t + [tick(0, base + 100, 1450)]  # a tick for minute 1 arriving after minute 2's: the highest day volume is still the end of that minute
    assert fp.live_minute_volumes(late)[base + 60] == 400
    assert fp.live_minute_volumes([tick(0, base + 10, 1), tick(0, base + 190, 9)]) == {}  # minutes that are not consecutive are never compared
    c1 = [{"start": base + 60, "volume": 400}, {"start": base + 120, "volume": 440}]
    cmp = fp.compare_live_to_history({base + 60: 400, base + 120: 400}, c1)
    assert cmp["minutes_compared"] == 2 and cmp["median_rel_diff"] == pytest.approx((0 + 40 / 440) / 2) and cmp["max_rel_diff"] == pytest.approx(40 / 440)
    in_units = fp.compare_live_to_history({base + 60: 400 * 65, base + 120: 440 * 65}, c1, 65)  # live volume in units, candles in lots of 65
    assert in_units["minutes_compared"] == 2 and in_units["max_rel_diff"] == 0
    assert fp.compare_live_to_history({base + 60: 400 * 65}, c1)["max_rel_diff"] > 60  # (compared without the lot size they differ by a factor of 65: the mistake this prevents)


# ====================================================================================================================
# The verdict, against a closed market
# ====================================================================================================================
def test_with_the_exchange_closed_the_history_checks_pass_and_the_live_ones_are_not_verified_never_passed():
    found, _ = probe_closed()
    ev = found["evaluation"]
    status = {r["id"]: r["status"] for r in ev["requirements"]}
    assert all(status[i] == fp.PASS for i in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9")), ev["requirements"]
    assert all(status[i] == fp.NOT_VERIFIED for i in ("R10", "R11", "R12", "R13", "R14"))
    assert ev["verdict"] == "NOT CONFIRMED" and ev["failed"] == [] and ev["not_verified"] == ["R10", "R11", "R12", "R13", "R14"]
    assert "the exchange is closed" in found["live"]["reason"] and "Saturday" in found["live"]["reason"]
    assert [d["day"] for d in found["days"]] == ["2026-11-04", "2026-11-05", "2026-11-06"]  # the three latest finished sessions, oldest first
    assert found["contract"]["symbol"] == "NIFTY23NOV26FUT" and found["stability"] == {"day": "2026-11-06", "candles": 375 + 3, "differences": 0, "identical": True}  # (the whole day: the 375 session minutes and the 3 of the closing session)
    assert found["live"]["closed_market_listen"]["ticks"] == 0  # (informational: nothing was pushed by the fake)


def test_only_read_only_calls_are_made_and_no_credential_reaches_the_findings(tmp_path):
    out = tmp_path / "findings.json"
    days = good_days()
    session = FakeSession(days)
    found = fp.run(FakeCfg(), days=3, now=SAT, session=session, master_rows=master(), out=str(out), ws_factory=lambda s: FakeWS(Clock(0.0), []), sleep=lambda s: time.sleep(0.02))
    assert set(session.calls) <= {"getCandleData", "getMarketData"} and set(session.calls) <= set(fp.READ_ONLY_CALLS)
    text = out.read_text(encoding="utf-8") + json.dumps(found, default=str) + fp.summary(found)
    assert not any(s in text for s in SECRETS) and "CLIENT1" not in text  # no password, token, key, TOTP secret or client id anywhere
    assert json.loads(out.read_text(encoding="utf-8"))["evaluation"]["verdict"] == "NOT CONFIRMED"
    with pytest.raises(ValueError):
        fp._call(session, "placeOrder", {})
    with pytest.raises(ValueError):
        fp._call(session, "getRMS")


def test_the_module_contains_no_order_position_or_account_call():
    src = Path(fp.__file__).read_text(encoding="utf-8")
    for word in ("placeOrder", "modifyOrder", "cancelOrder", "orderBook", "tradeBook", "getPosition", "convertPosition", "holding", "rmsLimit", "getProfile", "gtt", "createRule", "individual_order"):
        assert word not in src, word


def test_a_holiday_is_skipped_and_an_earlier_session_takes_its_place():
    days = good_days(5)
    holiday = sorted(days)[-2]
    del days[holiday]  # no candles that day
    found, _ = probe_closed(days)
    assert holiday.isoformat() not in [d["day"] for d in found["days"]] and len(found["days"]) == 3 and any(holiday.isoformat() in n for n in found["notes"])


def test_a_quote_for_another_session_or_no_quote_leaves_the_exchange_comparisons_not_verified():
    days = good_days()
    older = sorted(days)[0]
    found, _ = probe_closed(days, quote_for=older)  # the quote describes an older session than the latest one fetched
    status = {r["id"]: r for r in found["evaluation"]["requirements"]}
    assert status["R6"]["status"] == fp.NOT_VERIFIED and status["R7"]["status"] == fp.NOT_VERIFIED and "not for the latest" in status["R6"]["evidence"]
    found, _ = probe_closed(good_days(), fail_quote=True)
    assert found["quote"] is None and {r["id"]: r["status"] for r in found["evaluation"]["requirements"]}["R6"] == fp.NOT_VERIFIED and any("quote failed" in n for n in found["notes"])


def test_no_front_month_contract_is_a_failure_not_a_crash():
    found = fp.run(FakeCfg(), days=3, now=datetime(2027, 3, 1, 10, 0, tzinfo=IST), session=FakeSession({}), master_rows=master())
    assert found["evaluation"]["verdict"] == "NOT RELIABLE" and found["evaluation"]["failed"] == ["R1"]


# Each requirement fails on its own, on the data it is about
def tweak(path, fn):
    def apply(found):
        fn(found)

    return apply


@pytest.mark.parametrize("rid,change", [
    ("R2", lambda f: f["days"][1]["fut"].update(zero_or_missing_volume=40)),
    ("R3", lambda f: f["days"][2]["fut"].update(present=300)),
    ("R3", lambda f: f["days"][0]["fut"].update(bad_ohlc=1)),
    ("R4", lambda f: f["days"][0]["c1_c5"].update(volume_equal=60)),
    ("R5", lambda f: f["stability"].update(identical=False, differences=3)),
    ("R6", lambda f: f["days"][-1].update(vwap=f["days"][-1]["vwap"] * 1.001)),
    ("R7", lambda f: f["days"][-1].update(lots_total=int(f["days"][-1]["lots_total"] * 1.02))),
    ("R8", lambda f: f["days"][1].update(aligned=0.9)),
    ("R9", lambda f: f["days"][1]["basis"].update(step_p99=9.0)),
])  # fmt: skip
def test_each_history_requirement_fails_on_its_own(rid, change):
    found, _ = probe_closed()
    change(found)
    ev = fp.evaluate(found)
    assert ev["failed"] == [rid] and ev["verdict"] == "NOT RELIABLE"
    assert next(r for r in ev["requirements"] if r["id"] == rid)["evidence"]  # and it says why


# ====================================================================================================================
# The verdict, against an open market (the live requirements)
# ====================================================================================================================
def test_with_the_exchange_open_good_live_data_passes_the_live_requirements_but_the_exchange_comparisons_need_a_finished_session():
    found, session, ws_list = open_market_probe()
    ev = found["evaluation"]
    status = {r["id"]: r["status"] for r in ev["requirements"]}
    assert all(status[i] == fp.PASS for i in ("R1", "R2", "R3", "R4", "R5", "R8", "R9", "R10", "R11", "R12", "R13", "R14")), ev["requirements"]
    assert status["R6"] == status["R7"] == fp.NOT_VERIFIED  # the quote describes today's running session, not the finished one the candles are for
    assert ev["verdict"] == "NOT CONFIRMED" and ev["not_verified"] == ["R6", "R7"]
    assert set(session.calls) <= {"getCandleData", "getMarketData"}
    both = [(fp.QUOTE_MODE, [{"exchangeType": 2, "tokens": [FUT_TOKEN]}, {"exchangeType": 1, "tokens": [SPOT_TOKEN]}])]  # the futures token and the spot index, QUOTE mode
    assert len(ws_list) == 2 and ws_list[0].subscribed == both and ws_list[1].subscribed == both  # ... and again after the reconnect
    live = found["live"]
    assert live["analysis"]["ticks"] == 150 and live["analysis"]["lag_median"] == pytest.approx(1.0) and live["history_comparison"]["minutes_compared"] == 4
    assert live["basis_ticks"]["seconds"] > 100 and live["basis_ticks"]["mean"] == pytest.approx(55.0, abs=0.5) and live["basis_ticks"]["error_p99"] < 1.0
    assert live["recovery"]["missed_minutes"] == 2 and live["recovery"]["gap_minutes_in_history"] is True and live["recovery"]["first_tick_delay"] <= 10


@pytest.mark.parametrize("rid,make", [
    ("R10", lambda f: f["live"]["analysis"].update(volume_decreases=2)),
    ("R10", lambda f: f["live"]["analysis"].update(ticks=5)),
    ("R11", lambda f: f["live"]["analysis"].update(lag_median=4.0)),
    ("R11", lambda f: f["live"]["analysis"].update(lag_p95=12.0)),
    ("R11", lambda f: f["live"]["analysis"].update(exchange_time_decreases=1)),
    ("R12", lambda f: f["live"]["history_comparison"].update(median_rel_diff=0.02)),
    ("R12", lambda f: f["live"]["history_comparison"].update(p90_rel_diff=0.2)),
    ("R13", lambda f: f["live"]["recovery"].update(first_tick_delay=30.0)),
    ("R13", lambda f: f["live"]["recovery"].update(volume_continuous=False)),
    ("R13", lambda f: f["live"]["recovery"].update(time_continuous=False)),
    ("R13", lambda f: f["live"]["recovery"].update(gap_minutes_in_history=False)),
    ("R14", lambda f: f["live"]["basis_ticks"].update(error_p99=3.5)),
])  # fmt: skip
def test_each_live_requirement_fails_on_its_own(rid, make):
    found, _, _ = open_market_probe(mutate=make)
    ev = found["evaluation"]
    assert ev["failed"] == [rid] and ev["verdict"] == "NOT RELIABLE"


def test_a_feed_that_is_slow_or_that_misses_minutes_in_the_finished_candles_is_caught_end_to_end():
    found, _, _ = open_market_probe(lag=6.0)  # every tick arrives 6 s after its exchange time (the limit for the median is 3 s)
    r11 = next(r for r in found["evaluation"]["requirements"] if r["id"] == "R11")
    assert r11["status"] == fp.FAIL and "lag median 6.0" in r11["evidence"]
    assert found["evaluation"]["failed"] == ["R11"]  # (the first tick after reconnecting still comes 2 + 6 = 8 s after the connection opened: inside the 10 s limit)
    found, _, _ = open_market_probe(drop_history_minutes=(5,))  # the finished candles lack 10:25, a minute the feed missed
    r13 = next(r for r in found["evaluation"]["requirements"] if r["id"] == "R13")
    assert r13["status"] == fp.FAIL and "False" in r13["evidence"]


def test_two_runs_together_confirm_what_neither_could_alone_and_a_failure_always_wins(tmp_path):
    closed, _ = probe_closed()
    opened, _, _ = open_market_probe()
    both = fp.merge_evaluations([closed["evaluation"], opened["evaluation"]], ["after-close", "in-session"])
    assert both["verdict"] == "CONFIRMED" and both["failed"] == [] and both["not_verified"] == [] and both["runs"] == ["after-close", "in-session"]
    by = {r["id"]: r for r in both["requirements"]}
    assert by["R6"]["evidence"].startswith("[after-close]") and by["R10"]["evidence"].startswith("[in-session]")  # each verdict says which run it came from
    assert fp.merge_evaluations([closed["evaluation"]], ["only"])["verdict"] == "NOT CONFIRMED"  # the closed run alone still is not enough
    bad, _, _ = open_market_probe(mutate=lambda f: f["live"]["analysis"].update(volume_decreases=1))
    assert fp.merge_evaluations([closed["evaluation"], bad["evaluation"]], ["a", "b"])["verdict"] == "NOT RELIABLE"
    assert fp.merge_evaluations([bad["evaluation"], closed["evaluation"]], ["b", "a"])["failed"] == ["R10"]  # whatever the order
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(closed, default=str), encoding="utf-8")
    b.write_text(json.dumps(opened, default=str), encoding="utf-8")
    assert fp.main(["--merge", str(a), str(b)]) == 0  # the command line does it with no broker call and no credentials


def test_the_closing_session_after_1530_is_asked_for_and_counted_in_the_exchange_comparisons(monkeypatch):
    """The first real run asked for candles only to 15:30 and compared lots with units: R6 and R7 then failed although the data was fine. The futures trade to about
    15:40 and the exchange's day totals include that, in units. The comparison now asks to 15:45 and multiplies by the lot size."""
    found, _ = probe_closed()
    latest = found["days"][-1]
    assert latest["post_close"] == {"candles": 3, "lots": sum(POST_CLOSE_LOTS), "last": "15:32"} and latest["fut"]["outside_session"] == 3
    assert latest["fut"]["present"] == 375 and latest["fut"]["volume_total"] == latest["lots_total"] - sum(POST_CLOSE_LOTS)  # in-session figures leave the closing session out
    by = {r["id"]: r for r in found["evaluation"]["requirements"]}
    assert by["R6"]["status"] == by["R7"]["status"] == fp.PASS and f"x lot size 75 = {latest['lots_total'] * 75}" in by["R7"]["evidence"] and "after 15:30" in by["R7"]["evidence"]
    assert latest["vwap"] != latest["vwap_session"]  # the whole-day VWAP (compared with the exchange's) and the 09:15-15:30 one (what a strategy would use) are told apart
    monkeypatch.setattr(fp, "FUT_TO", "15:30")  # the first run's mistake
    wrong, _ = probe_closed()
    assert {r["id"]: r["status"] for r in wrong["evaluation"]["requirements"]}["R7"] == fp.FAIL
    monkeypatch.undo()
    nolot = copy.deepcopy(found)
    nolot["contract"]["lot_size"] = 0
    assert {r["id"]: r["status"] for r in fp.evaluate(nolot)["requirements"]}["R7"] == fp.NOT_VERIFIED  # an unknown lot size is not guessed


def test_alignment_means_every_spot_minute_has_a_futures_candle_and_the_spots_own_gaps_are_reported_not_blamed_on_the_futures():
    days = good_days()
    for day, (fut, spot) in list(days.items()):
        days[day] = (fut, [s for s in spot if not 15 * 3600 + 15 * 60 <= (datetime.fromtimestamp(s["start"], IST).hour * 3600 + datetime.fromtimestamp(s["start"], IST).minute * 60) < 15 * 3600 + 29 * 60])
    found, _ = probe_closed(days)  # a spot history that lacks 15:15-15:28, as the real one does
    r8 = next(r for r in found["evaluation"]["requirements"] if r["id"] == "R8")
    assert r8["status"] == fp.PASS and "lacks 14 of the 375 session minutes, 15:15-15:28" in r8["evidence"]
    assert found["days"][-1]["spot_gaps"] == {"present": 361, "missing": 14, "first_missing": "15:15", "last_missing": "15:28"}
    days = good_days()
    for day, (fut, spot) in list(days.items()):
        days[day] = ([c for c in fut if not 10 * 3600 <= (datetime.fromtimestamp(c["start"], IST).hour * 3600 + datetime.fromtimestamp(c["start"], IST).minute * 60) < 10 * 3600 + 20 * 60], spot)
    found, _ = probe_closed(days)  # ... while a FUTURES history that lacks minutes the spot has fails
    assert next(r for r in found["evaluation"]["requirements"] if r["id"] == "R8")["status"] == fp.FAIL


def test_a_smoothed_basis_is_scored_for_information_and_does_not_decide_the_basis_requirement():
    day = date(2026, 11, 6)
    fut, _ = synth(day)
    drift, wobble = [dict(c) for c in fut], [dict(c) for c in fut]
    spot_drift = [{**c, "close": round(c["close"] - (10 + 0.2 * i), 2)} for i, c in enumerate(fut)]  # a basis that rises 0.2 points a minute
    b = fp.basis_stats(drift, spot_drift, day)
    assert b["smoothed_error_median"] == pytest.approx(0.2 * 5.5, abs=0.15) and b["step_p99"] == pytest.approx(0.2, abs=0.05)  # the median of the last 10 minutes is 5.5 minutes old
    spot_noise = [{**c, "close": round(c["close"] - (50 + (12 if i % 7 == 0 else 0)), 2)} for i, c in enumerate(wobble)]  # minutes where the basis jumps by 12
    n = fp.basis_stats(wobble, spot_noise, day)
    assert n["step_p99"] >= 12 and n["smoothed_error_median"] == 0 and n["smoothed_error_p99"] >= 12  # a noisy basis shows in both, a smoothed one still errs on the jumps
    found, _ = probe_closed()
    assert "for information, a 10-minute median basis is off by a median of" in next(r for r in found["evaluation"]["requirements"] if r["id"] == "R9")["evidence"]


def test_an_unknown_lot_size_is_never_guessed_in_the_live_volume_comparison():
    found, _, _ = open_market_probe(lot="0")  # the master gives no lot size
    status = {r["id"]: r for r in found["evaluation"]["requirements"]}
    assert status["R12"]["status"] == fp.NOT_VERIFIED and status["R13"]["status"] == fp.NOT_VERIFIED  # units cannot be compared with lots without it
    assert "lot size is unknown" in found["live"]["history_error"] and status["R10"]["status"] == fp.PASS  # what the live feed itself showed is still judged


def test_when_two_runs_disagree_about_the_same_requirement_it_fails():
    closed, _ = probe_closed()
    broken = copy.deepcopy(closed)
    broken["days"][1]["fut"].update(zero_or_missing_volume=60)
    broken["evaluation"] = fp.evaluate(broken)
    assert next(r for r in closed["evaluation"]["requirements"] if r["id"] == "R2")["status"] == fp.PASS  # one run says the volume is there ...
    assert next(r for r in broken["evaluation"]["requirements"] if r["id"] == "R2")["status"] == fp.FAIL  # ... another found it missing
    for order in ((closed, broken), (broken, closed)):
        merged = fp.merge_evaluations([o["evaluation"] for o in order], ["x", "y"])
        assert merged["verdict"] == "NOT RELIABLE" and merged["failed"] == ["R2"]  # a pass never hides a failure


def test_if_the_finished_candles_cannot_be_fetched_the_live_comparisons_stay_not_verified_not_passed():
    found, _, _ = open_market_probe(with_history=False)
    status = {r["id"]: r["status"] for r in found["evaluation"]["requirements"]}
    assert status["R10"] == fp.PASS and status["R11"] == fp.PASS  # what the live feed itself showed is still judged
    assert status["R12"] == fp.NOT_VERIFIED and status["R13"] == fp.NOT_VERIFIED  # but without the finished candles nothing can be compared, and nothing is assumed
    assert "no data available" in found["live"]["history_error"]
    disagree, _, _ = open_market_probe(mutate=lambda f: f["live"]["history_comparison"].update(median_rel_diff=0.2, p90_rel_diff=0.4))  # and history that DISAGREES with the ticks is a failure
    assert {r["id"]: r["status"] for r in disagree["evaluation"]["requirements"]}["R12"] == fp.FAIL


def test_an_open_market_with_no_ticks_fails_instead_of_passing_by_default():
    session = FakeSession(good_days(), today_candles={FRI_OPEN.date(): synth(FRI_OPEN.date())[0]})
    found = fp.run(FakeCfg(), days=3, live_seconds=0.05, now=FRI_OPEN, session=session, master_rows=master(), ws_factory=lambda s: FakeWS(Clock(1e9), []), sleep=lambda s: time.sleep(0.02),
                   clock=Clock(datetime(2026, 11, 6, 10, 20, tzinfo=IST).timestamp()), settle=0)  # fmt: skip
    status = {r["id"]: r["status"] for r in found["evaluation"]["requirements"]}
    assert status["R10"] == fp.FAIL and status["R11"] == fp.FAIL and status["R13"] == fp.NOT_VERIFIED and status["R14"] == fp.NOT_VERIFIED and found["evaluation"]["verdict"] == "NOT RELIABLE"


def test_no_live_skips_the_websocket_entirely():
    days = good_days()
    session = FakeSession(days)

    def never(_):
        raise AssertionError("the websocket must not be opened with --no-live")

    found = fp.run(FakeCfg(), days=3, live_seconds=0, now=SAT, session=session, master_rows=master(), ws_factory=never, sleep=lambda s: None)
    assert found["live"] == {"verified": False, "reason": "skipped (--no-live)"} and found["evaluation"]["verdict"] == "NOT CONFIRMED"


def test_market_hours_and_candidate_days_are_computed_without_guessing_holidays():
    assert fp.market_open(FRI_OPEN) and not fp.market_open(SAT) and not fp.market_open(FRI_OPEN.replace(hour=15, minute=30)) and not fp.market_open(FRI_OPEN.replace(hour=9, minute=14))
    assert fp.recent_session_days(SAT, 2)[0] == date(2026, 11, 6)  # the Friday that just finished
    assert fp.recent_session_days(FRI_OPEN, 2)[0] == date(2026, 11, 5)  # today's session is not finished
    after = datetime(2026, 11, 6, 15, 33, tzinfo=IST)
    assert fp.recent_session_days(after, 2)[0] == date(2026, 11, 6)  # two minutes after the close its last candle is published
    assert all(d.weekday() < 5 for d in fp.recent_session_days(SAT, 8))


def test_the_summary_names_every_requirement_and_the_verdict():
    found, _ = probe_closed()
    text = fp.summary(found)
    assert text.startswith("NIFTY futures probe: NOT CONFIRMED") and all(f"R{i}" in text for i in range(1, 15)) and "NOT VERIFIED" in text
    assert re.search(r"R6\s+PASS", text) and copy.deepcopy(found["evaluation"])["verdict"] == "NOT CONFIRMED"
