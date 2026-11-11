"""Stage 5.1: the three additive, presentation-only additions to the backend payload, and the proof that REST and the WebSocket carry them identically.

  1. `market.change`: how far NIFTY is from the previous session's close, worked out in the backend (the dashboard only formats it). Unknown, never zero, when either number is missing.
  2. `health.instruments[].exchange_age / exchange_status / exchange_reason / exchange_limit / receipt_age`: the instrument's EXCHANGE time judged on its own, beside the receipt age,
     named exactly like the option rows' fields. The receipt time is never substituted for the exchange time, and none of it feeds a decision or `trusted`.
  3. `strategies[].sides`: each strategy's CE and PE state and note kept apart (the old `note` / `why` merged them).

Nothing here reads or changes a decision: the existing tests (and the golden recordings of the original strategies) hold that."""

import asyncio
import json

import pytest
from helpers import SPOT_TOKEN, ist
from test_exhaustion import ter_ctx
from test_options import engine_with_window, live_engine
from test_options_integration import FLOWS, KIND, SIDES, first_push, flow, served
from test_phase3_integration import World, create_app, route
from test_trend_family import decision_after

from app import registry
from app import trading as tr


# ====================================================================================================================
# 1. The move from the previous close
# ====================================================================================================================
def analysis(price=24310.0, pdc=24250.0, **pd):
    return {"price": price, "reference": {"previous_day": {"pdc": pdc, "date": "2026-11-05", **pd}}}


def test_the_move_from_the_previous_close_is_worked_out_in_the_backend():
    c = tr.price_change(analysis())
    assert c["available"] and c["points"] == pytest.approx(60.0) and c["percent"] == pytest.approx(100 * 60 / 24250) and c["price"] == 24310.0
    assert c["previous_close"] == 24250.0 and c["previous_close_date"] == "2026-11-05" and c["basis"] == "the previous session's close"
    down = tr.price_change(analysis(price=24200.0))
    assert down["points"] == pytest.approx(-50.0) and down["percent"] == pytest.approx(-100 * 50 / 24250)  # a fall is negative, not an absolute value
    flat = tr.price_change(analysis(price=24250.0))
    assert flat["available"] and flat["points"] == 0 and flat["percent"] == 0  # a real zero (price == previous close) is a zero: available


@pytest.mark.parametrize("a,why", [
    ({"price": None, "reference": {"previous_day": {"pdc": 24250.0}}}, "no NIFTY price yet"),
    ({"price": 24310.0, "reference": {"previous_day": None}}, "the previous close is not available"),
    ({"price": 24310.0, "reference": {}}, "the previous close is not available"),
    ({"price": 24310.0}, "the previous close is not available"),
    ({"price": 24310.0, "reference": {"previous_day": {"pdc": 0}}}, "the previous close is not available"),
    ({"price": 24310.0, "reference": {"previous_day": {"pdc": None}}}, "the previous close is not available"),
    (analysis(stale=True), "stale"),
])  # fmt: skip
def test_a_missing_or_stale_input_makes_the_move_unknown_never_zero(a, why):
    c = tr.price_change(a)
    assert c["available"] is False and why in c["why"]
    assert not {"points", "percent"} & set(c)  # no number to mistake for a zero


def test_the_real_engine_puts_the_move_in_the_market_payload_and_every_route_carries_it_unchanged():
    world = World()
    md, clock = world.engine()
    for slot in (16, 17, 18, 19):
        out = world.step(md, clock, slot)
    ch = out["market"]["change"]
    pdc = md.analysis()["reference"]["previous_day"]["pdc"]
    assert ch["available"] and ch["previous_close"] == pdc and ch["points"] == pytest.approx(out["price"] - pdc) and ch["percent"] == pytest.approx(100 * (out["price"] - pdc) / pdc)
    app = create_app(md, feed=None)
    app.state.md = md
    state = json.loads(json.dumps(route(app, "/api/state").endpoint()))
    rest = json.loads(json.dumps(route(app, "/api/trading").endpoint()))
    ws = asyncio.run(first_push(app))
    assert rest["market"]["change"] == state["trading"]["market"]["change"] == ws["trading"]["market"]["change"] == json.loads(json.dumps(ch))


# ====================================================================================================================
# 2. The exchange time of the spot, beside its receipt age
# ====================================================================================================================
def spot_row(md, now):
    return next(r for r in md.instrument_status(now) if r["role"] == "SPOT")


def tick_spot(md, clock, now, exchange_age):
    clock.t = now
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(24510.0 * 100), "exchange_timestamp": int((now - exchange_age) * 1000)})


