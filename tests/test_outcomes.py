"""The outcome review (app/outcomes.py): the setups Prism called ready, replayed against the day's 1-minute candles. Hand-made journal records and candles only; nothing touches a broker, and
the database is a fake. What is held:

  * the entry is the price on the journal record of the FIRST moment the setup was ready, with the levels it showed then; the replay starts at the next whole minute
  * the first of invalidation / target 1 / "Prism ended the setup" / the close decides the outcome; a candle that touches both is the invalidation (pessimistic), and says so
  * a long (CE) and a short (PE) are mirror images; R is measured in units of the risk taken at the entry; unresolved trades are marked at the last close
  * a target the price had already passed is not a target; a price already through the invalidation has no risk to measure
  * the summary counts and averages are right; setups that were never ready are listed, not replayed
  * the review reads and prints: it never changes the journal, and a database that is down costs the replay, not the review"""

import hashlib
import json
import re
from datetime import date, datetime

import pytest
from helpers import FRI, ist
from test_history import PG, FakeConn

from app import outcomes
from app.candles import IST
from app.outcomes import candles_from_db, candles_from_file, lifecycles, main, replay, review, summarise

DAY = FRI  # 2026-11-06


def t(h, m, s=0):
    return ist(h, m, s, day=DAY)


def setup(sid="trend_pullback:bull:1", direction="CE", stage="READY_TO_ACT", inv=24970.0, trigger=25000.0, targets=(25050.0, 25100.0), name="Trend Pullback", strategy="trend_pullback"):
    return {"id": sid, "strategy": strategy, "strategy_name": name, "direction": direction, "stage": stage, "invalidation": inv, "trigger": trigger,
            "plan": {"targets": [{"n": i + 1, "price": p} for i, p in enumerate(targets)]}}  # fmt: skip


def rec(at, price, setups=(), events=()):
    return {"v": 2, "t": at, "time": datetime.fromtimestamp(at, IST).isoformat(timespec="seconds"), "reason": "changed", "price": price, "decision": {"action": "WAIT"},
            "setups": list(setups), "events": list(events)}  # fmt: skip


def ended(sid, at, code="invalidated"):
    return {"seq": 1, "time": at, "id": sid, "status": "INVALIDATED", "text": f"INVALIDATED: x", "code": code}


def bars(*rows):
    """1-minute candles: (hour, minute, open, high, low, close)."""
    return [(t(h, m), o, hi, lo, c) for h, m, o, hi, lo, c in rows]


def flat(h, m, price):
    return (h, m, price, price, price, price)


ENTRY = 25005.0  # risk 35 against the 24970 invalidation: target 1 (25050) is +45/35 R, target 2 (25100) is +95/35 R
READY = [rec(t(10, 0, 20), ENTRY, [setup()])]


def one(records, candles, **kw):
    (res,) = review(records, candles)["setups"]
    return res


# -- long: target, invalidation, both ----------------------------------------------------------------------------------------------------------
def test_a_long_that_reaches_target_one_before_the_invalidation(tmp_path):
    c = bars(flat(10, 1, 25010), (10, 2, 25010, 25030, 25000, 25025), (10, 3, 25025, 25049.9, 25020, 25040), (10, 4, 25040, 25060, 25035, 25055), flat(10, 5, 25055))
    r = one(READY, c)
    assert r["outcome"] == "target1" and r["exit_at"] == t(10, 4) and r["exit_price"] == 25050.0 and r["ambiguous"] is False
    assert r["risk"] == pytest.approx(35.0) and r["r"] == pytest.approx(45 / 35)
    assert r["mfe_r"] == pytest.approx(55 / 35)  # the candle that reached the target went on to 25060
    assert r["mae_r"] == pytest.approx(-5 / 35)  # the 10:02 candle dipped to 25000, 5 points under the entry
    assert r["entry"] == ENTRY and r["target1"] == 25050.0 and r["target2"] == 25100.0


def test_a_long_that_touches_the_invalidation_first_is_minus_one_r(tmp_path):
    c = bars(flat(10, 1, 25000), (10, 2, 25000, 25010, 24970.0, 24980), (10, 3, 24980, 25060, 24980, 25055))  # the later rally does not matter
    r = one(READY, c)
    assert r["outcome"] == "invalidation" and r["exit_price"] == 24970.0 and r["exit_at"] == t(10, 2)
    assert r["r"] == pytest.approx(-1.0) and r["mae_r"] == pytest.approx(-1.0)


