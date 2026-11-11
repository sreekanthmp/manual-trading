"""Stage 6.5: after a restart the journal puts Prism back where it was, and says what it did.

Held here, through real journal files (written by the real journal from presented decisions, read back by a new engine that resumes from them):
  * all ten strategies, both sides: a live setup comes back from its own journal with every field it had, marked resumed, with the event numbering going on and the ended ones staying ended
  * a restart at any point of a setup's life (detected, waiting, confirmed, ended) changes nothing that follows: the restarted engine and the one that never stopped decide and announce the same
  * restarting twice in a row, and again later, keeps one story: each event once, numbered without a gap, and the file verifies clean
  * a record cut short in the middle of an event costs that record, and the event is announced once when the candles say so
  * another day, or another journal folder, starts afresh and touches nothing of the old file
  * the first record of a restarted run says what the restart carried over (or why it could not), in the file, the review and the API
  * the old records (real version-1 ones) restore as they always did"""

import json
import shutil
from pathlib import Path

import market as MK  # noqa: F401  (used through the World)
import pytest
from helpers import CFG, FRI, Clock, ist
from test_options_integration import FLOWS, NAMES, SIDES
from test_phase3_integration import World, create_app, restarted, route
from test_trend_family import P, oriented

from app import trading as tr
from app.engine import MarketData
from app.journal import Journal, read, timeline
from app.journal_check import verify

FIXTURE = Path(__file__).parent / "golden" / "journal_v1_sample.jsonl"
KEEP = ("id", "strategy", "side", "direction", "status", "level", "level_label", "trigger", "zone", "invalidation", "extreme", "detected_at", "status_since", "confirmed_at",
        "confirmed_bar_end", "confirm_text", "note", "reasons", "against", "blockers", "quality", "support", "resistance", "candle_confirmed", "plan", "group", "counter_trend",
        "confirm_candle", "break_candle")  # fmt: skip


def jsonable(x):
    return json.loads(json.dumps(x, allow_nan=False, default=str))


def present_step(ctx, core):
    """What the engine presents for one evaluation of the scenario (the same construction as test_trend_family.decision_after, done at each step)."""
    t = ctx.trend
    opposite = {"bullish": "bearish", "bearish": "bullish"}
    trend = t["1h"] if t["1h"] in opposite and t["15m"] != opposite[t["1h"]] else "sideways"
    a = {"price": ctx.price, "condition": {"trend": trend, "volatility": ctx.volatility, "alignment": {"label": ctx.alignment, "text": "x"}},
         "timeframes": {tf: {"trend": t[tf], "adx": ctx.adx.get(tf), "atr14_intraday": ctx.atr.get(tf), "atr14": ctx.atr.get(tf)} for tf in t}}  # fmt: skip
    env = {"session": tr.session_info(ctx.now, P), "data": {"ok": True, "issues": []}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None, "gap": None}
    return a, tr.present(core, a, env, ctx.now, P, 1.0)


def journaled(name, side, directory):
    """A strategy's own scenario, candle by candle through the real SetupBook, every evaluation journalled as MarketData.trading() does it."""
    build, throughs, kw = FLOWS[name]
    book, journal, last = tr.SetupBook(P), Journal(directory), None
    for n in throughs:
        ctx = oriented(build(n, **kw), side)
        core = book.update(ctx, {"session": tr.session_info(ctx.now, P)}, ctx.now)
        a, out = present_step(ctx, core)
        journal.observe(out, a, core["events"], ctx.now, None)
        last = (ctx, out)
    return book, journal, last


def announced_ended(book):
    """The setups that ended AND were announced (they have an INVALIDATED / EXPIRED event). `terminal` also holds structures that ended without ever being announced: the journal has nothing about
    those, and after a restart they are found again by the next scan and set aside silently, so nothing is announced either way."""
    return {e["id"] for e in book.events if e["status"] in ("INVALIDATED", "EXPIRED")}


def engine_at(directory, at):
    md = MarketData(CFG, Clock(at))
    md.journal = Journal(directory)
    return md, md.resume()


def day_file(directory, clock):
    return Journal(directory).path(clock)