def test_the_spot_exchange_age_and_status_sit_beside_the_receipt_age_with_the_option_rows_field_names():
    md, clock, _ = engine_with_window()
    now = ist(10, 0, 30)
    tick_spot(md, clock, now, exchange_age=0)
    r = spot_row(md, now + 2)
    assert r["receipt_age"] == r["age"] == pytest.approx(2.0) and r["exchange_age"] == pytest.approx(2.0) and r["exchange_status"] == "fresh" and r["exchange_reason"] is None
    assert r["exchange_limit"] == md.params.max_price_age == 20.0 and r["exchange_time"] == pytest.approx(now) and r["received_time"] == pytest.approx(now)  # the spot's own limit
    for name in ("exchange_time", "exchange_age", "exchange_status", "exchange_reason", "receipt_age"):  # the option table's row has exactly these names for the same things
        assert name in r and name in md.options_view(None, now + 2)["contracts"][0], name


def test_the_receipt_time_is_never_substituted_for_the_exchange_time():
    md, clock, _ = engine_with_window()
    now = ist(10, 0, 30)
    tick_spot(md, clock, now, exchange_age=15)  # the message arrived just now, carrying an exchange time 15 s old
    r = spot_row(md, now + 2)
    assert r["receipt_age"] == pytest.approx(2.0) and r["exchange_age"] == pytest.approx(17.0) and r["exchange_time"] == pytest.approx(now - 15) and r["received_time"] == pytest.approx(now)
    assert r["status"] == "fresh" and r["exchange_status"] == "fresh"  # 17 s is inside the 20 s limit
    r = spot_row(md, now + 8)  # six seconds later: received 8 s ago, but the exchange's own time is now 23 s old
    assert r["status"] == "fresh" and r["receipt_age"] == pytest.approx(8.0) and r["exchange_age"] == pytest.approx(23.0)
    assert r["exchange_status"] == "stale" and "23 s" in r["exchange_reason"] and "limit 20 s" in r["exchange_reason"]  # recent receipt is not fresh data


def test_a_spot_that_has_not_ticked_has_an_unknown_exchange_time_not_a_zero_age():
    md, clock, _ = engine_with_window()
    r = spot_row(md, ist(10, 0, 30))
    assert r["exchange_age"] is None and r["exchange_time"] is None and r["receipt_age"] is None and r["exchange_status"] == "unknown" and "no quote" in r["exchange_reason"]


def test_the_exchange_status_is_information_only_it_changes_neither_the_health_verdict_nor_the_receipt_status():
    md, clock, _ = engine_with_window()
    now = ist(10, 0, 30)
    tick_spot(md, clock, now, exchange_age=0)
    before = md.health(now + 2)
    clock.t = now + 2
    fresh_row = spot_row(md, now + 2)
    md2, clock2, _ = engine_with_window()
    tick_spot(md2, clock2, now, exchange_age=19)  # an old exchange time
    clock2.t = now + 2
    after = md2.health(now + 2)
    assert before["trusted"] == after["trusted"] and before["issues"] == after["issues"]  # the verdict is the same
    assert fresh_row["status"] == spot_row(md2, now + 2)["status"] == "fresh"


def test_the_option_rows_of_the_health_payload_get_the_same_fields_with_the_option_limit():
    md, _ = live_engine()
    rows = md.instrument_status(ist(10, 0, 22))
    options = [r for r in rows if r["role"] != "SPOT"]
    assert options and all({"exchange_age", "exchange_status", "exchange_reason", "exchange_limit", "receipt_age"} <= set(r) for r in rows)
    assert all(r["exchange_limit"] == md.option_params.exchange_stale_seconds for r in options) and all(r["exchange_status"] in ("fresh", "stale", "unknown") for r in rows)
    quoted = next(r for r in options if r["exchange_time"] is not None)
    via_options = next(x for x in md.options_view(None, ist(10, 0, 22))["contracts"] if x["token"] == quoted["token"])
    assert (quoted["exchange_age"], quoted["exchange_status"]) == (via_options["exchange_age"], via_options["exchange_status"])  # one definition of freshness, two places that show it


def test_the_health_payload_with_the_new_fields_is_identical_on_every_route():
    md, clock = live_engine()
    clock.t = ist(10, 0, 22)
    app = create_app(md, feed=None)
    app.state.md = md
    health = json.loads(json.dumps(route(app, "/api/health").endpoint()))
    state = json.loads(json.dumps(route(app, "/api/state").endpoint()))
    ws = asyncio.run(first_push(app))
    pick = lambda h: [{k: r[k] for k in ("role", "exchange_age", "exchange_status", "exchange_reason", "exchange_limit", "receipt_age", "exchange_time")} for r in h["instruments"]]  # noqa: E731
    assert pick(health) == pick(state["health"]) == pick(ws["health"]) and pick(health)[0]["role"] == "SPOT"