def test_a_candle_that_touches_both_is_the_invalidation_and_says_so(tmp_path):
    r = one(READY, bars((10, 1, 25005, 25060, 24965, 25000)))
    assert r["outcome"] == "invalidation" and r["ambiguous"] is True and r["r"] == pytest.approx(-1.0)


def test_touching_the_level_exactly_counts(tmp_path):
    assert one(READY, bars((10, 1, 25005, 25050.0, 25000, 25040)))["outcome"] == "target1"
    assert one(READY, bars((10, 1, 25005, 25010, 24970.0, 24990)))["outcome"] == "invalidation"
    assert one(READY, bars((10, 1, 25005, 25049.99, 24970.01, 25000)))["outcome"] == "unresolved"  # a tick short of either


def test_the_minute_the_alert_came_in_is_never_used(tmp_path):
    inside = bars((10, 0, 25005, 25100, 24900, 25005))  # the candle that CONTAINS 10:00:20: its low and high were not known at the time
    after = bars(flat(10, 1, 25010))
    r = one(READY, inside + after)
    assert r["outcome"] == "unresolved" and r["exit_price"] == 25010.0
    r = one([rec(t(10, 0, 0), ENTRY, [setup()])], inside + after)  # even at exactly 10:00:00 the next minute is where it starts
    assert r["outcome"] == "unresolved"


def test_candles_before_the_alert_are_ignored(tmp_path):
    c = bars((9, 40, 25000, 25100, 24900, 25000), flat(10, 1, 25010))
    assert one(READY, c)["outcome"] == "unresolved"


# -- short: the mirror image -------------------------------------------------------------------------------------------------------------------
SHORT = [rec(t(11, 0, 5), 25000.0, [setup("breakout_retest:bear:2", "PE", inv=25035.0, trigger=25000.0, targets=(24950.0, 24900.0), name="Breakout and Retest", strategy="breakout_retest")])]


def test_a_short_that_reaches_its_target_is_a_positive_r():
    r = one(SHORT, bars(flat(11, 1, 24990), (11, 2, 24990, 24995, 24950.0, 24960)))
    assert r["outcome"] == "target1" and r["exit_price"] == 24950.0 and r["risk"] == pytest.approx(35.0) and r["r"] == pytest.approx(50 / 35)
    assert r["mae_r"] == pytest.approx(0.0) and r["mfe_r"] == pytest.approx(50 / 35)


def test_a_short_that_touches_the_invalidation_above_is_minus_one_r():
    r = one(SHORT, bars(flat(11, 1, 25010), (11, 2, 25010, 25035.0, 25005, 25020), (11, 3, 25020, 25020, 24900, 24910)))
    assert r["outcome"] == "invalidation" and r["exit_price"] == 25035.0 and r["r"] == pytest.approx(-1.0) and r["ambiguous"] is False


def test_a_short_unresolved_is_marked_at_the_last_close():
    r = one(SHORT, bars(flat(11, 1, 24990), flat(11, 2, 24980)))
    assert r["outcome"] == "unresolved" and r["exit_price"] == 24980.0 and r["r"] == pytest.approx(20 / 35)


# -- unresolved, ended by Prism ----------------------------------------------------------------------------------------------------------------
def test_a_long_that_goes_nowhere_is_marked_at_the_last_close_of_the_session():
    r = one(READY, bars(flat(10, 1, 25010), flat(15, 29, 25020)))
    assert r["outcome"] == "unresolved" and r["exit_at"] == t(15, 29) and r["exit_price"] == 25020.0 and r["r"] == pytest.approx(15 / 35)


def test_a_setup_that_prism_ended_is_closed_at_the_price_of_that_record():
    sid = "trend_pullback:bull:1"
    records = READY + [rec(t(10, 4, 10), 24990.0, [], [ended(sid, t(10, 4, 10), "invalidated")])]
    r = one(records, bars(flat(10, 1, 25000), flat(10, 2, 24995), flat(10, 3, 24992), flat(10, 4, 24990), flat(10, 5, 24985)))
    assert r["outcome"] == "setup_ended" and r["exit_price"] == 24990.0 and r["end_code"] == "invalidated"
    assert r["r"] == pytest.approx((24990 - ENTRY) / 35)  # a small loss: Prism withdrew the idea before the level was touched


