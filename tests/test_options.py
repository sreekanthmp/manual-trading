"""Phase 4: option contract selection, live option quotes, the suitability checks and the selection rules. Mocked Angel One data only: no broker
session is needed. Prices are hand-made; none is a market price."""

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest
from helpers import CFG, SPOT_TOKEN, Clock, ist, master_rows
from test_phase3_integration import bull  # noqa: F401

from app import instruments, options as op
from app.candles import IST
from app.engine import MarketData
from app.feed import Feed, token_groups
from app.instruments import Chain
from app.journal import Journal, read
from app.ticks import Tick, TickValidator, decode

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=IST)  # a Friday; the master below expires on 10 and 17 November
P = op.OptionParams()


def chain_of(**kw):
    return Chain(master_rows(**kw), NOW)


# -- the chain: strikes and expiries come from the instrument data -----------------------------------------------------------------------
def test_the_window_is_the_atm_strike_and_its_neighbours_for_calls_and_puts_in_the_first_expiries():
    w = chain_of().window(24510.0, each_side=3, n_expiries=2)  # strikes 24000..25000 every 50: ATM is 24500
    options = [c for c in w.contracts if c.kind]
    assert w.contracts[0].role == "SPOT" and w.center == 24500.0 and w.primary == date(2026, 11, 10) and w.spot == 24510.0
    assert sorted({c.strike for c in options}) == [24350.0, 24400.0, 24450.0, 24500.0, 24550.0, 24600.0, 24650.0]  # three each side
    assert len(options) == 2 * 7 * 2 and {c.expiry for c in options} == {date(2026, 11, 10), date(2026, 11, 17)}  # both expiries, calls and puts
    assert {c.kind for c in options} == {"CE", "PE"} and all(c.symbol.endswith(c.kind) for c in options)  # a contract's side is read from its symbol
    first = {(c.role, c.strike) for c in options if c.expiry == w.primary and c.role != "OPT"}
    assert first == {("ATM_CE", 24500.0), ("ATM_PE", 24500.0), ("ITM_CE", 24450.0), ("ITM_PE", 24550.0)}  # the roles Phases 1-3 know
    assert all(c.role == "OPT" for c in options if c.expiry != w.primary) and len({c.token for c in w.contracts}) == len(w.contracts)


def test_no_strike_interval_and_no_expiry_date_is_assumed():
    rows = [r for r in master_rows(expiries=("12NOV2026", "19NOV2026", "26NOV2026"), step=100) if r["instrumenttype"] == "AMXIDX" or r["strike"] != "x"]
    c = Chain(rows, NOW)
    assert c.expiries == [date(2026, 11, 12), date(2026, 11, 19), date(2026, 11, 26)]  # whatever the master says
    w = c.window(24540.0, each_side=2, n_expiries=1)
    assert sorted({x.strike for x in w.contracts if x.kind}) == [24300.0, 24400.0, 24500.0, 24600.0, 24700.0] and w.center == 24500.0  # 100-point strikes
    irregular = [r for r in master_rows() if r["instrumenttype"] == "AMXIDX" or float(r["strike"]) / 100 in (24400, 24450, 24500, 24600, 24700, 24900)]
    w = Chain(irregular, NOW).window(24560.0, each_side=1, n_expiries=1)  # strikes are positions in the real list: 24550 is not in it
    assert w.center == 24600.0 and sorted({x.strike for x in w.contracts if x.kind}) == [24500.0, 24600.0, 24700.0]  # 40 points from 24600, 60 from 24500: the neighbours are those in the list


def test_the_expiry_follows_the_trading_date_and_the_close():
    morning = Chain(master_rows(), datetime(2026, 11, 10, 10, 0, tzinfo=IST))  # 10 November is an expiry day
    assert morning.expiries[0] == date(2026, 11, 10)  # still tradable: it is listed and flagged as expiry day, not silently skipped
    after = Chain(master_rows(), datetime(2026, 11, 10, 15, 31, tzinfo=IST))
    assert after.expiries == [date(2026, 11, 17)]  # expired today and the market is over
    assert Chain(master_rows(), datetime(2026, 11, 11, 10, 0, tzinfo=IST)).expiries == [date(2026, 11, 17)]
    assert morning.window(24510.0, 3, 2).contracts[1].expiry == date(2026, 11, 10)  # both are watched on the expiry day, so they can be compared


def test_an_expiry_with_no_strike_on_one_side_of_the_money_is_skipped_for_the_window():
    low_only = [r for r in master_rows() if r["instrumenttype"] == "AMXIDX" or r["expiry"] == "17NOV2026" or float(r["strike"]) / 100 <= 24500]
    w = Chain(low_only, NOW).window(24510.0, each_side=3, n_expiries=1)  # 10 November has nothing above 24500, so 17 November is used
    assert w.primary == date(2026, 11, 17)
    with pytest.raises(ValueError, match="no valid NIFTY expiry"):
        Chain([r for r in master_rows() if r["instrumenttype"] == "AMXIDX" or r["expiry"] == "10NOV2026" and float(r["strike"]) / 100 <= 24500], NOW).window(24510.0)


# -- the strikes follow the spot ---------------------------------------------------------------------------------------------------------
def test_the_window_is_recentred_only_when_the_spot_has_moved_far_enough_and_not_while_it_hovers_between_two_strikes():
    c = chain_of()
    w = c.window(24510.0)  # centred on 24500, the gaps to its neighbours are 50 points
    for spot in (24500.0, 24520.0, 24537.0, 24474.0, 24463.0):
        assert not c.needs_recentre(w, spot, 0.75), spot  # within 0.75 of a step: no change (24525 is already nearer 24550, but the window waits)
    for spot in (24538.0, 24560.0, 24462.0, 24400.0):
        assert c.needs_recentre(w, spot, 0.75), spot
    assert c.needs_recentre(w, 24526.0, 0.5) and not c.needs_recentre(w, 24526.0, 0.75)  # the hysteresis is what is configured


class Boom:
    def __call__(self, *a, **k):
        raise AssertionError("the instrument master was read again")


def engine_with_window(monkeypatch=None, now=ist(10, 0), spot=24510.0):
    clock = Clock(now)
    md = MarketData(CFG, clock)
    chain = chain_of()
    window = chain.window(spot)
    md.set_contracts(window.contracts, date(2026, 11, 6), spot)
    md.set_chain(chain, spot, window.contracts)
    md.set_state("connected")
    return md, clock, window


