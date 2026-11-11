"""Stage 8: the notebook of the trades you made yourself (app/trades.py), the size calculator (app/risk.py) and their routes.

Prism places no orders and reads no account. Nothing here touches a broker; the journal folder is a temporary one. What is held:

  * a trade is checked field by field; a SELL needs an open quantity; matching is first in first out per instrument, ignoring case and spacing
  * the file is only ever appended to: a mistake is voided by a line that names it, and a void that would leave a later sell with nothing to sell is refused
  * the realised result, the open positions, the daily-loss line and "did you follow Prism" are right, and a file that does not add up is said so, not summed
  * the calculator's lots, the limit that binds, and its warnings; limits come from RISK_* and are never invented
  * the two writes refuse a request that comes from another site or is addressed to a name that is not this computer; a refused request writes nothing
  * a GET never writes anything"""

import asyncio
import json
import math
from datetime import datetime

import pytest
from helpers import CFG, Clock, ist

from app import risk, trades
from app.candles import IST
from app.engine import MarketData
from app.main import create_app
from app.risk import Limits, limits_from_env, size
from app.trades import TradeLog, clean, followed, positions


def log(tmp_path, at=None):
    clock = Clock(at if at is not None else ist(10, 0))
    return TradeLog(tmp_path, clock), clock


def buy(instr="NIFTY 24500 CE", qty=65, price=100.0, **kw):
    return {"instrument": instr, "side": "BUY", "qty": qty, "price": price, **kw}


def sell(instr="NIFTY 24500 CE", qty=65, price=120.0, **kw):
    return {"instrument": instr, "side": "SELL", "qty": qty, "price": price, **kw}


# ====================================================================================================================
# 1. A trade is checked
# ====================================================================================================================
def test_a_good_trade_is_cleaned_and_a_number_may_arrive_as_text():
    assert clean({"instrument": "  NIFTY 24500 CE ", "side": "buy", "qty": "65", "price": "101.5", "setup_id": "x:bull:1", "note": " first "}) == {
        "instrument": "NIFTY 24500 CE", "side": "BUY", "qty": 65, "price": 101.5, "setup_id": "x:bull:1", "note": "first"}
    assert clean(buy(qty=65.0))["qty"] == 65 and clean(buy())["setup_id"] is None and clean(buy(note=""))["note"] is None


@pytest.mark.parametrize("bad, said", [
    (None, "object"), ([], "object"), ("x", "object"),
    ({**buy(), "instrument": ""}, "instrument is required"), ({**buy(), "instrument": "   "}, "instrument is required"), ({**buy(), "instrument": 5}, "instrument must be text"),
    ({**buy(), "instrument": "x" * 61}, "longer than 60"), ({**buy(), "side": "HOLD"}, "BUY or SELL"), ({**buy(), "side": None}, "side is required"),
    ({**buy(), "qty": 0}, "qty must be a positive"), ({**buy(), "qty": -65}, "qty must be a positive"), ({**buy(), "qty": 6.5}, "qty must be a whole"),
    ({**buy(), "qty": "lots"}, "qty must be a number"), ({**buy(), "qty": True}, "qty must be a positive"), ({**buy(), "qty": 10**7}, "qty is over"),
    ({**buy(), "price": 0}, "price must be a positive"), ({**buy(), "price": -1}, "price must be a positive"), ({**buy(), "price": float("nan")}, "price must be a positive"),
    ({**buy(), "price": float("inf")}, "price must be a positive"), ({**buy(), "price": "abc"}, "price must be a number"), ({**buy(), "price": 10**7}, "price is over"),
    ({**buy(), "note": "n" * 301}, "longer than 300"), ({k: v for k, v in buy().items() if k != "qty"}, "qty must be"),
])
def test_a_trade_that_is_not_one_is_refused_with_the_field_and_why(bad, said):
    with pytest.raises(ValueError, match=said):
        clean(bad)


