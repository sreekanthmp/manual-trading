"""Stage 6.4: a journal can be checked, and read, when it is not perfect.

  * `python -m app.journal FILE --verify` (read-only) says what is wrong with a file, how serious it is and on which lines, and what the file holds (records and bytes by kind, the share that is
    heartbeats, how many of those were written while the market was closed: the facts a decision about the file's size needs). Exit status 1 on an error, 0 otherwise, 2 when it cannot be opened.
  * the review command copes with an older or odd record (a dash for what it lacks, one line for a record it cannot show at all) and prints exactly what it always printed for a good one
  * `GET /api/journal` says how many lines it could not read and how many records it skipped, instead of hiding them
  * nothing about when a line is written changed: the once-per-candle heartbeat is still written, also on a closed market"""

import json
import os
from pathlib import Path

import pytest
from helpers import FRI, ist
from test_journal import stub_out
from test_phase3_integration import World, create_app, route

from app.journal import Journal, main, read, timeline
from app.journal_check import ACTIONS, REASONS, format_report, verify

FIXTURE = Path(__file__).parent / "golden" / "journal_v1_sample.jsonl"
T0 = ist(10, 0, 5, day=FRI)
A = {"timeframes": {}, "condition": {}, "warnings": []}
NAME = "decisions-2026-11-06.jsonl"


def rec(t=T0, reason="changed", action="WAIT", events=(), v=2, phase="morning", **over):
    r = {"v": v, "time": __import__("datetime").datetime.fromtimestamp(t, __import__("app.candles", fromlist=["IST"]).IST).isoformat(timespec="seconds"), "t": t, "reason": reason, "price": 24500.0,
         "decision": {"action": action, "headline": f"{action}: x", "reasons": [], "against": [], "setup_id": None, "strategy": None, "strategy_name": None, "also": []},
         "market": None, "session": {"phase": phase}, "events": list(events)}  # fmt: skip
    r.update(over)
    return r


def event(seq, sid="trend_pullback:bull:1", status="DETECTED", **extra):
    return {"seq": seq, "time": T0 + seq, "id": sid, "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "direction": "CE", "status": status, "text": f"{status}: x", **extra}


def ended(seq, sid="trend_pullback:bull:1", status="EXPIRED"):
    return event(seq, sid, status, code="expired", superseded_by=None, final={"status_before": "WAITING_FOR_CONFIRMATION"})


def write(tmp_path, records, name=NAME, tail=b"", head=b""):
    p = tmp_path / name
    p.write_bytes(head + b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in records) + tail)
    return p


def codes(rep, level=None):
    return sorted(x["code"] for x in rep["problems"] if level in (None, x["level"]))


# ====================================================================================================================
# 1. A good journal has nothing wrong with it, and is described
# ====================================================================================================================
def test_a_journal_written_by_the_engine_verifies_clean_and_is_described(tmp_path):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    for slot in (16, 17, 18, 19, 20):
        w.step(md, clock, slot)
    rep = verify(md.journal.path(clock.t))
    assert rep["ok"] and rep["problems"] == [] and rep["errors"] == rep["warnings"] == 0
    s = rep["summary"]
    records = read(md.journal.path(clock.t))[0]
    assert s["records"] == len(records) == s["lines"] and s["unreadable_lines"] == 0 and s["versions"] == {2: len(records)}
    assert s["events"] == {"written": 4, "distinct": 4, "setups": 1, "setups_not_ended_in_this_file": 0} and s["restarts"][0]["line"] == 1
    assert sum(v["records"] for v in s["by_reason"].values()) == len(records) and sum(v["bytes"] for v in s["by_reason"].values()) <= s["bytes"]
    text = "\n".join(format_report(rep))
    assert "no problem found" in text and "0 error(s), 0 warning(s)" in text and "heartbeats" in text


def test_four_real_version_1_records_verify_clean():
    rep = verify(FIXTURE)
    assert rep["ok"] and rep["problems"] == [] and rep["summary"]["versions"] == {1: 4} and rep["summary"]["events"]["written"] == 2


