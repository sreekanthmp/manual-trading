"""Exchange-time quote freshness: a quote that Prism received just now must never be mistaken for fresh exchange data.

Three clocks are kept apart for every option contract: the RECEIPT age (when Prism got the message), the EXCHANGE-TIME age (the exchange's own time for the
quote: Angel One's exchange_timestamp, which its quote API calls exchFeedTime) and the LAST-TRADE age (shown only: a last trade says nothing about the order
book). Mocked data only: no broker session is needed."""

import json

import pytest
from helpers import ist
from test_options import REAL_NIFTY09NOV2625050CE, P, analyse, codes, contract, decision, engine_with_window, row_of, snap

from app import options as op
from app.engine import MarketData
from app.ticks import Tick, TickValidator, decode

NOW = ist(10, 0, 30)


def tick(c, ltp=150.0, exch=NOW - 2, recv=NOW - 1, ltt=None, note=None, bid=None, ask=None, bq=600, aq=600):
    """A quote with its three clocks set by hand. bid / ask default to a 0.25 spread around the price."""
    bid = round(ltp - 0.125, 3) if bid is None else bid
    ask = round(ltp + 0.125, 3) if ask is None else ask
    return Tick(token=c.token, exch_type=2, ltp=ltp, exch_ts=exch, recv_ts=recv, volume=2_000_000, ltq=75, prev_close=None, bid=bid, ask=ask, bid_qty=bq, ask_qty=aq,
                oi=3_000_000, last_trade_ts=ltt, exch_note=note)  # fmt: skip


def row(c, t, **kw):
    return row_of([(c, t)], c, now=NOW, **kw)


# -- 1. received just now, exchange time recent -------------------------------------------------------------------------------------------------
def test_a_message_received_now_with_a_recent_exchange_time_is_fresh_on_both_clocks():
    c = contract(24500)
    r = row(c, tick(c, exch=NOW - 2, recv=NOW - 1))
    assert (r["receipt_age"], r["exchange_age"]) == (pytest.approx(1.0), pytest.approx(2.0)) and r["age"] == r["receipt_age"]  # `age` is still the receipt age
    assert (r["status"], r["exchange_status"], r["freshness"]) == ("fresh", "fresh", "fresh") and r["exchange_reason"] is None
    assert not (codes(r) & {"exchange_time_stale", "exchange_time_unknown", "stale_quote"}) and not r["blocking"]


# -- 2. received just now, exchange time OLD: the primary case -------------------------------------------------------------------------------------
@pytest.mark.parametrize("exchange_age,words", [(300.0, "5 min"), (6 * 3600.0, "6.0 h")])
def test_a_message_received_now_that_carries_an_old_exchange_time_is_stale_and_cannot_be_selected(exchange_age, words):
    old, fresh = contract(24500), contract(24550)
    rows = analyse([(old, tick(old, exch=NOW - exchange_age, recv=NOW - 1, bid=149.9, ask=150.1)), (fresh, tick(fresh, 120.0, bid=119.6, ask=120.4))], now=NOW)["contracts"]
    a = next(r for r in rows if r["strike"] == 24500.0)
    assert a["status"] == "fresh" and a["receipt_age"] == pytest.approx(1.0)  # received a second ago: by receipt age alone this quote looks live
    assert a["exchange_status"] == "stale" and a["freshness"] == "stale" and a["exchange_age"] == pytest.approx(exchange_age)
    text = next(f["text"] for f in a["flags"] if f["code"] == "exchange_time_stale")
    assert words in text and "received 1 s ago" in text and "Recent receipt is not fresh data" in text and a["blocking"]
    assert "exchange's last update" in a["exchange_reason"] and "30 s" in a["exchange_reason"]
    s = op.select(rows, "BUY CE")
    assert s["contract"]["strike"] == 24550.0 and all(x["strike"] != 24500.0 for x in s["alternatives"])  # the stale-exchange contract is not offered, even though it has the tighter spread
    alone = op.select(analyse([(old, tick(old, exch=NOW - exchange_age, recv=NOW - 1))], now=NOW)["contracts"], "BUY CE")
    assert alone["contract"] is None and "no suitable CE contract" in alone["notice"] and any("exchange-time age" in x for r in alone["rejected"] for x in r["reasons"])


