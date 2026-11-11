"""The Trades tab: the dashboard's own script under node (dashboard_harness.py), with a fake server behind its fetch.

A notebook of your own trades and a size calculator. Nothing here places an order. What is held:

  * the page asks for the trades ONCE as it starts, and a server that cannot answer is not asked again in a loop (it was, before this test existed: the first draft re-asked for ever); only Retry asks again
  * the table, the open positions, the realised result, the loss-limit line; an older server with no endpoint says to restart Prism
  * logging a trade sends exactly what was typed, applies the summary the server answered with, clears the quantity / price / note and keeps the instrument; a refusal says why
  * voiding a trade asks the server to; the daily loss limit reaching shows on every tab and raises one alert, again only after it has been left and reached again
  * the calculator sends only what was filled in and shows the lots, the limit that decides and the warnings; a server's refusal is shown in plain words
  * everything the server says is escaped"""

import copy
import json

import pytest
from dashboard_harness import base_snapshot, run_page, words

LIMITS = {"capital": 100000.0, "per_trade_pct": 2.0, "per_trade_loss": 2000.0, "max_daily_loss": 5000.0}


@pytest.fixture(scope="module")
def base():
    return base_snapshot()


def trade(i="t1-1", side="BUY", instrument="NIFTY 24500 CE", qty=65, price=100.0, **kw):
    return {"v": 1, "kind": "trade", "id": i, "t": 1793940000.0 + (int(i.split("-")[1]) if i.split("-")[-1].isdigit() else 0) * 60, "time": "2026-11-06T10:00:00+05:30", "instrument": instrument, "side": side, "qty": qty, "price": price,
            "setup_id": None, "note": None, "prism": None, "matched": 0, "pnl": None, "lot_pnl": None, "followed": None, **kw}  # fmt: skip


def summary(trades=(), realised=0.0, open_=(), lost=0.0, reached=False, limits=LIMITS, available=True, **kw):
    used = None if not limits or not limits.get("max_daily_loss") else 100.0 * lost / limits["max_daily_loss"]
    return {"available": available, "file": "trades-2026-11-06.jsonl", "trades": list(trades), "voided": [], "open": list(open_), "realised": realised, "lost_today": lost, "limits": limits,
            "daily_loss_used_pct": used, "daily_loss_reached": reached, "basis": "arithmetic on the prices you typed", **kw}  # fmt: skip


def ok(body):
    return {"status": 200, "body": body}


def go(tmp_path, snap, steps, fetch=None, extra_env=None):
    """The page after those steps (the last capture). `fetch`: the fake server's routes."""
    env = {"op": "env", "fetch": fetch or {}, **(extra_env or {})}
    probe = {"op": "eval", "code": "document.getElementById('alertprobe').textContent = JSON.stringify(ALERT_LOG)"}
    return run_page(tmp_path, [env, {"op": "message", "snap": snap}, *steps, probe, {"op": "capture", "label": "end"}])[-1]


def alert_log(state):
    """The alert events the page raised (its ALERT_LOG), newest first, as one string."""
    return " ".join(f"{x['title']} {x['text']}" for x in json.loads(state["els"]["alertprobe"]["text"]))


def requests(state, method=None, prefix=""):
    return [r for r in state["fx"]["requests"] if (method is None or r["method"] == method) and r["url"].startswith(prefix)]


def body(state):
    return words(state["els"]["trades-body"]["html"])


def typed(**fields):
    """An eval step that fills the form (field ids without their prefix: instr, side, qty, price, setup, note)."""
    code = "".join(f"$('t-{k}').value = {v!r}; " for k, v in fields.items())
    return {"op": "eval", "code": code}


TAB = {"op": "tab", "name": "trades"}


