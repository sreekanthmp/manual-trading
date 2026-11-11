"""Phase 3 end to end: synthetic markets -> real Phase 2 analysis -> strategies -> lifecycle -> decision.

The 28 earlier sessions are a rising staircase (a clean higher-timeframe uptrend); today's session is defined
candle by candle. Expected prices come from the candle table below, not from the engine:

    slot  time   5M candle (relative to the open price s)         what it is
     11   10:10  high s+58                                         the leg's high
     12-16 10:15-10:35 steady decline to s+27                       the pullback
     17   10:40  o s+27 h s+31 l s+22 c s+30                       hammer: lower wick 5 of 9 points (56%)
     18   10:45  o s+29 h s+30.5 l s+27.5 c s+28.5                 small candle, no break
     19   10:50  o s+28.5 h s+36 l s+28 c s+35                     bullish, closes above the base high s+31
     20   10:55  o s+35 h s+38 l s+34 c s+37                       follow-through (already 6 points beyond the trigger)
"""

import asyncio
import json
from dataclasses import replace
from datetime import date

import market as MK
import pytest
from helpers import CFG, FRI, SPOT_TOKEN, Clock, hhmm, ist, msg, sample_contracts

from app import analysis as an
from app import trading as tr
from app.engine import MarketData
from app.journal import Journal, read, timeline
from app.main import create_app
from app.strategy import CONFIRMED, Params, Setup

TODAY, DAY, PIVOT = FRI, date(2026, 11, 6), 50000.0


def specs(s):
    rows, px = [], s
    for _ in range(12):
        rows.append((px, px + 5.5, px - 0.5, px + 4.5))
        px += 4.5
    rows[-1] = (rows[-1][0], s + 58, rows[-1][2], s + 52)
    rows += [(s + 52, s + 53, s + 47, s + 48), (s + 48, s + 49, s + 42, s + 43), (s + 43, s + 44, s + 37, s + 38),
             (s + 38, s + 39, s + 32, s + 33), (s + 33, s + 34, s + 27, s + 28)]  # fmt: skip
    rows += [(s + 27, s + 31, s + 22, s + 30), (s + 29, s + 30.5, s + 27.5, s + 28.5), (s + 28.5, s + 36, s + 28, s + 35), (s + 35, s + 38, s + 34, s + 37)]
    return rows


class World:
    """The whole market for one scenario (bullish, or its mirrored bearish twin)."""

    def __init__(self, mirror=False, gap=0.0):
        """`gap`: points between the previous close and today's open, in the scenario's own direction (the whole
        session is shifted by it, so the candles themselves are the same ordinary candles)."""
        closes = MK.staircase(28 * 375, 19000.0, up=100, up_slope=0.9, down=40, down_slope=-0.5)
        self.s = closes[-1]
        self.rows = [(o + gap, h + gap, low + gap, c + gap) for o, h, low, c in specs(self.s)]
        if mirror:
            closes = MK.mirror_path(closes, PIVOT)
            self.rows = [(PIVOT - o, PIVOT - low, PIVOT - h, PIVOT - c) for o, h, low, c in self.rows]
            self.s = PIVOT - self.s
        self.sign = -1 if mirror else 1
        self.prior = MK.chain(MK.sessions_before(TODAY, 28), closes)
        self.today = MK.session_from_5m(TODAY, self.rows, closes[-1] if not mirror else closes[-1])
        self.m1 = self.prior + self.today

    def rel(self, x):
        """A price relative to the open, in the scenario's own direction."""
        return self.sign * (x - self.s)

    def at(self, off):
        return self.s + self.sign * off

    def clock_after(self, slot):
        return ist(9, 15, day=TODAY) + 300 * (slot + 1) + 5  # just after the candle of this slot completed

    def engine(self, params=None):
        clock = Clock(self.clock_after(-1))
        md = MarketData(CFG, clock)
        if params:
            md.params, md.book = params, tr.SetupBook(params)
        return md, clock

    def step(self, md, clock, slot):
        now = self.clock_after(slot)
        clock.t = now
        md.last_spot_recv = now - 1
        md.set_history(MK.to_history([c for c in self.m1 if c.end <= now]), {"state": "ready", "error": None}, current_date=DAY, loaded_at=now)
        return md.trading()


@pytest.fixture(scope="module")
def bull():
    return World()


@pytest.fixture(scope="module")
def bear():
    return World(mirror=True)


