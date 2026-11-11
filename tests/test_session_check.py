"""The automatic half of the validation runbook (app/session_check.py): a trading day's journal tested against Prism's own rules. Hand-made records; READ-ONLY.

Every rule has a record that breaks it and one that keeps it (on both sides of its time boundary where it has one), and breaking one rule fails that check and no other. What is held:

  * the BUY rules: in the session, not in the opening range, not late, on trusted data and a live feed, after the confirming candle, in the entry area, the stop on the right side, the 1H / 15M
    trend rule (with the counter-trend permission as the one exception), never a BUY with a CE and a PE setup both ready
  * nothing ended comes back; nothing is announced twice
  * the coverage: a journal that starts late, ends early or goes silent is a warning, not a failure
  * the kind of day (gap, regime, volatility), the coverage table over many days, the exit status, the JSON; a day with no session is said so; the file is never changed"""

import hashlib
import json
from datetime import datetime

import pytest
from helpers import ist

from app import session_check
from app.candles import IST
from app.session_check import check_file, check_records, coverage_table, day_type, format_report, main

DAY = (2026, 11, 6)


def t(h, m, s=0):
    return ist(h, m, s, day=DAY)


def good_setup(at, direction="CE", **over):
    s = {"id": f"trend_pullback:{'bull' if direction == 'CE' else 'bear'}:1", "strategy": "trend_pullback", "direction": direction, "status": "CONFIRMED", "stage": "READY_TO_ACT",
         "price_state": "in_entry_area", "invalidation": 24470.0 if direction == "CE" else 24530.0, "confirmed_bar_end": at - 30}  # fmt: skip
    return {**s, **over}


def rec(at, act="WAIT", price=24500.0, phase="morning", ok=True, feed="connected", stale=False, trends=None, setup=None, setups=None, events=(), counter=(), regime="trend_up", vol="normal", gap=None,
        reason="changed", restart=None, strategy="trend_pullback", issues=()):
    r = {"v": 2, "t": at, "time": datetime.fromtimestamp(at, IST).isoformat(timespec="seconds"), "reason": reason, "price": price,
         "decision": {"action": act, "headline": act, "reasons": [], "against": [], "setup_id": setup["id"] if setup else None, "strategy": strategy if setup else None},
         "decision_setup": setup, "setups": list(setups) if setups is not None else ([setup] if setup else []), "events": list(events),
         "market": {"trends": trends or {"1h": "bullish", "15m": "bullish"}, "regime": regime, "volatility": vol, "gap": gap}, "session": {"phase": phase},
         "data": {"ok": ok, "issues": list(issues) if issues else ([] if ok else ["historical data is stale"])}, "feed": {"state": feed, "spot": {"stale": stale}}, "counter_trend": list(counter)}  # fmt: skip
    if restart is not None:
        r["restart"] = restart
    return r


def buy(at, **kw):
    setup = kw.pop("setup", None) or good_setup(at, kw.get("direction", "CE"))
    direction = kw.pop("direction", "CE")
    return rec(at, f"BUY {direction}", setup=setup, **kw)


def heartbeats(start=t(9, 15, 5), end=t(15, 29, 0), step=300):
    out, x = [], start
    while x <= end:
        out.append(rec(x, reason="candle"))
        x += step
    return out


def by_id(checks):
    return {c["id"]: c for c in checks}


def statuses(records):
    return {k: c["status"] for k, c in by_id(check_records(records)).items()}


GOOD = [*heartbeats(), buy(t(10, 15, 0))]
ALL_PASS = {"buy_in_session": "pass", "buy_not_in_opening_range": "pass", "buy_not_late": "pass", "buy_on_trusted_data": "pass", "buy_after_confirmation": "pass", "buy_in_entry_area": "pass",
            "buy_stop_on_right_side": "pass", "buy_respects_trend": "pass", "conflict_is_wait": "na", "nothing_revived": "pass", "nothing_twice": "pass", "session_covered": "pass"}  # fmt: skip


def only_failing(records, *expected):
    s = statuses(records)
    assert {k for k, v in s.items() if v == "fail"} == set(expected), s


# -- a clean day -----------------------------------------------------------------------------------------------------------------------------
def test_a_clean_day_passes_every_rule():
    s = statuses(GOOD)
    for k, v in ALL_PASS.items():
        assert s[k] == v, k
    assert not [k for k, v in s.items() if v == "fail"]


