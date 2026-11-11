import pytest
from helpers import ist, tick

from app.candles import Candle, CandleBuilder, bucket, in_session

S, O = "S", "OPT"  # an index (no volume) and an option (volume)


def builder():
    return CandleBuilder([S, O], volume_tokens=[O])


def feed(b, token, prices_at):
    for ts, price in prices_at:
        b.on_tick(tick(token, price, ts))


def test_ohlc_in_all_four_timeframes():
    b = builder()
    feed(b, S, [(ist(9, 15, 0), 100), (ist(9, 15, 40), 105), (ist(9, 15, 59), 99), (ist(9, 16, 5), 101)])
    cur, last = b.view(S, "1m")
    assert last["start"] == ist(9, 15) and (last["open"], last["high"], last["low"], last["close"]) == (100, 105, 99, 99)
    assert last["ticks"] == 3 and not last["partial"]
    assert cur["start"] == ist(9, 16) and cur["open"] == cur["close"] == 101
    for tf in ("5m", "15m", "1h"):  # one candle still forming that holds all four ticks
        cur, last = b.view(S, tf)
        assert last is None and cur["start"] == ist(9, 15)
        assert (cur["open"], cur["high"], cur["low"], cur["close"], cur["ticks"]) == (100, 105, 99, 101, 4)


def test_candles_close_at_interval_boundaries_anchored_to_0915():
    b = builder()
    feed(b, S, [(ist(9, 15), 1), (ist(9, 19, 59), 2), (ist(9, 20), 3)])
    assert b.view(S, "5m")[1]["start"] == ist(9, 15) and b.view(S, "5m")[0]["start"] == ist(9, 20)
    assert b.view(S, "15m")[1] is None  # 09:15-09:30 still open
    feed(b, S, [(ist(10, 14, 59), 4), (ist(10, 15), 5)])
    assert b.view(S, "1h")[1]["start"] == ist(9, 15)  # hourly candles start at 09:15, not 09:00
    assert b.view(S, "1h")[0]["start"] == ist(10, 15)


def test_session_boundaries():
    assert bucket(ist(9, 14, 59), 60) is None and bucket(ist(15, 30), 60) is None
    assert in_session(ist(9, 15)) and in_session(ist(15, 29, 59))
    assert not in_session(ist(10, 0, day=(2026, 11, 7)))  # Saturday
    assert bucket(ist(15, 20), 3600) == (ist(15, 15), ist(15, 30))  # last hourly candle is cut at the close
    b = builder()
    feed(b, S, [(ist(9, 10), 1), (ist(15, 31), 2)])  # pre-open and post-close ticks make no candles
    assert not b.current and not b.closed


def test_clock_closes_candle_without_new_tick():
    b = builder()
    feed(b, S, [(ist(15, 29, 59), 7)])
    b.close_expired(ist(15, 30, 1))  # inside the grace period
    assert b.view(S, "1m")[0] is not None
    b.close_expired(ist(15, 30, 4))
    assert b.view(S, "1m")[0] is None and b.view(S, "1m")[1]["close"] == 7
    assert b.view(S, "1h")[1]["end"] == ist(15, 30)


def test_late_tick_does_not_reopen_a_closed_candle():
    b = builder()
    feed(b, S, [(ist(10, 0, 10), 1), (ist(10, 1, 10), 2)])
    feed(b, S, [(ist(10, 0, 50), 99)])
    assert [c.close for c in b.closed[(S, "1m")]] == [1] and b.view(S, "1m")[0]["close"] == 2


def test_option_volume_is_the_change_in_day_total_index_has_none():
    b = builder()
    for ts, vol in [(ist(10, 0, 0), 1000), (ist(10, 0, 10), 1040), (ist(10, 0, 20), 1100), (ist(10, 1, 0), 1110)]:
        b.on_tick(tick(O, 50, ts, volume=vol))
    assert b.view(O, "1m")[1]["volume"] == 100  # 1040-1000 + 1100-1040
    assert b.view(O, "5m")[0]["volume"] == 110
    assert b.view(O, "1m")[0]["volume"] == 10  # 10:01 candle owns the 1100->1110 trade
    b.on_tick(tick(S, 100, ist(10, 0, 5), volume=999))
    assert b.view(S, "1m")[0]["volume"] is None


def test_candle_started_before_we_were_watching_is_partial():
    b = builder()
    feed(b, S, [(ist(10, 0, 30), 1), (ist(10, 1, 5), 2)])
    assert b.view(S, "1m")[1]["partial"]  # 10:00 candle: first tick arrived mid-candle
    assert not b.view(S, "1m")[0]["partial"]  # 10:01 candle began after we were live


def test_gap_marks_overlapping_candles_partial_and_resets_volume_baseline():
    b = builder()
    for ts, vol in [(ist(10, 0, 0), 1000), (ist(10, 1, 0), 1010), (ist(10, 2, 0), 1020)]:
        b.on_tick(tick(O, 5, ts, volume=vol))
    assert not b.view(O, "1m")[1]["partial"]
    b.mark_gap(ist(10, 1, 20), ist(10, 2, 30))
    assert b.view(O, "1m")[0]["partial"]  # forming candle
    assert b.closed[(O, "1m")][1].partial and not b.closed[(O, "1m")][0].partial  # 10:01 overlaps, 10:00 does not
    b.on_tick(tick(O, 5, ist(10, 2, 40), volume=5000))
    assert b.view(O, "1m")[0]["volume"] == 10  # still 10 (1010->1020): the 1020->5000 jump is not credited


def test_missing_candles_are_never_invented():
    b = builder()
    feed(b, S, [(ist(10, 0), 1), (ist(10, 5), 2)])
    starts = [c.start for c in b.closed[(S, "1m")]]
    assert starts == [ist(10, 0)]  # nothing for 10:01-10:04
    miss = b.missing_in(ist(10, 0, 30), ist(10, 4, 30), now=ist(10, 5, 10))
    assert miss[(S, "1m")] == [ist(10, 1), ist(10, 2), ist(10, 3), ist(10, 4)]


def test_fill_replaces_partial_and_adds_missing_but_keeps_good_live_candles():
    b = builder()
    feed(b, S, [(ist(10, 0, 30), 1), (ist(10, 1, 0), 2), (ist(10, 2, 0), 3)])  # 10:00 partial, 10:01 good
    hist = [Candle(ist(10, m) if m >= 0 else ist(9, 59), ist(10, m + 1), 9, 9, 9, 9, volume=5, source="history") for m in (-1, 0, 1, 3)]  # -1 = 09:59
    assert b.fill(S, "1m", hist + [Candle(ist(10, 2), ist(10, 3), 0, 0, 0, 0)]) == 2  # 09:59 added, 10:00 replaced
    by = {c.start: c for c in b.closed[(S, "1m")]}
    assert ist(9, 59) in by
    assert by[ist(10, 0)].source == "history" and not by[ist(10, 0)].partial
    assert by[ist(10, 1)].source == "live"
    assert ist(10, 3) not in by  # 10:03 is not before the forming candle (10:02): not filled
    assert by[ist(10, 0)].volume is None  # index: history volume ignored


@pytest.mark.parametrize("secs,expected", [(60, 60), (300, 300), (900, 900), (3600, 3600)])
def test_bucket_lengths(secs, expected):
    s, e = bucket(ist(11, 0, 17), secs)
    assert e - s == expected