def test_control_characters_in_text_cannot_break_the_file_or_the_page():
    c = clean(buy(instr="NIFTY\n24500\tCE\x00", note="a\r\nb"))
    assert c["instrument"] == "NIFTY 24500 CE " .strip() or "\n" not in c["instrument"]
    assert "\n" not in c["instrument"] and "\t" not in c["instrument"] and "\x00" not in c["instrument"] and "\n" not in c["note"] and "\r" not in c["note"]


# ====================================================================================================================
# 2. First in, first out; the result
# ====================================================================================================================
def test_one_buy_and_one_sell_make_a_realised_result(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy(qty=65, price=100.0))
    book.add(sell(qty=65, price=120.0))
    s = book.summary()
    assert s["realised"] == pytest.approx(1300.0) and s["open"] == [] and s["lost_today"] == 0.0 and s["available"] is True
    assert [t["pnl"] for t in s["trades"]] == [None, pytest.approx(1300.0)]  # only the sell carries the result: a column of pnl adds up to the realised figure
    assert sum(t["pnl"] for t in s["trades"] if t["pnl"] is not None) == pytest.approx(s["realised"]) and s["trades"][0]["lot_pnl"] == pytest.approx(1300.0) and s["trades"][1]["lot_pnl"] is None


def test_sells_match_the_oldest_buys_first_and_a_partial_sell_leaves_the_rest_open(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy(qty=65, price=100.0))
    book.add(buy(qty=65, price=110.0))
    book.add(sell(qty=90, price=105.0))  # 65 against the 100 buy (+325), 25 against the 110 buy (-125)
    s = book.summary()
    assert s["realised"] == pytest.approx(200.0)
    assert s["open"] == [{"instrument": "NIFTY 24500 CE", "qty": 40, "average_price": pytest.approx(110.0)}]
    first, second, last = s["trades"]
    assert first["matched"] == 65 and first["lot_pnl"] == pytest.approx(325.0) and second["matched"] == 25 and second["lot_pnl"] == pytest.approx(-125.0)
    assert last["pnl"] == pytest.approx(200.0) and first["pnl"] is None and second["pnl"] is None


def test_the_average_price_of_what_is_open_is_by_quantity(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy(qty=65, price=100.0))
    book.add(buy(qty=130, price=130.0))
    (o,) = book.summary()["open"]
    assert o["qty"] == 195 and o["average_price"] == pytest.approx((65 * 100 + 130 * 130) / 195)


def test_instruments_are_matched_ignoring_case_and_spacing_and_kept_apart_otherwise(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy("NIFTY 24500 CE", 65, 100.0))
    book.add(buy("NIFTY 24500 PE", 65, 90.0))
    book.add(sell("  nifty   24500 ce ", 65, 110.0))
    s = book.summary()
    assert s["realised"] == pytest.approx(650.0) and [o["instrument"] for o in s["open"]] == ["NIFTY 24500 PE"]
    with pytest.raises(ValueError, match="nothing to sell"):
        book.add(sell("NIFTY 24500 CE", 65, 110.0))  # that one is closed


def test_a_sell_with_nothing_to_sell_or_more_than_is_open_is_refused_and_writes_nothing(tmp_path):
    book, _ = log(tmp_path)
    with pytest.raises(ValueError, match="nothing to sell: NIFTY 24500 CE has 0 unit"):
        book.add(sell())
    assert not book.path(ist(10, 0)).exists()
    book.add(buy(qty=65))
    before = book.path(ist(10, 0)).read_bytes()
    with pytest.raises(ValueError, match="has 65 unit"):
        book.add(sell(qty=66))
    assert book.path(ist(10, 0)).read_bytes() == before