def test_a_buy_in_the_bear_direction_that_keeps_the_rules_passes_too():
    bear = buy(t(10, 15, 0), direction="PE", trends={"1h": "bearish", "15m": "bearish"})
    assert not [k for k, v in statuses([*heartbeats(), bear]).items() if v == "fail"]


def test_a_day_with_no_buy_has_nothing_to_test_for_the_buy_rules():
    s = statuses(heartbeats())
    assert s["buy_in_session"] == "na" and s["buy_after_confirmation"] == "na" and s["nothing_revived"] == "pass" and not [v for v in s.values() if v == "fail"]


# -- the time rules, on both sides of each boundary ------------------------------------------------------------------------------------
@pytest.mark.parametrize("at, failing", [
    (t(9, 14, 59), "buy_in_session"), (t(9, 15, 0), "buy_not_in_opening_range"), (t(9, 29, 59), "buy_not_in_opening_range"),
    (t(14, 45, 0), "buy_not_late"), (t(15, 29, 59), "buy_not_late"), (t(15, 30, 0), "buy_in_session"), (t(20, 0, 0), "buy_in_session")])
def test_a_buy_outside_its_time_window_fails_that_rule(at, failing):
    r = buy(at, phase="morning")
    only_failing([r], failing)


@pytest.mark.parametrize("at", [t(9, 30, 0), t(9, 30, 1), t(12, 0, 0), t(14, 44, 59)])
def test_a_buy_inside_the_window_passes_the_time_rules(at):
    s = statuses([buy(at)])
    assert s["buy_in_session"] == s["buy_not_in_opening_range"] == s["buy_not_late"] == "pass"


@pytest.mark.parametrize("phase", ["pre_open", "closed"])
def test_a_buy_in_a_closed_phase_fails_even_at_a_session_clock_time(phase):
    only_failing([buy(t(10, 15, 0), phase=phase)], "buy_in_session")


# -- the data rules --------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [{"ok": False}, {"feed": "disconnected"}, {"feed": "connecting"}, {"stale": True}])
def test_a_buy_on_untrusted_data_or_a_dead_feed_fails(kw):
    only_failing([buy(t(10, 15, 0), **kw)], "buy_on_trusted_data")


def test_the_reason_a_buy_was_untrusted_is_in_the_example():
    c = by_id(check_records([buy(t(10, 15, 0), ok=False, issues=["5M candles missing 10:25-11:20"])]))["buy_on_trusted_data"]
    assert c["status"] == "fail" and "5M candles missing 10:25-11:20" in c["examples"][0] and c["examples"][0].startswith("10:15:00 BUY CE")


def test_a_record_with_no_data_section_is_not_trusted():
    r = buy(t(10, 15, 0))
    del r["data"]
    only_failing([r], "buy_on_trusted_data")


# -- the setup rules -------------------------------------------------------------------------------------------------------------------------
def test_a_buy_before_its_confirming_candle_completed_fails_and_at_that_moment_passes():
    at = t(10, 15, 0)
    only_failing([buy(at, setup=good_setup(at, confirmed_bar_end=at + 2))], "buy_after_confirmation")
    assert statuses([buy(at, setup=good_setup(at, confirmed_bar_end=at + 1))])["buy_after_confirmation"] == "pass"  # a second of clock difference is not a rule break
    assert statuses([buy(at, setup=good_setup(at, confirmed_bar_end=at))])["buy_after_confirmation"] == "pass"


@pytest.mark.parametrize("over", [{"status": "WAITING_FOR_CONFIRMATION"}, {"status": "DETECTED"}, {"confirmed_bar_end": None}])
def test_a_buy_whose_setup_is_not_confirmed_fails(over):
    at = t(10, 15, 0)
    only_failing([buy(at, setup=good_setup(at, **over))], "buy_after_confirmation")


def test_a_buy_that_names_no_setup_fails():
    at = t(10, 15, 0)
    r = rec(at, "BUY CE")
    s = statuses([r])
    assert s["buy_after_confirmation"] == "fail" and s["buy_in_entry_area"] == "fail"