# -- asking for the trades ---------------------------------------------------------------------------------------------------------------------
def test_the_page_asks_for_the_trades_once_as_it_starts_and_shows_them_when_the_tab_opens(tmp_path, base):
    s = summary([trade("t1-1"), trade("t1-2", "SELL", price=120.0, pnl=1300.0, matched=0)], realised=1300.0)
    s["trades"][0].update(matched=65, lot_pnl=1300.0)
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(s)})
    assert len(requests(st, "GET", "/api/trades")) == 1
    text = body(st)
    assert "Today's trades trades-2026-11-06.jsonl" in text and "Realised Rs 1,300" in text and "Loss limit for the day Rs 5,000" in text and "(0% used)" in text and "Open nothing" in text
    assert "NIFTY 24500 CE" in text and "BUY" in text and "SELL" in text and "Rs 1,300" in text
    assert st["errors"] == [] and st["els"].get("t-msg", {"text": ""})["text"] == ""  # (nothing touched the message line)


def test_a_server_that_cannot_answer_is_asked_once_and_never_in_a_loop(tmp_path, base):
    steps = [TAB, {"op": "tab", "name": "overview"}, TAB, {"op": "tab", "name": "overview"}, TAB, {"op": "message", "snap": base}, {"op": "message", "snap": base}]
    st = go(tmp_path, base, steps, {})  # no route: every request is a network failure
    assert len(requests(st, "GET", "/api/trades")) == 1  # the start-up ask, and no more
    assert "could not load the trades" in body(st) and "no network" in body(st) and 'id="t-refresh"' in st["els"]["trades-body"]["html"]


def test_retry_asks_again_and_a_server_that_has_come_back_shows_the_trades(tmp_path, base):
    fetch = {"GET /api/trades": [{"status": 500, "body": {"detail": "boom"}}, ok(summary([trade()], open_=[{"instrument": "NIFTY 24500 CE", "qty": 65, "average_price": 100.0}]))]}
    st = go(tmp_path, base, [TAB, {"op": "click", "id": "t-refresh"}], fetch)
    assert len(requests(st, "GET", "/api/trades")) == 2
    assert "Open NIFTY 24500 CE 65 @ 100.00" in body(st) and "could not load" not in body(st)


def test_a_server_that_has_no_trade_log_says_to_restart_prism(tmp_path, base):
    st = go(tmp_path, base, [TAB], {"GET /api/trades": {"status": 404, "body": {"detail": "Not Found"}}})
    assert "no trade log yet (restart Prism to load the current version)" in body(st)


def test_a_trade_file_that_does_not_add_up_is_said_not_shown_as_numbers(tmp_path, base):
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok({"available": False, "reason": "the trade file does not add up", "trades": [], "file": "x"})})
    assert "the trade file does not add up" in body(st) and "Realised" not in body(st)


def test_no_trade_yet_and_no_limit_set(tmp_path, base):
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(summary(limits={"capital": None, "per_trade_pct": None, "per_trade_loss": None, "max_daily_loss": None}))})
    assert "no trade logged today" in body(st) and "Loss limit for the day not set" in body(st) and "% used" not in body(st)


def test_the_result_column_adds_up_a_buy_shows_its_part_in_grey_and_a_sell_the_result(tmp_path, base):
    b = trade("t1-1", matched=65, lot_pnl=1300.0)
    s = trade("t1-2", "SELL", price=120.0, pnl=1300.0)
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(summary([b, s], realised=1300.0))})
    html = st["els"]["trades-body"]["html"]
    assert html.count("Rs 1,300") == 3 and 'title="what the part of this buy that has been sold made"' in html  # (the realised figure, the buy's grey part, the sell's result)
    assert "adds up to the realised figure" in body(st)


def test_what_prism_said_and_whether_you_followed_it_is_shown(tmp_path, base):
    followed = trade("t1-1", prism={"action": "BUY CE", "setup_id": "x", "price": 24505.0}, followed="yes: the setup you named")
    against = trade("t1-2", prism={"action": "WAIT", "setup_id": None, "price": 24400.0}, followed="no")
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(summary([followed, against]))})
    text = body(st)
    assert "BUY CE at 24505.00 · followed Prism: yes: the setup you named" in text and "WAIT at 24400.00 · followed Prism: no" in text