def test_an_empty_file_has_nothing_in_it_and_nothing_wrong(tmp_path):
    p = tmp_path / NAME
    p.write_bytes(b"")
    rep = verify(p)
    assert rep["ok"] and rep["problems"] == [] and rep["summary"]["records"] == 0 and rep["summary"]["record_bytes"] is None
    assert main([str(p), "--verify"]) == 0


# ====================================================================================================================
# 2. Each problem is found, and called what it is
# ====================================================================================================================
def garbage_line(tmp):
    return write(tmp, [rec()], tail=b"this is not json\n" + json.dumps(rec(T0 + 5)).encode() + b"\n")


CASES = {  # name: (file builder, the code expected, its level)
    "a line that is not JSON": (garbage_line, "unreadable_line", "warn"),
    "a line that is JSON but not a record": (lambda d: write(d, [rec()], tail=b"[1, 2]\n"), "unreadable_line", "warn"),
    "a decision that is not an object": (lambda d: write(d, [rec()], tail=b'{"t": 1, "decision": "WAIT"}\n'), "unreadable_line", "warn"),
    "a record cut short with no line ending": (lambda d: write(d, [rec()], tail=b'{"t": 1, "reason": "changed", "deci'), "no_final_newline", "warn"),
    "a byte-order mark": (lambda d: write(d, [rec()], head=b"\xef\xbb\xbf"), "bom", "warn"),
    "a decision that is not one of the four": (lambda d: write(d, [rec(action="MAYBE")]), "bad_action", "error"),
    "a reason nobody uses": (lambda d: write(d, [rec(reason="sometimes")]), "bad_reason", "warn"),
    "a record with no time": (lambda d: write(d, [{**rec(), "t": None}]), "no_time", "error"),
    "time going backwards": (lambda d: write(d, [rec(T0 + 100), rec(T0 + 10)]), "time_backwards", "error"),
    "a record of another day": (lambda d: write(d, [rec(T0 + 86400)]), "other_day", "warn"),
    "a time that disagrees with itself": (lambda d: write(d, [{**rec(), "time": "earlier today"}]), "time_disagrees", "warn"),
    "a time in another zone": (lambda d: write(d, [{**rec(), "time": "2026-11-06T04:30:05+00:00"}]), "time_disagrees", "warn"),
    "a version from the future": (lambda d: write(d, [rec(v=3)]), "newer_version", "warn"),
    "an event that is not an event": (lambda d: write(d, [rec(events=[{"text": "no number"}, "x"])]), "malformed_event", "warn"),
    "an event written twice": (lambda d: write(d, [rec(events=[event(0)]), rec(T0 + 5, events=[event(0)])]), "event_repeated", "warn"),
    "two events with one number": (lambda d: write(d, [rec(events=[event(0), event(0, "other:bull:2")])]), "event_number_shared", "warn"),
    "an event number missing": (lambda d: write(d, [rec(events=[event(0), event(3, status="WAITING_FOR_CONFIRMATION")])]), "event_number_missing", "warn"),
    "an event after the setup ended": (lambda d: write(d, [rec(events=[event(0), ended(1), event(2, status="CONFIRMED")])]), "event_after_end", "error"),
    "a status that goes backwards": (lambda d: write(d, [rec(events=[event(0, status="CONFIRMED"), event(1, status="DETECTED")])]), "status_backwards", "error"),
    "a status announced twice": (lambda d: write(d, [rec(events=[event(0), event(1)])]), "status_twice", "warn"),
    "a status nobody knows": (lambda d: write(d, [rec(events=[event(0, status="PAUSED")])]), "unknown_status", "warn"),
    "an ending with no code": (lambda d: write(d, [rec(events=[event(0), event(1, status="EXPIRED")])]), "ending_without_code", "warn"),
    "an outage that cost records": (lambda d: write(d, [rec(outage={"from": T0, "to": T0 + 5, "failed_writes": 3, "written_late": 4, "lost": 2})]), "records_lost", "warn"),
}  # fmt: skip


