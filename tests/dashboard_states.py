"""The states the dashboard has to be able to show, as real engines (mocked market: no broker, no database), so a real browser can be pointed at each in turn.

    STATES[name] -> (build(journal_dir) -> MarketData, the decision's action)

The first group runs the real decision engine over the Phase 3 test world (a real analysis, real setups, a real decision); the second serves the counter-trend (Trend Exhaustion
Reversal) decision payloads of the Stage 3 fixtures on that engine, which is how the permission and the regime facts reach the page."""

import test_regime as R
from dashboard_harness import base_engine
from helpers import SPOT_TOKEN, ist
from test_exhaustion import ter_ctx
from test_options import snap as option_snap
from test_phase3_integration import World
from test_trend_family import decision_after

from datetime import date, datetime

from app import regime as rg
from app.candles import IST
from app.engine import MarketData
from app.instruments import Chain
from app.journal import Journal

CONFLICTING = {"trend": {"1h": "bearish", "15m": "bullish", "5m": "sideways", "1m": "sideways"}, "alignment": "conflicting"}


def scenario(name, journal_dir):
    """The Phase 3 world after its pullback, in one of the states a trader meets. Same builder as the scratch scenario server of Stage 5.1."""
    from helpers import master_rows

    w = World(mirror=name == "buy_pe")
    md, clock = w.engine()
    md.journal = Journal(f"{journal_dir}/{name}")
    slots = {"wait": (16, 17), "no_trade": (), "closed": (16, 17, 18, 19)}.get(name, (16, 17, 18, 19))
    for slot in slots:
        w.step(md, clock, slot)
        md.safe_trading()
    now = clock.t
    spot = w.at(37 if name != "wait" else 28) if slots else w.at(0)
    chain = Chain(master_rows(lo=24000, hi=26000), datetime.fromtimestamp(now, IST))
    window = chain.window(spot)
    md.set_contracts(window.contracts, date(2026, 11, 6), spot)
    md.set_chain(chain, spot, window.contracts)
    md.set_state("disconnected" if name == "disconnected" else "connected", "the broker closed the connection (test)" if name == "disconnected" else None)
    if slots:
        md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(spot * 100), "exchange_timestamp": int((now - 1) * 1000)})
    if name not in ("no_option_quote", "no_trade"):
        for c in md.contracts:
            if not c.kind or (name == "missing_one" and c.role == "ATM_CE"):
                continue
            intrinsic = max(spot - c.strike, 0) if c.kind == "CE" else max(c.strike - spot, 0)
            price = round(intrinsic + 45.0 - abs(spot - c.strike) / 20, 2)
            half = 6.0 if name == "no_suitable" else 0.15
            md.on_message(option_snap(c.token, price, now - 2, bid=(price - half, 600), ask=(price + half, 600), volume=2_000_000, oi=3_000_000, ltt=now - 20))
    clock.t = now + 1
    if name == "closed":
        clock.t = ist(16, 0, 0)
    if name == "stale":
        clock.t = now + 95  # no new NIFTY tick for about 95 s
    if name == "disconnected":
        md.down_since = clock.t - 6001  # the feed has been down for a long time: no live connection, no decision, and alerts
    return md


def facts(available=True):
    """Regime facts from the hand-built regime fixtures: a strong climb that is stalling (weakening, extended)."""
    t15 = R.tfd(adx=34.0, adx_slope=-4.0, pattern="mixed", e20atr=2.6, e50atr=3.6, run=7)
    if not available:
        return rg.regime_facts(R.analysis(R.tfd(), t15), {"15m": R.candles(R.fading())}, R.P, data_ok=False, data_why="5M candles missing 10:05-10:15")
    return rg.regime_facts(R.analysis(R.tfd(), t15), {"15m": R.candles(R.fading())}, R.P)


def reversal(name, journal_dir):
    """The real engine, serving the counter-trend decision of the Stage 3 fixtures (a mocked payload: the permission, the regime facts and a Trend Exhaustion Reversal setup)."""
    md = scenario("buy_ce", journal_dir)
    build = {
        "ter_waiting": lambda: decision_after(ter_ctx, range(47, 51), "bull", **CONFLICTING),
        "ter_denied": lambda: decision_after(ter_ctx, range(47, 52), "bull", weak="none", **CONFLICTING),
        "ter_buy_ce": lambda: decision_after(ter_ctx, range(47, 52), "bull", **CONFLICTING),
        "ter_buy_pe": lambda: decision_after(ter_ctx, range(47, 52), "bear", **CONFLICTING),
        "ter_regime_unavailable": lambda: decision_after(ter_ctx, range(47, 52), "bull", **CONFLICTING),
    }[name]
    payload = build()
    payload["market"]["regime_facts"] = facts(available=name != "ter_regime_unavailable")
    md.safe_trading = lambda: payload
    return md


STATES = {
    "buy_ce": (lambda d: scenario("buy_ce", d), "BUY CE"),
    "buy_pe": (lambda d: scenario("buy_pe", d), "BUY PE"),
    "wait": (lambda d: scenario("wait", d), "WAIT"),
    "no_trade": (lambda d: scenario("no_trade", d), "NO TRADE"),
    "closed": (lambda d: scenario("closed", d), "NO TRADE"),
    "disconnected": (lambda d: scenario("disconnected", d), "NO TRADE"),
    "stale": (lambda d: scenario("stale", d), "WAIT"),
    "no_option_quote": (lambda d: scenario("no_option_quote", d), "BUY CE"),
    "no_suitable": (lambda d: scenario("no_suitable", d), "BUY CE"),
    "ter_waiting": (lambda d: reversal("ter_waiting", d), "WAIT"),
    "ter_denied": (lambda d: reversal("ter_denied", d), "NO TRADE"),
    "ter_buy_ce": (lambda d: reversal("ter_buy_ce", d), "BUY CE"),
    "ter_buy_pe": (lambda d: reversal("ter_buy_pe", d), "BUY PE"),
    "ter_regime_unavailable": (lambda d: reversal("ter_regime_unavailable", d), "BUY CE"),
}

__all__ = ["STATES", "MarketData", "base_engine"]