# -- the pullback, slot by slot -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("world_name,action,option", [("bull", "BUY CE", "CE"), ("bear", "BUY PE", "PE")])
def test_a_trend_pullback_goes_from_waiting_to_a_buy_and_the_entry_then_expires(request, world_name, action, option):
    w = request.getfixturevalue(world_name)
    md, clock = w.engine()
    out = w.step(md, clock, 16)  # the pullback has come down into the 5M EMA zone, but buyers have not shown up
    assert out["decision"]["action"] == "WAIT" and [x["status"] for x in out["setups"]] == ["DETECTED"]
    out = w.step(md, clock, 17)  # the hammer completes: buyers showed up, but nothing has broken out yet
    assert out["decision"]["action"] == "WAIT" and out["setups"][0]["status"] == "WAITING_FOR_CONFIRMATION"
    base_high = w.at(31)  # the hammer's own high: the highest high of the base so far
    assert out["setups"][0]["trigger"] == pytest.approx(base_high) and out["market"]["regime"] == ("trend_up" if option == "CE" else "trend_down")
    assert w.step(md, clock, 18)["decision"]["action"] == "WAIT"  # slot 18 stays under the base high: no confirmation

    out = w.step(md, clock, 19)  # slot 19 closes at s+35, above the base high s+31, body 6.5, closing at 87% of its range
    d, s = out["decision"], out["setups"][0]
    atr5 = md.analysis()["timeframes"]["5m"]["atr14_intraday"]  # Phase 2's ATR without the overnight gap is the unit the rules use
    assert (d["action"], d["strategy"]) == (action, "trend_pullback") and s["direction"] == option and s["status"] == "CONFIRMED"
    assert s["trigger"] == pytest.approx(base_high)
    assert s["invalidation"] == pytest.approx(w.at(22) - w.sign * 0.25 * atr5)  # the pullback low less a quarter of ATR5
    assert sorted(s["zone"]) == pytest.approx(sorted([base_high, base_high + w.sign * atr5]))  # up to one ATR5 beyond the trigger
    assert s["actionable"] and s["price_state"] == "in_entry_area" and s["blockers"] == []
    assert any("rejection candle" in r for r in d["reasons"]) and "completed 5M" in s["confirm_text"]

    out = w.step(md, clock, 20)  # the next candle closes 6 points (1.0x ATR5) beyond the trigger: do not chase
    assert out["decision"]["action"] == "WAIT" and not out["setups"]
    assert [e["status"] for e in reversed(md.book.events)] == ["DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED", "EXPIRED"]
    assert "entry opportunity has passed" in md.book.recent[0].note


def test_the_buy_is_announced_once_however_often_it_is_read(bull):
    md, clock = bull.engine()
    for slot in (16, 17, 18, 19):
        bull.step(md, clock, slot)
    events = len(md.book.events)
    for _ in range(25):  # the dashboard reads this several times a second
        out = md.trading()
        assert out["decision"]["action"] == "BUY CE"
    assert len(md.book.events) == events == 3  # DETECTED, WAITING, CONFIRMED: nothing more
    assert md.book.events[0]["status"] == "CONFIRMED" and out["events"][0]["status"] == "CONFIRMED"


def test_the_lifecycle_does_not_advance_on_every_tick(bull):
    md, clock = bull.engine()
    bull.step(md, clock, 19)
    core = md._trade["core"]
    for _ in range(20):
        clock.t += 0.2  # ticks arriving inside the same minute change the price, not the setups
        md.trading()
    assert md._trade["core"] is core


# -- timing rules are enforced and configurable ---------------------------------------------------------------------------
def test_the_session_rules_decide_what_the_engine_will_do_at_the_same_moment(bull):
    """The BUY of slot 19 is at 10:55. The same market, judged under different (configurable) timing rules:"""
    def at_slot_19(**over):
        md, clock = bull.engine(replace(Params(), **over))
        for slot in (16, 17, 18, 19):
            out = bull.step(md, clock, slot)
        return out

    assert at_slot_19()["decision"]["action"] == "BUY CE"
    late = at_slot_19(last_entry="10:50")  # 10:55 is past the last entry time
    assert late["decision"]["action"] == "NO TRADE" and "no new entries after 10:50" in late["decision"]["headline"]
    opening = at_slot_19(observe_until="11:00")  # the opening range is still forming at 10:55
    assert opening["decision"]["action"] == "WAIT" and "no entries before 11:00" in opening["decision"]["headline"]
    midday = at_slot_19(midday_start="10:00", midday_end="12:00")  # stricter confirmation: the break candle needs follow-through
    assert midday["decision"]["action"] == "WAIT" and midday["setups"][0]["status"] == "WAITING_FOR_CONFIRMATION"
    assert any("midday" in a for a in midday["setups"][0]["against"])
    closed = at_slot_19(close_time="10:50", last_entry="10:40")
    assert closed["decision"]["action"] == "NO TRADE" and "closed" in closed["decision"]["headline"]