def test_the_snapshot_sent_when_a_contract_is_subscribed_is_judged_by_the_exchange_time_not_by_when_it_arrived():
    md, clock, _ = engine_with_window()
    now = ist(10, 5, 0)
    clock.t = now
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": md.spot_token, "last_traded_price": int(24510.0 * 100), "exchange_timestamp": int(now * 1000)})
    quiet = next(c for c in md.contracts if c.role == "ATM_CE")
    live = next(c for c in md.contracts if c.role == "ITM_CE")
    md.on_message(snap(quiet.token, 62.0, now - 3 * 3600, bid=(61.8, 600), ask=(62.2, 600), volume=3_000_000, oi=4_000_000))  # last changed three hours ago, arrives now
    md.on_message(snap(live.token, 95.0, now - 1, bid=(94.8, 600), ask=(95.2, 600), volume=3_000_000, oi=4_000_000))
    v = md.options_view(decision("BUY CE"), now + 1)
    rows = {r["token"]: r for r in v["contracts"]}
    q = rows[quiet.token]
    assert q["receipt_age"] == pytest.approx(1.0) and q["status"] == "fresh" and q["exchange_age"] == pytest.approx(3 * 3600 + 1) and q["freshness"] == "stale" and q["blocking"]
    assert rows[live.token]["freshness"] == "fresh" and v["selection"]["contract"]["token"] == live.token  # the live contract is offered, the old snapshot is not
    snap_ = md.snapshot()["options"]["contracts"]  # the new fields travel in the snapshot, hence over /api/state and the WebSocket
    assert {"receipt_age", "exchange_age", "exchange_status", "exchange_reason", "last_trade_age", "freshness"} <= set(snap_[0])
    json.dumps(md.snapshot())


# -- 3. missing, 4. invalid, 5. future exchange time -------------------------------------------------------------------------------------------------
def test_a_missing_exchange_timestamp_is_unknown_never_the_receipt_time_and_blocks_selection():
    base = {"subscription_mode": 3, "exchange_type": 2, "token": "111", "last_traded_price": 15000}
    t = decode(base, 1000.0, strict_time=False)
    assert t.exch_ts is None and t.exch_note == "the exchange timestamp was not provided"  # not replaced by 1000.0, the receipt time
    c = contract(24500)
    r = row(c, tick(c, exch=None, note=t.exch_note))
    assert r["exchange_age"] is None and r["exchange_status"] == "unknown" and r["freshness"] == "unknown" and r["receipt_age"] == pytest.approx(1.0)
    assert "exchange_time_unknown" in codes(r, "avoid") and r["blocking"] and "was not provided" in next(f["text"] for f in r["flags"] if f["code"] == "exchange_time_unknown")
    other = contract(24550)
    rows = analyse([(c, tick(c, exch=None, note=t.exch_note)), (other, tick(other, 120.0))], now=NOW)["contracts"]
    s = op.select(rows, "BUY CE")
    assert s["contract"]["strike"] == 24550.0  # an unknown exchange time does not pass silently
    none = op.select(analyse([(c, tick(c, exch=None, note=t.exch_note))], now=NOW)["contracts"], "BUY CE")
    assert none["contract"] is None and any("was not provided" in x for r in none["rejected"] for x in r["reasons"])
    with pytest.raises(ValueError, match="missing field"):
        decode(base, 1000.0)  # for the spot (strict, the default) a missing exchange time is still an error, as it always was


@pytest.mark.parametrize("bad,why", [("2026", "is not a number"), (True, "is not a number"), (1.5e12, "is not a number"), (0, "zero or negative"), (-5, "zero or negative")])
def test_an_invalid_exchange_timestamp_is_unknown_with_its_reason(bad, why):
    t = decode({"subscription_mode": 3, "exchange_type": 2, "token": "111", "last_traded_price": 15000, "exchange_timestamp": bad}, 1000.0, strict_time=False)
    assert t.exch_ts is None and why in t.exch_note and t.ltp == 150.0  # the quote itself is kept: its freshness is what is unknown
    c = contract(24500)
    r = row(c, tick(c, exch=None, note=t.exch_note))
    assert r["exchange_status"] == "unknown" and why in r["exchange_reason"] and "exchange_time_unknown" in codes(r, "avoid") and r["blocking"]
    strict = {"subscription_mode": 1, "exchange_type": 2, "token": "111", "last_traded_price": 15000, "exchange_timestamp": bad}  # the spot path: strict, as it always was
    if isinstance(bad, int) and not isinstance(bad, bool):
        assert TickValidator({"111": 2}).check(decode(strict, 1000.0)) == "bad_timestamp"  # an integer is decoded; the validator rejects zero and negative
    else:
        with pytest.raises(ValueError):
            decode(strict, 1000.0)  # not an integer: malformed