# ====================================================================================================================
# 1. Every strategy's setup comes back from its own journal
# ====================================================================================================================
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_every_strategys_live_setup_comes_back_from_its_own_journal_file_exactly(tmp_path, name, side):
    book, journal, (ctx, out) = journaled(name, side, tmp_path)
    assert book.active, "the scenario ends with a live setup"
    md, n = engine_at(tmp_path, ctx.now + 20)
    assert n == len(book.active) and set(md.book.active) == set(book.active) and all(s.resumed for s in md.book.active.values())
    for slot, s in book.active.items():
        was, now = jsonable(tr.setup_dict(s, ctx.price, True, True)), jsonable(tr.setup_dict(md.book.active[slot], ctx.price, True, True))
        assert [k for k in KEEP if was.get(k) != now.get(k)] == [], (name, side)  # every field it had, the permission record of a reversal included
    assert md.book.seq == book.seq and md.book.terminal == announced_ended(book) and md.book.terminal <= book.terminal  # the numbering goes on; what had ended (and was announced) stays ended
    assert [e["seq"] for e in md.book.events] == [e["seq"] for e in list(book.events)[:60]]
    md.safe_trading()  # the first evaluation of the new run (the history is still loading) is the restart record
    first = read(journal.path(ctx.now))[0][-1]
    assert first["reason"] == "restart" and first["restart"] == {"ok": True, "error": None, "records_read": len(read(journal.path(ctx.now))[0]) - 1, "restored": sorted(s.id for s in book.active.values()),
                                                                 "ended": sorted(announced_ended(book)), "events_carried": len(book.events), "next_event": book.seq}  # fmt: skip


# ====================================================================================================================
# 2. A restart at any point of a setup's life changes nothing that follows
# ====================================================================================================================
@pytest.mark.parametrize("slot,state", [(16, "DETECTED"), (17, "WAITING_FOR_CONFIRMATION"), (19, "CONFIRMED"), (20, None)])
def test_a_restart_at_any_point_of_a_setups_life_changes_nothing_that_follows(tmp_path, slot, state):
    w = World()
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    md_a, clock_a = w.engine()
    md_a.journal = Journal(a_dir)
    for s in range(16, slot + 1):
        w.step(md_a, clock_a, s)
    live = [x.status for x in md_a.book.active.values()]
    assert live == ([state] if state else [])  # the setup is where the scenario says it is
    shutil.copytree(a_dir, b_dir)  # the journal as it was when the process died
    md_b, clock_b, resumed = restarted(w, b_dir, clock_a.t + 20, clock_a.t)
    assert resumed == len(live) and [x.status for x in md_b.book.active.values()] == live and md_b.book.seq == md_a.book.seq and md_b.book.terminal == announced_ended(md_a.book)
    out_a, out_b = w.step(md_a, clock_a, slot + 1), w.step(md_b, clock_b, slot + 1)  # the next candle completes: the one that never stopped, and the one that restarted
    assert out_a["decision"] == out_b["decision"] and [(s["id"], s["status"], s["stage"]) for s in out_a["setups"]] == [(s["id"], s["status"], s["stage"]) for s in out_b["setups"]]
    assert [(e["seq"], e["id"], e["status"], e["text"]) for e in out_a["events"]] == [(e["seq"], e["id"], e["status"], e["text"]) for e in out_b["events"]]  # nothing announced twice, nothing missed
    assert md_a.book.seq == md_b.book.seq and announced_ended(md_a.book) == announced_ended(md_b.book) and md_b.book.terminal <= md_a.book.terminal


def test_a_setup_that_had_ended_stays_ended_through_a_restart_even_if_the_candles_still_look_the_same(tmp_path):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for s in (16, 17, 18, 19, 20):
        w.step(md1, clock1, s)
    (key,) = announced_ended(md1.book)  # the pullback: announced, then expired
    md2, clock2, resumed = restarted(w, tmp_path, clock1.t + 20, clock1.t)
    assert resumed == 0 and key in md2.book.terminal  # ended, and remembered as ended
    out = md2.trading()
    assert key not in [s["id"] for s in out["setups"]] and [e["status"] for e in out["events"]].count("DETECTED") == 1  # not announced again