def snap(token, price, ts, bid=None, ask=None, volume=None, oi=None, closed=None, ltt=None, mode_depth=True):
    """A message as SmartWebSocketV2 parses a snap-quote packet: prices in paise, the best five buy (bids) and sell (asks) levels."""
    m = {"subscription_mode": 3, "exchange_type": 2, "token": token, "last_traded_price": int(round(price * 100)), "exchange_timestamp": int(ts * 1000)}
    if volume is not None:
        m["volume_trade_for_the_day"] = volume
    if oi is not None:
        m["open_interest"] = oi
    if closed is not None:
        m["closed_price"] = int(round(closed * 100))
    if ltt is not None:
        m["last_traded_timestamp"] = int(ltt)
    empty = {"flag": 0, "quantity": 0, "price": 0, "no of orders": 0}
    if mode_depth:
        m["best_5_buy_data"] = ([{"flag": 1, "quantity": bid[1], "price": int(bid[0] * 100), "no of orders": 3}] if bid else []) + [empty] * 4
        m["best_5_sell_data"] = ([{"flag": 0, "quantity": ask[1], "price": int(ask[0] * 100), "no of orders": 2}] if ask else []) + [empty] * 4
    return m


def tick_msg(md, clock, ts, token, price, **kw):
    clock.t = ts
    md.on_message(snap(token, price, ts, **kw))


def test_a_move_of_the_spot_recentres_the_strikes_without_touching_the_spot_and_without_reading_the_master_again(monkeypatch):
    md, clock, _ = engine_with_window()
    monkeypatch.setattr(instruments, "load_master", Boom())  # the chain is in memory: a re-centre is a lookup
    for s in range(0, 40, 10):  # the spot's own ticks and candle
        clock.t = ist(10, 0, s)
        md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int((24510 + s / 10) * 100), "exchange_timestamp": int(clock.t * 1000)})
    spot_candle = dict(md.builder.current[(SPOT_TOKEN, "1m")].__dict__)
    gone, kept = next(c for c in md.contracts if c.strike == 24350.0), next(c for c in md.contracts if c.strike == 24500.0)
    tick_msg(md, clock, ist(10, 0, 39), gone.token, 170.0, volume=1)
    assert gone.token in md.latest
    clock.t = ist(10, 0, 40)
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24536.0 * 100), "exchange_timestamp": int(clock.t * 1000)})
    assert md.maybe_recentre(clock.t) is False  # 36 of 50 points: not yet the 37.5 (0.75 of a step) that moves the window
    clock.t = ist(10, 0, 41)
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24540.0 * 100) + 800, "exchange_timestamp": int(clock.t * 1000)})  # 24548
    assert md.maybe_recentre(clock.t) is True and md.window.center == 24550.0  # now it is centred on the nearest strike
    strikes = sorted({c.strike for c in md.contracts if c.kind})
    assert strikes == [24400.0, 24450.0, 24500.0, 24550.0, 24600.0, 24650.0, 24700.0]  # still seven strikes: one dropped, one added
    assert md.latest[SPOT_TOKEN].ltp == pytest.approx(24548.0) and md.builder.current[(SPOT_TOKEN, "1m")].open == spot_candle["open"]  # the spot's candle and ticks are as they were
    assert gone.token not in md.latest and all(k[0] != gone.token for k in md.builder.current) and kept.token in {c.token for c in md.contracts}
    added, dropped = md.take_subscription_change()
    assert {c.strike for c in added} == {24700.0} and {c.strike for c in dropped} == {24350.0} and len(added) == len(dropped) == 4  # 2 expiries x CE and PE
    assert md.take_subscription_change() is None  # consumed once
    ev = md.window_events[-1]
    assert ev["center"] == 24550.0 and "24550" in ev["reason"] and (ev["added"], ev["dropped"]) == (4, 4)
    new = next(c for c in added if c.kind == "CE")
    tick_msg(md, clock, clock.t + 1, new.token, 25.0, volume=5)  # a contract added by the re-centre is accepted...
    assert new.token in md.latest
    before = md.accepted
    tick_msg(md, clock, clock.t + 2, gone.token, 171.0, volume=2)  # ...and one that was dropped is not
    assert md.accepted == before and md.rejected["unknown_instrument"] == 1
    assert md.maybe_recentre(clock.t) is False  # centred: nothing more to do


def test_the_strikes_do_not_move_on_a_stale_spot_an_untrusted_clock_or_outside_the_session(monkeypatch):
    md, clock, _ = engine_with_window()
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24560.0 * 100), "exchange_timestamp": int(clock.t * 1000)})
    clock.t += 60  # the spot has not ticked for a minute: its last price is not believed
    assert md.maybe_recentre(clock.t) is False and md.window.center == 24500.0
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24560.0 * 100), "exchange_timestamp": int(clock.t * 1000)})
    from app.health import TimeGuard

    md.timeguard = TimeGuard(lambda: clock.t)
    md.timeguard.trusted = False
    assert md.maybe_recentre(clock.t) is False
    md.timeguard.trusted = True
    assert md.maybe_recentre(ist(16, 0)) is False  # the market is closed
    assert md.maybe_recentre(clock.t) is True and md.window.center == 24550.0


class FakeWS:
    def __init__(self, fail=False):
        self.calls, self.fail, self.closed = [], fail, False

    def subscribe(self, cid, mode, groups):
        if self.fail:
            raise RuntimeError("socket is closed")
        self.calls.append(("subscribe", mode, groups))

    def unsubscribe(self, cid, mode, groups):
        self.calls.append(("unsubscribe", mode, groups))

    def close_connection(self):
        self.closed = True


def feed_for(md):
    return Feed(CFG, object(), md, lambda session: None, lambda session: (None, 0.0), lambda s: None)


def move_spot(md, clock, price):
    clock.t += 1
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(price * 100), "exchange_timestamp": int(clock.t * 1000)})
    return md.maybe_recentre(clock.t)


def test_the_feed_applies_a_recentre_on_the_live_connection_in_snap_quote_mode_with_fresh_token_lists():
    md, clock, _ = engine_with_window()
    feed = feed_for(md)
    feed.ws = FakeWS()
    assert move_spot(md, clock, 24560.0)
    feed.watchdog()
    (sub, unsub) = feed.ws.calls
    assert sub[:2] == ("subscribe", 3) and unsub[:2] == ("unsubscribe", 3)  # snap-quote mode: bid, ask, open interest
    assert sub[2][0]["exchangeType"] == 2 and len(sub[2][0]["tokens"]) == 4 and len(unsub[2][0]["tokens"]) == 4
    sub[2][0]["tokens"].append("x")  # the SDK extends the list it is given: what Prism keeps is never that list
    assert len(token_groups(md.contracts)[0]["tokens"]) in (1, 29)
    feed.watchdog()
    assert len(feed.ws.calls) == 2  # nothing is sent twice


def test_a_failed_subscription_change_resets_the_connection_so_the_reconnect_subscribes_the_whole_window():
    md, clock, _ = engine_with_window()
    feed = feed_for(md)
    feed.ws = FakeWS(fail=True)
    assert move_spot(md, clock, 24560.0)
    feed.watchdog()
    assert feed.ws.closed and any("option subscription failed" in e["message"] for e in md.errors)