# -- logging a trade -------------------------------------------------------------------------------------------------------------------------
def test_logging_sends_what_was_typed_applies_the_answer_clears_the_numbers_and_keeps_the_instrument(tmp_path, base):
    answer = summary([trade("t2-1", price=101.5, qty=65)], open_=[{"instrument": "NIFTY 24500 CE", "qty": 65, "average_price": 101.5}])
    fetch = {"GET /api/trades": ok(summary()), "POST /api/trades": ok({"ok": True, "trade": trade("t2-1"), "summary": answer})}
    st = go(tmp_path, base, [TAB, typed(instr="NIFTY 24500 CE", side="BUY", qty="65", price="101.5", setup="trend_pullback:bull:1", note="first"), {"op": "click", "id": "t-log"}], fetch)
    (post,) = requests(st, "POST", "/api/trades")
    assert post["body"] == {"instrument": "NIFTY 24500 CE", "side": "BUY", "qty": "65", "price": "101.5", "setup_id": "trend_pullback:bull:1", "note": "first"}
    assert st["els"]["t-msg"]["text"] == "logged" and st["els"]["t-msg"]["cls"] == ""
    assert "Open NIFTY 24500 CE 65 @ 101.50" in body(st)
    probe = run_page(tmp_path, [{"op": "env", "fetch": fetch}, {"op": "message", "snap": base}, TAB, typed(instr="NIFTY 24500 CE", qty="65", price="101.5", note="first", setup="x:bull:1"), {"op": "click", "id": "t-log"},
                                {"op": "eval", "code": "document.getElementById('probe').textContent = [$('t-instr').value, $('t-qty').value, $('t-price').value, $('t-note').value, $('t-setup').value].join('|')"},
                                {"op": "capture", "label": "p"}])[-1]
    assert probe["els"]["probe"]["text"] == "NIFTY 24500 CE||||"  # the instrument stays for the next one; the numbers, the note and the setup (which belongs to that one trade) do not


def test_a_refused_trade_says_why_in_red_and_changes_nothing(tmp_path, base):
    fetch = {"GET /api/trades": ok(summary([trade()])), "POST /api/trades": {"status": 422, "body": {"detail": "nothing to sell: NIFTY 24500 CE has 0 unit(s) open and the sell is for 65"}}}
    st = go(tmp_path, base, [TAB, typed(instr="NIFTY 24500 CE", side="SELL", qty="65", price="90"), {"op": "click", "id": "t-log"}], fetch)
    assert st["els"]["t-msg"]["text"] == "not logged: nothing to sell: NIFTY 24500 CE has 0 unit(s) open and the sell is for 65" and st["els"]["t-msg"]["cls"] == "err"
    assert "NIFTY 24500 CE" in body(st) and len(requests(st, "POST")) == 1


def test_a_server_that_does_not_know_the_route_says_to_restart(tmp_path, base):
    st = go(tmp_path, base, [TAB, typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}], {"GET /api/trades": ok(summary()), "POST /api/trades": {"status": 404, "body": {"detail": "Not Found"}}})
    assert "restart Prism" in st["els"]["t-msg"]["text"] and st["els"]["t-msg"]["cls"] == "err"


def test_a_network_failure_while_logging_is_said_and_nothing_is_cleared(tmp_path, base):
    st = go(tmp_path, base, [TAB, typed(instr="NIFTY 24500 CE", qty="65", price="101.5"), {"op": "click", "id": "t-log"}], {"GET /api/trades": ok(summary())})
    assert st["els"]["t-msg"]["text"].startswith("not logged: ") and "no network" in st["els"]["t-msg"]["text"]


def test_a_validation_error_list_from_the_server_is_shown_not_hidden(tmp_path, base):
    st = go(tmp_path, base, [TAB, {"op": "click", "id": "t-log"}], {"GET /api/trades": ok(summary()), "POST /api/trades": {"status": 422, "body": {"detail": [{"loc": ["body"], "msg": "bad"}]}}})
    assert "not logged:" in st["els"]["t-msg"]["text"] and "bad" in st["els"]["t-msg"]["text"]