def test_stricter_confirmation_needs_two_closes_and_never_chases(bull):
    md, clock = bull.engine(replace(Params(), midday_start="10:00", midday_end="12:00"))
    for slot in (16, 17, 18, 19):
        out = bull.step(md, clock, slot)
    assert out["decision"]["action"] == "WAIT" and out["setups"][0]["status"] == "WAITING_FOR_CONFIRMATION"  # one close is not enough
    out = bull.step(md, clock, 20)  # the follow-through closes at s+37: a second close above s+31, but 6 points beyond the trigger
    assert out["decision"]["action"] == "WAIT" and not out["setups"]  # so the entry area is already behind us: nothing is issued
    assert md.book.events[0]["status"] == "EXPIRED" and "entry opportunity has passed" in md.book.events[0]["text"]
    assert "CONFIRMED" not in [e["status"] for e in md.book.events]


def test_extreme_volatility_means_no_trade(bull):
    md, clock = bull.engine(replace(Params(), vol_extreme=0.5))  # 15M ATR ratio is about 1: "unusually high" under this setting
    for slot in (16, 17, 18, 19):
        out = bull.step(md, clock, slot)
    assert out["decision"]["action"] == "NO TRADE" and "volatility is unusually high" in out["decision"]["headline"]
    assert out["setups"] == [] or out["setups"][0]["status"] != "CONFIRMED"  # nothing was confirmed while it lasted


def test_strategy_parameters_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("STRATEGY_LAST_ENTRY", "13:00")
    monkeypatch.setenv("STRATEGY_MAX_RISK_ATR15", "0.2")
    md = MarketData(CFG, Clock(ist(10, 0, day=TODAY)))
    assert (md.params.last_entry, md.params.max_risk_atr15) == ("13:00", 0.2) and md.book.p is md.params
    monkeypatch.setenv("STRATEGY_MAX_RISK_ATR15", "wide")  # a typo must not take the application down
    md = MarketData(CFG, Clock(ist(10, 0, day=TODAY)))
    assert md.params == Params() and any("strategy settings ignored" in e["message"] for e in md.errors)


# -- the market decides which strategy may run -----------------------------------------------------------------------------
def test_a_sideways_market_selects_range_reversal_and_a_bounce_that_has_already_run_is_not_an_entry():
    """The prices sit 60% of the way across the range (the 24,484 opening-range high to the 24,560 previous-day high),
    after a rise of 40 points from the edge: the reversal that started there is over, and nothing is offered."""
    closes = MK.oscillation(28 * 375, 24500.0, 60.0, period=180)
    m1 = MK.chain(MK.sessions_before(TODAY, 28), closes) + MK.minute_candles(TODAY, MK.legs(closes[-1], [(60, 24490.0), (60, 24530.0)]), closes[-1])
    now = ist(9, 15, day=TODAY) + 60 * 120 + 5
    clock = Clock(now)
    md = MarketData(CFG, clock)
    md.set_history(MK.to_history([c for c in m1 if c.end <= now]), {"state": "ready", "error": None}, current_date=DAY, loaded_at=now)
    md.last_spot_recv = now - 1
    out = md.trading()
    a = md.analysis()
    assert a["condition"]["alignment"]["label"] == "range_bound" and out["market"]["regime"] == "range"
    by = {s["id"]: s for s in out["strategies"]}
    assert not by["trend_pullback"]["active"] and "1H is sideways" in by["trend_pullback"]["why"]  # no trend to buy a pullback in
    assert by["range_reversal"]["active"] and "opportunity has passed" in by["range_reversal"]["note"]
    assert out["decision"]["action"] == "WAIT" and not out["setups"]  # it was never announced, so it is not announced late


def test_a_trending_market_does_not_run_the_range_strategy(bull):
    md, clock = bull.engine()
    out = bull.step(md, clock, 16)
    by = {s["id"]: s for s in out["strategies"]}
    assert by["trend_pullback"]["active"] and by["breakout_retest"]["active"] and not by["range_reversal"]["active"]
    assert "not range-bound" in by["range_reversal"]["why"]


