"""The comparison of the live tick-built candles with Angel One's official candles. Hand-made candles, a fake API: no real service."""

import json
import threading
from datetime import date, datetime

import pytest
from helpers import CFG, SPOT_TOKEN, Clock, ist, msg, sample_contracts

from app import validation as vd
from app.candles import IST, Candle
from app.engine import MarketData

T0 = ist(10, 0)
M = 60


def c(minute, o, h, low, cl, source="live", partial=False, secs=M, hour=10):
    start = ist(hour, 0) + minute * secs
    return Candle(start, start + secs, o, h, low, cl, source=source, partial=partial)


def official():  # 09:59 (before Prism was watching) and 10:00..10:05
    return [Candle(T0 - M, T0, 99, 100, 98, 100, source="history")] + [
        c(0, 100, 103, 99, 102), c(1, 102, 105, 101, 104), c(2, 104, 106, 103, 105), c(3, 105, 107, 104, 106), c(4, 106, 108, 105, 107), c(5, 107, 109, 106, 108)]  # fmt: skip


def live():
    return [
        c(0, 100, 103, 99, 102),  # identical to the official candle
        c(1, 102, 104, 101, 103.5),  # a lower high and a lower close, but inside the official range [101, 105]: what sampling causes
        c(2, 104, 107, 103, 105),  # a high of 107 when the official high is 106: a price that does not belong to this candle
        c(3, 105, 107, 104, 106, partial=True),  # a gap overlapped it: not compared
        c(4, 106, 108, 105, 107, source="history"),  # it IS the official candle (recovered): comparing it with itself proves nothing
        c(6, 108, 110, 107, 109),  # the official history has no 10:06 candle
    ]  # fmt: skip


def test_each_live_candle_is_classified_by_an_objective_rule_and_nothing_is_called_a_match_that_is_not():
    r = vd.compare(live(), official(), "1m")
    assert (r["compared"], r["match"], r["sampling"], r["material"]) == (3, 1, 1, 1)
    assert (r["unverified_partial"], r["recovered_from_official"], r["missing_live"], r["missing_official"], r["before_live_start"]) == (1, 1, 1, 1, 1)
    assert r["examples"]["material"][0]["start"] == c(2, 0, 0, 0, 0).start and r["examples"]["material"][0]["diff"] == {"high": 1.0}  # 107 against 106
    assert r["examples"]["sampling"][0]["diff"] == {"high": -1.0, "close": -0.5} and r["examples"]["sampling"][0]["official_range"] == {"low": 101, "high": 105}
    assert r["examples"]["missing_live"] == [c(5, 0, 0, 0, 0).start] and r["examples"]["missing_official"] == [c(6, 0, 0, 0, 0).start]


def test_the_differences_are_reported_per_field_in_points_and_percent_and_exactly():
    f = vd.compare(live(), official(), "1m")["fields"]
    assert f["open"] == {"mean_abs": 0.0, "max_abs": 0.0, "mean_pct": 0.0, "max_pct": 0.0}
    assert f["high"]["mean_abs"] == pytest.approx(2 / 3) and f["high"]["max_abs"] == 1.0  # |0|, |-1|, |+1| over three compared candles
    assert f["high"]["mean_pct"] == pytest.approx((100 / 104 + 100 / 105) / 3) and f["high"]["max_pct"] == pytest.approx(100 / 104)  # against the official close
    assert f["close"]["mean_abs"] == pytest.approx(0.5 / 3) and f["close"]["max_abs"] == 0.5 and f["close"]["mean_pct"] == pytest.approx(50 / 104 / 3)
    assert f["low"]["max_abs"] == 0.0


def test_a_live_candle_with_no_official_twin_and_a_shifted_timestamp_is_flagged_as_possible_shift():
    live_c = [c(0, 1, 2, 0.5, 1.5), c(2, 1, 2, 0.5, 1.5), c(4, 1, 2, 0.5, 1.5)]
    off = [c(1, 1, 2, 0.5, 1.5), c(3, 1, 2, 0.5, 1.5), c(5, 1, 2, 0.5, 1.5)]  # every live candle sits one interval from an official candle
    r = vd.compare(live_c, off, "1m")
    assert r["missing_official"] == 3 and r["missing_live"] == 3 and r["possible_timestamp_shift"] is True
    assert vd.compare(live(), official(), "1m")["possible_timestamp_shift"] is False  # one stray candle is not a shift


def test_five_and_fifteen_minute_candles_are_compared_on_their_own_grid():
    live5 = [c(0, 100, 110, 95, 105, secs=300), c(1, 105, 112, 104, 111, secs=300)]
    off5 = [c(0, 100, 110, 95, 105, secs=300), c(1, 105, 112, 104, 111, secs=300)]
    r = vd.compare(live5, off5, "5m")
    assert (r["compared"], r["match"], r["missing_live"], r["missing_official"]) == (2, 2, 0, 0)
    assert vd.compare([], off5, "5m")["unverified"] == "no live candles were built for this timeframe"  # no live candles: nothing to verify, and it says so