def test_the_use_prisms_setup_button_fills_in_the_setup_the_decision_is_about(tmp_path, base):
    sid = base["trading"]["decision"]["setup_id"]
    probe = go(tmp_path, base, [TAB, {"op": "click", "id": "t-use-setup"}, {"op": "eval", "code": "document.getElementById('probe').textContent = $('t-setup').value"}], {"GET /api/trades": ok(summary())})
    assert sid and probe["els"]["probe"]["text"] == sid
    none = variant_decision(base, setup_id=None)
    probe = go(tmp_path, none, [TAB, {"op": "click", "id": "t-use-setup"}, {"op": "eval", "code": "document.getElementById('probe').textContent = '[' + $('t-setup').value + ']'"}], {"GET /api/trades": ok(summary())})
    assert probe["els"]["probe"]["text"] == "[]"  # nothing to fill in


def variant_decision(base, **over):
    s = copy.deepcopy(base)
    s["trading"]["decision"].update(over)
    return s


# -- voiding ---------------------------------------------------------------------------------------------------------------------------------
def test_void_asks_the_server_to_cancel_that_trade_and_applies_the_answer(tmp_path, base):
    fetch = {"GET /api/trades": ok(summary([trade("t1-1"), trade("t1-2", "SELL", pnl=100.0)], realised=100.0)),
             "POST /api/trades/t1-2/void": ok({"ok": True, "summary": summary([trade("t1-1")], open_=[{"instrument": "NIFTY 24500 CE", "qty": 65, "average_price": 100.0}])})}
    st = go(tmp_path, base, [TAB, {"op": "click", "target_dataset": {"void": "t1-2"}}], fetch)
    assert requests(st, "POST", "/api/trades/t1-2/void") and st["els"]["t-msg"]["text"] == "voided" and "Open NIFTY 24500 CE 65 @ 100.00" in body(st)
    assert 'data-void="t1-1"' in st["els"]["trades-body"]["html"] and 'data-void="t1-2"' not in st["els"]["trades-body"]["html"]


def test_a_refused_void_says_why(tmp_path, base):
    fetch = {"GET /api/trades": ok(summary([trade("t1-1")])), "POST /api/trades/t1-1/void": {"status": 422, "body": {"detail": "a later sell depends on this buy: void the sell first"}}}
    st = go(tmp_path, base, [TAB, {"op": "click", "target_dataset": {"void": "t1-1"}}], fetch)
    assert st["els"]["t-msg"]["text"] == "not voided: a later sell depends on this buy: void the sell first" and st["els"]["t-msg"]["cls"] == "err"


def test_a_trade_id_is_encoded_in_the_address_it_is_voided_at(tmp_path, base):
    st = go(tmp_path, base, [TAB, {"op": "click", "target_dataset": {"void": "a/b c?"}}], {"GET /api/trades": ok(summary()), "POST /api/trades/*": ok({"ok": True, "summary": summary()})})
    assert requests(st, "POST")[0]["url"] == "/api/trades/a%2Fb%20c%3F/void"


# -- the daily loss limit --------------------------------------------------------------------------------------------------------------------
LOST = summary([trade()], realised=-5200.0, lost=5200.0, reached=True)


def test_a_loss_limit_already_reached_is_on_every_tab_from_the_start(tmp_path, base):
    st = go(tmp_path, base, [], {"GET /api/trades": ok(LOST)})  # the overview, never having opened the Trades tab
    alerts = words(st["els"]["alerts"]["html"])
    assert "1 alert" in alerts and "Your daily loss limit is reached" in alerts and "Rs 5,200 lost of Rs 5,000" in alerts and "this limit is yours" in alerts
    assert 'class="alerts bad"' in st["els"]["alerts"]["html"]