def test_the_selector_may_hand_back_the_chain_and_the_feed_keeps_it_for_the_day():
    clock = Clock(ist(10, 0))
    md = MarketData(CFG, clock)
    chain = chain_of()
    window = chain.window(24510.0)
    feed = Feed(CFG, type("S", (), {"ensure": lambda self: None})(), md, lambda s: None, lambda s: (window.contracts, 24510.0, chain), lambda s: None)
    feed._prepare()
    assert md.chain is chain and md.window.center == 24500.0 and md.window.primary == date(2026, 11, 10) and len(md.contracts) == 29
    old = Feed(CFG, type("S", (), {"ensure": lambda self: None})(), MarketData(CFG, clock), lambda s: None, lambda s: (window.contracts, 24510.0), lambda s: None)
    old._prepare()  # a selector with no chain (the two-value form) still works: the strikes are then fixed for the day
    assert old.md.chain is None and old.md.maybe_recentre(clock.t) is False


# -- the option quote: what is decoded, what is not invented --------------------------------------------------------------------------------
def test_a_snap_quote_gives_bid_ask_quantities_open_interest_previous_close_and_the_last_trade_time():
    m = snap("111", 151.5, ist(10, 0, 5), bid=(151.0, 600), ask=(151.5, 450), volume=2_000_000, oi=5_000_000, closed=140.25, ltt=ist(10, 0, 4))
    t = decode(m, ist(10, 0, 6))
    assert (t.ltp, t.bid, t.bid_qty, t.ask, t.ask_qty, t.volume, t.oi, t.prev_close) == (151.5, 151.0, 600, 151.5, 450, 2_000_000, 5_000_000, 140.25)
    assert t.last_trade_ts == pytest.approx(ist(10, 0, 4))
    assert decode({**m, "last_traded_timestamp": int(ist(10, 0, 4) * 1000)}, ist(10, 0, 6)).last_trade_ts == pytest.approx(ist(10, 0, 4))  # milliseconds are understood
    assert decode({**m, "last_traded_timestamp": int(ist(10, 30))}, ist(10, 0, 6)).last_trade_ts is None  # a last trade in the future is not believed


def test_the_best_bid_is_the_highest_bid_and_the_best_ask_the_lowest_ask_whatever_the_order_and_empty_levels_are_ignored():
    m = snap("111", 100.0, ist(10, 0, 5), bid=(99.0, 10), ask=(101.0, 20))
    m["best_5_buy_data"] = [{"flag": 1, "quantity": 5, "price": 9850, "no of orders": 1}, {"flag": 1, "quantity": 40, "price": 9900, "no of orders": 2}, {"flag": 1, "quantity": 0, "price": 9950, "no of orders": 0}]
    m["best_5_sell_data"] = [{"flag": 0, "quantity": 7, "price": 10150, "no of orders": 1}, {"flag": 0, "quantity": 9, "price": 10100, "no of orders": 1}, {"flag": 0, "quantity": 3, "price": 0, "no of orders": 0}]
    t = decode(m, ist(10, 0, 6))
    assert (t.bid, t.bid_qty, t.ask, t.ask_qty) == (99.0, 40, 101.0, 9)  # a 99.50 level with no quantity does not count; a zero price does not either


def test_what_the_broker_does_not_provide_is_none_and_never_guessed():
    quote_only = {"subscription_mode": 2, "exchange_type": 2, "token": "111", "last_traded_price": 15150, "exchange_timestamp": int(ist(10, 0, 5) * 1000)}
    t = decode(quote_only, ist(10, 0, 6))
    assert (t.bid, t.ask, t.bid_qty, t.ask_qty, t.oi, t.prev_close, t.last_trade_ts, t.volume) == (None, None, None, None, None, None, None, None)
    empty_book = decode(snap("111", 151.5, ist(10, 0, 5)), ist(10, 0, 6))
    assert empty_book.bid is None and empty_book.ask is None and empty_book.ltp == 151.5  # an unfilled book: no bid or ask, but the last price is real
    junk = decode({**snap("111", 151.5, ist(10, 0, 5), oi=-5), "closed_price": "x", "best_5_buy_data": [{"price": "a", "quantity": 1}, "nonsense", {"price": 100, "quantity": True}]}, ist(10, 0, 6))
    assert junk.oi is None and junk.prev_close is None and junk.bid is None  # negative or malformed values are dropped, not coerced


def test_a_change_of_the_book_alone_is_a_new_quote_and_not_a_duplicate():
    v = TickValidator({"111": 2})
    base = dict(token="111", exch_type=2, ltp=151.5, exch_ts=ist(10, 0, 5), recv_ts=ist(10, 0, 6), volume=100, ltq=75)
    assert v.check(Tick(**base, bid=151.0, ask=151.5, bid_qty=600, ask_qty=450)) is None
    assert v.check(Tick(**base, bid=151.0, ask=151.5, bid_qty=600, ask_qty=450)) == "duplicate"  # the very same quote
    assert v.check(Tick(**base, bid=150.5, ask=151.5, bid_qty=600, ask_qty=450)) is None  # only the bid moved: the price and volume did not
    assert v.check(Tick(**base, bid=150.5, ask=151.5, bid_qty=900, ask_qty=450)) is None  # only a quantity changed


# -- the suitability checks ------------------------------------------------------------------------------------------------------------------------
SPOT = 24510.0


def contract(strike, kind="CE", expiry=date(2026, 11, 10), token=None, lot=75):
    return instruments.Contract("OPT", token or f"{kind}{int(strike)}{expiry.day}", f"NIFTY{expiry.day}NOV26{int(strike)}{kind}", "NFO", expiry, strike, lot)


def quote(c, ltp, bid="auto", ask="auto", bq=600, aq=600, vol=2_000_000, oi=3_000_000, age=1.0, closed=None, ltt=None, now=ist(10, 0, 30)):
    bid = round(ltp - 0.25, 2) if bid == "auto" else bid
    ask = round(ltp + 0.25, 2) if ask == "auto" else ask
    return Tick(c.token, 2, ltp, now - age, now - age, vol, 75, closed, bid, ask, bq if bid is not None else None, aq if ask is not None else None, oi, ltt)


def analyse(pairs, spot=SPOT, spot_age=1.0, now=ist(10, 0, 30), p=P):
    contracts = [c for c, _ in pairs]
    ticks = {c.token: t for c, t in pairs if t is not None}
    return op.analyse(contracts, ticks, spot, spot_age, 20.0, now, p)


def row_of(pairs, c, **kw):
    return next(r for r in analyse(pairs, **kw)["contracts"] if r["token"] == c.token)