# -- the day: fetch, compare, report, never overwrite -----------------------------------------------------------------------------
class OfficialApi:
    def __init__(self, fail=()):
        self.fail, self.requests = set(fail), []

    def call(self, method, params):
        assert method == "getCandleData"
        self.requests.append(params)
        if params["interval"] in self.fail:
            raise Exception("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")
        secs = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900}[params["interval"]]
        rows = [[datetime.fromtimestamp(ist(10, 0) + k * secs, IST).isoformat(), 24500 + k, 24502 + k, 24499 + k, 24501 + k, 0] for k in range(3)]
        return {"status": True, "data": rows}


def engine_with_live_candles():
    clock = Clock(ist(10, 0))
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), date(2026, 11, 6), 24510.0)
    for minute in range(3):  # three 1M candles built from ticks, each with an open, a high, a low and a close
        for s, price in ((0, 24500 + minute), (10, 24501 + minute), (20, 24502 + minute), (30, 24500 + minute), (40, 24499 + minute), (50, 24500 + minute), (59, 24501 + minute)):
            clock.t = ist(10, minute, s)
            md.on_message(msg(SPOT_TOKEN, price, clock.t))
    clock.t = ist(10, 16)  # long enough for the 5M and the 15M candle of 10:00 to have closed on the clock too
    md.watchdog()
    return md


def test_the_day_is_validated_reported_to_a_file_and_the_live_candles_are_left_exactly_as_they_were(tmp_path):
    md = engine_with_live_candles()
    before = [(x.start, x.open, x.high, x.low, x.close, x.source) for x in md.builder.closed[(SPOT_TOKEN, "1m")]]
    api = OfficialApi()
    result = vd.validate_day(api, md, date(2026, 11, 6), sleep=lambda s: None, directory=tmp_path)
    one = result["timeframes"]["1m"]
    assert result["status"] == "complete" and (one["compared"], one["match"]) == (3, 3)  # the fake official candles equal the ticks' open/high/low/close
    assert {r["interval"] for r in api.requests} == {"ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"}
    assert all(r["symboltoken"] == SPOT_TOKEN and r["fromdate"] == "2026-11-06 09:15" for r in api.requests)
    saved = json.loads((tmp_path / "candle-validation-2026-11-06.json").read_text(encoding="utf-8"))
    assert saved["date"] == "2026-11-06" and saved["limitations"] and saved["timeframes"]["1m"]["compared"] == 3
    assert md.validation["status"] == "complete" and md.validation["lines"][0].startswith("1M: 3 compared: 3 exact")
    assert before == [(x.start, x.open, x.high, x.low, x.close, x.source) for x in md.builder.closed[(SPOT_TOKEN, "1m")]]  # never overwritten


def test_a_timeframe_that_cannot_be_read_is_reported_as_unverified_and_never_invented(tmp_path):
    md = engine_with_live_candles()
    result = vd.validate_day(OfficialApi(fail={"FIFTEEN_MINUTE"}), md, date(2026, 11, 6), sleep=lambda s: None, directory=tmp_path)
    assert result["status"] == "partial" and "unverified" in result["timeframes"]["15m"] and result["timeframes"]["1m"]["compared"] == 3
    assert any(line.startswith("15M: not verified") and "could not be read" in line for line in result["lines"])
    down = vd.validate_day(OfficialApi(fail={"ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"}), md, date(2026, 11, 6), sleep=lambda s: None, directory=tmp_path)
    assert down["status"] == "unavailable" and all("unverified" in t for t in down["timeframes"].values())


def test_the_validation_runs_once_after_the_close_on_a_trading_day_and_not_before(tmp_path):
    md = engine_with_live_candles()
    now, stop, calls = [ist(14, 0)], threading.Event(), []
    api = OfficialApi()
    api.call = (lambda orig: lambda method, params: calls.append(params["interval"]) or orig(method, params))(api.call)
    polls = [0]

    def wait(_):
        polls[0] += 1
        now[0] = [ist(14, 0), ist(15, 41), ist(15, 42), ist(15, 43), ist(15, 44)][min(polls[0], 4)]
        if polls[0] >= 5:
            stop.set()

    vd.run(CFG, md, api, stop, now=lambda: now[0], wait=wait, sleep=lambda s: None, directory=tmp_path)
    assert calls.count("ONE_MINUTE") == 1  # not at 14:00; once at 15:41; not again on the later polls the same day
    assert md.validation["date"] == "2026-11-06" and md.validation["status"] == "complete"


def test_the_validation_does_not_run_on_a_weekend(tmp_path):
    md = engine_with_live_candles()
    stop, polls, api = threading.Event(), [0], OfficialApi()

    def wait(_):
        polls[0] += 1
        if polls[0] >= 2:
            stop.set()

    vd.run(CFG, md, api, stop, now=lambda: ist(15, 45, day=(2026, 11, 7)), wait=wait, sleep=lambda s: None, directory=tmp_path)  # a Saturday
    assert api.requests == [] and md.validation is None