def test_conflicting_timeframes_mean_no_trade(bull, monkeypatch):
    """Phase 2 reports 1H bullish but 15M bearish: the engine stays out and says why, even with a perfect pullback."""
    real = an.analyze

    def conflicted(series_by_tf, daily, forming, now):
        core = real(series_by_tf, daily, forming, now)
        core["timeframes"]["15m"]["trend"] = "bearish"
        core["condition"] = {**core["condition"], "trend": "sideways", "alignment": {"label": "conflicting", "text": "1H is bullish but 15M is bearish: conflicting timeframes."}}
        return core

    monkeypatch.setattr(an, "analyze", conflicted)
    md, clock = bull.engine()
    for slot in (16, 17, 18, 19):
        out = bull.step(md, clock, slot)
    assert out["decision"]["action"] == "NO TRADE" and "conflicting timeframes" in out["decision"]["headline"]
    assert not out["setups"] and out["market"]["regime"] == "conflicting"
    # Trend Exhaustion Reversal (Stage 3) is the one strategy that considers a conflicting market on purpose: a reversal is exactly where the timeframes disagree. Here the 1H bullish trend is
    # established but not weakening, so its counter-trend permission is DENIED, it holds no setup, the decision stays NO TRADE, and the denial is on record. No other strategy is active.
    assert not any(s["active"] for s in out["strategies"] if s["id"] not in ("breakout_retest", "trend_exhaustion_reversal"))
    ter = next(s for s in out["strategies"] if s["id"] == "trend_exhaustion_reversal")
    assert ter["state"] == "NO SETUP" and "permission denied" in (ter["note"] or ter["why"])
    assert [(c["side"], c["needed"], c["granted"]) for c in out["counter_trend"]] == [("PE", True, False)]


# -- live ticks through the Phase 1 candle builder ---------------------------------------------------------------------------
def live_engine(world):
    """History up to the open, then today's candles arrive as live ticks."""
    clock = Clock(ist(9, 14, day=TODAY))
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(), DAY, world.s)
    md.set_history(MK.to_history(world.prior), {"state": "ready", "error": None}, current_date=DAY, loaded_at=clock.t)
    return md, clock


def stream(md, clock, candles):
    """Each 1M candle as five ticks, 12 seconds apart (inside the feed's gap threshold)."""
    for c in candles:
        up = c.close >= c.open
        for k, price in enumerate([c.open, c.low if up else c.high, c.high if up else c.low, (c.open + c.close) / 2, c.close]):
            clock.t = c.start + 12 * k
            md.on_message(msg(SPOT_TOKEN, price, clock.t))


def minutes(world, first_slot, last_slot, upto_minute=None):
    cs = [c for c in world.today if ist(9, 15, day=TODAY) + 300 * first_slot <= c.start < ist(9, 15, day=TODAY) + 300 * (last_slot + 1)]
    return cs if upto_minute is None else cs[:upto_minute]


def test_live_ticks_a_forming_candle_never_confirms_a_completed_one_does(bull):
    md, clock = live_engine(bull)
    stream(md, clock, minutes(bull, 0, 18))
    clock.t = ist(9, 15, day=TODAY) + 300 * 19 + 3
    md.watchdog()  # slot 18 completes on the clock
    md.last_spot_recv = clock.t - 1
    out = md.trading()
    assert out["decision"]["action"] == "WAIT" and out["setups"][0]["status"] == "WAITING_FOR_CONFIRMATION"

    stream(md, clock, minutes(bull, 19, 19, upto_minute=4))  # slot 19's first four minutes: price is already above the trigger...
    md.last_spot_recv = clock.t
    a = md.analysis()
    assert a["forming"]["5m"]["close"] > bull.s + 31 and a["price"] > bull.s + 31  # the FORMING candle has closed above s+31
    out = md.trading()
    assert out["decision"]["action"] == "WAIT" and out["setups"][0]["status"] == "WAITING_FOR_CONFIRMATION"  # ...but nothing is confirmed
    assert not any(s["actionable"] for s in out["setups"])

    stream(md, clock, minutes(bull, 19, 19)[4:])  # the fifth minute completes the candle
    clock.t = ist(9, 15, day=TODAY) + 300 * 20 + 3
    md.watchdog()
    md.last_spot_recv = clock.t - 1
    out = md.trading()
    assert out["decision"]["action"] == "BUY CE" and out["setups"][0]["trigger"] == pytest.approx(bull.s + 31)
    assert md.analysis()["timeframes"]["5m"]["last_completed"]["start"] == ist(9, 15, day=TODAY) + 300 * 19  # the live candle is now in the series