@pytest.mark.parametrize("over", [{"stage": "CONFIRMED"}, {"stage": "BLOCKED"}, {"price_state": "above_entry_area"}, {"price_state": None}])
def test_a_buy_outside_the_entry_area_or_blocked_fails(over):
    at = t(10, 15, 0)
    only_failing([buy(at, setup=good_setup(at, **over))], "buy_in_entry_area")


@pytest.mark.parametrize("direction, inv, price, ok", [("CE", 24470.0, 24500.0, True), ("CE", 24500.0, 24500.0, False), ("CE", 24530.0, 24500.0, False),
                                                       ("PE", 24530.0, 24500.0, True), ("PE", 24500.0, 24500.0, False), ("PE", 24470.0, 24500.0, False)])
def test_the_invalidation_is_on_the_right_side_of_the_price(direction, inv, price, ok):
    at = t(10, 15, 0)
    trends = {"1h": "bearish", "15m": "bearish"} if direction == "PE" else None
    r = buy(at, direction=direction, price=price, setup=good_setup(at, direction, invalidation=inv), trends=trends)
    assert statuses([r])["buy_stop_on_right_side"] == ("pass" if ok else "fail")


def test_a_buy_with_no_invalidation_fails_that_rule():
    at = t(10, 15, 0)
    only_failing([buy(at, setup=good_setup(at, invalidation=None))], "buy_stop_on_right_side")


# -- the trend rule and its one exception ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction, trends, ok", [
    ("CE", {"1h": "bullish", "15m": "bullish"}, True), ("CE", {"1h": "neutral", "15m": "bullish"}, True), ("CE", {"1h": "bullish", "15m": "bearish"}, False), ("CE", {"1h": "bearish", "15m": "bullish"}, False),
    ("CE", {"1h": "bearish", "15m": "bearish"}, False), ("CE", {}, True), ("PE", {"1h": "bearish", "15m": "bearish"}, True), ("PE", {"1h": "bullish", "15m": "bearish"}, False),
    ("PE", {"1h": "bearish", "15m": "bullish"}, False), ("PE", {"1h": "neutral", "15m": "neutral"}, True)])
def test_a_buy_against_the_1h_or_15m_trend_fails(direction, trends, ok):
    r = buy(t(10, 15, 0), direction=direction, trends=trends)
    assert statuses([r])["buy_respects_trend"] == ("pass" if ok else "fail")


def test_a_granted_counter_trend_permission_for_that_strategy_and_side_is_the_one_exception():
    at = t(10, 15, 0)
    against = {"1h": "bearish", "15m": "bullish"}
    granted = [{"strategy": "trend_exhaustion_reversal", "side": "CE", "granted": True}]
    s = good_setup(at, strategy="trend_exhaustion_reversal")
    assert statuses([buy(at, trends=against, setup=s, counter=granted, strategy="trend_exhaustion_reversal")])["buy_respects_trend"] == "pass"
    for counter in ([{"strategy": "trend_exhaustion_reversal", "side": "CE", "granted": False}], [{"strategy": "other", "side": "CE", "granted": True}], [{"strategy": "trend_exhaustion_reversal", "side": "PE", "granted": True}], []):
        assert statuses([buy(at, trends=against, setup=s, counter=counter, strategy="trend_exhaustion_reversal")])["buy_respects_trend"] == "fail"


def test_a_ce_and_a_pe_setup_both_ready_must_not_give_a_buy():
    at = t(10, 15, 0)
    both = [good_setup(at), good_setup(at, "PE")]
    assert statuses([rec(at, "WAIT", setups=both)])["conflict_is_wait"] == "pass"
    assert statuses([rec(at, "BUY CE", setups=both, setup=good_setup(at))])["conflict_is_wait"] == "fail"
    assert statuses([rec(at, "WAIT", setups=[good_setup(at), good_setup(at, direction="CE", id="second")])])["conflict_is_wait"] == "na"  # two calls are not a conflict


# -- nothing revived, nothing twice -----------------------------------------------------------------------------------------------------------
def ended_event(seq, sid, at, code="invalidated", status="INVALIDATED"):
    return {"seq": seq, "time": at, "id": sid, "status": status, "code": code, "text": status}