def test_the_level_touched_before_prism_ended_it_wins():
    sid = "trend_pullback:bull:1"
    records = READY + [rec(t(10, 9, 0), 24990.0, [], [ended(sid, t(10, 9, 0))])]
    r = one(records, bars(flat(10, 1, 25000), (10, 2, 25000, 25001, 24965, 24990), flat(10, 3, 24990)))
    assert r["outcome"] == "invalidation" and r["exit_at"] == t(10, 2)


def test_a_target_reached_before_prism_ended_it_wins():
    sid = "trend_pullback:bull:1"
    records = READY + [rec(t(10, 9, 0), 25020.0, [], [ended(sid, t(10, 9, 0), "expired")])]
    assert one(records, bars(flat(10, 1, 25000), (10, 2, 25000, 25055, 25000, 25050)))["outcome"] == "target1"


def test_an_end_after_the_last_candle_there_is_still_counts():
    sid = "trend_pullback:bull:1"
    records = READY + [rec(t(15, 40, 0), 25011.0, [], [ended(sid, t(15, 40, 0), "session_ended")])]
    r = one(records, bars(flat(10, 1, 25010)))
    assert r["outcome"] == "setup_ended" and r["exit_price"] == 25011.0 and r["end_code"] == "session_ended"


def test_a_setup_that_is_never_ended_and_never_touched_is_unresolved():
    assert one(READY, bars(flat(10, 1, 25010)))["end_code"] is None


# -- target 2 ----------------------------------------------------------------------------------------------------------------------------------
def test_target_two_is_followed_after_target_one_with_the_invalidation_still_the_stop():
    after = bars((10, 1, 25005, 25050, 25000, 25049), (10, 2, 25049, 25110, 25040, 25100))
    assert one(READY, after)["target2_reached"] is True
    stopped = bars((10, 1, 25005, 25050, 25000, 25049), (10, 2, 25049, 25055, 24965, 24970), (10, 3, 24970, 25120, 24970, 25100))
    assert one(READY, stopped)["target2_reached"] is False  # the level was hit first: the later run does not count
    assert one(READY, bars((10, 1, 25005, 25050, 25000, 25049)))["target2_reached"] is False  # the day ended


def test_there_is_no_target_two_verdict_for_a_trade_that_did_not_reach_target_one():
    assert one(READY, bars(flat(10, 1, 25010)))["target2_reached"] is None


# -- what is not a trade ---------------------------------------------------------------------------------------------------------------------------
def test_a_target_the_price_had_already_passed_is_not_a_target():
    records = [rec(t(10, 0, 20), ENTRY, [setup(targets=(25001.0, 25050.0, 25100.0))])]  # the first is below the entry of a long
    r = one(records, bars((10, 1, 25005, 25052, 25000, 25050)))
    assert r["target1"] == 25050.0 and r["target2"] == 25100.0 and r["outcome"] == "target1"


def test_with_no_target_beyond_the_entry_only_the_level_or_the_close_can_decide():
    records = [rec(t(10, 0, 20), ENTRY, [setup(targets=(24990.0,))])]
    r = one(records, bars((10, 1, 25005, 25500, 25000, 25400)))
    assert r["target1"] is None and r["outcome"] == "unresolved" and r["r"] == pytest.approx(395 / 35)
    assert one([rec(t(10, 0, 20), ENTRY, [setup(targets=())])], bars(flat(10, 1, 25010)))["target1"] is None


def test_a_price_already_through_the_invalidation_has_no_risk_to_measure():
    records = [rec(t(10, 0, 20), 24960.0, [setup()])]  # 24960 is below the 24970 invalidation of a long
    r = one(records, bars(flat(10, 1, 24950)))
    assert r["outcome"] is None and "no risk" in r["note"] and r["risk"] is None
    short = [rec(t(10, 0, 20), 25100.0, [setup("x:bear:1", "PE", inv=25035.0)])]
    assert one(short, bars(flat(10, 1, 25100)))["outcome"] is None