def test_a_loss_is_a_loss_and_the_daily_limit_is_reported(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy(qty=65, price=100.0))
    book.add(sell(qty=65, price=80.0))  # -1300
    s = book.summary(Limits(capital=100000, per_trade_pct=1, max_daily_loss=2000))
    assert s["realised"] == pytest.approx(-1300.0) and s["lost_today"] == pytest.approx(1300.0)
    assert s["daily_loss_used_pct"] == pytest.approx(65.0) and s["daily_loss_reached"] is False
    book.add(buy(qty=65, price=100.0))
    book.add(sell(qty=65, price=90.0))  # -650 more: 1950
    assert book.summary(Limits(max_daily_loss=2000))["daily_loss_reached"] is False
    book.add(buy(qty=65, price=100.0))
    book.add(sell(qty=65, price=99.0))
    s = book.summary(Limits(max_daily_loss=2000))
    assert s["lost_today"] == pytest.approx(2015.0) and s["daily_loss_reached"] is True and s["daily_loss_used_pct"] > 100
    assert book.summary()["daily_loss_reached"] is False and book.summary()["daily_loss_used_pct"] is None  # no limit set: nothing is invented


def test_a_profitable_day_has_no_loss_to_count(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy())
    book.add(sell(price=150.0))
    assert book.summary(Limits(max_daily_loss=1000))["lost_today"] == 0.0


# ====================================================================================================================
# 3. Append-only: a mistake is voided
# ====================================================================================================================
def test_the_file_is_only_appended_to_and_one_line_is_one_trade(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy())
    first = book.path(ist(10, 0)).read_bytes()
    book.add(sell())
    both = book.path(ist(10, 0)).read_bytes()
    assert both.startswith(first) and both.count(b"\n") == 2 and first.count(b"\n") == 1
    lines = [json.loads(x) for x in both.decode().splitlines()]
    assert [x["kind"] for x in lines] == ["trade", "trade"] and all(x["v"] == 1 and x["id"].startswith("t") for x in lines)
    assert len({x["id"] for x in lines}) == 2 and lines[0]["time"].endswith("+05:30")


def test_a_void_adds_a_line_and_removes_the_trade_from_the_arithmetic(tmp_path):
    book, _ = log(tmp_path)
    rec = book.add(buy(price=100.0))
    before = book.path(ist(10, 0)).read_bytes()
    book.void(rec["id"])
    after = book.path(ist(10, 0)).read_bytes()
    assert after.startswith(before) and after.count(b"\n") == 2  # the buy is still in the file
    s = book.summary()
    assert s["trades"] == [] and s["open"] == [] and s["voided"] == [rec["id"]]


def test_voiding_something_that_is_not_there_or_twice_is_refused(tmp_path):
    book, _ = log(tmp_path)
    rec = book.add(buy())
    with pytest.raises(ValueError, match="no such trade"):
        book.void("t0-0")
    book.void(rec["id"])
    with pytest.raises(ValueError, match="no such trade today, or it is already voided"):
        book.void(rec["id"])


def test_a_buy_that_a_later_sell_depends_on_cannot_be_voided_until_the_sell_is(tmp_path):
    book, _ = log(tmp_path)
    b, s = book.add(buy()), book.add(sell())
    with pytest.raises(ValueError, match="void the sell first"):
        book.void(b["id"])
    assert len(book.summary()["trades"]) == 2
    book.void(s["id"])
    assert book.summary()["open"][0]["qty"] == 65  # the position is open again
    book.void(b["id"])
    assert book.summary()["trades"] == []


def test_a_voided_buy_that_another_buy_covers_may_go(tmp_path):
    book, _ = log(tmp_path)
    b1, _b2 = book.add(buy(qty=65)), book.add(buy(qty=65))
    book.add(sell(qty=65))
    book.void(b1["id"])  # the second buy still covers the sell
    assert book.summary()["open"] == []


# ====================================================================================================================
# 4. What Prism said, and whether you followed it
# ====================================================================================================================
def test_a_buy_keeps_what_prism_said_and_a_sell_does_not(tmp_path):
    book, _ = log(tmp_path)
    prism = {"action": "BUY CE", "setup_id": "trend_pullback:bull:1", "strategy": "trend_pullback", "price": 24505.0}
    b = book.add(buy(setup_id="trend_pullback:bull:1"), prism)
    s = book.add(sell(), prism)
    assert b["prism"] == prism and s["prism"] is None