def test_a_setup_that_ended_and_is_live_again_fails():
    sid = "trend_pullback:bull:1"
    records = [rec(t(10, 0), "WAIT", setups=[good_setup(t(10, 0))], events=[ended_event(1, sid, t(10, 0))]), rec(t(10, 5), "WAIT", setups=[good_setup(t(10, 5), stage="CONFIRMED", status="CONFIRMED")])]
    s = by_id(check_records(records))["nothing_revived"]
    assert s["status"] == "fail" and sid in s["examples"][0] and "10:05:00" in s["examples"][0]


def test_a_setup_that_ended_and_shows_only_as_ended_stays_ended():
    sid = "trend_pullback:bull:1"
    records = [rec(t(10, 0), setups=[good_setup(t(10, 0))], events=[ended_event(1, sid, t(10, 0))]), rec(t(10, 5), setups=[good_setup(t(10, 5), stage="INVALIDATED", status="INVALIDATED")]), rec(t(10, 10))]
    assert by_id(check_records(records))["nothing_revived"]["status"] == "pass"


def test_a_transition_event_that_is_not_an_ending_does_not_count_as_ended():
    sid = "trend_pullback:bull:1"
    first = {"seq": 0, "time": t(10, 0), "id": sid, "status": "CONFIRMED", "code": None, "text": "x"}
    records = [rec(t(10, 0), events=[first], setups=[good_setup(t(10, 0))]), rec(t(10, 5), setups=[good_setup(t(10, 5))])]
    assert by_id(check_records(records))["nothing_revived"]["status"] == "pass"


def test_an_event_written_twice_fails():
    e = ended_event(3, "a:bull:1", t(10, 0))
    records = [rec(t(10, 0), events=[e]), rec(t(10, 1), events=[e])]
    c = by_id(check_records(records))["nothing_twice"]
    assert c["status"] == "fail" and "event 3" in c["examples"][0]
    other = {**e, "seq": 4}
    assert by_id(check_records([rec(t(10, 0), events=[e]), rec(t(10, 1), events=[other])]))["nothing_twice"]["status"] == "pass"


# -- coverage: warnings ----------------------------------------------------------------------------------------------------------------------
def covered(records):
    return by_id(check_records(records))["session_covered"]


def test_a_full_session_is_covered_and_each_gap_in_it_is_a_warning_not_a_failure():
    assert covered(heartbeats())["status"] == "pass"
    late = covered(heartbeats(start=t(11, 9, 45)))
    assert late["status"] == "warn" and "starts at 11:09:45" in late["detail"]
    early = covered(heartbeats(end=t(13, 0, 0)))
    assert early["status"] == "warn" and "ends at 12:55:05" in early["detail"]
    silent = covered([r for r in heartbeats() if not (t(11, 0) < r["t"] < t(12, 0))])
    assert silent["status"] == "warn" and "no record between 10:5" in silent["detail"]
    assert not [c for c in check_records(heartbeats(start=t(11, 9, 45))) if c["status"] == "fail"]


def test_a_start_just_after_the_open_is_tolerated_a_late_start_is_not():
    assert covered(heartbeats(start=t(9, 16, 50)))["status"] == "pass"  # two minutes of grace for Prism to write its first line
    assert covered(heartbeats(start=t(9, 17, 5)))["status"] == "warn"
    assert covered(heartbeats(start=t(9, 20, 0)))["status"] == "warn"


def test_a_journal_with_nothing_in_the_session_covers_nothing():
    assert covered([rec(t(8, 0), phase="pre_open")])["status"] == "na"


def test_the_share_of_the_session_on_untrusted_data_is_a_warning_over_ten_percent():
    rows = heartbeats()
    bad = [{**r, "data": {"ok": False, "issues": ["historical data is stale"]}} for r in rows[:15]]
    c = by_id(check_records(bad + rows[15:]))["data_trusted_share"]
    assert c["status"] == "warn" and "historical data is stale" in c["examples"]
    assert by_id(check_records(rows))["data_trusted_share"]["status"] == "info"


def test_a_restart_that_could_not_restore_its_setups_is_a_warning():
    ok = rec(t(11, 0), reason="restart", restart={"ok": True, "restored": []})
    bad = rec(t(11, 5), reason="restart", restart={"ok": False, "error": "x"})
    assert by_id(check_records([*heartbeats(), ok]))["restarts"]["status"] == "info"
    c = by_id(check_records([*heartbeats(), ok, bad]))["restarts"]
    assert c["status"] == "warn" and "2 restart(s)" in c["detail"] and "11:05:00" in c["examples"][0]