def codes(r, severity=None):
    return {f["code"] for f in r["flags"] if severity in (None, f["severity"])}


def test_the_spread_is_in_rupees_and_as_a_percentage_of_the_mid_price():
    c = contract(24500)
    r = row_of([(c, quote(c, 150.0, bid=150.0, ask=150.5))], c)
    assert (r["bid"], r["ask"], r["mid"], r["spread"]) == (150.0, 150.5, 150.25, 0.5)  # (150.0 + 150.5) / 2; 150.5 - 150.0
    assert r["spread_pct"] == pytest.approx(100 * 0.5 / 150.25) and codes(r) <= {"near_expiry"} and not r["blocking"]  # 0.33%: within every limit
    assert r["book"] == "valid" and r["status"] == "fresh" and r["ref_price"] == 150.25


def test_a_wide_spread_is_a_warning_and_a_very_wide_one_rules_the_contract_out():
    c = contract(24500)
    warn = row_of([(c, quote(c, 101.0, bid=100.0, ask=102.0))], c)  # 2 / 101 = 1.98%: over the 1% warning, under the 3% maximum
    assert "spread_high" in codes(warn, "warn") and not warn["blocking"] and warn["spread_pct"] == pytest.approx(200 / 101)
    avoid = row_of([(c, quote(c, 103.0, bid=100.0, ask=106.0))], c)  # 6 / 103 = 5.83%
    assert "wide_spread" in codes(avoid, "avoid") and avoid["blocking"]
    tuned = row_of([(c, quote(c, 103.0, bid=100.0, ask=106.0))], c, p=op.OptionParams(warn_spread_pct=5.0, max_spread_pct=8.0))  # the limits are configurable
    assert "wide_spread" not in codes(tuned) and "spread_high" in codes(tuned)


def test_a_missing_bid_or_ask_or_a_crossed_book_is_never_papered_over_by_the_last_price():
    c = contract(24500)
    none = row_of([(c, quote(c, 150.0, bid=None, ask=None))], c)
    assert none["spread"] is None and none["spread_pct"] is None and none["mid"] is None and none["book"] == "missing" and none["ltp"] == 150.0
    assert "no_bid_ask" in codes(none, "avoid") and none["blocking"]
    one_side = row_of([(c, quote(c, 150.0, bid=149.5, ask=None))], c)
    assert "no_bid_ask" in codes(one_side, "avoid") and one_side["spread"] is None
    crossed = row_of([(c, quote(c, 150.0, bid=151.0, ask=150.0))], c)  # a bid above the ask: invalid, or the SDK's two sides are reversed
    assert crossed["book"] == "crossed" and "crossed_book" in codes(crossed, "avoid") and crossed["spread"] is None and crossed["mid"] is None


def test_a_stale_quote_is_never_shown_as_live_and_each_contract_has_its_own_age():
    a, b = contract(24500), contract(24550)
    r = analyse([(a, quote(a, 150.0, age=5.0)), (b, quote(b, 120.0, age=45.0))])["contracts"]
    fresh, stale = r[0], r[1]
    assert (fresh["status"], fresh["age"]) == ("fresh", pytest.approx(5.0)) and not fresh["blocking"]
    assert stale["status"] == "stale" and stale["age"] == pytest.approx(45.0) and "stale_quote" in codes(stale, "avoid") and stale["blocking"]
    assert "30 s" in next(f["text"] for f in stale["flags"] if f["code"] == "stale_quote")
    c = contract(24600)
    absent = row_of([(c, None)], c)
    assert absent["status"] == "no data" and absent["ltp"] is None and absent["age"] is None and "no_quote" in codes(absent, "avoid")


def test_the_spot_and_the_market_must_be_live_for_any_contract_to_be_usable():
    c = contract(24500)
    pairs = [(c, quote(c, 150.0))]
    assert "spot_stale" in codes(row_of(pairs, c, spot_age=60.0), "avoid") and "spot_stale" in codes(row_of(pairs, c, spot_age=None), "avoid")
    closed = row_of([(c, quote(c, 150.0, now=ist(16, 0)))], c, now=ist(16, 0))
    assert closed["status"] == "market closed" and "market_closed" in codes(closed, "avoid")


def test_liquidity_missing_or_thin_is_flagged_and_an_ltp_alone_is_not_liquidity():
    c = contract(24500)
    nothing = row_of([(c, quote(c, 150.0, bid=None, ask=None, vol=None, oi=None))], c)
    assert "no_liquidity_info" in codes(nothing, "avoid")  # nothing at all tells how liquid it is
    no_volume = row_of([(c, quote(c, 150.0, vol=None))], c)
    assert "no_volume" in codes(no_volume, "warn") and "low_volume" not in codes(no_volume) and not no_volume["blocking"]  # the open interest and quantities are there
    no_oi = row_of([(c, quote(c, 150.0, oi=None))], c)
    assert "no_oi" in codes(no_oi, "warn") and not no_oi["blocking"]
    low = row_of([(c, quote(c, 150.0, vol=99_999, oi=99_999))], c)
    assert "low_volume" in codes(low, "avoid") and "low_oi" in codes(low, "warn") and low["blocking"]  # the minimums are 100,000
    thin = row_of([(c, quote(c, 150.0, bq=20, aq=900))], c)
    assert "thin_top_quantity" in codes(thin, "warn") and "20" in next(f["text"] for f in thin["flags"] if f["code"] == "thin_top_quantity")  # under one lot of 75
    off = op.OptionParams(min_volume=0, min_open_interest=0, min_top_quantity_lots=0, min_relative_volume=0)  # every minimum can be switched off
    assert codes(row_of([(c, quote(c, 150.0, vol=1, oi=1, bq=1, aq=1))], c, p=off)) <= {"near_expiry"}


def test_a_contract_that_trades_far_less_than_its_neighbours_is_flagged_relative_to_them():
    a, b = contract(24500), contract(24550)
    r = analyse([(a, quote(a, 150.0, vol=5_000_000)), (b, quote(b, 120.0, vol=400_000))])["contracts"]
    assert "thin_relative_volume" not in codes(r[0]) and "thin_relative_volume" in codes(r[1], "warn")  # 400,000 is 8% of 5,000,000
    assert "8%" in next(f["text"] for f in r[1]["flags"] if f["code"] == "thin_relative_volume")
    ce, pe = contract(24500), contract(24500, "PE")
    mixed = analyse([(ce, quote(ce, 150.0, vol=5_000_000)), (pe, quote(pe, 140.0, vol=400_000))])["contracts"]
    assert all("thin_relative_volume" not in codes(x) for x in mixed)  # calls are compared with calls, puts with puts