@pytest.mark.parametrize("action, named, expected", [("BUY CE", "trend_pullback:bull:1", "yes: the setup you named"), ("BUY CE", "something:else", "yes"), ("BUY PE", None, "yes"),
                                                     ("WAIT", None, "no"), ("NO TRADE", "trend_pullback:bull:1", "no")])
def test_followed_says_whether_prism_called_a_buy_when_you_bought(action, named, expected):
    r = {"side": "BUY", "setup_id": named, "prism": {"action": action, "setup_id": "trend_pullback:bull:1"}}
    assert followed(r) == expected


def test_followed_is_unknown_for_a_sell_or_when_nothing_was_recorded():
    assert followed({"side": "SELL", "prism": {"action": "BUY CE"}}) is None and followed({"side": "BUY", "prism": None}) is None and followed({"side": "BUY"}) is None
    assert followed({"side": "BUY", "prism": {"action": None}}) is None


# ====================================================================================================================
# 5. The file, the day, odd files
# ====================================================================================================================
def test_each_day_has_its_own_file_by_the_ist_date_and_a_new_day_starts_empty(tmp_path):
    book, clock = log(tmp_path, ist(23, 59, 0, day=(2026, 11, 6)))
    book.add(buy())
    assert book.path(clock.t).name == "trades-2026-11-06.jsonl"
    clock.t = ist(0, 1, 0, day=(2026, 11, 7))
    assert book.summary()["trades"] == [] and book.summary()["file"] == "trades-2026-11-07.jsonl"
    with pytest.raises(ValueError, match="nothing to sell"):
        book.add(sell())  # yesterday's position is not today's (these are intraday trades)


def test_a_line_cut_short_by_a_crash_is_skipped_and_the_rest_is_kept(tmp_path):
    book, clock = log(tmp_path)
    book.add(buy())
    with open(book.path(clock.t), "a", encoding="utf-8") as f:
        f.write('{"v":1,"kind":"trade","id":"t9","instrument":"NIF\n')
    book.add(sell())
    s = book.summary()
    assert len(s["trades"]) == 2 and s["realised"] == pytest.approx(1300.0)


def test_a_file_that_does_not_add_up_is_said_not_summed(tmp_path):
    book, clock = log(tmp_path)
    book.dir.mkdir(parents=True, exist_ok=True)
    rec = {"v": 1, "kind": "trade", "id": "t1-1", "t": clock.t, "time": "x", **sell(), "prism": None}
    book.path(clock.t).write_text(json.dumps(rec) + "\n", encoding="utf-8")  # a sell with no buy: only a hand edit gets here
    s = book.summary()
    assert s["available"] is False and "does not add up" in s["reason"] and "realised" not in s


def test_a_summary_with_no_file_writes_nothing(tmp_path):
    book, clock = log(tmp_path / "nothing-here")
    assert book.summary()["trades"] == [] and not (tmp_path / "nothing-here").exists()


def test_the_summary_is_strict_json(tmp_path):
    book, _ = log(tmp_path)
    book.add(buy())
    book.add(sell())
    json.dumps(book.summary(Limits(100000, 1, 2000)), allow_nan=False)


def test_two_threads_logging_at_once_never_interleave_a_line(tmp_path):
    import threading

    book, _ = log(tmp_path)
    errors = []

    def work():
        try:
            for _ in range(20):
                book.add(buy(qty=1, price=10.0))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = book.path(ist(10, 0)).read_text(encoding="utf-8").splitlines()
    assert not errors and len(lines) == 80 and all(json.loads(x)["kind"] == "trade" for x in lines) and len({json.loads(x)["id"] for x in lines}) == 80


