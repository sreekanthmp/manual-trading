import struct

import pytest
from helpers import SPOT_TOKEN, ist, msg, tick

from app.feed import AngelWS
from app.ticks import TickValidator, decode


def test_decode_converts_paise_to_rupees_and_ms_to_seconds():
    t = decode(msg(price=24512.35, ts=ist(10, 0), volume=500), recv_ts=ist(10, 0, 1))
    assert t.ltp == 24512.35
    assert t.exch_ts == ist(10, 0) and t.recv_ts == ist(10, 0, 1)  # exchange and local receipt time kept apart
    assert t.volume == 500


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"token": SPOT_TOKEN},
        {"token": "", "exchange_type": 1, "last_traded_price": 1, "exchange_timestamp": 1},
        {"token": "1", "exchange_type": 1, "last_traded_price": "x", "exchange_timestamp": 1},
        {"token": "1", "exchange_type": 1, "last_traded_price": 1.5, "exchange_timestamp": 1},
        {"token": "1", "exchange_type": 1, "last_traded_price": 1, "exchange_timestamp": None},
    ],
)
def test_decode_rejects_malformed(bad):
    with pytest.raises(ValueError):
        decode(bad, 0)


def test_binary_quote_packet_decodes_end_to_end(tmp_path, monkeypatch):
    """A real V2 QUOTE-mode packet: SDK parser -> our decode."""
    monkeypatch.chdir(tmp_path)  # the SDK creates a logs/ folder in the working directory
    ts_ms = int(ist(10, 0) * 1000)
    packet = struct.pack("<BB25sqqq", 2, 2, b"43210", 7, ts_ms, 2450075)  # mode, exchange, token, seq, ts, ltp(paise)
    packet += struct.pack("<qqq", 75, 2449000, 1234567)  # last qty, avg price, day volume
    packet += struct.pack("<dd", 1.0, 2.0) + struct.pack("<qqqq", 1, 2, 3, 4)  # buy/sell qty, ohlc
    t = decode(AngelWS("a", "b", "c", "d")._parse_binary_data(packet), recv_ts=ist(10, 0, 1))
    assert (t.token, t.exch_type, t.ltp, t.volume, t.ltq) == ("43210", 2, 24500.75, 1234567, 75)
    assert t.exch_ts == ist(10, 0)


def validator():
    return TickValidator({SPOT_TOKEN: 1, "555": 2})


def test_validator_accepts_good_tick_and_checks_identity():
    v = validator()
    assert v.check(tick(SPOT_TOKEN, 24500, ist(10, 0))) is None
    assert v.check(tick("999", 24500, ist(10, 0))) == "unknown_instrument"
    assert v.check(tick("555", 100, ist(10, 0), exch=1)) == "unknown_instrument"  # right token, wrong exchange


def test_validator_prices_and_timestamps():
    v = validator()
    assert v.check(tick(SPOT_TOKEN, 0, ist(10, 0))) == "bad_price"
    assert v.check(tick(SPOT_TOKEN, float("nan"), ist(10, 0))) == "bad_price"
    assert v.check(tick(SPOT_TOKEN, 1, 0)) == "bad_timestamp"
    assert v.check(tick(SPOT_TOKEN, 1, ist(10, 5), recv=ist(10, 0))) == "bad_timestamp"  # from the future


def test_validator_duplicates_and_out_of_order():
    v = validator()
    assert v.check(tick(SPOT_TOKEN, 100, ist(10, 0, 5), volume=10)) is None
    assert v.check(tick(SPOT_TOKEN, 100, ist(10, 0, 5), volume=10)) == "duplicate"
    assert v.check(tick(SPOT_TOKEN, 101, ist(10, 0, 5), volume=10)) is None  # same second, new price: a real tick
    assert v.check(tick(SPOT_TOKEN, 99, ist(10, 0, 4))) == "out_of_order"
    assert v.check(tick(SPOT_TOKEN, 99, ist(10, 0, 6))) is None