def test_an_outage_during_the_setup_stops_a_confirmation_from_being_misleading(bull):
    md, clock = live_engine(bull)
    stream(md, clock, minutes(bull, 0, 17))  # up to and including the hammer
    stream(md, clock, minutes(bull, 19, 19))  # the feed was down for all of slot 18 (10:45-10:50)
    clock.t = ist(9, 15, day=TODAY) + 300 * 20 + 3
    md.watchdog()
    md.last_spot_recv = clock.t - 1
    out = md.trading()
    assert out["decision"]["action"] == "NO TRADE" and "not reliable" in out["decision"]["headline"]
    assert any("5M candles missing" in i for i in out["data"]["issues"]) and out["data"]["ok"] is False
    assert not any(s["status"] == "CONFIRMED" for s in out["setups"])  # a break above a base that is missing candles is not a signal
    assert md.analysis()["warnings"] and md.gaps.gaps  # Phase 1 recorded the gap and Phase 2 reports the missing candles


def test_a_price_that_has_gone_stale_is_not_acted_on(bull):
    md, clock = bull.engine()
    for slot in (16, 17, 18, 19):
        out = bull.step(md, clock, slot)
    assert out["decision"]["action"] == "BUY CE"
    clock.t += 25  # the feed stopped 25 seconds ago (limit 20)
    stale = md.trading()
    assert stale["decision"]["action"] == "WAIT" and not stale["setups"][0]["actionable"] and stale["data"]["price_ok"] is False


# -- API, dashboard feed and isolation --------------------------------------------------------------------------------------------
def route(app, path):
    return next(r for r in app.routes if getattr(r, "path", None) == path)


def test_the_api_serves_the_decision_and_the_snapshot_includes_it(bull):
    md, clock = bull.engine()
    for slot in (16, 17, 18, 19):
        bull.step(md, clock, slot)
    app = create_app(md, feed=None)
    app.state.md = md
    trading, state = route(app, "/api/trading").endpoint(), route(app, "/api/state").endpoint()
    for payload in (trading, state["trading"]):
        json.dumps(payload, allow_nan=False)  # strictly valid JSON
        assert payload["decision"]["action"] == "BUY CE" and payload["setups"][0]["status"] == "CONFIRMED"
        assert {"decision", "market", "session", "strategies", "setups", "recent", "events", "data"} <= set(payload)
        assert [s["id"] for s in payload["strategies"]] == ["trend_pullback", "breakout_retest", "range_reversal", "momentum_continuation", "ema_trend_continuation", "volatility_expansion",
                                                            "opening_range_breakout", "failed_breakout", "range_bounce", "trend_exhaustion_reversal"]  # fmt: skip
    assert state["analysis"]["available"] and state["instruments"] == []  # Phase 2 and Phase 1 parts are still there


def test_the_dashboard_websocket_gets_the_decision_when_a_candle_completes(bull):
    async def scenario():
        md, clock = live_engine(bull)
        stream(md, clock, minutes(bull, 0, 18))
        clock.t = ist(9, 15, day=TODAY) + 300 * 19 + 3
        md.watchdog()
        md.last_spot_recv = clock.t
        app = create_app(md, feed=None)
        sent = []

        class Client:
            async def accept(self):
                pass

            async def send_text(self, text):
                sent.append(json.loads(text))

            async def receive_text(self):
                await asyncio.sleep(3600)

        async with app.router.lifespan_context(app):
            task = asyncio.create_task(route(app, "/ws").endpoint(Client()))
            await asyncio.sleep(0.1)
            assert sent[0]["trading"]["decision"]["action"] == "WAIT"
            stream(md, clock, minutes(bull, 19, 19))
            clock.t = ist(9, 15, day=TODAY) + 300 * 20 + 3
            md.watchdog()
            md.last_spot_recv = clock.t - 1
            md.on_change()
            await asyncio.sleep(0.9)
            assert sent[-1]["trading"]["decision"]["action"] == "BUY CE"
            task.cancel()

    asyncio.run(scenario())


def test_a_failing_trading_engine_cannot_disturb_the_feed_or_the_analysis(bull, monkeypatch):
    md, clock = live_engine(bull)
    stream(md, clock, minutes(bull, 0, 10))

    def boom(*a, **k):
        raise ZeroDivisionError("bug in the trading layer")

    monkeypatch.setattr(tr, "build_context", boom)
    md.last_spot_recv = clock.t
    snap = md.snapshot()
    assert snap["trading"]["available"] is False and "bug in the trading layer" in snap["trading"]["decision"]["headline"]
    assert snap["analysis"]["available"] and snap["connection"]["ticks_accepted"] > 100  # Phase 1 and Phase 2 carry on
    accepted = md.accepted
    stream(md, clock, minutes(bull, 11, 11))
    assert md.accepted > accepted
    json.dumps(snap, allow_nan=False)
    monkeypatch.undo()
    assert md.safe_trading()["available"] is True  # and it recovers by itself