def test_an_invalidated_setup_stays_ended_through_a_restart(tmp_path):
    """(Hand-written records: the scenario worlds end their setups by expiry. An invalidation is the other ending, and has its own code.)"""
    sid = "trend_pullback:bull:1793940000.0"
    events = [{"seq": 0, "time": 1.0, "id": sid, "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "direction": "CE", "status": "WAITING_FOR_CONFIRMATION", "text": "x"},
              {"seq": 1, "time": 2.0, "id": sid, "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "direction": "CE", "status": "INVALIDATED", "text": "INVALIDATED: closed below", "code": "invalidated"}]  # fmt: skip
    rec = {"v": 2, "time": "t", "t": ist(10, 0, day=FRI), "reason": "changed", "decision": {"action": "WAIT", "headline": "WAIT: x"}, "market": {"regime": "trend_up"}, "setups": [], "events": events}
    md, _ = World().engine()
    path = Journal(tmp_path).path(md.clock())
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    md.journal = Journal(tmp_path)
    assert md.resume() == 0 and sid in md.book.terminal and md.book.seq == 2 and md.journal._restart_info["ended"] == [sid]


# ====================================================================================================================
# 3. Restarting twice, and again later
# ====================================================================================================================
def test_restarting_twice_in_a_row_and_again_later_keeps_one_story_and_a_file_that_verifies_clean(tmp_path):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for s in (16, 17):
        w.step(md1, clock1, s)
    md2, clock2, r2 = restarted(w, tmp_path, clock1.t + 20, clock1.t)  # restarted and killed again before it evaluated anything: it wrote nothing
    md3, clock3, r3 = restarted(w, tmp_path, clock1.t + 40, clock1.t)
    assert r2 == r3 == 1
    w.step(md3, clock3, 18)
    w.step(md3, clock3, 19)  # the buy
    md4, clock4, r4 = restarted(w, tmp_path, clock3.t + 20, clock3.t)  # and once more, after the buy
    assert r4 == 1
    w.step(md4, clock4, 20)  # the opportunity passes
    path = day_file(tmp_path, clock4.t)
    recs, bad = read(path)
    events = [e for r in recs for e in r["events"]]
    assert bad == [] and [e["seq"] for e in events] == [0, 1, 2, 3] and [e["status"] for e in events] == ["DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED", "EXPIRED"]
    assert [r["reason"] for r in recs].count("start") == 1 and [r["reason"] for r in recs].count("restart") == 2  # (md2 wrote nothing: its restart was never evaluated)
    rep = verify(path)
    assert rep["problems"] == [] and rep["summary"]["restarts"][0]["line"] == 1 and [x.get("restored") for x in rep["summary"]["restarts"][1:]] == [1, 1]


# ====================================================================================================================
# 4. A record cut short in the middle of an event
# ====================================================================================================================
def test_a_record_cut_in_the_middle_of_an_event_costs_that_record_and_the_event_is_announced_once(tmp_path):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for s in (16, 17, 18, 19):
        w.step(md1, clock1, s)
    path = day_file(tmp_path, clock1.t)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "CONFIRMED" in lines[-1]  # the last record carries the CONFIRMED event
    path.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][: len(lines[-1]) // 2], encoding="utf-8")  # the power went while it was being written
    md2, clock2, resumed = restarted(w, tmp_path, clock1.t + 20, clock1.t)
    assert resumed == 1 and [s.status for s in md2.book.active.values()] == ["WAITING_FOR_CONFIRMATION"] and md2.book.seq == 2  # the last whole record said waiting
    out = md2.trading()
    assert out["decision"]["action"] == "BUY CE"  # the candles say it was confirmed
    recs, bad = read(path)
    events = [e for r in recs for e in r["events"]]
    assert len(bad) == 1 and [e["status"] for e in events] == ["DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED"] and [e["seq"] for e in events] == [0, 1, 2]  # once, and no number skipped
    rep = verify(path)
    assert [x["code"] for x in rep["problems"]] == ["unreadable_line"] and rep["ok"]  # the cut line is the file's only problem
    app = create_app(md2, feed=None)
    app.state.md = md2
    api = route(app, "/api/journal").endpoint()
    assert api["bad_lines"] == 1 and len(api["events"]) == 3


# ====================================================================================================================
# 5. Another day, another folder
# ====================================================================================================================
def test_a_restart_on_another_day_starts_afresh_and_leaves_the_old_file_alone(tmp_path):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for s in (16, 17, 18, 19):
        w.step(md1, clock1, s)
    old = day_file(tmp_path, clock1.t)
    before = (old.read_bytes(), old.stat().st_mtime_ns)
    md2, n = engine_at(tmp_path, ist(10, 0, day=(2026, 11, 9)))  # the next trading day
    assert n == 0 and not md2.book.active and md2.journal._restart_info is None and not md2.journal._restarted
    md2.safe_trading()
    new = day_file(tmp_path, ist(10, 0, day=(2026, 11, 9)))
    recs = read(new)[0]
    assert new != old and [r["reason"] for r in recs] == ["start"] and "restart" not in recs[0]  # a new day starts; it does not "restart"
    assert (old.read_bytes(), old.stat().st_mtime_ns) == before


def test_a_restart_with_another_journal_folder_starts_afresh_and_leaves_the_old_one_alone(tmp_path):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path / "logs")
    for s in (16, 17, 18, 19):
        w.step(md1, clock1, s)
    old = day_file(tmp_path / "logs", clock1.t)
    before = (old.read_bytes(), old.stat().st_mtime_ns)
    md2, n = engine_at(tmp_path / "elsewhere", clock1.t + 20)
    assert n == 0 and not md2.book.active and md2.book.seq == 0  # nothing to restore here: the setups are re-derived from the candles, as the README says
    md2.safe_trading()
    assert [r["reason"] for r in read(day_file(tmp_path / "elsewhere", clock1.t))[0]] == ["start"] and (old.read_bytes(), old.stat().st_mtime_ns) == before


# ====================================================================================================================
# 6. The restart record says what the restart did
# ====================================================================================================================
def test_the_restart_record_says_what_was_carried_over_in_the_file_the_review_and_the_api(tmp_path):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for s in (16, 17, 18, 19):
        w.step(md1, clock1, s)
    sid = next(iter(md1.book.active.values())).id
    md2, clock2, resumed = restarted(w, tmp_path, clock1.t + 20, clock1.t)
    md2.trading()
    path = day_file(tmp_path, clock1.t)
    rec = read(path)[0][-1]
    assert rec["reason"] == "restart" and rec["restart"] == {"ok": True, "error": None, "records_read": 4, "restored": [sid], "ended": [], "events_carried": 3, "next_event": 3}
    text = "\n".join(timeline(path))
    assert f"carried over 1 live setup(s): {sid}; 0 that ended earlier today stay ended; 3 event(s) carried, the next is number 3" in text
    app = create_app(md2, feed=None)
    app.state.md = md2
    assert route(app, "/api/journal").endpoint()["decisions"][0]["restart"] == rec["restart"]
    assert "restart" not in {k for r in read(path)[0][:-1] for k in r}  # (only the first record of a run has it)


def test_a_restore_that_failed_says_why_and_the_checker_warns(tmp_path, monkeypatch):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for s in (16, 17, 18, 19):
        w.step(md1, clock1, s)
    md2, clock2 = w.engine()
    clock2.t = clock1.t + 20
    md2.journal = Journal(tmp_path)
    monkeypatch.setattr(tr.SetupBook, "restore", lambda self, records, now: (_ for _ in ()).throw(ValueError("restore failed (test)")))
    assert md2.resume() == 0
    monkeypatch.undo()
    md2.trading()
    path = day_file(tmp_path, clock1.t)
    rec = read(path)[0][-1]
    assert rec["reason"] == "restart" and rec["restart"]["ok"] is False and "restore failed (test)" in rec["restart"]["error"] and rec["restart"]["next_event"] == 3 and rec["restart"]["restored"] == []
    text = "\n".join(timeline(path))
    assert "could NOT be restored" in text and "numbered from 3" in text
    rep = verify(path)
    assert [x["code"] for x in rep["problems"]] == ["restore_failed"] and rep["ok"] and rep["summary"]["restarts"][-1]["ok"] is False


def test_the_first_run_of_a_day_has_no_restart_to_report(tmp_path):
    md, n = engine_at(tmp_path, ist(10, 0, day=FRI))
    assert n == 0 and md.journal._restart_info is None
    md.safe_trading()
    recs = read(day_file(tmp_path, ist(10, 0, day=FRI)))[0]
    assert [r["reason"] for r in recs] == ["start"] and "restart" not in recs[0]


# ====================================================================================================================
# 7. The old records
# ====================================================================================================================
def test_real_version_1_records_restore_as_they_always_did_and_the_restart_says_so(tmp_path):
    md, clock = World().engine()
    clock.t = ist(11, 30, day=FRI)  # after the last record of the fixture (11:20)
    path = Journal(tmp_path).path(md.clock())
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    md.journal = Journal(tmp_path)
    assert md.resume() == 1 and [s.resumed for s in md.book.active.values()] == [True]
    info = md.journal._restart_info
    assert info["ok"] and len(info["restored"]) == 1 and len(info["ended"]) == 1 and info["events_carried"] == 2 and info["records_read"] == 4 and info["next_event"] == md.book.seq
    md.safe_trading()
    recs = read(path)[0]
    assert [r.get("v", 1) for r in recs] == [1, 1, 1, 1, 2] and recs[-1]["reason"] == "restart" and recs[-1]["restart"] == info
    assert verify(path)["problems"] == []  # version 1 and version 2 in one file, restarted: clean


@pytest.mark.parametrize("flow", ["trend_pullback", "trend_exhaustion_reversal"])
def test_a_journal_of_version_1_records_of_a_live_setup_restores_the_same_setup(tmp_path, flow):
    """The records of a live setup, with the keys version 2 added taken away (what a journal written before Stage 6.2 looks like), restore to the same setup."""
    book, journal, (ctx, out) = journaled(flow, "bull", tmp_path)
    path = journal.path(ctx.now)
    old = []
    for r in read(path)[0]:
        r.pop("v", None)
        r.pop("decision_setup", None)
        r["decision"].pop("also_setups", None)
        for e in r["events"]:
            for k in ("code", "superseded_by", "final"):
                e.pop(k, None)
        old.append(json.dumps(r))
    path.write_text("\n".join(old) + "\n", encoding="utf-8")
    md, n = engine_at(tmp_path, ctx.now + 20)
    assert n == len(book.active)
    for slot, s in book.active.items():
        was, now = jsonable(tr.setup_dict(s, ctx.price, True, True)), jsonable(tr.setup_dict(md.book.active[slot], ctx.price, True, True))
        assert [k for k in KEEP if was.get(k) != now.get(k)] == []