def test_a_setup_with_no_invalidation_or_no_known_direction_is_not_replayed():
    assert one([rec(t(10, 0, 20), ENTRY, [setup(inv=None)])], bars(flat(10, 1, 25010)))["outcome"] is None
    assert one([rec(t(10, 0, 20), ENTRY, [setup(direction="XX")])], bars(flat(10, 1, 25010)))["outcome"] is None


def test_no_candle_after_the_entry_means_no_replay():
    r = one(READY, bars(flat(9, 30, 25000)))
    assert r["outcome"] is None and "no candle" in r["note"]


# -- the first ready moment --------------------------------------------------------------------------------------------------------------------
def test_the_entry_is_the_first_time_it_was_ready_with_the_levels_it_showed_then(tmp_path):
    first = setup(inv=24970.0, targets=(25050.0,))
    later = setup(inv=24990.0, targets=(25030.0,))  # the same setup, a minute later, its levels moved
    records = [rec(t(9, 58), 24990.0, [setup(stage="CONFIRMED")]), rec(t(10, 0, 20), ENTRY, [first]), rec(t(10, 1, 20), 24995.0, [setup(stage="CONFIRMED")]), rec(t(10, 2, 20), 25001.0, [later])]
    (life,) = lifecycles(records).values()
    assert life["ready"] == {"t": t(10, 0, 20), "price": ENTRY, "invalidation": 24970.0, "trigger": 25000.0, "targets": [25050.0]}
    assert life["stages"] == ["CONFIRMED", "READY_TO_ACT", "CONFIRMED", "READY_TO_ACT"]
    assert len(review(records, bars(flat(10, 3, 25000)))["setups"]) == 1  # one trade, not two


def test_a_record_with_no_price_cannot_be_the_entry_the_next_one_is(tmp_path):
    records = [rec(t(10, 0, 20), None, [setup()]), rec(t(10, 0, 40), 25006.0, [setup()])]
    (life,) = lifecycles(records).values()
    assert life["ready"]["t"] == t(10, 0, 40) and life["ready"]["price"] == 25006.0


def test_setups_that_were_never_ready_are_listed_with_why_and_not_replayed():
    records = [rec(t(10, 0), 25000.0, [setup("a:bull:1", stage="DETECTED", name="Range Bounce", strategy="range_bounce"), setup("b:bull:2", stage="WAITING_FOR_CONFIRMATION", name="Range Bounce", strategy="range_bounce")]),
               rec(t(10, 30), 25000.0, [], [ended("a:bull:1", t(10, 30), "expired")]), rec(t(10, 31), 25000.0, [setup("b:bull:2", stage="WAITING_FOR_CONFIRMATION")])]
    rep = review(records, bars(flat(10, 5, 25000)))
    assert rep["setups"] == [] and {(n["id"], n["reached"], n["end_code"]) for n in rep["never_ready"]} == {("a:bull:1", "DETECTED", "expired"), ("b:bull:2", "WAITING_FOR_CONFIRMATION", "still live at the end")}


# -- the summary ---------------------------------------------------------------------------------------------------------------------------------
def test_the_summary_counts_and_averages():
    results = [{"outcome": "target1", "r": 1.5, "mfe_r": 2.0, "mae_r": -0.2, "ambiguous": False}, {"outcome": "target1", "r": 2.5, "mfe_r": 3.0, "mae_r": 0.0, "ambiguous": False},
               {"outcome": "invalidation", "r": -1.0, "mfe_r": 0.5, "mae_r": -1.0, "ambiguous": True}, {"outcome": "setup_ended", "r": -0.5, "mfe_r": 0.0, "mae_r": -0.5, "ambiguous": False},
               {"outcome": "unresolved", "r": 0.25, "mfe_r": 0.5, "mae_r": -0.25, "ambiguous": False}, {"outcome": None, "r": None, "note": "no risk"}]
    s = summarise(results)
    assert (s["ready"], s["replayed"], s["target1"], s["invalidation"], s["setup_ended"], s["unresolved"], s["ambiguous"]) == (6, 5, 2, 1, 1, 1, 1)
    assert s["target1_share_of_decided"] == pytest.approx(2 / 3)  # unresolved and Prism-ended ones are not "decided"
    assert s["average_r"] == pytest.approx((1.5 + 2.5 - 1.0 - 0.5 + 0.25) / 5) and s["average_mfe_r"] == pytest.approx(6.0 / 5) and s["average_mae_r"] == pytest.approx(-1.95 / 5)
    assert summarise([])["average_r"] is None and summarise([])["target1_share_of_decided"] is None