# ====================================================================================================================
# 6. The calculator
# ====================================================================================================================
def test_limits_come_from_the_environment_and_a_limit_nobody_set_is_not_invented():
    assert limits_from_env({}) == Limits(None, None, None) and Limits().per_trade_loss is None
    got = limits_from_env({"RISK_CAPITAL": "200000", "RISK_PER_TRADE_PCT": "1.5", "RISK_MAX_DAILY_LOSS": " 5000 "})
    assert got == Limits(200000.0, 1.5, 5000.0) and got.per_trade_loss == pytest.approx(3000.0)
    assert limits_from_env({"RISK_CAPITAL": "", "RISK_PER_TRADE_PCT": "  "}) == Limits()  # blank is unset
    assert Limits(100000.0, None, 5000.0).per_trade_loss is None  # a percentage nobody gave


@pytest.mark.parametrize("env, name", [({"RISK_CAPITAL": "lots"}, "RISK_CAPITAL"), ({"RISK_PER_TRADE_PCT": "0"}, "RISK_PER_TRADE_PCT"), ({"RISK_MAX_DAILY_LOSS": "-5"}, "RISK_MAX_DAILY_LOSS"),
                                       ({"RISK_CAPITAL": "nan"}, "RISK_CAPITAL"), ({"RISK_CAPITAL": "inf"}, "RISK_CAPITAL"), ({"RISK_PER_TRADE_PCT": "150"}, "RISK_PER_TRADE_PCT")])
def test_a_limit_that_is_not_a_usable_number_is_an_error_that_names_it(env, name):
    with pytest.raises(ValueError, match=name):
        limits_from_env(env)


L = Limits(capital=100_000.0, per_trade_pct=2.0, max_daily_loss=5_000.0)  # one trade may lose 2000, the day 5000


def test_lots_are_what_the_loss_at_the_stop_allows():
    r = size(100.0, 80.0, 65, L)  # 20 points x 65 = 1300 a lot: 2000 // 1300 = 1 lot by the trade limit; 5000 // 1300 = 3 by the day
    assert (r["loss_per_unit"], r["loss_per_lot"], r["outlay_per_lot"]) == (20.0, 1300.0, 6500.0)
    assert (r["lots_by_trade_limit"], r["lots_by_daily_limit"], r["lots"], r["binding"], r["quantity"]) == (1, 3, 1, "lots_by_trade_limit", 65)
    assert r["loss_at_stop"] == 1300.0 and r["outlay"] == 6500.0 and r["outlay_pct_of_capital"] == pytest.approx(6.5) and r["warnings"] == []


def test_the_daily_limit_binds_when_part_of_it_is_gone():
    r = size(100.0, 90.0, 65, L, lost_today=4_000.0)  # 650 a lot; 2000 // 650 = 3 by the trade limit; (5000 - 4000) // 650 = 1 by the day
    assert (r["lots_by_trade_limit"], r["lots_by_daily_limit"], r["lots"], r["binding"], r["left_today"]) == (3, 1, 1, "lots_by_daily_limit", 1000.0)


def test_no_lot_fits_says_which_limit_and_what_one_lot_would_lose():
    r = size(100.0, 50.0, 65, L)  # 3250 a lot: more than 2000
    assert r["lots"] == 0 and r["quantity"] == 0 and r["loss_at_stop"] == 0 and "not even one lot fits" in r["warnings"][0] and "Rs 3,250" in r["warnings"][0] and "per-trade limit" in r["warnings"][0]
    r = size(100.0, 90.0, 65, L, lost_today=4_800.0)
    assert r["lots"] == 0 and "today's loss limit" in r["warnings"][0]


def test_a_day_that_has_used_its_limit_up_says_so():
    r = size(100.0, 90.0, 65, L, lost_today=9_000.0)
    assert r["left_today"] == 0.0 and r["lots"] == 0 and any("already used up" in w for w in r["warnings"])


def test_with_no_limit_set_it_says_how_to_set_one_and_gives_no_lots():
    r = size(100.0, 90.0, 65, Limits())
    assert r["lots"] is None and r["quantity"] is None and r["loss_per_lot"] == 650.0 and "RISK_CAPITAL" in r["warnings"][0]


