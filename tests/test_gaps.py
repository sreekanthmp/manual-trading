from datetime import datetime

from helpers import CFG, SPOT_TOKEN, ist, msg, sample_contracts

from app.candles import IST
from app.engine import MarketData
from app.gaps import API_INTERVAL, GapTracker, recover

STEP = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "ONE_HOUR": 3600}


def test_gap_detected_when_ticks_resume_late():
    g = GapTracker(15)
    assert g.on_tick(ist(10, 0, 0)) is None
    assert g.on_tick(ist(10, 0, 10)) is None  # 10s of silence is normal
    gap = g.on_tick(ist(10, 2, 10))
    assert (gap["start"], gap["end"]) == (ist(10, 0, 10), ist(10, 2, 10))  # interruption and recovery time
    assert g.on_tick(ist(10, 2, 11)) is None


def test_watchdog_opens_gap_and_next_tick_closes_it():
    g = GapTracker(15)
    g.on_tick(ist(10, 0, 0))
    g.check(ist(10, 0, 10))
    assert g.open is None
    g.check(ist(10, 0, 20))
    assert g.open and g.open["start"] == ist(10, 0, 0) and g.open["end"] is None
    assert g.on_tick(ist(10, 1, 0))["end"] == ist(10, 1, 0) and g.open is None


def test_no_gap_outside_the_session_or_across_days():
    g = GapTracker(15)
    g.on_tick(ist(15, 29, 59))
    g.check(ist(15, 45))  # market closed: silence is expected
    assert g.open is None
    assert g.on_tick(ist(9, 15, 1, day=(2026, 11, 9))) is None  # next trading day is not a gap
    g.on_tick(ist(10, 0))
    g.start(ist(10, 0, 5), "disconnect")
    g.check(ist(15, 31))  # market closed before data came back
    assert g.open is None and g.gaps[-1]["ended_by"] == "session_close"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class HistorySession:
    """Pretends to be Angel One's getCandleData: one candle per interval between fromdate and todate."""

    def __init__(self):
        self.requests = []

    def call(self, method, params):
        assert method == "getCandleData"
        self.requests.append(params)
        step = STEP[params["interval"]]
        fmt = "%Y-%m-%d %H:%M"
        t = datetime.strptime(params["fromdate"], fmt).replace(tzinfo=IST).timestamp()
        end = datetime.strptime(params["todate"], fmt).replace(tzinfo=IST).timestamp()
        rows = []
        while t <= end:
            rows.append([datetime.fromtimestamp(t, IST).isoformat(), 1, 2, 0.5, 1.5, 77])
            t += step
        return {"status": True, "data": rows}


def test_gap_then_recovery_from_history_api():
    clock = Clock(ist(10, 0))
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), datetime(2026, 11, 6).date(), 24510.0)
    spot = lambda ts: msg(SPOT_TOKEN, 24500 + ts % 7, ts)  # noqa: E731

    def tick_at(ts):
        clock.t = ts
        md.on_message(spot(ts))

    tick_at(ist(10, 0, 0))
    tick_at(ist(10, 0, 10))
    tick_at(ist(10, 3, 30))  # connection was down 10:00:10 -> 10:03:30
    assert len(md.gaps.gaps) == 1
    one_min = md.builder.closed[(SPOT_TOKEN, "1m")]
    assert [c.start for c in one_min] == [ist(10, 0)] and one_min[0].partial  # damaged, not hidden
    clock.t = ist(10, 5)

    sess = HistorySession()
    assert recover(sess, md, sleep=lambda s: None) > 0
    fixed = {c.start: c for c in md.builder.closed[(SPOT_TOKEN, "1m")]}
    assert sorted(fixed) == [ist(10, 0), ist(10, 1), ist(10, 2)]  # 10:03 is still forming: left alone
    assert all(c.source == "history" and not c.partial for c in fixed.values())
    assert md.builder.view(SPOT_TOKEN, "1m")[0]["start"] == ist(10, 3)
    assert {r["interval"] for r in sess.requests} <= set(API_INTERVAL.values())
    assert all(r["exchange"] in ("NSE", "NFO") for r in sess.requests)
    assert recover(sess, md, sleep=lambda s: None) == 0  # nothing left to recover; no refetch loop


def test_recovery_failure_is_logged_not_fatal():
    clock = Clock(ist(10, 0))
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), datetime(2026, 11, 6).date(), 24510.0)
    for ts in (ist(10, 0), ist(10, 3)):
        clock.t = ts
        md.on_message(msg(SPOT_TOKEN, 24500, ts))
    clock.t = ist(10, 5)

    class Down:
        def call(self, *a):
            raise ConnectionError("api down")

    assert recover(Down(), md, sleep=lambda s: None) == 0
    assert any("recovery" in e["message"] for e in md.errors)