def test_the_trades_tab_shows_the_limit_in_red(tmp_path, base):
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(LOST)})
    assert "You have reached your own loss limit for the day (Rs 5,200 of Rs 5,000)" in body(st) and "(104% used)" in body(st) and 'class="down">(104% used)' in st["els"]["trades-body"]["html"]


def test_logging_the_trade_that_reaches_the_limit_raises_one_warning_and_not_a_second_time(tmp_path, base):
    under = summary([trade()], realised=-4000.0, lost=4000.0)
    fetch = {"GET /api/trades": ok(under), "POST /api/trades": [ok({"ok": True, "trade": trade("t2-1"), "summary": LOST}), ok({"ok": True, "trade": trade("t3-1"), "summary": LOST})]}
    one = go(tmp_path, base, [TAB, typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}], fetch)
    log = alert_log(one)
    assert "Your daily loss limit is reached" in log and "Rs 5,200 lost of Rs 5,000" in log and one["fx"]["tones"] == [520, 390]
    fetch["POST /api/trades"] = [ok({"ok": True, "trade": trade("t2-1"), "summary": LOST}), ok({"ok": True, "trade": trade("t3-1"), "summary": LOST})]
    two = go(tmp_path, base, [TAB, typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}, typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}], {**fetch})
    assert two["fx"]["tones"] == [520, 390] and alert_log(two).count("Your daily loss limit is reached") == 1  # still reached: not said again


def test_a_loss_limit_that_is_left_and_reached_again_is_said_again(tmp_path, base):
    back = summary([trade()], realised=-1000.0, lost=1000.0)
    fetch = {"GET /api/trades": ok(summary()), "POST /api/trades": [ok({"ok": True, "trade": trade(), "summary": LOST}), ok({"ok": True, "trade": trade(), "summary": back}), ok({"ok": True, "trade": trade(), "summary": LOST})]}
    fill = [typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}]
    st = go(tmp_path, base, [TAB, *fill, *fill, *fill], fetch)
    assert alert_log(st).count("Your daily loss limit is reached") == 2 and st["fx"]["tones"] == [520, 390, 520, 390]


def test_a_limit_that_is_not_reached_raises_nothing(tmp_path, base):
    st = go(tmp_path, base, [TAB, typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}],
            {"GET /api/trades": ok(summary()), "POST /api/trades": ok({"ok": True, "trade": trade(), "summary": summary([trade()], realised=-1000.0, lost=1000.0)})})
    assert alert_log(st) == "" and st["fx"]["tones"] == [] and st["els"].get("alerts", {"html": ""})["html"] == ""


# -- the calculator --------------------------------------------------------------------------------------------------------------------------
RISK = {"entry": 100.0, "stop": 80.0, "lot_size": 65, "loss_per_unit": 20.0, "loss_per_lot": 1300.0, "outlay_per_lot": 6500.0, "per_trade_loss": 2000.0, "max_daily_loss": 5000.0, "lost_today": 0.0,
        "left_today": 5000.0, "lots_by_trade_limit": 1, "lots_by_daily_limit": 3, "lots": 1, "quantity": 65, "loss_at_stop": 1300.0, "outlay": 6500.0, "outlay_pct_of_capital": 6.5,
        "binding": "lots_by_trade_limit", "warnings": []}  # fmt: skip


def calc(tmp_path, base, route, **fields):
    return go(tmp_path, base, [TAB, {"op": "eval", "code": "".join(f"$('r-{k}').value = {v!r}; " for k, v in fields.items())}, {"op": "click", "id": "t-calc"}], {"GET /api/trades": ok(summary()), **route})