def test_a_future_exchange_timestamp_is_unknown_but_one_inside_the_tolerance_is_age_zero():
    recv = 1_000_000.0
    msg = {"subscription_mode": 3, "exchange_type": 2, "token": "111", "last_traded_price": 15000}
    future = decode({**msg, "exchange_timestamp": int((recv + 40) * 1000)}, recv, strict_time=False)
    assert future.exch_ts is None and "40 s in the future" in future.exch_note
    c = contract(24500)
    r = row(c, tick(c, exch=None, note=future.exch_note))
    assert r["exchange_status"] == "unknown" and "in the future" in next(f["text"] for f in r["flags"] if f["code"] == "exchange_time_unknown") and r["blocking"]
    near = decode({**msg, "exchange_timestamp": int((recv + 3) * 1000)}, recv, strict_time=False)  # three seconds ahead of the PC clock: normal skew
    assert near.exch_ts == pytest.approx(recv + 3) and near.exch_note is None
    r = row(c, tick(c, exch=NOW + 3, recv=NOW - 1))
    assert r["exchange_age"] == 0.0 and r["exchange_status"] == "fresh" and not r["blocking"]  # never a negative age


def test_the_engine_keeps_an_option_quote_with_an_unusable_time_as_unknown_and_still_rejects_the_same_for_the_spot():
    md, clock, _ = engine_with_window()
    clock.t = ist(10, 0, 30)
    opt = next(c for c in md.contracts if c.role == "ATM_CE")

    def message(token, exch, exch_type):
        m = {"subscription_mode": 3, "exchange_type": exch_type, "token": token, "last_traded_price": 6200, "volume_trade_for_the_day": 1000, "last_traded_quantity": 65}
        if exch is not None:
            m["exchange_timestamp"] = exch
        return m

    for i, exch in enumerate((None, "x", 0, int((clock.t + 60) * 1000))):  # missing, not a number, zero, a minute in the future
        before = md.accepted
        md.on_message({**message(opt.token, exch, 2), "last_traded_price": 6200 + i})
        t = md.latest[opt.token]
        assert md.accepted == before + 1 and t.exch_ts is None and t.exch_note and md.rejected["malformed"] == 0 and md.rejected["bad_timestamp"] == 0
    assert opt.token not in {k[0] for k in md.builder.current}  # no candle is built from a message with no exchange time, and nothing crashed
    md.on_message(message(md.spot_token, None, 1))  # the same omission for the NIFTY spot: still malformed, still rejected
    md.on_message(message(md.spot_token, int((clock.t + 60) * 1000), 1))
    assert md.rejected["malformed"] == 1 and md.rejected["bad_timestamp"] == 1 and md.spot_token not in md.latest


# -- 6. out-of-order exchange timestamps --------------------------------------------------------------------------------------------------------------
def test_an_older_exchange_time_never_replaces_newer_data_even_across_a_message_that_had_none():
    md, clock, _ = engine_with_window()
    opt = next(c for c in md.contracts if c.role == "ATM_CE")
    clock.t = ist(10, 0, 30)
    md.on_message(snap(opt.token, 62.0, ist(10, 0, 20), bid=(61.8, 600), ask=(62.2, 600), volume=1000, oi=1000))
    assert md.latest[opt.token].exch_ts == pytest.approx(ist(10, 0, 20))
    clock.t = ist(10, 0, 31)
    md.on_message(snap(opt.token, 60.0, ist(10, 0, 10), bid=(59.8, 600), ask=(60.2, 600), volume=999, oi=1000))  # ten seconds EARLIER than what is held
    assert md.rejected["out_of_order"] == 1 and md.latest[opt.token].ltp == 62.0  # the newer data is kept
    md.on_message({"subscription_mode": 3, "exchange_type": 2, "token": opt.token, "last_traded_price": 6300, "volume_trade_for_the_day": 1001})  # a message with no time
    assert md.latest[opt.token].exch_ts is None
    md.on_message(snap(opt.token, 61.0, ist(10, 0, 5), bid=(60.8, 600), ask=(61.2, 600), volume=1002, oi=1000))  # older than the 10:00:20 already seen: still out of order
    assert md.rejected["out_of_order"] == 2
    clock.t = ist(10, 0, 40)
    md.on_message(snap(opt.token, 63.0, ist(10, 0, 35), bid=(62.8, 600), ask=(63.2, 600), volume=1003, oi=1000))  # newer: accepted, and the exchange time is known again
    assert md.latest[opt.token].exch_ts == pytest.approx(ist(10, 0, 35)) and md.latest[opt.token].ltp == 63.0