def test_a_price_below_the_intrinsic_value_or_out_of_order_with_its_neighbours_is_called_inconsistent():
    deep = contract(24400)  # intrinsic value 110 with the spot at 24510
    below = row_of([(deep, quote(deep, 90.0, bid=89.75, ask=90.25))], deep)
    assert below["intrinsic"] == 110.0 and "below_intrinsic" in codes(below, "avoid")
    near = row_of([(deep, quote(deep, 109.5, bid=109.25, ask=109.75))], deep)  # half a point under: inside the 1.0 tolerance
    assert "below_intrinsic" not in codes(near)
    a, b = contract(24450), contract(24500)
    r = analyse([(a, quote(a, 60.0)), (b, quote(b, 80.0))])["contracts"]  # the call at the lower strike is cheaper than the one above it
    assert all("premium_inconsistent" in codes(x, "warn") for x in r)
    pa, pb = contract(24450, "PE"), contract(24500, "PE")
    ok = analyse([(pa, quote(pa, 60.0)), (pb, quote(pb, 80.0))])["contracts"]  # puts get dearer with the strike: this order is right
    assert all("premium_inconsistent" not in codes(x) for x in ok)
    bad_put = analyse([(pa, quote(pa, 90.0)), (pb, quote(pb, 80.0))])["contracts"]
    assert all("premium_inconsistent" in codes(x) for x in bad_put)


def test_the_strike_is_classified_against_the_live_spot_and_far_strikes_are_flagged():
    strikes = (24350, 24400, 24450, 24500, 24550, 24600, 24650)
    ce = [contract(k) for k in strikes]
    pe = [contract(k, "PE") for k in strikes]
    rows = {(r["kind"], r["strike"]): r for r in analyse([(c, quote(c, 100.0 + i)) for i, c in enumerate(ce + pe)])["contracts"]}
    assert [rows[("CE", k)]["moneyness"] for k in strikes] == ["ITM", "ITM", "ITM", "ATM", "OTM", "OTM", "OTM"]
    assert [rows[("PE", k)]["moneyness"] for k in strikes] == ["OTM", "OTM", "OTM", "ATM", "ITM", "ITM", "ITM"]
    assert rows[("CE", 24500)]["steps_from_atm"] == 0 and rows[("CE", 24550)]["steps_from_atm"] == 1 and rows[("CE", 24350)]["steps_from_atm"] == 3
    assert rows[("CE", 24550)]["distance_points"] == pytest.approx(40.0) and rows[("CE", 24450)]["distance_points"] == pytest.approx(-60.0)
    assert "far_from_atm" in codes(rows[("CE", 24350)], "warn") and "far_from_atm" in codes(rows[("CE", 24650)], "warn")  # 3 and 3 strikes: over the 2 allowed
    assert "far_from_atm" not in codes(rows[("CE", 24550)])
    assert "gain" in next(f["text"] for f in rows[("CE", 24650)]["flags"] if f["code"] == "far_from_atm")  # a far OTM call says what that means


def test_expiry_day_and_the_days_to_expiry_are_shown_with_the_decay_warning():
    today, tomorrow, later = date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 10)
    cs = [contract(24500, expiry=e) for e in (today, tomorrow, later)]
    a = analyse([(c, quote(c, 150.0)) for c in cs])
    rows = {r["expiry"]: r for r in a["contracts"]}
    assert [rows[e.isoformat()]["days_to_expiry"] for e in (today, tomorrow, later)] == [0, 1, 4]
    assert rows["2026-11-06"]["expiry_day"] and "expiry_day" in codes(rows["2026-11-06"], "warn") and "faster" not in "" and "fastest" in next(f["text"] for f in rows["2026-11-06"]["flags"] if f["code"] == "expiry_day")
    assert "near_expiry" in codes(rows["2026-11-07"], "warn") and not rows["2026-11-07"]["expiry_day"]
    assert not (codes(rows["2026-11-10"]) & {"expiry_day", "near_expiry"})
    assert [(e["expiry"], e["days_to_expiry"], e["expiry_day"], e["decay_warning"]) for e in a["expiries"]] == [
        ("2026-11-06", 0, True, True), ("2026-11-07", 1, False, True), ("2026-11-10", 4, False, False)]  # fmt: skip
    assert not any(c["blocking"] for c in a["contracts"])  # an expiry warning is a warning: it does not rule the contract out by itself


def test_the_option_thresholds_come_from_the_environment_and_bad_values_are_refused():
    p = op.OptionParams.from_env(env={"OPTIONS_MAX_SPREAD_PCT": "4.5", "OPTIONS_MIN_VOLUME": "5000", "OPTIONS_STRIKES_EACH_SIDE": "2"})
    assert (p.max_spread_pct, p.min_volume, p.strikes_each_side) == (4.5, 5000, 2) and p.warn_spread_pct == 1.0
    for bad in ({"OPTIONS_MAX_SPREAD_PCT": "wide"}, {"OPTIONS_STRIKES_EACH_SIDE": "0"}, {"OPTIONS_RECENTRE_HYSTERESIS": "0.2"}, {"OPTIONS_MIN_VOLUME": "-1"},
                {"OPTIONS_WARN_SPREAD_PCT": "9", "OPTIONS_MAX_SPREAD_PCT": "3"}):  # fmt: skip
        with pytest.raises(ValueError):
            op.OptionParams.from_env(env=bad)
    md = MarketData(CFG, Clock(ist(10, 0)))
    assert md.option_params == op.OptionParams()


# -- the selection: only for a BUY decision, only from the right side, in a documented order ----------------------------------------------------------
def chain_rows():
    """CE and PE quotes for three strikes in two expiries, all healthy unless a test changes one."""
    out = []
    for e in (date(2026, 11, 10), date(2026, 11, 17)):
        for k, ce, pe in ((24450, 95.0, 45.0), (24500, 62.0, 62.0), (24550, 35.0, 90.0)):
            for kind, price in (("CE", ce), ("PE", pe)):
                c = contract(k, kind, e)
                out.append((c, quote(c, price, vol=3_000_000, oi=4_000_000)))
    return out


def test_no_contract_is_selected_unless_phase_3_says_buy_ce_or_buy_pe():
    rows = analyse(chain_rows())["contracts"]
    for action in ("WAIT", "NO TRADE", None, "BUY"):
        s = op.select(rows, action)
        assert s["actionable"] is False and s["contract"] is None and s["alternatives"] == [] and s["why"] == []
        assert "no options selection" in s["notice"] and "information only" in s["notice"]
    s = op.select(rows, "WAIT")
    assert "Phase 3 says WAIT" in s["notice"] and any(r["kind"] == "CE" and not r["blocking"] for r in rows)  # good contracts exist: they are still not offered