def test_the_review_groups_by_strategy_and_in_total():
    records = [rec(t(10, 0, 20), ENTRY, [setup("a:bull:1")]), rec(t(10, 20, 20), ENTRY, [setup("b:bull:2", name="Range Bounce", strategy="range_bounce")]), rec(t(10, 40, 20), ENTRY, [setup("c:bull:3")])]
    candles = bars((10, 1, 25005, 25060, 25000, 25055), (10, 21, 25005, 25006, 24960, 24965), (10, 41, 25005, 25060, 25000, 25055))
    rep = review(records, candles)
    assert [r["outcome"] for r in rep["setups"]] == ["target1", "invalidation", "target1"]
    assert rep["by_strategy"]["Trend Pullback"]["target1"] == 2 and rep["by_strategy"]["Range Bounce"]["invalidation"] == 1
    assert rep["total"]["ready"] == 3 and rep["total"]["target1_share_of_decided"] == pytest.approx(2 / 3)


def test_without_candles_the_ready_setups_are_listed_and_not_replayed():
    rep = review(READY, None)
    (r,) = rep["setups"]
    assert r["outcome"] is None and "not replayed" in r["note"] and r["entry"] == ENTRY and rep["total"]["replayed"] == 0


# -- odd records -------------------------------------------------------------------------------------------------------------------------------
def test_odd_records_are_skipped_not_fatal():
    odd = [{"t": None, "setups": [setup()]}, {"t": t(10, 0), "setups": [None, 3, {"stage": "READY_TO_ACT"}, {"id": "x:bull:9"}], "events": [None, {"id": "nope", "code": "x"}, 4]},
           {"t": t(10, 1), "price": "high", "setups": [setup("y:bull:1")]}, {"t": float("nan"), "setups": [setup("z:bull:1")]}]
    rep = review(odd, bars(flat(10, 5, 25000)))
    assert rep["setups"] == []  # nothing usable was ready


# -- candles: a file, the database -----------------------------------------------------------------------------------------------------------------
def test_candles_come_from_a_file_as_rows_or_as_objects(tmp_path):
    rows, objs = tmp_path / "rows.json", tmp_path / "objs.json"
    rows.write_text(json.dumps([[t(10, 1), 1, 2, 0.5, 1.5], [t(10, 2), 2, 3, 1, 2]]), encoding="utf-8")
    objs.write_text(json.dumps([{"start": t(10, 1), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9}]), encoding="utf-8")
    assert candles_from_file(rows) == [(t(10, 1), 1.0, 2.0, 0.5, 1.5), (t(10, 2), 2.0, 3.0, 1.0, 2.0)] and candles_from_file(objs) == [(t(10, 1), 1.0, 2.0, 0.5, 1.5)]


def test_candles_from_the_database_are_that_days_in_session_one_minute_candles_through_the_read_only_loader():
    d = lambda day, h, m: datetime(2026, 11, day, h, m, tzinfo=IST)  # noqa: E731
    rows = {"1min": [(d(9, 10, 1), 1, 1, 1, 1, 0), (d(6, 15, 29), 5, 6, 4, 5.5, 3), (d(6, 9, 15), 100, 102, 99, 101, 0), (d(6, 8, 30), 1, 1, 1, 1, 0), (d(5, 10, 0), 7, 7, 7, 7, 0)]}
    conn, seen = FakeConn(rows), {}

    def connect(**kw):
        seen.update(kw)
        return conn

    got = candles_from_db(date(2026, 11, 6), connect=connect, settings=PG)
    assert got == [(t(9, 15), 100.0, 102.0, 99.0, 101.0), (t(15, 29), 5.0, 6.0, 4.0, 5.5)]  # that day only, in the session, oldest first
    assert conn.session == {"readonly": True, "autocommit": True} and "default_transaction_read_only=on" in seen["options"] and conn.closed
    assert all(sql.lstrip().upper().startswith("SELECT") and not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", sql, re.I) for sql, _ in conn.executed)


def test_the_database_needs_a_password_here_too(monkeypatch):
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="PG_PASSWORD"):
        candles_from_db(date(2026, 11, 6), connect=lambda **kw: pytest.fail("must not connect"))