# -- the kind of day ---------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("percent, label", [(0.49, "flat open"), (0.5, "gap up"), (1.2, "gap up"), (-0.49, "flat open"), (-0.5, "gap down"), (-2.0, "gap down")])
def test_the_opening_gap_names_the_day(percent, label):
    g = {"points": 1.0, "percent": percent, "open": 1.0, "previous_close": 1.0}
    assert day_type([rec(t(10, 0), gap=g), rec(t(10, 5), gap=g)])["gap"] == label


def test_the_regime_is_weighted_by_how_long_it_held_and_volatility_names_the_day():
    rows = [rec(t(10, 0), regime="trend_up"), rec(t(10, 10), regime="trend_up")] + [rec(t(10, 20 + i), regime="range_bound") for i in range(5)]  # 2 records that held 10 minutes each, 5 that held a minute each
    d = day_type(rows)
    assert d["regime"] == "trend_up" and d["regime_share"] == pytest.approx(1200 / 1500)  # the long stretch decides, not the number of records (5 against 2)
    assert day_type([rec(t(10, 0), vol="high"), rec(t(10, 5), vol="high"), rec(t(10, 10))])["volatile"] is True
    assert day_type([rec(t(10, 0)), rec(t(10, 5))])["volatile"] is False
    assert day_type([rec(t(10, 0), regime=None), rec(t(10, 5), regime=None)])["regime"] is None


def test_a_quarter_of_the_time_in_high_volatility_makes_the_day_volatile_and_less_does_not():
    exactly = [rec(t(10, 0), vol="high"), rec(t(10, 1), vol="normal"), rec(t(10, 3), vol="normal")]  # weights 60, 120, 60: 60 of 240 is a quarter
    assert day_type(exactly)["volatile"] is True and day_type(exactly)["high_volatility_share"] == pytest.approx(0.25)
    less = [rec(t(10, 0), vol="high"), rec(t(10, 1), vol="normal"), rec(t(10, 4), vol="normal")]  # weights 60, 180, 60: 60 of 300 is a fifth
    assert day_type(less)["volatile"] is False and day_type(less)["high_volatility_share"] == pytest.approx(0.2)
    assert day_type([rec(t(10, 0), vol="high"), rec(t(10, 5), vol="high")])["volatile"] is True


def test_the_day_type_of_a_day_with_no_session_is_empty_not_an_error():
    d = day_type([rec(t(8, 0), phase="pre_open")])
    assert d["session_records"] == 0 and d["regime"] is None and d["gap"] is None


# -- files, the report, the command ----------------------------------------------------------------------------------------------------------------
def write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in sorted(records, key=lambda r: r["t"])) + "\n", encoding="utf-8")  # (a journal is in time order)
    return path


def test_a_clean_file_reports_pass_and_lists_what_only_a_person_can_check(tmp_path):
    f = write(tmp_path / "decisions-2026-11-06.jsonl", GOOD)
    rep = check_file(f)
    assert rep["ok"] is True and rep["failed"] == 0 and rep["session"] is True and rep["day"] == "2026-11-06" and rep["checks"][0]["id"] == "journal_sound"
    text = "\n".join(format_report(rep))
    assert "PASS The journal file itself is sound" in text and "Only you can check" in text and "[ ] a BUY turned into WAIT" in text and "expiry day" in text
    assert "no counter-example in the journal" in text and "small sample" in text and "FAIL" not in text


def test_a_file_with_a_broken_rule_fails_and_the_report_says_which_record(tmp_path):
    f = write(tmp_path / "decisions-2026-11-06.jsonl", [*heartbeats(), buy(t(9, 20, 0)), buy(t(10, 15, 0))])
    rep = check_file(f)
    assert rep["ok"] is False and rep["failed"] == 1
    text = "\n".join(format_report(rep))
    assert "FAIL No BUY from 09:15 to 09:30" in text and "09:20:00 BUY CE: the opening range was still forming" in text and "FAILED." in text