def test_without_history_the_decision_is_no_trade_with_the_reason():
    md = MarketData(CFG, Clock(ist(10, 0, day=TODAY)))
    out = md.trading()
    assert out["available"] is False and out["decision"]["action"] == "NO TRADE" and "still loading" in out["decision"]["headline"]
    assert hhmm(md.clock()) == "10:00"


# -- overnight gaps: the same ordinary candles, opened after a gap -------------------------------------------------------------
@pytest.mark.parametrize("kwargs,points", [(dict(), 0.0), (dict(gap=400.0), 400.0), (dict(mirror=True, gap=400.0), -400.0)], ids=["normal", "gap_up", "gap_down"])
def test_an_overnight_gap_is_a_price_jump_and_not_volatility(kwargs, points):
    """Today's candles are the table at the top of this file (6 points tall), shifted by the gap. The gap is 400 points
    (1.6%); the plain ATR still carries it at 10:35 (5M: 14.6, 15M: 34.1, a 15M ratio of 2.6 = 'unusually high' = NO TRADE),
    but the candles are ordinary and the engine must see ordinary candles."""
    w = World(**kwargs)
    md, clock = w.engine()
    out = w.step(md, clock, 16)  # 10:35
    tfs = md.analysis()["timeframes"]
    typical = sorted(h - low for _, h, low, _ in w.rows[:17])[8]  # median candle height so far, from the scenario table
    used, with_gap = out["market"]["atr"]["5m"]["used"], out["market"]["atr"]["5m"]["with_gap"]
    assert used < 1.2 * typical and out["market"]["vol_ratio"] < 1.3 and out["market"]["volatility"] == "normal"
    assert out["market"]["gap"]["points"] == pytest.approx(points)
    if points:
        assert with_gap > 2 * used and tfs["15m"]["atr_ratio"] >= Params().vol_extreme  # what the plain ATR would have said
    else:
        assert with_gap == pytest.approx(used, rel=0.01)  # a normal session: both measures agree
    assert out["decision"]["action"] == "WAIT" and "volatil" not in out["decision"]["headline"]


def test_the_decision_is_evaluated_even_when_no_dashboard_is_open(bull):
    """The lifecycle (and the journal) used to advance only when somebody read the snapshot."""
    async def scenario():
        md, clock = live_engine(bull)
        stream(md, clock, minutes(bull, 0, 19))
        clock.t = ist(9, 15, day=TODAY) + 300 * 20 + 3
        md.watchdog()
        md.last_spot_recv = clock.t - 1
        app = create_app(md, feed=None)
        assert md._trade["key"] is None and not md.book.events  # nobody has asked for the decision
        async with app.router.lifespan_context(app):
            md.on_change()  # what the next tick does
            await asyncio.sleep(0.6)  # no WebSocket client, no API call
        assert md._trade["key"] is not None and md.book.events[0]["status"] == "CONFIRMED"

    asyncio.run(scenario())


def test_a_break_made_of_opening_range_candles_is_not_a_buy_when_the_range_ends(bull):
    """The BUY of slot 19 completes at 10:55. Had the opening range run until 10:55, that candle would be opening-range trading."""
    md, clock = bull.engine(replace(Params(), observe_until="10:55"))
    for slot in (16, 17, 18, 19):
        out = bull.step(md, clock, slot)
    assert out["session"]["phase"] == "morning" and out["decision"]["action"] == "WAIT"
    setup = out["setups"][0]
    assert setup["status"] == "WAITING_FOR_CONFIRMATION" and any("inside the opening range" in a for a in setup["against"])


def test_15m_and_1h_candles_that_are_still_forming_are_never_used(bull):
    md, clock = live_engine(bull)
    stream(md, clock, minutes(bull, 0, 16))
    stream(md, clock, minutes(bull, 17, 17, upto_minute=3))  # 10:40-10:43: the 5M, 15M (10:30-10:45) and 1H (10:15-11:15) candles are all forming
    md.last_spot_recv = clock.t
    a = md.analysis()
    now = clock.t
    for tf in ("5m", "15m", "1h"):
        assert a["timeframes"][tf]["last_completed"]["end"] <= now, tf  # the newest completed candle ended before now
        assert md._cache["series"][tf][-1].end <= now, tf
    assert a["forming"]["15m"]["start"] == ist(10, 30, day=TODAY) and a["timeframes"]["15m"]["last_completed"]["end"] == ist(10, 30, day=TODAY)
    assert a["forming"]["1m"] is not None and md.trading()["available"]


