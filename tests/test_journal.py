"""The decision journal: every decision and every lifecycle event is written down once, with the reasons, so that a
session can be reviewed after it has ended."""

import json
import re

import pytest
from helpers import FRI, ist
from test_phase3_integration import World

from app.journal import Journal, main, read, timeline


@pytest.fixture(scope="module")
def bull():
    return World()


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def run(world, directory, slots):
    md, clock = world.engine()
    md.journal = Journal(directory)
    for slot in slots:
        world.step(md, clock, slot)
    return md, clock, next(directory.glob("decisions-*.jsonl"))


def stub_out(headline="WAIT: nothing yet"):
    return {"price": 24500.0, "decision": {"action": "WAIT", "headline": headline, "setup_id": None, "reasons": [], "against": []},
            "market": None, "session": {"phase": "morning"}, "data": {"ok": True}, "setups": [], "strategies": []}  # fmt: skip


# -- what is written, and when ---------------------------------------------------------------------------------------------------------
def test_every_decision_and_lifecycle_event_is_written_exactly_once_with_its_details(bull, tmp_path):
    path = run(bull, tmp_path, (16, 17, 18, 19, 20))[2]
    assert path.name == "decisions-2026-11-06.jsonl"
    recs = lines(path)
    assert recs[0]["reason"] == "start" and [r["decision"]["action"] for r in recs] == ["WAIT", "WAIT", "WAIT", "BUY CE", "WAIT"]
    events = [e for r in recs for e in r["events"]]
    assert [e["status"] for e in events] == ["DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED", "EXPIRED"]
    assert len({e["seq"] for e in events}) == 4  # each event is on exactly one line
    assert "entry opportunity has passed" in events[-1]["text"]  # the reason it ended is on record
    buy = recs[3]
    s = buy["setups"][0]
    assert s["status"] == "CONFIRMED" and s["actionable"] and s["price_state"] == "in_entry_area"
    assert s["trigger"] and s["zone"] and s["invalidation"] and s["reasons"] and s["detected_at"] and s["confirmed_at"]
    candle = next(q for q in s["quality"] if q["name"] == "Trigger candle")
    assert "10:50-10:55 5M" in candle["text"] and "O " in candle["text"] and "C " in candle["text"]  # which candle, and its prices
    assert buy["market"]["regime"] == "trend_up" and buy["session"]["phase"] == "morning" and buy["data"]["ok"]
    assert buy["timeframes"]["5m"]["trend"] and buy["timeframes"]["5m"]["atr14_intraday"] and buy["analysis"]["reasons"]
    assert [m["id"] for m in buy["strategies"]] == ["trend_pullback", "breakout_retest", "range_reversal", "momentum_continuation", "ema_trend_continuation", "volatility_expansion",
                                                    "opening_range_breakout", "failed_breakout", "range_bounce", "trend_exhaustion_reversal"]  # fmt: skip


def test_reading_the_decision_many_times_does_not_flood_the_journal(bull, tmp_path):
    md, clock, path = run(bull, tmp_path, (19,))
    before = len(lines(path))
    for _ in range(50):  # the dashboard reads this several times a second; the price drifts, nothing else does
        clock.t += 0.1
        md.trading()
    assert len(lines(path)) == before


def test_each_candle_leaves_a_heartbeat_and_nothing_else_is_written_between(tmp_path):
    j, a, t0 = Journal(tmp_path), {"timeframes": {}, "condition": {}, "warnings": []}, ist(10, 0, 5, day=FRI)
    j.observe(stub_out(), a, [], t0)
    j.observe(stub_out(), a, [], t0 + 30)  # same candle, same state
    j.observe(stub_out(), a, [], t0 + 301)  # the next candle: "nothing changed" is also a fact worth having
    j.observe(stub_out("WAIT: something else"), a, [], t0 + 310)  # a change inside that candle
    recs = lines(tmp_path / "decisions-2026-11-06.jsonl")
    assert [r["reason"] for r in recs] == ["start", "candle", "changed"]


def test_an_event_is_written_once_even_if_the_state_is_unchanged(tmp_path):
    j, a, t0 = Journal(tmp_path), {"timeframes": {}, "condition": {}, "warnings": []}, ist(10, 0, 5, day=FRI)
    event = {"seq": 0, "time": t0, "text": "DETECTED: swing level 24,500.00", "direction": "CE", "strategy_name": "Trend Pullback", "status": "DETECTED"}
    j.observe(stub_out(), a, [], t0)
    j.observe(stub_out(), a, [event], t0 + 1)
    j.observe(stub_out(), a, [event], t0 + 2)  # the book still remembers it; the journal must not repeat it
    recs = lines(tmp_path / "decisions-2026-11-06.jsonl")
    assert [len(r["events"]) for r in recs] == [0, 1]