# ====================================================================================================================
# 3. Each strategy's CE and PE, kept apart
# ====================================================================================================================
@pytest.mark.parametrize("name", list(FLOWS))
@pytest.mark.parametrize("side", SIDES)
def test_every_strategy_has_a_ce_and_a_pe_entry_with_their_own_state_and_note(name, side):
    out = flow(name, side)
    mine = next(m for m in out["strategies"] if m["id"] == name)
    assert [s["side"] for s in mine["sides"]] == ["CE", "PE"] and all({"side", "scanned", "applicable", "note", "state", "setup_id"} == set(s) for s in mine["sides"])
    held = next(s for s in mine["sides"] if s["side"] == KIND[side])
    other = next(s for s in mine["sides"] if s["side"] != KIND[side])
    assert held["setup_id"] == out["decision"]["setup_id"] and held["state"] == "CONFIRMED" and held["applicable"] and held["scanned"]  # the side the strategy has a setup on
    assert other["setup_id"] is None and other["state"] in ("NO SETUP", "INACTIVE")  # the other side holds none
    assert mine["state"] == "CONFIRMED" and mine["setups"] == [held["setup_id"]]  # the existing fields are exactly what they were


def test_the_ce_and_the_pe_reason_are_never_merged_into_one_line():
    out = flow("trend_pullback", "bull")
    for m in out["strategies"]:
        ce, pe = m["sides"]
        for mine, theirs in ((ce, pe), (pe, ce)):
            if mine["note"] and theirs["note"] and mine["note"] != theirs["note"]:
                assert mine["note"] not in theirs["note"] and theirs["note"] not in mine["note"]  # each side carries only its own scanner's words
    pull = next(m for m in out["strategies"] if m["id"] == "trend_pullback")
    assert pull["sides"][1]["applicable"] is False and "1H is bullish" in pull["sides"][1]["note"]  # the put side's own reason: pullbacks are only taken with a bearish 1H for a put
    assert pull["sides"][0]["applicable"] is True and pull["sides"][0]["state"] == "CONFIRMED"
    rr = next(m for m in out["strategies"] if m["id"] == "range_reversal")  # an inactive strategy: both sides say so, each in its own entry
    assert [s["applicable"] for s in rr["sides"]] == [False, False] and all(s["note"] for s in rr["sides"])
    assert rr["why"] in [rr["sides"][0]["note"], rr["sides"][1]["note"]]  # (the old `why` is one of them: it was the first side's note, the other was lost)


def test_a_reversal_that_is_denied_on_one_side_says_so_on_that_side_only():
    out = decision_after(ter_ctx, range(47, 52), "bull", weak="none")
    ter = next(m for m in out["strategies"] if m["id"] == "trend_exhaustion_reversal")
    ce, pe = ter["sides"]
    assert ce["applicable"] and "counter-trend permission denied" in ce["note"] and ce["state"] == "NO SETUP"  # the call side: the permission was asked for and denied, with the reason
    assert pe["applicable"] is False and "established" in pe["note"] and "counter-trend" not in pe["note"]  # the put side: nothing to reverse, a different reason entirely


def test_with_no_scan_at_all_the_sides_say_not_scanned_and_are_never_empty_guesses():
    names = list(registry.NAMES)
    monitors = tr.monitor({}, [], {"text": "m"}, {"phase": "closed", "note": "the market is closed"})
    assert [m["id"] for m in monitors] == names
    for m in monitors:
        assert [(s["side"], s["scanned"], s["applicable"], s["state"], s["note"], s["setup_id"]) for s in m["sides"]] == [("CE", False, False, "INACTIVE", "", None), ("PE", False, False, "INACTIVE", "", None)]


@pytest.mark.parametrize("name", list(FLOWS))
@pytest.mark.parametrize("side", SIDES)
def test_the_sides_are_identical_over_rest_and_the_websocket(name, side):
    md, app, payload = served(name, side)
    rest = json.loads(json.dumps(route(app, "/api/trading").endpoint()))
    state = json.loads(json.dumps(route(app, "/api/state").endpoint()))
    ws = asyncio.run(first_push(app))
    expected = [(m["id"], m["sides"]) for m in json.loads(json.dumps(payload))["strategies"]]
    for got in (rest, state["trading"], ws["trading"]):
        assert [(m["id"], m["sides"]) for m in got["strategies"]] == expected
