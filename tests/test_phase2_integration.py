"""The analysis wired into the live application: history + live ticks, caching, API and WebSocket."""

import asyncio
import json
from datetime import date

import pytest
from helpers import CFG, FRI, MON, SPOT_TOKEN, Clock, hhmm, ist, msg, ramp, sample_contracts, session_candles

from app import analysis as an
from app.candles import Candle
from app.engine import MarketData
from app.main import create_app

PRICE = 24226.0  # roughly where the 1M history ends


def history(**over):
    daily = Candle(ist(0, 0, day=FRI), ist(0, 0, day=MON), 24100, 24260, 24050, 24226, 0, source="db")
    h = {"1m": ramp(60, 3, 0.2, MON), "5m": ramp(300, 10, 1.0, MON), "15m": ramp(900, 25, 3.0, MON), "1d": [daily]}
    h.update(over)
    return h


def live_md(start=ist(9, 14, 30, day=MON), **over):
    clock = Clock(start)
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), date(2026, 11, 9), PRICE)
    md.set_history(history(**over), {"state": "ready", "error": None}, current_date=date(2026, 11, 9), loaded_at=start)
    return md, clock


def tick(md, clock, ts, price=PRICE):
    clock.t = ts
    md.on_message(msg(SPOT_TOKEN, price, ts))


def stream(md, clock, first, last, step=10, price=PRICE):
    """Ticks every `step` seconds (inside the gap threshold) from `first` to `last` inclusive."""
    for ts in range(int(first), int(last) + 1, step):
        tick(md, clock, ts, price)


# -- live candles flow into the analysis ----------------------------------------------------------------------------------
def test_the_analysis_uses_history_until_a_live_candle_completes_and_then_the_new_candle():
    md, clock = live_md()
    a = md.analysis()
    assert a["available"] and a["timeframes"]["1m"]["last_completed"]["start"] == ist(15, 29, day=FRI)  # last historical candle
    assert a["forming"]["1m"] is None and a["price"] == pytest.approx(24225.0)  # no tick yet: the last completed 1M close

    stream(md, clock, ist(9, 15, day=MON), ist(9, 16, 5, day=MON))
    a = md.analysis()
    assert a["timeframes"]["1m"]["last_completed"]["start"] == ist(9, 15, day=MON)  # the live 09:15 candle completed...
    assert a["forming"]["1m"]["start"] == ist(9, 16, day=MON)  # ...and 09:16 is only forming
    assert a["timeframes"]["15m"]["last_completed"]["start"] == ist(15, 15, day=FRI)  # no live 15M candle has completed yet
    assert a["price"] == PRICE

    stream(md, clock, ist(9, 16, 15, day=MON), ist(9, 30, 5, day=MON))
    a = md.analysis()
    assert a["timeframes"]["15m"]["last_completed"]["start"] == ist(9, 15, day=MON)  # now the 15M candle completed too
    assert a["timeframes"]["5m"]["last_completed"]["start"] == ist(9, 25, day=MON)
    assert a["reliable"] is True and a["warnings"] == []  # the live session has no gaps


def test_the_forming_candle_is_never_part_of_the_completed_series():
    md, clock = live_md()
    stream(md, clock, ist(9, 15, day=MON), ist(9, 15, 55, day=MON))  # every candle is still forming
    a = md.analysis()
    assert a["timeframes"]["1m"]["last_completed"]["start"] == ist(15, 29, day=FRI)
    assert a["forming"]["1m"]["start"] == ist(9, 15, day=MON) and a["forming"]["15m"]["start"] == ist(9, 15, day=MON)
    assert a["reference"]["session_date"] == "2026-11-09" and a["reference"]["session_open"] == PRICE  # open from the forming minute


def test_the_heavy_analysis_is_not_recomputed_for_every_tick():
    md, clock = live_md()
    stream(md, clock, ist(9, 15, day=MON), ist(9, 16, 20, day=MON))
    md.analysis()
    core = md._cache["core"]
    for price in (24226.5, 24227.0, 24227.5):  # more ticks inside the same minute and no candle completes
        tick(md, clock, clock.t + 3, price)
        a = md.analysis()
        assert md._cache["core"] is core  # nothing was recomputed...
        assert a["price"] == price and a["forming"]["1m"]["close"] == price  # ...but the live numbers are fresh
    tick(md, clock, ist(9, 17, day=MON), 24228.0)  # a candle completes: now it is recomputed
    md.analysis()
    assert md._cache["core"] is not core