def test_a_journal_that_cannot_be_written_never_disturbs_trading(bull, tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    md, clock = bull.engine()
    md.journal = Journal(blocker)
    out = bull.step(md, clock, 19)
    assert out["decision"]["action"] == "BUY CE" and md.journal._failed  # the decision is unaffected; the failure is remembered


def test_the_file_is_named_after_the_session_date(bull, tmp_path):
    j, a = Journal(tmp_path), {"timeframes": {}, "condition": {}, "warnings": []}
    j.observe(stub_out(), a, [], ist(15, 0, day=FRI))
    j2 = Journal(tmp_path)
    j2.observe(stub_out(), a, [], ist(10, 0, day=(2026, 11, 9)))
    assert sorted(p.name for p in tmp_path.glob("decisions-*.jsonl")) == ["decisions-2026-11-06.jsonl", "decisions-2026-11-09.jsonl"]


# -- reading it back -------------------------------------------------------------------------------------------------------------------
def test_the_review_command_tells_the_story_of_the_session(bull, tmp_path, capsys):
    path = run(bull, tmp_path, (16, 17, 18, 19, 20))[2]
    text = "\n".join(timeline(path))
    assert "BUY CE: Trend Pullback setup confirmed" in text and "CE Trend Pullback: CONFIRMED ACTIONABLE" in text
    assert "detected 10:" in text and "confirmed 10:55" in text and "trigger " in text and "invalidation " in text
    assert "event: " in text and "EXPIRED: price closed" in text
    detail = "\n".join(timeline(path, detail=True))
    assert "Trigger candle: 10:50-10:55 5M" in detail and "+ bullish rejection candle" in detail and "strategy Trend Pullback" in detail
    main([str(path)])
    assert "BUY CE" in capsys.readouterr().out


def test_the_review_shows_a_gap_open_and_leaves_out_heartbeats_unless_asked(tmp_path):
    path = run(World(gap=400.0), tmp_path, (3, 4, 5))[2]
    assert "opened +400 pts (+1.65%) from the previous close" in "\n".join(timeline(path))
    quiet, everything = timeline(path), timeline(path, everything=True)
    assert len(everything) >= len(quiet)
    # Every timestamped line is a decision line ("09:35:05  WAIT: ..." / "BUY CE: ..."), everything else is an indented detail of it. (This world used to give
    # nothing but WAIT in its first minutes; since Stage 2.2 its gap-up breakout out of the opening range is, correctly, an Opening Range Breakout buy.)
    assert all(re.match(r"\d\d:\d\d:\d\d  (WAIT|BUY CE|BUY PE|NO TRADE): ", line) or line.startswith("      ") for line in everything)


def test_a_session_without_history_still_leaves_its_reason_on_record(tmp_path):
    from helpers import CFG, Clock

    from app.engine import MarketData

    md = MarketData(CFG, Clock(ist(10, 0, day=FRI)))
    md.journal = Journal(tmp_path)
    md.trading()
    md.trading()
    recs = lines(next(tmp_path.glob("decisions-*.jsonl")))
    assert len(recs) == 1 and recs[0]["decision"]["action"] == "NO TRADE" and "still loading" in recs[0]["decision"]["headline"]
    assert recs[0]["setups"] == [] and recs[0]["market"] is None


# -- a crash, a restart and the connection ------------------------------------------------------------------------------------------
def cut_short(path):
    good = path.read_text(encoding="utf-8")
    path.write_text(good + good.splitlines()[-1][:300], encoding="utf-8")  # the last record cut off mid-line, as after a crash or a power cut


def test_a_journal_cut_short_by_a_crash_is_still_readable(bull, tmp_path, capsys):
    path = run(bull, tmp_path, (16, 17, 18, 19))[2]
    cut_short(path)
    records, bad = read(path)
    assert len(bad) == 1 and len(records) == 4
    text = timeline(path)
    assert any("BUY CE: Trend Pullback setup confirmed" in line for line in text) and "1 line(s) could not be read" in text[-1]
    main([str(path)])
    assert "BUY CE" in capsys.readouterr().out  # the command no longer stops at the damaged line


def test_a_line_that_is_not_a_journal_record_is_skipped_too(bull, tmp_path):
    path = run(bull, tmp_path, (16, 17))[2]
    path.write_text(path.read_text(encoding="utf-8") + "\n{\"hello\": 1}\nnot json at all\n[1, 2]\n", encoding="utf-8")
    records, bad = read(path)
    assert len(records) == 2 and len(bad) == 3


def test_the_next_run_starts_on_a_fresh_line_after_a_cut_record(bull, tmp_path):
    path = run(bull, tmp_path, (16, 17, 18, 19))[2]
    cut_short(path)
    Journal(tmp_path).observe(stub_out("WAIT: after the crash"), {"timeframes": {}, "condition": {}, "warnings": []}, [], ist(15, 0, day=FRI))  # started again
    records, bad = read(path)
    assert len(bad) == 1 and records[-1]["decision"]["headline"] == "WAIT: after the crash"  # only the cut record was lost, not the new one


def feed(t0, **over):
    base = {"state": "connected", "since": t0, "reconnects": 0, "last_tick_age": 0.4, "ticks_accepted": 10, "ticks_rejected": {}, "open_gap": None,
            "last_gap": None, "history": {"state": "ready", "error": None}, "errors": []}  # fmt: skip
    return {**base, **over}


def test_the_connection_is_on_record_when_it_changes_and_not_when_only_the_tick_age_moves(tmp_path):
    j, a, t0 = Journal(tmp_path), {"timeframes": {}, "condition": {}, "warnings": []}, ist(10, 0, 5, day=FRI)
    gap = {"start": t0 + 5, "end": None, "reason": "disconnect", "ended_by": None}
    closed = dict(gap, end=t0 + 70)
    steps = [
        (0, feed(t0)),
        (1, feed(t0, last_tick_age=9.0, ticks_accepted=500)),  # only the tick age and a counter move: nothing is written
        (30, feed(t0 + 30, state="disconnected", open_gap=gap, errors=[{"time": t0 + 30, "message": "websocket closed"}])),
        (31, feed(t0 + 30, state="disconnected", open_gap=gap, last_tick_age=26.0)),  # still down: nothing new
        (60, feed(t0 + 60, state="connecting", reconnects=1, open_gap=gap)),
        (62, feed(t0 + 62, state="connected", reconnects=1, open_gap=gap)),
        (70, feed(t0 + 62, state="connected", reconnects=1, last_gap=closed)),  # ticks are back: the gap closed
        (80, feed(t0 + 62, state="connected", reconnects=1, last_gap=closed, history={"state": "ready", "error": "backfill incomplete (1 failed)"})),
    ]  # fmt: skip
    for dt, f in steps:
        j.observe(stub_out(), a, [], t0 + dt, f)
    path = tmp_path / "decisions-2026-11-06.jsonl"
    recs = lines(path)
    assert [(r["feed"]["state"], r["feed"]["reconnects"], bool(r["feed"]["open_gap"]), bool(r["feed"]["last_gap"])) for r in recs] == [
        ("connected", 0, False, False), ("disconnected", 0, True, False), ("connecting", 1, True, False), ("connected", 1, True, False),
        ("connected", 1, False, True), ("connected", 1, False, True)]  # fmt: skip
    assert recs[1]["feed"]["errors"][-1]["message"] == "websocket closed" and recs[-1]["feed"]["history"]["error"].startswith("backfill incomplete")
    text = "\n".join(timeline(path))
    assert "feed: connected since 10:00:05 · reconnects 0" in text
    assert "feed: disconnected since 10:00:35 · reconnects 0 · NO TICKS since 10:00:10 (disconnect) · last error: websocket closed" in text
    assert "last gap 10:00:10 to 10:01:15 (disconnect)" in text and "history ready: backfill incomplete (1 failed)" in text


def test_the_first_line_after_a_restart_says_so_and_earlier_events_are_not_written_again(tmp_path):
    j, a, t0 = Journal(tmp_path), {"timeframes": {}, "condition": {}, "warnings": []}, ist(10, 0, 5, day=FRI)

    def event(n):
        return {"seq": n, "time": t0, "id": f"x:{n}", "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "direction": "CE", "status": "DETECTED", "text": "DETECTED: x"}

    j.resumed(5)  # the engine picked up a run whose events 0..4 are already in the file
    j.observe(stub_out(), a, [event(4), event(3)], t0)  # restored events, newest first: already on record
    j.observe(stub_out("WAIT: next"), a, [event(5), event(4), event(3)], t0 + 1)  # a new one
    path = tmp_path / "decisions-2026-11-06.jsonl"
    recs = lines(path)
    assert [r["reason"] for r in recs] == ["restart", "changed"] and recs[0]["events"] == [] and [e["seq"] for e in recs[1]["events"]] == [5]
    assert any("the app was restarted" in line for line in timeline(path))
    assert Journal(tmp_path).load_day(t0)[0]["reason"] == "restart" and Journal(tmp_path / "nowhere").load_day(t0) == []


def test_the_review_does_not_crash_on_a_console_that_cannot_show_a_tick_mark(bull, tmp_path, monkeypatch):
    import io
    import sys

    path = run(bull, tmp_path, (16, 17, 18, 19))[2]
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")  # a Windows pipe or file in the ANSI code page
    monkeypatch.setattr(sys, "stdout", legacy)
    main([str(path), "--detail"])  # the detail view prints "✓" and "⏸", which that code page cannot encode
    legacy.flush()
    assert b"BUY CE" in legacy.buffer.getvalue() and b"Trigger candle" in legacy.buffer.getvalue()
