import json
from datetime import date

from helpers import CFG, SPOT_TOKEN, ist, msg, sample_contracts

from app.engine import MarketData


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(t=ist(10, 0)):
    clock = Clock(t)
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), date(2026, 11, 6), 24510.0)
    return md, clock


def send(md, clock, ts, **kw):
    clock.t = ts
    md.on_message(msg(ts=ts, **kw))


def test_good_ticks_update_latest_and_candles():
    md, clock = make()
    send(md, clock, ist(10, 0, 1), price=24500.5)
    send(md, clock, ist(10, 0, 2), price=24501.0)
    snap = md.snapshot()
    spot = next(i for i in snap["instruments"] if i["role"] == "SPOT")
    assert spot["ltp"] == 24501.0 and spot["exchange_time"] == ist(10, 0, 2) and spot["received_time"] == ist(10, 0, 2)
    assert spot["candles"]["1m"]["current"]["open"] == 24500.5
    assert snap["connection"]["ticks_accepted"] == 2
    assert [i["role"] for i in snap["instruments"]] == ["SPOT", "ATM_CE", "ATM_PE", "ITM_CE", "ITM_PE"]
    json.dumps(snap)  # the dashboard receives this as JSON


def test_bad_ticks_are_counted_and_do_not_reach_candles():
    md, clock = make()
    send(md, clock, ist(10, 0, 5), price=24500)
    send(md, clock, ist(10, 0, 5), price=24500)  # duplicate
    send(md, clock, ist(10, 0, 4), price=24400)  # out of order
    send(md, clock, ist(10, 0, 6), price=0)  # bad price
    send(md, clock, ist(10, 0, 7), token="424242")  # not one of our instruments
    clock.t = ist(10, 0, 8)
    md.on_message({"token": SPOT_TOKEN})  # malformed
    md.on_message(msg(ts=ist(10, 30)))  # timestamp in the future
    snap = md.snapshot()
    assert snap["connection"]["ticks_accepted"] == 1
    assert snap["connection"]["ticks_rejected"] == {
        "duplicate": 1, "out_of_order": 1, "bad_price": 1, "unknown_instrument": 1, "malformed": 1, "bad_timestamp": 1,
    }  # fmt: skip
    spot = snap["instruments"][0]
    assert spot["candles"]["1m"]["current"]["ticks"] == 1 and spot["ltp"] == 24500
    assert len(snap["errors"]) == 4  # duplicate/out-of-order are not logged as errors


def test_watchdog_requests_reconnect_only_after_silence_in_market_hours():
    md, clock = make()
    md.set_state("connected")
    send(md, clock, ist(10, 0, 1))
    clock.t = ist(10, 0, 40)
    assert md.watchdog() is False
    clock.t = ist(10, 1, 0)
    assert md.watchdog() is True
    md.set_state("connected")  # fresh connection gets a full grace period, no reconnect storm
    assert md.watchdog() is False
    clock.t = ist(15, 45)
    assert md.watchdog() is False  # market closed: silence is fine


def test_disconnect_opens_a_gap_that_closes_on_first_fresh_tick():
    md, clock = make()
    md.set_state("connected")
    send(md, clock, ist(10, 0, 1))
    clock.t = ist(10, 0, 3)
    md.set_state("disconnected")
    md.set_state("connecting")
    assert md.reconnects == 1
    md.set_state("connected")
    send(md, clock, ist(10, 0, 9), price=24510)
    gap = md.snapshot()["gaps"][-1]
    assert gap["reason"] == "disconnect" and (gap["start"], gap["end"]) == (ist(10, 0, 1), ist(10, 0, 9))