def test_with_only_a_daily_limit_or_only_a_trade_limit_the_one_that_exists_decides():
    assert size(100.0, 90.0, 65, Limits(max_daily_loss=2000.0))["lots"] == 3 and size(100.0, 90.0, 65, Limits(capital=100000.0, per_trade_pct=1.0))["lots"] == 1
    assert size(100.0, 90.0, 65, Limits(max_daily_loss=2000.0))["outlay_pct_of_capital"] is None


def test_paying_more_than_the_capital_is_warned():
    r = size(1000.0, 999.0, 65, Limits(capital=50_000.0, per_trade_pct=100.0, max_daily_loss=None))  # 65 a lot: lots = 50000 // 65 = 769; outlay 50 million
    assert any("more than your capital" in w for w in r["warnings"])


def test_a_stop_at_or_above_the_entry_a_zero_or_odd_lot_or_a_nonsense_number_is_refused():
    for args, said in [((100.0, 100.0, 65), "below the entry"), ((100.0, 110.0, 65), "below the entry"), ((100.0, 0.0, 65), "stop premium"), ((0.0, -1.0, 65), "entry premium"),
                       ((100.0, 90.0, 0), "lot size"), ((100.0, 90.0, 65.5), "whole number"), ((float("nan"), 90.0, 65), "entry premium"), ((100.0, 90.0, float("inf")), "lot size"),
                       ((True, 0.5, 65), "entry premium"), (("100", 90.0, 65), "entry premium")]:
        with pytest.raises(ValueError, match=said):
            size(*args, L)
    assert size(100.0, 90.0, 65.0, L)["lot_size"] == 65  # a whole number that arrives as a float is fine


def test_the_answer_is_strict_json():
    for r in (size(100.0, 90.0, 65, L), size(100.0, 90.0, 65, Limits())):
        json.dumps(r, allow_nan=False)
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in size(100.0, 90.0, 65, L).values())


def test_the_calculator_has_no_order_code():
    import re

    for mod in (risk, trades):
        src = open(mod.__file__, encoding="utf-8").read()
        assert not re.search(r"placeOrder|place_order|modifyOrder|cancelOrder|SmartConnect|generateSession|requests\.|socket", src), mod.__name__


# ====================================================================================================================
# 7. The routes
# ====================================================================================================================
def make_app(tmp_path, monkeypatch, at=None, md=None):
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    for n in ("RISK_CAPITAL", "RISK_PER_TRADE_PCT", "RISK_MAX_DAILY_LOSS", "PRISM_ALLOW_REMOTE"):
        monkeypatch.delenv(n, raising=False)
    clock = Clock(at if at is not None else ist(10, 0))
    md = md or MarketData(CFG, clock)
    app = create_app(md, feed=None)
    app.state.md = md
    return app, md


def call(app, method, path, body=None, headers=None, query=""):
    """The app as an ASGI server calls it: (status, parsed body)."""
    hs = {"host": "127.0.0.1:8000", **(headers or {})}
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    if body is not None and "content-type" not in hs:
        hs["content-type"] = "application/json"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "headers": [(k.encode(), v.encode()) for k, v in hs.items()], "root_path": "", "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 50000)}  # fmt: skip
    out, sent = {"status": None, "body": b""}, False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], json.loads(out["body"] or b"null")


def files(tmp_path):
    return sorted(p.name for p in tmp_path.glob("trades-*.jsonl"))