def test_buy_ce_considers_only_calls_and_buy_pe_only_puts():
    rows = analyse(chain_rows())["contracts"]
    ce, pe = op.select(rows, "BUY CE"), op.select(rows, "BUY PE")
    assert ce["actionable"] and ce["contract"]["kind"] == "CE" and all(a["kind"] == "CE" for a in ce["alternatives"])
    assert pe["actionable"] and pe["contract"]["kind"] == "PE" and all(a["kind"] == "PE" for a in pe["alternatives"])
    assert "does not place orders" in ce["notice"] and "not a score" in ce["why"][-1] and "profit" in ce["why"][-1]


def test_the_order_is_fewest_warnings_then_the_tightest_spread_then_the_nearest_the_money():
    a, b, d = contract(24500), contract(24550), contract(24450)
    wide_but_clean = (b, quote(b, 100.0, bid=99.6, ask=100.4))  # 0.8% spread, no warning
    tight_but_warned = (a, quote(a, 150.0, bid=149.95, ask=150.05, oi=50_000))  # 0.07% spread, but a low-OI warning
    rows = analyse([wide_but_clean, tight_but_warned])["contracts"]
    assert [r["warnings"] for r in rows if r["strike"] == 24550] == [0] and [r["warnings"] for r in rows if r["strike"] == 24500] == [1]
    assert op.select(rows, "BUY CE")["contract"]["strike"] == 24550.0  # fewer warnings beats a tighter spread
    tie = analyse([(a, quote(a, 150.0, bid=149.9, ask=150.1)), (b, quote(b, 100.0, bid=99.95, ask=100.05))])["contracts"]  # both clean: the tighter spread wins
    assert op.select(tie, "BUY CE")["contract"]["strike"] == 24550.0
    same = analyse([(a, quote(a, 150.0, bid=149.9, ask=150.1)), (d, quote(d, 190.0, bid=189.87, ask=190.07))])["contracts"]  # identical spread % to the paisa: ATM wins
    s = op.select(same, "BUY CE")
    assert s["alternatives"][0]["strike"] in (24450.0, 24500.0) and s["contract"]["strike"] in (24450.0, 24500.0)
    ranked = sorted([r for r in same], key=op.order_key)
    assert ranked[0]["steps_from_atm"] <= ranked[1]["steps_from_atm"] or ranked[0]["spread_pct"] < ranked[1]["spread_pct"]


def test_an_expiry_day_contract_does_not_win_just_because_it_is_the_nearest_expiry():
    today, later = date(2026, 11, 6), date(2026, 11, 10)
    a, b = contract(24500, expiry=today), contract(24500, expiry=later)
    rows = analyse([(a, quote(a, 80.0, bid=79.95, ask=80.05)), (b, quote(b, 150.0, bid=149.9, ask=150.1))])["contracts"]
    near = next(r for r in rows if r["expiry"] == "2026-11-06")
    far = next(r for r in rows if r["expiry"] == "2026-11-10")
    assert near["spread_pct"] < far["spread_pct"] and near["warnings"] == 1 and far["warnings"] == 0  # the nearer expiry has the tighter spread and the expiry-day warning
    s = op.select(rows, "BUY CE")
    assert s["contract"]["expiry"] == "2026-11-10" and s["alternatives"][0]["expiry"] == "2026-11-06"


def test_a_contract_with_a_blocking_problem_is_never_selected_and_when_none_is_fit_the_reasons_are_given():
    good, stale, nobook = contract(24500), contract(24550), contract(24450)
    rows = analyse([(good, quote(good, 150.0)), (stale, quote(stale, 100.0, age=60.0)), (nobook, quote(nobook, 190.0, bid=None, ask=None))])["contracts"]
    assert op.select(rows, "BUY CE")["contract"]["strike"] == 24500.0 and op.select(rows, "BUY CE")["alternatives"] == []
    bad = analyse([(stale, quote(stale, 100.0, age=60.0)), (nobook, quote(nobook, 190.0, bid=None, ask=None))])["contracts"]
    s = op.select(bad, "BUY CE")
    assert s["actionable"] is False and s["contract"] is None and "no suitable CE contract" in s["notice"]
    reasons = {r["strike"]: r["reasons"] for r in s["rejected"]}
    assert any("60 s old" in x for x in reasons[24550.0]) and any("bid or the ask is not provided" in x for x in reasons[24450.0])
    none_for_side = op.select(analyse([(good, quote(good, 150.0))])["contracts"], "BUY PE")
    assert none_for_side["contract"] is None and "no PE contract is being watched" in none_for_side["notice"]
    stale_spot = op.select(analyse([(good, quote(good, 150.0))], spot_age=90.0)["contracts"], "BUY CE")
    assert stale_spot["contract"] is None and any("NIFTY spot is stale" in x for r in stale_spot["rejected"] for x in r["reasons"])  # nothing is offered on a stale spot


# -- the engine: Phase 3's decision gates the selection, the quotes come from the feed ---------------------------------------------------------------
def live_engine():
    md, clock, _ = engine_with_window()
    ts = ist(10, 0, 20)
    clock.t = ts
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24510.0 * 100), "exchange_timestamp": int(ts * 1000)})
    price = {"CE": {24450: 95.0, 24500: 62.0, 24550: 35.0}, "PE": {24450: 40.0, 24500: 60.0, 24550: 88.0}}
    for c in md.contracts:
        if c.kind and c.strike in price[c.kind]:
            p = price[c.kind][c.strike]
            tick_msg(md, clock, ts, c.token, p, bid=(round(p - 0.2, 2), 600), ask=(round(p + 0.2, 2), 600), volume=3_000_000, oi=4_000_000, closed=p - 1)
    return md, clock


def decision(action, headline=None):
    return {"decision": {"action": action, "headline": headline or action, "strategy_name": "Trend Pullback" if action.startswith("BUY") else None, "setup_id": "x"}}


def test_the_options_view_offers_a_contract_only_for_buy_and_always_shows_the_table():
    md, _ = live_engine()
    now = ist(10, 0, 22)
    wait = md.options_view(decision("WAIT"), now)
    assert wait["available"] and wait["spot"] == 24510.0 and wait["spot_fresh"] and wait["selection"]["contract"] is None and wait["selection"]["actionable"] is False
    assert wait["decision"]["action"] == "WAIT" and any(r["status"] == "fresh" for r in wait["contracts"])  # the table is there to compare, but nothing is selected
    buy = md.options_view(decision("BUY CE"), now)
    c = buy["selection"]["contract"]
    # the three calls are all clean; the in-the-money 24450 call has the tightest spread (0.4 on 95 = 0.42% against 0.4 on 62 = 0.64%)
    assert buy["selection"]["actionable"] and c["kind"] == "CE" and (c["bid"], c["ask"], c["bid_qty"], c["volume"], c["oi"], c["prev_close"]) == (94.8, 95.2, 600, 3_000_000, 4_000_000, 94.0)
    assert c["moneyness"] == "ITM" and c["strike"] == 24450.0 and c["status"] == "fresh" and buy["window"]["center"] == 24500.0 and buy["window"]["contracts"] == 28
    assert (c["expiry"], [(a["expiry"], a["strike"]) for a in buy["selection"]["alternatives"]]) == ("2026-11-10", [("2026-11-17", 24450.0), ("2026-11-10", 24500.0)])  # the same strike a week later, then the ATM call
    assert md.options_view(decision("NO TRADE"), now)["selection"]["contract"] is None and md.options_view(None, now)["selection"]["contract"] is None
    assert {x["expiry"] for x in buy["expiries"]} == {"2026-11-10", "2026-11-17"} and buy["expiries"][0]["days_to_expiry"] == 4