@pytest.mark.parametrize("case", list(CASES))
def test_each_problem_is_found_and_called_what_it_is(tmp_path, case):
    build, code, level = CASES[case]
    p = build(tmp_path)
    rep = verify(p)
    found = next(x for x in rep["problems"] if x["code"] == code)
    assert found["level"] == level and found["message"]
    assert rep["ok"] is (level != "error" and not rep["errors"]) and rep["errors"] == len(codes(rep, "error"))
    assert main([str(p), "--verify"]) == (1 if rep["errors"] else 0)  # the exit status says whether there is an error
    assert code in "\n".join(format_report(rep))


def test_a_problem_says_which_lines_it_is_on(tmp_path):
    p = garbage_line(tmp_path)
    x = next(x for x in verify(p)["problems"] if x["code"] == "unreadable_line")
    assert x["lines"] == [2] and x["count"] == 1  # the second line, as the reader's own "line 2"
    assert read(p)[1] == [2]


def test_a_legal_story_and_a_clean_file_raise_nothing(tmp_path):
    p = write(tmp_path, [rec(events=[event(0)]), rec(T0 + 10, events=[event(1, status="WAITING_FOR_CONFIRMATION")]), rec(T0 + 20, events=[event(2, status="CONFIRMED")]),
                         rec(T0 + 30, events=[ended(3)])])  # fmt: skip
    assert verify(p)["problems"] == []


def test_a_setup_that_never_ended_in_the_file_is_counted_not_blamed(tmp_path):
    rep = verify(write(tmp_path, [rec(events=[event(0)])]))
    assert rep["problems"] == [] and rep["summary"]["events"]["setups_not_ended_in_this_file"] == 1  # (today's file is like that all day)


# ====================================================================================================================
# 3. Read-only, and the report
# ====================================================================================================================
def test_checking_a_file_changes_nothing(tmp_path, capsys):
    p = garbage_line(tmp_path)
    before = (p.read_bytes(), p.stat().st_mtime_ns, sorted(os.listdir(tmp_path)))
    verify(p)
    main([str(p), "--verify"])
    main([str(p), "--verify", "--json"])
    main([str(p), "--detail", "--all"])
    assert (p.read_bytes(), p.stat().st_mtime_ns, sorted(os.listdir(tmp_path))) == before  # not a byte, not a timestamp, not a new file
    capsys.readouterr()


def test_the_report_can_be_asked_for_as_json_and_the_exit_status_says_the_rest(tmp_path, capsys):
    bad = write(tmp_path, [rec(action="MAYBE")])
    assert main([str(bad), "--verify", "--json"]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] is False and rep["errors"] == 1 and rep["problems"][0]["code"] == "bad_action" and rep["summary"]["records"] == 1
    assert main([str(tmp_path / "no-such-file.jsonl"), "--verify"]) == 2  # a file that cannot be opened
    assert "cannot be read" in capsys.readouterr().err
    assert main([str(tmp_path / "no-such-file.jsonl")]) == 2


def test_the_report_says_how_much_of_a_file_is_heartbeats_and_how_many_were_written_on_a_closed_market(tmp_path):
    recs = [rec(T0, "start"), rec(T0 + 300, "candle"), rec(T0 + 600, "candle", phase="closed"), rec(T0 + 900, "candle", phase="pre_open"), rec(T0 + 905, "changed"), rec(T0 + 1200, "candle")]
    rep = verify(write(tmp_path, recs))
    h = rep["summary"]["heartbeats"]
    assert h["records"] == 4 and h["while_market_closed"] == 2 and 0 < h["share_of_bytes"] < 1 and h["closed_bytes"] <= h["bytes"]
    assert rep["summary"]["by_reason"]["candle"]["records"] == 4 and rep["summary"]["by_reason"]["start"]["records"] == 1 and rep["summary"]["restarts"] == [{"line": 1, "time": recs[0]["time"]}]
    text = "\n".join(format_report(rep))
    assert "2 of 4 were written while the market was closed" in text


def test_the_outage_blocks_and_restarts_are_listed(tmp_path):
    rep = verify(write(tmp_path, [rec(T0, "start"), rec(T0 + 50, "restart", outage={"from": 1, "to": 2, "failed_writes": 1, "written_late": 2, "lost": 0})]))
    assert [x["line"] for x in rep["summary"]["restarts"]] == [1, 2] and rep["summary"]["outages"][0]["line"] == 2 and rep["problems"] == []