def test_a_buy_does_not_survive_the_data_stopping(bull):
    """The machine sleeps for half an hour: no ticks and no new candles. The BUY must not outlive its data."""
    md, clock = bull.engine()
    for slot in (16, 17, 18, 19):
        out = bull.step(md, clock, slot)
    assert out["decision"]["action"] == "BUY CE"
    clock.t += 1800
    out = md.trading()
    assert out["decision"]["action"] == "NO TRADE" and "not reliable" in out["decision"]["headline"]
    assert not any(s["actionable"] for s in out["setups"]) and out["data"]["price_ok"] is False
    assert any("5M candles missing" in i for i in out["data"]["issues"])


# -- a restart, a crash and a feed outage, through the journal ------------------------------------------------------------------------
def restarted(world, directory, resume_at, history_until):
    """A second process for the same day: a new engine on the same journal folder, resumed from it."""
    md, clock = world.engine()
    md.journal = Journal(directory)
    clock.t = resume_at
    resumed = md.resume()
    md.last_spot_recv = clock.t - 1
    md.set_history(MK.to_history([c for c in world.m1 if c.end <= history_until]), {"state": "ready", "error": None}, current_date=DAY, loaded_at=clock.t)
    return md, clock, resumed


def test_a_restart_inside_the_entry_window_does_not_announce_the_buy_again(bull, tmp_path):
    md1, clock1 = bull.engine()
    md1.journal = Journal(tmp_path)
    for slot in (16, 17, 18, 19):
        out1 = bull.step(md1, clock1, slot)
    assert out1["decision"]["action"] == "BUY CE"
    s1 = md1.book.active[("trend_pullback", "bull")]

    md2, clock2, resumed = restarted(bull, tmp_path, clock1.t + 20, clock1.t)  # the process dies and a new one starts 20 seconds later
    assert resumed == 1
    out2 = md2.trading()
    s2 = md2.book.active[("trend_pullback", "bull")]
    assert out2["decision"]["action"] == "BUY CE" and out2["setups"][0]["actionable"] and out2["setups"][0]["resumed"]  # the state carries on...
    assert (s2.detected_at, s2.confirmed_at) == (s1.detected_at, s1.confirmed_at)  # ...with its original times
    path = next(tmp_path.glob("decisions-*.jsonl"))
    records, bad = read(path)
    events = [e for r in records for e in r["events"]]
    assert bad == [] and [e["status"] for e in events if e["id"] == s1.id] == ["DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED"]  # announced once, across both runs
    assert [r["reason"] for r in records].count("restart") == 1 and records[-1]["reason"] == "restart" and records[-1]["feed"]["state"] == "starting"
    assert records[-1]["setups"][0]["resumed"] is True and records[-1]["setups"][0]["confirmed_at"] == s1.confirmed_at

    out3 = bull.step(md2, clock2, 20)  # the story goes on in the new run: the next candle ends the entry
    assert out3["decision"]["action"] == "WAIT" and md2.book.events[0]["status"] == "EXPIRED" and md2.book.events[0]["seq"] == 3
    records, _ = read(path)
    assert [e["seq"] for r in records for e in r["events"]] == [0, 1, 2, 3]  # the numbering continues across the restart: no number is used twice
    assert "(resumed)" in "\n".join(timeline(path))


def test_a_restart_after_the_last_record_was_cut_short_loses_only_that_record(bull, tmp_path):
    md1, clock1 = bull.engine()
    md1.journal = Journal(tmp_path)
    for slot in (16, 17, 18, 19):
        bull.step(md1, clock1, slot)
    path = next(tmp_path.glob("decisions-*.jsonl"))
    written = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(written[:-1]) + "\n" + written[-1][:200], encoding="utf-8")  # the BUY record was being written when the power went
    md2, _, resumed = restarted(bull, tmp_path, clock1.t + 20, clock1.t)
    assert resumed == 1 and md2.book.active[("trend_pullback", "bull")].status == "WAITING_FOR_CONFIRMATION"  # the last complete record said waiting
    out = md2.trading()
    assert out["decision"]["action"] == "BUY CE"  # the candles say it was confirmed: announced now, because the journal never held that announcement
    records, bad = read(path)
    assert len(bad) == 1 and sum(1 for r in records for e in r["events"] if e["status"] == "CONFIRMED") == 1  # exactly one announcement is readable
    assert records[-1]["decision"]["action"] == "BUY CE" and records[-1]["reason"] == "restart"