def test_an_option_that_goes_quiet_is_stale_on_its_own_while_the_others_stay_live():
    md, clock = live_engine()
    atm = next(c for c in md.contracts if c.role == "ATM_CE")
    clock.t = ist(10, 1, 0)
    for c in md.contracts:  # every contract but the ATM call keeps ticking
        if c.kind and c.token in md.latest and c.token != atm.token:
            tick_msg(md, clock, clock.t, c.token, md.latest[c.token].ltp + 0.05, bid=(md.latest[c.token].bid, 600), ask=(md.latest[c.token].ask, 600), volume=3_000_001, oi=4_000_000)
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24510.0 * 100), "exchange_timestamp": int(clock.t * 1000)})
    v = md.options_view(decision("BUY CE"), clock.t + 1)
    rows = {r["token"]: r for r in v["contracts"]}
    assert rows[atm.token]["status"] == "stale" and rows[atm.token]["age"] > 30 and rows[atm.token]["blocking"]
    assert v["selection"]["contract"]["token"] != atm.token  # the stale ATM call is not offered; a live neighbour is
    assert all(r["status"] == "fresh" for t, r in rows.items() if t != atm.token and r["ltp"] is not None)
    status = {i["token"]: i for i in md.instrument_status(clock.t + 1)}
    assert status[atm.token]["status"] == "stale"  # the health panel agrees


def test_a_stale_spot_leaves_nothing_to_select():
    md, _ = live_engine()
    v = md.options_view(decision("BUY CE"), ist(10, 1, 30))  # the spot last ticked at 10:00:20: 70 s ago; the options too
    assert v["spot_fresh"] is False and v["selection"]["contract"] is None and v["selection"]["actionable"] is False


def test_the_snapshot_and_the_api_carry_the_options_and_stay_json_and_the_dashboard_candles_stay_light():
    md, _ = live_engine()
    snap_ = md.snapshot()
    assert snap_["options"]["available"] and len(snap_["options"]["contracts"]) == 28
    json.dumps(snap_)
    roles_with_candles = {i["role"] for i in snap_["instruments"] if i["candles"]}
    assert roles_with_candles == {"SPOT", "ATM_CE", "ATM_PE", "ITM_CE", "ITM_PE"}  # the rest of the window is analysed, not charted
    from app.main import create_app

    app = create_app(md=md, feed=None)
    app.state.md = md  # what the application's startup does
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/options")
    out = route.endpoint()
    assert out["available"] and out["selection"]["notice"].startswith("no options selection")  # no BUY: nothing offered
    assert md.safe_options.__name__ == "safe_options"
    empty = MarketData(CFG, Clock(ist(10, 0))).options_view(None, ist(10, 0))
    assert empty == {"available": False, "reason": "no option contracts are selected yet"}


def test_one_failing_analysis_cannot_disturb_the_snapshot(monkeypatch):
    md, _ = live_engine()
    monkeypatch.setattr(op, "analyse", lambda *a, **k: 1 / 0)
    s = md.snapshot()
    assert s["options"]["available"] is False and "options analysis failed" in s["options"]["reason"] and s["trading"] is not None


def test_the_journal_records_the_selected_contract_for_a_buy_and_nothing_for_a_wait(bull, tmp_path):  # noqa: F811
    md, clock = bull.engine()
    md.set_state("connected")
    md.journal = Journal(tmp_path)
    bull.step(md, clock, 18)  # a WAIT
    buy = bull.step(md, clock, 19)  # the BUY of the scenario, before any option is watched
    price, now = buy["price"], clock.t
    chain = chain_of()
    window = chain.window(price)
    md.set_contracts(window.contracts, date(2026, 11, 6), price)  # now the option window is watched, around the scenario's own price
    md.set_chain(chain, price, window.contracts)
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(round(price * 100)), "exchange_timestamp": int((now - 1) * 1000)})
    for c in md.contracts:
        if c.kind == "CE" and c.expiry == window.primary and abs(c.strike - window.center) <= 50:
            tick_msg(md, clock, now - 1, c.token, 62.0, bid=(61.8, 600), ask=(62.2, 600), volume=3_000_000, oi=4_000_000)
    clock.t = now
    out = md.trading()
    assert out["decision"]["action"] == "BUY CE"
    records, _ = read(next(tmp_path.glob("decisions-*.jsonl")))
    with_options = records[-1]
    assert with_options["decision"]["action"] == "BUY CE" and with_options["options"]["kind"] == "CE" and with_options["options"]["symbol"] and with_options["options"]["bid"] == 61.8
    assert with_options["options"]["flags"] is not None and with_options["options"]["why"]  # the contract and why it was offered
    assert "options" not in records[0] and "options" not in records[-2]  # neither the WAIT nor the BUY from before an option was watched has one
    assert len(json.dumps(with_options["options"])) < 1500  # a brief, not the table