def test_a_trade_is_logged_through_the_route_and_stamped_with_what_prism_said(tmp_path, monkeypatch):
    from dashboard_harness import base_engine

    md, clock = base_engine()  # the Phase 3 world with a real BUY CE and a spot tick
    app, _ = make_app(tmp_path, monkeypatch, md=md)
    status, body = call(app, "POST", "/api/trades", buy(setup_id=md.safe_trading()["decision"]["setup_id"]))
    assert status == 200 and body["ok"] is True
    p = body["trade"]["prism"]
    assert p["action"] == "BUY CE" and p["setup_id"] and p["strategy"] == "trend_pullback" and isinstance(p["price"], float)
    assert body["summary"]["trades"][0]["followed"] == "yes: the setup you named" and len(body["summary"]["open"]) == 1
    s, again = call(app, "GET", "/api/trades")
    assert s == 200 and again["trades"][0]["id"] == body["trade"]["id"]


def test_logging_a_trade_does_not_advance_the_decision_or_write_the_journal(tmp_path, monkeypatch):
    from dashboard_harness import base_engine

    md, clock = base_engine()
    app, _ = make_app(tmp_path, monkeypatch, md=md)
    md.safe_trading()
    out, listing = md._last_out, sorted(p.name for p in tmp_path.iterdir())
    call(app, "POST", "/api/trades", buy())
    assert md._last_out is out  # the decision was read, not evaluated again
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(listing + ["trades-2026-11-06.jsonl"])  # the only new file is the trade file: no journal line


def test_a_sell_and_a_void_through_the_routes(tmp_path, monkeypatch):
    app, md = make_app(tmp_path, monkeypatch)
    _, b = call(app, "POST", "/api/trades", buy(price=100.0))
    _, s = call(app, "POST", "/api/trades", sell(price=110.0))
    assert s["summary"]["realised"] == pytest.approx(650.0) and s["trade"]["prism"] is None
    code, err = call(app, "POST", f"/api/trades/{b['trade']['id']}/void")
    assert code == 422 and "void the sell first" in err["detail"]
    code, ok = call(app, "POST", f"/api/trades/{s['trade']['id']}/void")
    assert code == 200 and ok["summary"]["realised"] == 0 and ok["summary"]["open"][0]["qty"] == 65