def test_an_unsound_journal_file_fails_on_its_own_integrity(tmp_path):
    rows = sorted(GOOD, key=lambda r: r["t"])
    rows[3], rows[4] = rows[4], rows[3]  # a time that goes backwards
    f = tmp_path / "decisions-2026-11-06.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    rep = check_file(f)
    assert rep["ok"] is False and rep["checks"][0]["id"] == "journal_sound" and rep["checks"][0]["status"] == "fail" and rep["checks"][0]["examples"]


def test_a_day_with_no_session_is_said_not_checked(tmp_path):
    rep = check_file(write(tmp_path / "decisions-2026-11-07.jsonl", [rec(t(8, 0), phase="pre_open"), rec(t(20, 0), phase="closed")]))
    text = "\n".join(format_report(rep))
    assert rep["session"] is False and rep["ok"] is True and len(rep["checks"]) == 1 and "NOT A SESSION" in text and "Only you can check" not in text


def test_a_cut_line_does_not_stop_the_check(tmp_path):
    f = write(tmp_path / "decisions-2026-11-06.jsonl", GOOD)
    f.write_text(f.read_text(encoding="utf-8") + '{"t": 1, "dec\n', encoding="utf-8")
    rep = check_file(f)
    assert rep["session"] is True and "1 line(s) unreadable" in rep["checks"][0]["detail"]


def test_the_command_exit_status_and_output(tmp_path, capsys):
    good = write(tmp_path / "decisions-2026-11-06.jsonl", GOOD)
    bad = write(tmp_path / "decisions-2026-11-09.jsonl", [*heartbeats(), buy(t(15, 0, 0))])
    assert main([str(good)]) == 0 and "Automatic checks" in capsys.readouterr().out
    assert main([str(bad)]) == 1 and "FAIL No BUY from 14:45" in capsys.readouterr().out
    assert main([str(good), str(bad)]) == 1
    assert main([str(tmp_path / "nope.jsonl")]) == 2 and "cannot be read" in capsys.readouterr().err


def test_the_command_can_print_json(tmp_path, capsys):
    good = write(tmp_path / "decisions-2026-11-06.jsonl", GOOD)
    assert main([str(good), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["day"] == "2026-11-06" and isinstance(data["checks"], list) and data["day_type"]["buys"] == 1
    other = write(tmp_path / "decisions-2026-11-09.jsonl", GOOD)
    assert main([str(good), str(other), "--json"]) == 0 and len(json.loads(capsys.readouterr().out)) == 2


def test_the_coverage_table_counts_only_clean_days_with_a_buy_in_them(tmp_path, capsys):
    g = {"points": 80.0, "percent": 0.7, "open": 1.0, "previous_close": 1.0}
    up = write(tmp_path / "decisions-2026-11-06.jsonl", [rec(t(9, 15, 5), gap=g, reason="candle"), *heartbeats()[1:], buy(t(10, 15, 0), gap=g)])
    no_buy = write(tmp_path / "decisions-2026-11-09.jsonl", heartbeats())
    broken = write(tmp_path / "decisions-2026-11-10.jsonl", [*heartbeats(), buy(t(9, 20, 0), gap=g)])
    closed = write(tmp_path / "decisions-2026-11-07.jsonl", [rec(t(8, 0), phase="pre_open")])
    assert main([str(up), str(no_buy), str(broken), str(closed), "--coverage"]) == 1
    out = capsys.readouterr().out
    assert "2026-11-06  yes" in out and "clean" in out and "no BUY" in out and "FAILED" in out and "2026-11-07  not a session" in out
    assert "gap up: 1" in out and "gap down: 0" in out and "volatile: 0" in out and "trending (regime trend_up / trend_down): 1" in out  # only the clean day with a BUY counts
    rows = coverage_table([check_file(up)])
    assert any("gap up: 1" in r for r in rows)


def test_the_checker_never_changes_the_journal_and_has_no_network_or_order_code(tmp_path):
    import re

    f = write(tmp_path / "decisions-2026-11-06.jsonl", GOOD)
    before = hashlib.sha256(f.read_bytes()).hexdigest()
    check_file(f)
    main([str(f)])
    assert hashlib.sha256(f.read_bytes()).hexdigest() == before
    src = open(session_check.__file__, encoding="utf-8").read()
    assert not re.search(r"\bopen\([^)]*['\"][wa]b?\+?['\"]|write_text|write_bytes|requests|socket|SmartConnect|placeOrder|psycopg2", src)