# -- nothing here can place an order ---------------------------------------------------------------------------------------------------------------
def test_no_order_placement_exists_in_the_application():
    forbidden = re.compile(r"placeOrder|modifyOrder|cancelOrder|place_order|modify_order|cancel_order|gttCreate|createRule|/order/|orderbook|tradebook|SmartWebSocketOrderUpdate", re.I)
    for path in sorted((ROOT / "app").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"{path.name} mentions an order call"
    broker_calls = set()
    for path in sorted((ROOT / "app").glob("*.py")):
        broker_calls |= set(re.findall(r"""\.call\(\s*["'](\w+)["']""", path.read_text(encoding="utf-8")))
    assert broker_calls == {"ltpData", "getCandleData"}  # the only things Prism ever asks Angel One for through its session: a price and candles
    from app.main import create_app

    app = create_app(md=MarketData(CFG, Clock(ist(10, 0))), feed=None)
    methods = {m for r in app.routes for m in (getattr(r, "methods", None) or set())}
    assert methods <= {"GET", "HEAD"}  # no route accepts a POST, PUT, PATCH or DELETE: nothing can be sent to the application, let alone to a broker


def test_analysing_and_selecting_never_touches_the_broker_session():
    class Session:
        def __getattr__(self, name):
            raise AssertionError(f"the session was used: {name}")

    md, clock = live_engine()
    md.options_view(decision("BUY CE"), ist(10, 0, 22))
    md.snapshot()
    feed = Feed(CFG, Session(), md, lambda s: None, lambda s: None, lambda s: None)
    feed.ws = FakeWS()
    move_spot(md, clock, 24560.0)
    feed.watchdog()  # a re-centre subscribes and unsubscribes market data: it is not an order
    assert {c[0] for c in feed.ws.calls} == {"subscribe", "unsubscribe"}


# -- pinned to what the REAL feed sent: a snap-quote captured from Angel One on 2026-11-06 (after the close), checked against its read-only quote API ------------
REAL_NIFTY09NOV2625050CE = {  # the SDK's parsed dict, as received (token 40039, lot 65)
    "subscription_mode": 3, "exchange_type": 2, "token": "40039", "sequence_number": 63242438, "exchange_timestamp": 1793959801000, "last_traded_price": 14170,
    "subscription_mode_val": "SNAP_QUOTE", "last_traded_quantity": 65, "average_traded_price": 6706, "volume_trade_for_the_day": 132858700, "total_buy_quantity": 137345.0,
    "total_sell_quantity": 96265.0, "open_price_of_the_day": 2000, "high_price_of_the_day": 15880, "low_price_of_the_day": 1175, "closed_price": 3015, "last_traded_timestamp": 1793959799,
    "open_interest": 2225470, "open_interest_change_percentage": 4612908190886089993, "upper_circuit_limit": 22775, "lower_circuit_limit": 5, "52_week_high_price": 15880, "52_week_low_price": 0,
    "best_5_buy_data": [{"flag": 1, "quantity": 845, "price": 14000, "no of orders": 1}, {"flag": 1, "quantity": 910, "price": 13930, "no of orders": 1},
                        {"flag": 1, "quantity": 910, "price": 13925, "no of orders": 1}, {"flag": 1, "quantity": 650, "price": 13920, "no of orders": 2},
                        {"flag": 1, "quantity": 390, "price": 13910, "no of orders": 2}],
    "best_5_sell_data": [{"flag": 0, "quantity": 520, "price": 14155, "no of orders": 1}, {"flag": 0, "quantity": 130, "price": 14165, "no of orders": 1},
                         {"flag": 0, "quantity": 455, "price": 14170, "no of orders": 1}, {"flag": 0, "quantity": 195, "price": 14180, "no of orders": 1},
                         {"flag": 0, "quantity": 65, "price": 14190, "no of orders": 1}],
}  # fmt: skip


def test_a_real_captured_snap_quote_decodes_to_what_the_brokers_own_quote_api_reported_for_it():
    t = decode(REAL_NIFTY09NOV2625050CE, 1793980000.0)  # received that evening
    # the broker's read-only quote for the same token: ltp 141.7, depth.buy[0] 140.0 x 845, depth.sell[0] 141.55 x 520, tradeVolume 132858700,
    # opnInterest 2225470, close 30.15, exchFeedTime 06-Nov-2026 15:40:01, exchTradeTime 06-Nov-2026 15:39:59
    assert (t.ltp, t.bid, t.bid_qty, t.ask, t.ask_qty) == (141.7, 140.0, 845, 141.55, 520)
    assert (t.volume, t.oi, t.prev_close, t.ltq) == (132858700, 2225470, 30.15, 65)
    assert t.exch_ts == pytest.approx(datetime(2026, 11, 6, 15, 40, 1, tzinfo=IST).timestamp())  # the feed time, in milliseconds
    assert t.last_trade_ts == pytest.approx(datetime(2026, 11, 6, 15, 39, 59, tzinfo=IST).timestamp())  # the last trade, in SECONDS: two seconds earlier
    assert t.bid < t.ask  # the SDK's best_5_buy_data really are the bids (raw flag 1) and best_5_sell_data the asks (raw flag 0)
    assert {lv["flag"] for lv in REAL_NIFTY09NOV2625050CE["best_5_buy_data"]} == {1} and {lv["flag"] for lv in REAL_NIFTY09NOV2625050CE["best_5_sell_data"]} == {0}
    lot = 65  # the real master's lot size for NIFTY options: every quantity the feed reports is a whole number of lots, i.e. the unit is the contract unit, not the lot
    assert all(x % lot == 0 for x in (t.volume, t.oi, t.bid_qty, t.ask_qty, t.ltq))
    assert not hasattr(t, "open_interest_change_percentage")  # the SDK reads that field as an integer (it is a double): Prism does not use it


def test_the_real_close_book_goes_through_the_checks_with_the_numbers_the_flags_quote():
    c = instruments.Contract("ATM_CE", "40039", "NIFTY09NOV2625050CE", "NFO", date(2026, 11, 9), 25050.0, 65)
    t = decode(REAL_NIFTY09NOV2625050CE, ist(10, 0, 30))
    t = Tick(**{**t.__dict__, "exch_ts": ist(10, 0, 29), "recv_ts": ist(10, 0, 29), "last_trade_ts": None})  # received "live" for the check; only the quote itself is real
    r = row_of([(c, t)], c, spot=25030.35)
    assert r["spread"] == pytest.approx(1.55) and r["spread_pct"] == pytest.approx(100 * 1.55 / 140.775)  # 141.55 - 140.00 on a mid of 140.775
    assert "spread_high" in codes(r, "warn") and not r["blocking"] and r["moneyness"] == "ATM" and r["days_to_expiry"] == 3


def test_volume_and_open_interest_are_labelled_as_units_everywhere_they_are_shown():
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for label in ("Volume (units)", "Open interest (units)", "Day volume (units)", "1 lot = ", "Bid × units"):
        assert label in html, label
    c = contract(24500)
    low = row_of([(c, quote(c, 150.0, vol=99_999, oi=99_999))], c)
    text = " ".join(f["text"] for f in low["flags"])
    assert "99,999 units" in text and "100,000 units" in text and not re.search(r"volume 99,999 (is|and)", text)  # the unit is in the sentence, not only in a column header
    thin = row_of([(c, quote(c, 150.0, bq=20, aq=900))], c)
    assert "20 units" in next(f["text"] for f in thin["flags"] if f["code"] == "thin_top_quantity") and "75 units" in next(f["text"] for f in thin["flags"] if f["code"] == "thin_top_quantity")


def test_the_dashboard_script_is_valid_javascript(tmp_path):
    """The unit tests cannot see JavaScript, and one syntax error (a quote inside a quoted string) blanks the whole dashboard: so the page's script is
    syntax-checked with node when it is installed. (This exact mistake was made once, in a label edit, and found only in a browser.)"""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the dashboard script was not syntax-checked")
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    path = tmp_path / "dashboard.js"
    path.write_text(scripts[0], encoding="utf-8")
    done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[:400]