def test_the_clock_alone_raises_the_missing_candle_warning_when_the_feed_stops():
    md, clock = live_md()
    stream(md, clock, ist(9, 15, day=MON), ist(9, 20, day=MON))
    clock.t = ist(9, 20, 5, day=MON)
    assert md.analysis()["warnings"] == []  # 09:19 closed a moment ago and is not yet expected to have been delivered
    clock.t = ist(9, 25, 30, day=MON)  # five minutes with no ticks and no new candles
    md.watchdog()  # closes the 09:20 candle on the clock
    a = md.analysis()
    assert a["reliable"] is False
    assert any(w.startswith("1M: 4 of 10 intervals missing in the latest session (2026-11-09)") and "09:21-09:24 (4)" in w for w in a["warnings"])
    assert a["timeframes"]["1m"]["missing"]["latest_session_missing"] == 4
    assert a["timeframes"]["15m"]["missing"]["latest_session_missing"] == 0  # 15M is unaffected (its 09:15 candle is not due yet)


def test_levels_follow_the_live_price_without_recomputing():
    secs_zig = lambda secs, days: zigzag(secs, days)  # noqa: E731
    md, clock = live_md(**{"15m": secs_zig(900, 25), "5m": secs_zig(300, 10), "1m": secs_zig(60, 3)})
    stream(md, clock, ist(9, 15, day=MON), ist(9, 15, 30, day=MON), price=24050.0)
    a = md.analysis()
    core = md._cache["core"]
    assert a["levels"]["resistance"] and 24100 <= a["levels"]["resistance"][0]["price"] <= 24101
    assert a["levels"]["support"] and 23999 <= a["levels"]["support"][0]["price"] <= 24001
    tick(md, clock, ist(9, 15, 40, day=MON), 24200.0)  # the price climbs above every level
    b = md.analysis()
    assert md._cache["core"] is core and b["levels"]["resistance"] == []
    assert 24100 <= b["levels"]["support"][0]["price"] <= 24101  # the old resistance is now the nearest support


def zigzag(secs, days, amp=100.0, period=20, base=24000.0):
    """A triangle wave between 24000 and 24100: a clear swing every period/2 candles."""
    out, k = [], 0
    weekdays, d = [], date(2026, 11, 9)
    while len(weekdays) < days:
        d = date.fromordinal(d.toordinal() - 1)
        if d.weekday() < 5:
            weekdays.append(d)
    for d in reversed(weekdays):
        for c in session_candles((d.year, d.month, d.day), secs):
            phase = (k % period) / period
            px = base + amp * (1 - abs(2 * phase - 1))
            out.append(Candle(c.start, c.end, px, px + 0.5, px - 0.5, px, 0, source="db"))
            k += 1
    return out


def test_a_new_contract_selection_does_not_leave_a_stale_analysis():
    md, clock = live_md()
    stream(md, clock, ist(9, 15, day=MON), ist(9, 17, 5, day=MON))
    before = md.analysis()
    assert before["timeframes"]["1m"]["last_completed"]["start"] == ist(9, 16, day=MON)
    md.set_contracts(sample_contracts(), date(2026, 11, 9), PRICE)  # the daily re-selection replaces the candle builder
    after = md.analysis()
    assert after["timeframes"]["1m"]["last_completed"]["start"] == ist(15, 29, day=FRI)  # the old live candles are gone
    assert after["price"] is not None and after["forming"]["1m"] is None


def test_analysis_works_from_history_alone_before_any_contracts_exist():
    clock = Clock(ist(8, 0, day=MON))
    md = MarketData(CFG, clock)
    md.set_history(history(), {"state": "ready", "error": None}, current_date=date(2026, 11, 9), loaded_at=clock.t)
    a = md.analysis()
    assert a["available"] and a["price"] == a["timeframes"]["1m"]["last_completed"]["close"] and a["forming"]["1m"] is None


# -- API ---------------------------------------------------------------------------------------------------------------------
def route(app, path):
    return next(r for r in app.routes if getattr(r, "path", None) == path)