def test_the_validator_keeps_the_highest_exchange_time_as_its_mark():
    v = TickValidator({"111": 2})

    def t(exch, ltp):
        return Tick(token="111", exch_type=2, ltp=ltp, exch_ts=exch, recv_ts=2000.0)

    assert v.check(t(1000.0, 1.0)) is None and v.check(t(None, 2.0)) is None and v.check(t(900.0, 3.0)) == "out_of_order" and v.check(t(1000.0, 3.0)) is None
    assert v.check(t(None, 4.0)) is None and v.check(t(None, 4.0)) == "duplicate"  # the same unknown-time quote twice in a row is a duplicate
    assert v.check(t(None, 5.0)) is None and v.check(t(999.0, 5.0)) == "out_of_order"  # and the mark of 1000 survived all of it


# -- 7. / 8. the last trade is a different clock ----------------------------------------------------------------------------------------------------
def test_an_old_last_trade_with_a_recent_exchange_update_is_fresh_the_order_book_moved():
    c = contract(24500)
    r = row(c, tick(c, exch=NOW - 2, recv=NOW - 1, ltt=NOW - 3 * 3600))
    assert r["last_trade_age"] == pytest.approx(3 * 3600) and r["exchange_status"] == "fresh" and r["freshness"] == "fresh" and not r["blocking"]
    assert not any("trade" in f["code"] for f in r["flags"])  # an old last trade is information, not a freshness problem


def test_a_recent_last_trade_cannot_make_an_old_exchange_update_fresh():
    c = contract(24500)
    r = row(c, tick(c, exch=NOW - 900, recv=NOW - 1, ltt=NOW - 5))
    assert r["last_trade_age"] == pytest.approx(5.0) and r["exchange_status"] == "stale" and r["freshness"] == "stale" and "exchange_time_stale" in codes(r, "avoid") and r["blocking"]
    assert row(c, tick(c, ltt=None))["last_trade_age"] is None  # no last-trade time: None, not zero and not the receipt time


# -- 11. the receipt age keeps working on its own ------------------------------------------------------------------------------------------------------
def test_the_receipt_age_is_judged_on_its_own_and_the_exchange_age_on_its_own():
    c = contract(24500)
    lax = op.OptionParams(exchange_stale_seconds=120.0)  # the two limits are separate settings
    both = row(c, tick(c, exch=NOW - 46, recv=NOW - 45))
    assert {"stale_quote", "exchange_time_stale"} <= codes(both, "avoid") and both["freshness"] == "stale"
    only_receipt = row(c, tick(c, exch=NOW - 46, recv=NOW - 45), p=lax)  # received 45 s ago (limit 30), exchange time 46 s old (limit 120)
    assert codes(only_receipt, "avoid") == {"stale_quote"} and only_receipt["status"] == "stale" and only_receipt["exchange_status"] == "fresh" and only_receipt["freshness"] == "stale"
    only_exchange = row(c, tick(c, exch=NOW - 600, recv=NOW - 1))
    assert codes(only_exchange, "avoid") == {"exchange_time_stale"} and only_exchange["status"] == "fresh"
    assert (P.quote_stale_seconds, P.exchange_stale_seconds) == (30.0, 30.0)  # the existing receipt limit was not touched; the new one is a documented placeholder