def test_the_real_startup_picks_todays_setups_up_from_the_journal(tmp_path, monkeypatch):
    import time

    from app import main as app_main

    now = time.time()
    s = Setup("trend_pullback", "bull", "trend_pullback:bull:123", CONFIRMED, 123.0, level=24500.0, level_label="leg origin", trigger=24590.0,
              zone=(24590.0, 24600.0), invalidation=24559.5, detected_at=now - 900, status_since=now - 600, confirmed_at=now - 600)  # fmt: skip
    event = {"seq": 4, "time": now - 600, "id": s.id, "strategy": s.strategy, "strategy_name": "Trend Pullback", "direction": "CE", "status": "CONFIRMED", "text": "CONFIRMED: x"}
    record = {"time": "earlier today", "t": now - 300, "reason": "changed", "decision": {"action": "BUY CE", "headline": "BUY CE: earlier", "setup_id": s.id},
              "market": {"regime": "trend_up"}, "setups": [tr.setup_dict(s, 24595.0, True, True)], "events": [event]}  # fmt: skip
    path = Journal(tmp_path).path(now)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    class FakeFeed:  # no network: what is under test is the real wiring of the journal and the restart
        def __init__(self, cfg, session, md):
            self.cfg, self.session, self.md = cfg, session, md

        def start(self):
            pass

        def stop(self):
            pass

        def watchdog(self):
            self.md.watchdog()

    monkeypatch.setattr(app_main, "load_config", lambda: CFG)
    monkeypatch.setattr(app_main, "Feed", FakeFeed)
    monkeypatch.setattr(app_main, "AngelSession", lambda cfg, limiter=None: None)
    monkeypatch.setattr(app_main, "run_history", lambda *a, **k: None)
    monkeypatch.setattr(app_main, "run_validation", lambda *a, **k: None)
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))

    async def scenario():
        app = app_main.create_app()
        async with app.router.lifespan_context(app):
            md = app.state.md
            assert md.journal.dir == tmp_path
            carried = md.book.active[("trend_pullback", "bull")]
            assert carried.resumed and carried.confirmed_at == now - 600 and md.book.seq == 5 and md.book.events[0]["seq"] == 4
            md.on_change()
            await asyncio.sleep(0.5)  # the first evaluation (the history is still loading) is written down as the restart

    asyncio.run(scenario())
    records, bad = read(path)
    assert bad == [] and [r["reason"] for r in records][:2] == ["changed", "restart"] and "still loading" in records[1]["decision"]["headline"]


def test_a_feed_outage_is_on_record_when_it_happens_and_when_it_ends(bull, tmp_path):
    md, clock = live_engine(bull)
    md.journal = Journal(tmp_path)
    md.set_state("connecting")
    md.set_state("connected")
    stream(md, clock, minutes(bull, 0, 17))
    md.last_spot_recv = clock.t
    md.trading()
    md.set_state("disconnected", "websocket closed")  # what the feed supervisor does when the socket drops
    md.trading()
    clock.t += 30
    md.watchdog()
    md.trading()
    md.set_state("connecting")
    md.trading()
    md.set_state("connected")
    md.trading()
    stream(md, clock, minutes(bull, 19, 19))  # ticks are back: the gap closes
    md.last_spot_recv = clock.t
    md.trading()
    path = next(tmp_path.glob("decisions-*.jsonl"))
    records, bad = read(path)
    states = [(r["feed"]["state"], r["feed"]["reconnects"], bool(r["feed"]["open_gap"]), bool(r["feed"]["last_gap"])) for r in records]
    assert bad == [] and states[0] == ("connected", 0, False, False) and ("disconnected", 0, True, False) in states
    assert ("connecting", 1, True, False) in states and ("connected", 1, True, False) in states and states[-1] == ("connected", 1, False, True)
    down = next(r for r in records if r["feed"]["state"] == "disconnected")
    assert down["feed"]["errors"][-1]["message"] == "websocket closed" and down["feed"]["open_gap"]["reason"] == "disconnect"
    assert records[-1]["feed"]["last_gap"]["end"] is not None and records[-1]["feed"]["history"]["state"] == "ready"
    assert len(records) < 14  # the connection is written when it changes, not every second
    text = "\n".join(timeline(path))
    assert "feed: disconnected" in text and "NO TICKS since" in text and "last gap" in text and "last error: websocket closed" in text