def test_api_endpoints_return_the_analysis_as_plain_json():
    md, clock = live_md()
    stream(md, clock, ist(9, 15, day=MON), ist(9, 16, 5, day=MON))
    app = create_app(md, feed=None)
    app.state.md = md  # the lifespan normally sets this
    analysis = route(app, "/api/analysis").endpoint()
    state = route(app, "/api/state").endpoint()
    for payload in (analysis, state["analysis"]):
        json.dumps(payload, allow_nan=False)  # strictly valid JSON: no NaN / Infinity anywhere
        assert payload["available"] and set(payload["timeframes"]) == {"1h", "15m", "5m", "1m"}
        assert {"condition", "reference", "levels", "warnings", "reliable", "history", "forming", "price"} <= set(payload)
        assert payload["condition"]["trend"] in ("bullish", "bearish", "sideways", "unknown")
        assert payload["history"]["coverage"]["1m"]["bars"] > 1000 and payload["history"]["as_of_date"] == "2026-11-09"
    candles = route(app, "/api/candles/{token}/{timeframe}").endpoint(SPOT_TOKEN, "1m")  # Phase 1 endpoint is unchanged
    assert candles["closed"][-1]["start"] == ist(9, 15, day=MON)


def test_dashboard_websocket_receives_analysis_updates_when_a_candle_completes():
    async def scenario():
        md, clock = live_md()
        app = create_app(md, feed=None)
        sent = []

        class Client:  # a stand-in for the browser's WebSocket
            async def accept(self):
                pass

            async def send_text(self, text):
                sent.append(json.loads(text))

            async def receive_text(self):
                await asyncio.sleep(3600)

        async with app.router.lifespan_context(app):
            task = asyncio.create_task(route(app, "/ws").endpoint(Client()))
            await asyncio.sleep(0.1)
            assert sent and sent[0]["analysis"]["timeframes"]["1m"]["last_completed"]["start"] == ist(15, 29, day=FRI)
            stream(md, clock, ist(9, 15, day=MON), ist(9, 16, 5, day=MON))  # a live 1M candle completes
            await asyncio.sleep(0.9)  # the app pushes on change (throttled), at the latest every second
            latest = sent[-1]["analysis"]
            assert latest["timeframes"]["1m"]["last_completed"]["start"] == ist(9, 15, day=MON)
            assert latest["price"] == PRICE and latest["forming"]["1m"]["start"] == ist(9, 16, day=MON)
            task.cancel()

    asyncio.run(scenario())


# -- the analysis can never disturb the live data pipeline ------------------------------------------------------------------
def test_a_failing_analysis_is_reported_and_live_processing_carries_on(monkeypatch):
    md, clock = live_md()

    def boom(*a, **k):
        raise ZeroDivisionError("bug in the analysis")

    monkeypatch.setattr(an, "analyze", boom)
    stream(md, clock, ist(9, 15, day=MON), ist(9, 16, 5, day=MON))
    snap = md.snapshot()  # the dashboard push still works
    assert snap["analysis"]["available"] is False and "bug in the analysis" in snap["analysis"]["history"]["error"]
    assert snap["connection"]["ticks_accepted"] == 7 and snap["instruments"][0]["ltp"] == PRICE  # Phase 1 data is intact
    accepted = md.accepted
    tick(md, clock, ist(9, 16, 15, day=MON))
    assert md.accepted == accepted + 1  # ticks are still processed and candles still built
    assert md.builder.view(SPOT_TOKEN, "1m")[1]["start"] == ist(9, 15, day=MON)
    json.dumps(snap, allow_nan=False)
    monkeypatch.undo()
    assert md.analysis()["available"] is True  # and it recovers by itself


def test_ticks_are_not_slowed_by_the_analysis_between_candle_completions():
    md, clock = live_md()
    stream(md, clock, ist(9, 15, day=MON), ist(9, 16, 5, day=MON))
    clock.t = ist(9, 16, 12, day=MON)  # just past the once-a-minute refresh point; the burst below stays inside this minute
    md.analysis()
    calls = []
    original = an.analyze
    an.analyze = lambda *a, **k: calls.append(1) or original(*a, **k)
    try:
        for i in range(200):  # a burst of ticks and dashboard reads inside one minute
            tick(md, clock, clock.t + 0.1, PRICE + i * 0.01)
            md.snapshot()
        assert calls == []  # not a single recomputation
    finally:
        an.analyze = original
    assert hhmm(clock.t) == "09:16" and clock.t < ist(9, 17, day=MON)