# -- the command line ----------------------------------------------------------------------------------------------------------------------------
def write_journal(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_the_command_prints_the_review_and_leaves_the_journal_untouched(tmp_path, capsys):
    j = tmp_path / "decisions-2026-11-06.jsonl"
    write_journal(j, READY)
    c = tmp_path / "day.json"
    c.write_text(json.dumps(bars((10, 1, 25005, 25060, 25000, 25055))), encoding="utf-8")
    before = hashlib.sha256(j.read_bytes()).hexdigest()
    assert main([str(j), "--candles", str(c)]) == 0
    out = capsys.readouterr().out
    assert "2026-11-06" in out and "target1 at 10:01 25050.00 = +1.29 R" in out and "NOT option profit" in out and "small sample" in out
    assert "10:00 CE Trend Pullback" in out and "Trend Pullback: 1 / 1 / 0 / 0 / 0, 100%" in out
    assert hashlib.sha256(j.read_bytes()).hexdigest() == before


def test_the_command_can_print_json(tmp_path, capsys):
    j = tmp_path / "decisions-2026-11-06.jsonl"
    write_journal(j, READY)
    c = tmp_path / "day.json"
    c.write_text(json.dumps(bars((10, 1, 25005, 25060, 25000, 25055))), encoding="utf-8")
    assert main([str(j), "--candles", str(c), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["day"] == "2026-11-06" and data["replayed"] is True and data["setups"][0]["outcome"] == "target1" and data["total"]["ready"] == 1


def test_a_database_that_is_down_costs_the_replay_not_the_review(tmp_path, capsys, monkeypatch):
    j = tmp_path / "decisions-2026-11-06.jsonl"
    write_journal(j, READY)

    def down(day, **kw):
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(outcomes, "candles_from_db", down)
    assert main([str(j)]) == 0
    out = capsys.readouterr().out
    assert "nothing was replayed" in out and "10:00 CE Trend Pullback" in out and "RuntimeError: could not connect to server" in out


def test_a_day_with_no_candles_says_so(tmp_path, capsys, monkeypatch):
    j = tmp_path / "decisions-2026-11-06.jsonl"
    write_journal(j, READY)
    monkeypatch.setattr(outcomes, "candles_from_db", lambda day, **kw: [])
    assert main([str(j)]) == 0
    assert "no 1-minute candle for that day" in capsys.readouterr().out


def test_a_missing_journal_is_exit_two(tmp_path, capsys):
    assert main([str(tmp_path / "nope.jsonl")]) == 2
    assert "cannot be read" in capsys.readouterr().err


def test_a_journal_with_a_cut_line_is_reviewed_and_says_how_many_lines_were_skipped(tmp_path, capsys):
    j = tmp_path / "decisions-2026-11-06.jsonl"
    j.write_text(json.dumps(READY[0]) + '\n{"t": 17, "dec\n', encoding="utf-8")
    assert main([str(j), "--candles", str(write_candles(tmp_path))]) == 0
    assert "1 journal line(s) could not be read" in capsys.readouterr().out


def write_candles(tmp_path):
    c = tmp_path / "day.json"
    c.write_text(json.dumps(bars(flat(10, 1, 25010))), encoding="utf-8")
    return c


def test_the_day_comes_from_the_file_name_or_else_from_the_first_record():
    assert outcomes.journal_day("logs/decisions-2026-11-06.jsonl", []) == date(2026, 11, 6)
    assert outcomes.journal_day("whatever.jsonl", READY) == date(2026, 11, 6)
    assert outcomes.journal_day("whatever.jsonl", []) is None


def test_a_real_version_one_journal_record_is_read_without_complaint():
    from pathlib import Path

    from app.journal import read

    records, bad = read(Path(__file__).parent / "golden" / "journal_v1_sample.jsonl")
    rep = review(records, [])
    assert bad == [] and isinstance(rep["setups"], list) and isinstance(rep["never_ready"], list)  # (the fixture's setups may or may not have been ready: either way it reads)


def test_the_module_never_writes_a_file_and_issues_no_order():
    src = open(outcomes.__file__, encoding="utf-8").read()
    assert not re.search(r"\bopen\([^)]*['\"][wa]b?\+?['\"]|write_text|write_bytes|\.unlink|os\.remove|placeOrder|place_order|modifyOrder|cancelOrder", src)