def test_the_exchange_limit_is_configurable_and_checked():
    assert op.OptionParams.from_env(env={"OPTIONS_EXCHANGE_STALE_SECONDS": "90"}).exchange_stale_seconds == 90.0
    for bad in ("soon", "-1"):
        with pytest.raises(ValueError):
            op.OptionParams.from_env(env={"OPTIONS_EXCHANGE_STALE_SECONDS": bad})
    c = contract(24500)
    assert row(c, tick(c, exch=NOW - 60), p=op.OptionParams(exchange_stale_seconds=90.0))["exchange_status"] == "fresh"
    assert row(c, tick(c, exch=NOW - 60), p=op.OptionParams(exchange_stale_seconds=45.0))["exchange_status"] == "stale"


def test_outside_the_session_the_ages_are_shown_but_the_market_closed_flag_is_the_reason():
    c = contract(24500)
    late = ist(21, 55, 0)
    r = row_of([(c, tick(c, exch=ist(15, 40, 1), recv=late - 1))], c, now=late)
    assert r["freshness"] == "market closed" and r["exchange_status"] == "stale" and "market_closed" in codes(r, "avoid") and "exchange_time_stale" not in codes(r)


# -- the real captured message (Angel One, 2026-11-06 after the close) ---------------------------------------------------------------------------------
def test_the_real_captured_message_has_a_feed_time_distinct_from_its_last_trade_time_and_ages_correctly():
    recv = ist(15, 40, 11)
    t = decode(REAL_NIFTY09NOV2625050CE, recv, strict_time=False)
    assert t.exch_ts == pytest.approx(ist(15, 40, 1)) and t.last_trade_ts == pytest.approx(ist(15, 39, 59)) and t.exch_note is None
    c = contract(25050, expiry=__import__("datetime").date(2026, 11, 9))
    c = type(c)(c.role, "40039", c.symbol, "NFO", c.expiry, 25050.0, 65)
    x, status, why = op.exchange_freshness(t, recv, P)
    assert (round(x), status, why) == (10, "fresh", None)  # received ten seconds after the exchange's own time: fresh on both clocks
    x, status, why = op.exchange_freshness(t, recv + 6 * 3600, P)  # the same message judged six hours later, as the after-hours validation saw it
    assert status == "stale" and x == pytest.approx(6 * 3600 + 10) and "6.0 h" in why


# -- Phase 3 is not touched -------------------------------------------------------------------------------------------------------------------------------
def test_wait_and_no_trade_still_give_no_selection_whatever_the_freshness():
    a, b = contract(24500), contract(24550)
    rows = analyse([(a, tick(a)), (b, tick(b, 120.0, exch=NOW - 900))], now=NOW)["contracts"]
    for action in ("WAIT", "NO TRADE", None):
        s = op.select(rows, action)
        assert s["actionable"] is False and s["contract"] is None and "no options selection" in s["notice"]
    assert op.select(rows, "BUY CE")["contract"]["strike"] == 24500.0  # a BUY still offers the fresh one, and only that one
    md, _, _ = engine_with_window()
    assert md.options_view(decision("WAIT"), ist(10, 0, 30))["selection"]["contract"] is None
    assert not op.OptionParams().__dataclass_fields__.get("strategy")  # a freshness setting is an option setting: nothing of Phase 3 was changed


def test_the_journal_brief_of_a_selected_contract_carries_the_three_ages():
    md, clock, _ = engine_with_window()
    now = ist(10, 5, 0)
    clock.t = now
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": md.spot_token, "last_traded_price": int(24510.0 * 100), "exchange_timestamp": int(now * 1000)})
    atm = next(c for c in md.contracts if c.role == "ATM_CE")
    md.on_message(snap(atm.token, 62.0, now - 2, bid=(61.8, 600), ask=(62.2, 600), volume=3_000_000, oi=4_000_000, ltt=now - 600))
    brief = md.options_brief({"decision": {"action": "BUY CE", "headline": "x", "strategy_name": None, "setup_id": None}}, now + 1)
    assert brief["exchange_status"] == "fresh" and brief["exchange_age"] == pytest.approx(3.0) and brief["last_trade_age"] == pytest.approx(601.0) and brief["freshness"] == "fresh"


def test_the_dashboard_labels_the_three_clocks_and_the_freshness():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for label in ("receipt age", "exchange-time age", "last-trade age", "Freshness: receipt / exchange / last trade", "Receipt age", "exchange_status", "FRESH_CLS", "exchange_stale_seconds"):
        assert label in html, label
    assert "Recent receipt" not in html  # the dashboard shows the flag text from the API, it does not restate it
    assert isinstance(MarketData, type)