def test_a_bad_trade_is_a_422_that_says_why_and_writes_nothing(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    for body, said in [({**buy(), "qty": 0}, "qty must be a positive"), (sell(), "nothing to sell"), (["x"], "object"), ("text", "object")]:
        code, err = call(app, "POST", "/api/trades", body)
        assert code == 422 and said in str(err["detail"]), body
    code, err = call(app, "POST", "/api/trades", b"{not json")
    assert code == 422
    assert files(tmp_path) == []


@pytest.mark.parametrize("headers", [
    {"origin": "http://evil.example"}, {"origin": "https://127.0.0.1:8000"}, {"origin": "http://127.0.0.1:9999"}, {"origin": "null"}, {"origin": "http://localhost:8000"},
    {"sec-fetch-site": "cross-site"}, {"host": "evil.example:8000", "origin": "http://evil.example:8000"}, {"host": "192.168.1.20:8000"}, {"host": "my-pc:8000"}, {"host": ""}])
def test_a_write_from_another_site_or_to_another_name_is_refused_and_writes_nothing(tmp_path, monkeypatch, headers):
    app, _ = make_app(tmp_path, monkeypatch)
    code, err = call(app, "POST", "/api/trades", buy(), headers)
    assert code == 403 and "refused" in err["detail"], headers
    code, err = call(app, "POST", "/api/trades/t1-1/void", None, headers)
    assert code == 403
    assert files(tmp_path) == []


@pytest.mark.parametrize("headers", [{}, {"origin": "http://127.0.0.1:8000"}, {"host": "localhost:8000", "origin": "http://localhost:8000"}, {"host": "[::1]:8000", "origin": "http://[::1]:8000"},
                                     {"host": "LOCALHOST:8000"}, {"sec-fetch-site": "same-origin"}, {"host": "127.0.0.1"}, {"origin": "http://127.0.0.1:8000", "sec-fetch-site": "same-origin"}])
def test_a_write_from_this_computers_own_page_or_a_script_on_it_is_accepted(tmp_path, monkeypatch, headers):
    app, _ = make_app(tmp_path, monkeypatch)
    code, body = call(app, "POST", "/api/trades", buy(), headers)
    assert code == 200, (headers, body)


def test_the_owner_who_opened_prism_to_the_network_can_write_from_a_named_host(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    code, _ = call(app, "POST", "/api/trades", buy(), {"host": "192.168.1.20:8000", "origin": "http://192.168.1.20:8000"})
    assert code == 403
    monkeypatch.setenv("PRISM_ALLOW_REMOTE", "1")
    code, _ = call(app, "POST", "/api/trades", buy(), {"host": "192.168.1.20:8000", "origin": "http://192.168.1.20:8000"})
    assert code == 200
    code, _ = call(app, "POST", "/api/trades", buy(), {"host": "192.168.1.20:8000", "origin": "http://evil.example"})
    assert code == 403  # even then a page from another site is refused


def test_a_get_never_writes(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    for path, q in (("/api/trades", ""), ("/api/risk", "entry=100&stop=90&lot=65")):
        code, _ = call(app, "GET", path, None, None, q)
        assert code == 200
    assert list(tmp_path.iterdir()) == []


def test_the_risk_route_uses_the_environment_limits_the_days_loss_and_lets_a_page_override(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    monkeypatch.setenv("RISK_CAPITAL", "100000")
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "2")
    monkeypatch.setenv("RISK_MAX_DAILY_LOSS", "5000")
    code, r = call(app, "GET", "/api/risk", query="entry=100&stop=80&lot=65")
    assert code == 200 and (r["lots"], r["binding"], r["per_trade_loss"], r["max_daily_loss"]) == (1, "lots_by_trade_limit", 2000.0, 5000.0)
    call(app, "POST", "/api/trades", buy(price=100.0))
    call(app, "POST", "/api/trades", sell(price=40.0))  # -3900 realised
    code, r = call(app, "GET", "/api/risk", query="entry=100&stop=90&lot=65")
    assert r["lost_today"] == pytest.approx(3900.0) and r["left_today"] == pytest.approx(1100.0) and r["lots_by_daily_limit"] == 1 and r["binding"] == "lots_by_daily_limit"
    code, r = call(app, "GET", "/api/risk", query="entry=100&stop=90&lot=65&capital=1000000&risk_pct=5&max_loss=100000")
    assert r["per_trade_loss"] == 50000.0 and r["max_daily_loss"] == 100000.0 and r["lots"] == 76


@pytest.mark.parametrize("query, said", [("entry=100&stop=100&lot=65", "below the entry"), ("entry=100&stop=90&lot=0", "lot size"), ("entry=100&stop=90&lot=65&capital=-5", "capital"),
                                         ("entry=100&stop=90&lot=65&risk_pct=0", "risk_pct"), ("entry=abc&stop=90&lot=65", ""), ("stop=90&lot=65", "")])
def test_the_risk_route_refuses_nonsense_with_a_422(tmp_path, monkeypatch, query, said):
    app, _ = make_app(tmp_path, monkeypatch)
    code, err = call(app, "GET", "/api/risk", query=query)
    assert code == 422 and said in json.dumps(err)


def test_a_risk_setting_that_is_not_a_number_is_a_500_that_names_it(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    monkeypatch.setenv("RISK_CAPITAL", "a lot")
    for path, q in (("/api/trades", ""), ("/api/risk", "entry=100&stop=90&lot=65")):
        code, err = call(app, "GET", path, None, None, q)
        assert code == 500 and "RISK_CAPITAL" in err["detail"]


def test_the_trade_file_lives_beside_the_journal_when_there_is_one(tmp_path, monkeypatch):
    from app.journal import Journal

    app, md = make_app(tmp_path / "ignored", monkeypatch)
    md.journal = Journal(tmp_path / "j")
    call(app, "POST", "/api/trades", buy())
    assert (tmp_path / "j").exists() and files(tmp_path / "j") == ["trades-2026-11-06.jsonl"] and not (tmp_path / "ignored").exists()