def test_the_reasons_and_decisions_the_checker_knows_are_the_ones_the_journal_writes(tmp_path):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    for slot in (16, 17, 18, 19, 20):
        w.step(md, clock, slot)
    recs = read(md.journal.path(clock.t))[0]
    assert {r["reason"] for r in recs} <= set(REASONS) and {r["decision"]["action"] for r in recs} <= set(ACTIONS)


# ====================================================================================================================
# 4. A record that is odd is shown as far as it goes
# ====================================================================================================================
ODD = [
    {"decision": {}, "t": "yesterday"},  # no reason, no headline, a time that is a word
    {"decision": {"headline": 5}, "reason": "changed", "t": T0, "market": "oops", "session": [], "setups": [None, {}, {"zone": "abc"}, {"direction": "CE", "zone": [1.0]}],
     "events": [None, {"time": "n"}], "counter_trend": [1, {}], "strategies": [None, {}], "price": "high"},  # fmt: skip
    {"decision": {"headline": "WAIT: ok"}, "reason": "changed", "t": T0 + 1, "feed": {"open_gap": "not a dict"}},  # one the review cannot show at all
    {"decision": {"headline": "WAIT: after"}, "reason": "changed", "t": T0 + 2},
]


def test_a_record_that_is_odd_is_shown_with_dashes_and_the_review_goes_on(tmp_path):
    p = write(tmp_path, ODD)
    lines = timeline(p, everything=True, detail=True)
    text = "\n".join(lines)
    assert "10:00:05  5   [price –]" in text  # a headline that is not text, a market that is not an object, a price that is not a number
    assert "–  –   [price –]" in text  # no headline, no action, a time that is a word
    assert "event: – – – — –" in text  # an event with nothing in it (the one that is not a dict is left out)
    assert any("could not be shown" in x for x in lines) and "WAIT: after" in text  # the record it cannot show costs one line, and the next one is still there
    assert main([str(p), "--all", "--detail"]) == 0


def test_a_decision_that_is_not_an_object_is_not_a_record(tmp_path):
    p = write(tmp_path, [rec()], tail=b'{"t": 1, "decision": null}\n{"t": 2, "decision": []}\n')
    records, bad = read(p)
    assert len(records) == 1 and bad == [2, 3]


# ====================================================================================================================
# 5. The API says what it could not read
# ====================================================================================================================
def served(tmp_path, tail=b""):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    path = md.journal.path(md.clock())
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in (rec(T0), rec(T0 + 10, action="WAIT"))) + tail)
    app = create_app(md, feed=None)
    app.state.md = md
    return route(app, "/api/journal").endpoint()


def test_the_api_says_how_many_lines_it_could_not_read_and_how_many_records_it_skipped(tmp_path):
    out = served(tmp_path, tail=b"not json\n" + json.dumps({**rec(T0 + 20), "market": "oops"}).encode() + b"\n" + json.dumps(rec(T0 + 30)).encode() + b"\n")
    assert out["available"] and out["bad_lines"] == 1 and out["bad_line_numbers"] == [3] and out["unreadable_records"] == 1
    assert out["records"] == 4 and len(out["decisions"]) == 3  # (the odd record is skipped, the others are all there)


def test_a_clean_journal_says_there_is_nothing_it_could_not_read(tmp_path):
    out = served(tmp_path)
    assert out["bad_lines"] == 0 and out["bad_line_numbers"] == [] and out["unreadable_records"] == 0 and len(out["decisions"]) == 2


# ====================================================================================================================
# 6. What gets written did not change
# ====================================================================================================================
def test_the_heartbeat_is_still_written_once_per_candle_also_on_a_closed_market(tmp_path):
    j = Journal(tmp_path)
    closed = stub_out()
    closed["session"] = {"phase": "closed"}
    j.observe(closed, A, [], T0)
    j.observe(closed, A, [], T0 + 301)
    j.observe(closed, A, [], T0 + 602)
    recs = read(tmp_path / NAME)[0]
    assert [r["reason"] for r in recs] == ["start", "candle", "candle"] and all(r["session"]["phase"] == "closed" for r in recs)