def test_the_calculator_sends_only_what_was_filled_in_and_shows_the_lots_and_the_limit_that_decides(tmp_path, base):
    st = calc(tmp_path, base, {"GET /api/risk*": ok(RISK)}, entry="100", stop="80", lot="65")
    (req,) = requests(st, "GET", "/api/risk")
    assert req["url"] == "/api/risk?entry=100&stop=80&lot=65"  # no capital / risk_pct / max_loss: the server's own limits apply
    out = words(st["els"]["risk-out"]["html"])
    assert "1 lot (65 units): loss at the stop Rs 1,300, premium outlay Rs 6,500 (6.5% of capital). Decided by your per-trade limit (Rs 2,000)." in out
    assert "One lot (65 units) loses Rs 1,300 at your stop (100.00 to 80.00) and costs Rs 6,500 to buy." in out and "per-trade 1 lot(s), today 3 lot(s)" in out and "You decide." in out


def test_overrides_are_sent_and_the_daily_limit_can_be_the_one_that_decides(tmp_path, base):
    daily = {**RISK, "binding": "lots_by_daily_limit", "left_today": 1100.0, "lots": 0, "quantity": 0, "loss_at_stop": 0.0, "outlay": 0.0, "outlay_pct_of_capital": 0.0,
             "warnings": ["not even one lot fits: one lot loses Rs 1,300 at that stop, more than what is left of today's loss limit allows"]}  # fmt: skip
    st = calc(tmp_path, base, {"GET /api/risk*": ok(daily)}, entry="100", stop="80", lot="65", capital="200000", pct="1.5", max="9000")
    assert requests(st, "GET", "/api/risk")[0]["url"] == "/api/risk?entry=100&stop=80&lot=65&capital=200000&risk_pct=1.5&max_loss=9000"
    out = words(st["els"]["risk-out"]["html"])
    assert "0 lots (0 units)" in out and "Decided by what is left of today's loss limit (Rs 1,100)" in out and "⚠ not even one lot fits" in out


def test_with_no_limit_set_the_calculator_gives_the_loss_per_lot_and_says_how_to_set_one(tmp_path, base):
    bare = {**RISK, "lots": None, "quantity": None, "loss_at_stop": None, "outlay": None, "outlay_pct_of_capital": None, "binding": None, "per_trade_loss": None, "max_daily_loss": None,
            "lots_by_trade_limit": None, "lots_by_daily_limit": None, "left_today": None, "warnings": ["no limit is set: set RISK_CAPITAL and RISK_PER_TRADE_PCT"]}  # fmt: skip
    out = words(calc(tmp_path, base, {"GET /api/risk*": ok(bare)}, entry="100", stop="80", lot="65")["els"]["risk-out"]["html"])
    assert "One lot (65 units) loses Rs 1,300" in out and "⚠ no limit is set: set RISK_CAPITAL" in out and "lot(s)" not in out


def test_a_refusal_from_the_server_is_shown_in_plain_words(tmp_path, base):
    out = words(calc(tmp_path, base, {"GET /api/risk*": {"status": 422, "body": {"detail": "the stop premium must be below the entry premium"}}}, entry="80", stop="100", lot="65")["els"]["risk-out"]["html"])
    assert "the stop premium must be below the entry premium" in out
    out = words(calc(tmp_path, base, {"GET /api/risk*": {"status": 422, "body": {"detail": [{"loc": ["query", "entry"], "msg": "field required"}]}}})["els"]["risk-out"]["html"])
    assert "fill in the entry premium, the stop premium and the lot size (numbers)" in out


def test_an_old_server_with_no_calculator_says_to_restart(tmp_path, base):
    out = words(calc(tmp_path, base, {"GET /api/risk*": {"status": 404, "body": {"detail": "Not Found"}}}, entry="100", stop="80", lot="65")["els"]["risk-out"]["html"])
    assert "no calculator yet (restart Prism" in out


def test_the_lot_size_is_filled_in_from_the_contracts_being_watched_until_you_type_another(tmp_path, base):
    snap = copy.deepcopy(base)
    lots = {i.get("lot_size") for i in snap["instruments"]}
    assert lots - {None}, "the base snapshot has option contracts with a lot size"
    expected = str(next(i["lot_size"] for i in snap["instruments"] if i.get("lot_size")))
    st = go(tmp_path, snap, [TAB, {"op": "eval", "code": "document.getElementById('probe').textContent = $('r-lot').value"}], {"GET /api/trades": ok(summary())})
    assert st["els"]["probe"]["text"] == expected
    st = go(tmp_path, snap, [TAB, {"op": "eval", "code": "$('r-lot').value = '50'"}, {"op": "message", "snap": snap}, {"op": "eval", "code": "document.getElementById('probe').textContent = $('r-lot').value"}], {"GET /api/trades": ok(summary())})
    assert st["els"]["probe"]["text"] == "50"


# -- escaping ----------------------------------------------------------------------------------------------------------------------------------
EVIL = '<img src=x onerror="alert(1)">'


def test_everything_the_server_says_is_escaped(tmp_path, base):
    t = trade("t1-1", instrument=EVIL, note=EVIL, setup_id=EVIL, prism={"action": EVIL, "setup_id": None, "price": 1.0}, followed=EVIL)
    s = summary([t], open_=[{"instrument": EVIL, "qty": 1, "average_price": 1.0}], file=EVIL, basis=EVIL)
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(s)})
    assert "<img" not in st["els"]["trades-body"]["html"] and "&lt;img" in st["els"]["trades-body"]["html"]
    st = go(tmp_path, base, [TAB, {"op": "click", "id": "t-log"}], {"GET /api/trades": ok(summary([t])), "POST /api/trades": {"status": 422, "body": {"detail": EVIL}}})
    assert st["els"]["t-msg"]["text"] == "not logged: " + EVIL  # (set as text: it is never parsed as markup)
    st = calc(tmp_path, base, {"GET /api/risk*": ok({**RISK, "warnings": [EVIL]})}, entry="1", stop="0.5", lot="1")
    assert "<img" not in st["els"]["risk-out"]["html"] and "&lt;img" in st["els"]["risk-out"]["html"]
    st = calc(tmp_path, base, {"GET /api/risk*": {"status": 422, "body": {"detail": EVIL}}}, entry="1", stop="0.5", lot="1")
    assert "<img" not in st["els"]["risk-out"]["html"]
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(summary([t], reached=True, lost=1.0, limits={**LIMITS, "max_daily_loss": 1.0}))})
    assert "<img" not in st["els"]["alerts"]["html"]


def test_the_trade_ids_in_the_void_buttons_are_escaped(tmp_path, base):
    st = go(tmp_path, base, [TAB], {"GET /api/trades": ok(summary([trade('t1-1" onclick="x')]))})
    assert 'data-void="t1-1&quot; onclick=&quot;x"' in st["els"]["trades-body"]["html"]


# -- the page as a whole ------------------------------------------------------------------------------------------------------------------------
def test_the_trades_tab_is_hidden_content_when_the_link_is_lost_and_the_page_has_no_script_error(tmp_path, base):
    st = go(tmp_path, base, [TAB, {"op": "close"}, {"op": "tab", "name": "overview"}], {"GET /api/trades": ok(summary([trade()]))})  # (the open tab keeps what it showed, dimmed; the others are cleared)
    assert st["conn"] == "stale" and st["els"]["trades-body"]["html"] == "" and st["els"]["jserr"]["text"] == "" and st["errors"] == []


def test_no_script_error_anywhere_in_a_full_session_of_the_tab(tmp_path, base):
    fetch = {"GET /api/trades": ok(summary([trade()])), "POST /api/trades": ok({"ok": True, "trade": trade(), "summary": summary([trade()])}), "GET /api/risk*": ok(RISK)}
    steps = [TAB, typed(instr="x", qty="1", price="1"), {"op": "click", "id": "t-log"}, {"op": "click", "id": "t-calc"}, {"op": "tab", "name": "journal"}, TAB, {"op": "click", "id": "t-use-setup"}]
    st = go(tmp_path, base, steps, fetch)
    assert st["errors"] == [] and st["els"]["jserr"]["text"] == ""
