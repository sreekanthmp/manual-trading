"""Stage 6.3: a journal record that was made reaches the disk, and the journal says where it is and whether it is being written.

Found in the Stage 6.0 audit and held here:
  * a storage that cannot be written for a while (a full disk, a locked file, a folder that is gone) used to cost every decision change in that time, and the lifecycle events beyond the book's
    last 60. Now each record is made as usual and kept in memory (bounded), then written in order, with its own time, when the storage is back; the first record after says what the outage cost
  * a write that fails halfway leaves a cut line: the next one starts on a fresh line and the record is written again whole
  * every record but the once-per-candle heartbeat is synced to the disk (fsync); a sync that fails does not write a line twice
  * the folder is absolute: `logs` beside the app (or JOURNAL_DIR), whichever folder Prism was started in, and the one-copy lock lives in the same folder; /api/health and /api/journal say where it is
    and whether records are waiting
  * a run whose restore failed numbers its events after those already on record, and an event is identified by more than its number
  * a NaN costs a number (it is written as null), not the record"""

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest
from helpers import CFG, FRI, ist
from test_journal import stub_out
from test_phase3_integration import World, create_app, route

from app import main as app_main
from app import trading as tr
from app.journal import Journal, read

A = {"timeframes": {}, "condition": {}, "warnings": []}
T0 = ist(10, 0, 5, day=FRI)


@pytest.fixture
def storage(monkeypatch):
    """The storage, which can be taken away: `down.on = True` makes every write raise (what a full disk or a locked file does)."""
    real = Journal._append

    class State:
        on = False  # every write fails
        fail = 0  # the next this-many writes fail, then it works (a storage that flickers)
        calls = 0

    def flaky(self, path, line, sync=True):
        State.calls += 1
        if State.fail > 0:
            State.fail -= 1
            raise OSError(5, "I/O error (test)")
        if State.on:
            raise OSError(28, "No space left on device (test)")
        return real(self, path, line, sync)

    monkeypatch.setattr(Journal, "_append", flaky)
    return State


def view(records):
    return [(r["t"], r["reason"], r["decision"]["headline"], [e["seq"] for e in r["events"]]) for r in records]


def play(directory, slots, between=None):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(directory)
    outs = []
    for slot in slots:
        if between:
            between(slot)
        outs.append(w.step(md, clock, slot))
    return md, outs


# ====================================================================================================================
# 1. A storage that comes back loses nothing
# ====================================================================================================================
def test_a_journal_that_cannot_be_written_for_a_while_loses_nothing_when_the_storage_is_back(tmp_path, storage):
    control, control_outs = play(tmp_path / "control", (16, 17, 18, 19, 20))
    down = {16, 17, 18}  # the storage is gone for the first three evaluations
    md, outs = play(tmp_path / "outage", (16, 17, 18, 19, 20), between=lambda slot: setattr(storage, "on", slot in down))
    assert [o["decision"]["headline"] for o in outs] == [o["decision"]["headline"] for o in control_outs]  # trading never noticed
    got, bad = read(md.journal.path(md.clock()))
    want, _ = read(control.journal.path(control.clock()))
    assert bad == [] and view(got) == view(want)  # every record, in order, with the time it was made (not the time it was written), each event once
    late = next(r for r in got if "outage" in r)
    assert late["outage"]["written_late"] >= 2 and late["outage"]["lost"] == 0 and late["outage"]["failed_writes"] >= 3 and late["outage"]["from"] < late["outage"]["to"]
    assert sum("outage" in r for r in got) == 1 and "outage" not in {k for r in want for k in r}  # said once, by the first record after
    assert [e["status"] for r in got for e in r["events"]] == ["DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED", "EXPIRED"]
    st = md.journal.status(md.clock())
    assert st["ok"] and st["pending"] == 0 and st["lost"] == 0 and st["written"] == len(got)


def test_while_it_waits_the_journal_says_so_and_the_health_page_warns_without_stopping_anything(tmp_path, storage):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    storage.on = True
    out = w.step(md, clock, 17)
    assert out["decision"]["action"] == "WAIT" and md.journal._failed  # (the decision is unaffected)
    st = md.journal.status(clock.t)
    assert st["ok"] is False and st["pending"] == 1 and "No space left" in st["last_error"] and st["since"] == clock.t and Path(st["directory"]).is_absolute()
    h = md.health(clock.t)
    assert h["journal"] == st and any("decision journal cannot be written" in x and "1 record(s) are held in memory" in x for x in h["warnings"])
    assert not any("journal" in x for x in h["issues"])  # a warning, never an issue: the data is still trusted for the decision
    storage.on = False
    md.trading()  # the next evaluation finds the storage back
    assert md.journal.status(clock.t)["ok"] and not any("journal" in x for x in md.health(clock.t)["warnings"])
    assert len(read(md.journal.path(clock.t))[0]) == 1


def test_a_full_queue_drops_a_heartbeat_first_and_counts_what_it_lost(tmp_path, storage):
    j = Journal(tmp_path, max_pending=3)
    storage.on = True
    event = {"seq": 0, "time": T0, "id": "x", "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "direction": "CE", "status": "DETECTED", "text": "DETECTED: x"}
    j.observe(stub_out(), A, [], T0)  # start
    j.observe(stub_out(), A, [], T0 + 301)  # a heartbeat
    j.observe(stub_out("WAIT: changed"), A, [event], T0 + 305)  # a change that carries an event
    j.observe(stub_out("WAIT: changed"), A, [], T0 + 602)  # a heartbeat: the queue is full (3), so the OLDER heartbeat goes
    assert [p[2] for p in j._pending] == [False, False, True] and j.status(T0)["lost"] == 1
    j.observe(stub_out("WAIT: again"), A, [], T0 + 610)  # another change: now the newest heartbeat goes
    assert [p[2] for p in j._pending] == [False, False, False] and j.status(T0)["lost"] == 2
    j.observe(stub_out("WAIT: and again"), A, [], T0 + 620)  # no heartbeat left: the oldest record goes
    assert j.status(T0)["lost"] == 3 and len(j._pending) == 3
    storage.on = False
    j.observe(stub_out("WAIT: back"), A, [], T0 + 700)
    recs, bad = read(tmp_path / "decisions-2026-11-06.jsonl")
    assert bad == [] and [r["decision"]["headline"] for r in recs] == ["WAIT: changed", "WAIT: again", "WAIT: and again", "WAIT: back"]
    assert recs[-1]["outage"]["lost"] == 3 and recs[-1]["outage"]["written_late"] == 6 and [e["seq"] for r in recs for e in r["events"]] == [0]  # the event survived: it was on a kept record


def test_records_are_never_written_ahead_of_older_ones_that_are_waiting(tmp_path, storage):
    j = Journal(tmp_path)
    storage.on = True
    j.observe(stub_out("WAIT: one"), A, [], T0)
    j.observe(stub_out("WAIT: two"), A, [], T0 + 10)
    storage.on = False
    j.observe(stub_out("WAIT: two"), A, [], T0 + 11)  # the storage works again, and nothing is new: the waiting ones are written
    storage.on = True
    j.observe(stub_out("WAIT: three"), A, [], T0 + 20)  # down again
    storage.on = False
    j.observe(stub_out("WAIT: four"), A, [], T0 + 30)
    recs = read(tmp_path / "decisions-2026-11-06.jsonl")[0]
    assert [r["decision"]["headline"] for r in recs] == ["WAIT: one", "WAIT: two", "WAIT: three", "WAIT: four"] and [r["t"] for r in recs] == sorted(r["t"] for r in recs)


def test_a_record_is_not_written_ahead_of_older_ones_even_when_the_storage_flickers(tmp_path, storage):
    j = Journal(tmp_path)
    storage.on = True
    j.observe(stub_out("WAIT: one"), A, [], T0)  # waiting
    storage.on = False
    storage.fail = 1  # the next write fails and the one after it would work
    j.observe(stub_out("WAIT: two"), A, [], T0 + 10)  # the waiting record is tried first and fails: the new one must not take its place on disk
    assert read(tmp_path / "decisions-2026-11-06.jsonl")[0] == [] if (tmp_path / "decisions-2026-11-06.jsonl").exists() else True
    j.observe(stub_out("WAIT: two"), A, [], T0 + 11)
    assert [r["decision"]["headline"] for r in read(tmp_path / "decisions-2026-11-06.jsonl")[0]] == ["WAIT: one", "WAIT: two"]


def test_the_status_is_not_ok_while_any_record_is_waiting_whatever_else_it_knows():
    j = Journal("nowhere-in-particular")
    assert j.status(T0)["ok"] is True and j.status(T0)["pending"] == 0
    j._pending.append((j.path(T0), "{}" + chr(10), False))  # (a record waiting, with no failure recorded: a bug elsewhere must not read as health)
    assert j.status(T0)["ok"] is False and j.status(T0)["pending"] == 1


def test_a_folder_that_is_gone_comes_back(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    j = Journal(blocker)
    j.observe(stub_out("WAIT: one"), A, [], T0)
    assert j._failed and len(j._pending) == 1 and j.status(T0)["ok"] is False
    blocker.unlink()  # the folder can be made again
    j.observe(stub_out("WAIT: two"), A, [], T0 + 5)
    recs = read(blocker / "decisions-2026-11-06.jsonl")[0]
    assert [r["decision"]["headline"] for r in recs] == ["WAIT: one", "WAIT: two"] and j.status(T0)["ok"]


# ====================================================================================================================
# 2. A write that fails halfway, and a sync that fails
# ====================================================================================================================
def test_a_write_that_fails_halfway_leaves_a_cut_line_and_the_record_is_written_again_whole(tmp_path, monkeypatch):
    j = Journal(tmp_path)
    j.observe(stub_out("WAIT: first"), A, [], T0)
    real = Journal._append
    state = {"failed": False}

    def half(self, path, line, sync=True):
        if not state["failed"]:
            state["failed"] = True
            with path.open("a", encoding="utf-8") as f:
                f.write(line[: len(line) // 2])  # the disk filled up in the middle of the line
            raise OSError(28, "No space left on device (test)")
        return real(self, path, line, sync)

    monkeypatch.setattr(Journal, "_append", half)
    j.observe(stub_out("WAIT: second"), A, [], T0 + 5)
    assert j._failed and len(j._pending) == 1
    j.observe(stub_out("WAIT: second"), A, [], T0 + 6)  # the next evaluation retries
    recs, bad = read(tmp_path / "decisions-2026-11-06.jsonl")
    assert [r["decision"]["headline"] for r in recs] == ["WAIT: first", "WAIT: second"] and len(bad) == 1  # the cut line is the only casualty, and it is just a bad line to a reader
    assert j.status(T0)["ok"]


def test_every_record_but_the_heartbeat_is_synced_to_the_disk(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr("app.journal.os.fsync", lambda fd: synced.append(fd))
    j = Journal(tmp_path)
    j.observe(stub_out(), A, [], T0)  # start
    j.observe(stub_out(), A, [], T0 + 30)  # nothing new: nothing written
    j.observe(stub_out(), A, [], T0 + 301)  # a heartbeat: written, not synced
    j.observe(stub_out("WAIT: changed"), A, [], T0 + 310)  # a change
    assert len(synced) == 2 and j.status(T0)["fsync"] is True and len(read(tmp_path / "decisions-2026-11-06.jsonl")[0]) == 4 - 1


def test_the_sync_can_be_turned_off(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr("app.journal.os.fsync", lambda fd: synced.append(fd))
    j = Journal(tmp_path, sync=False)
    j.observe(stub_out(), A, [], T0)
    j.observe(stub_out("WAIT: changed"), A, [], T0 + 10)
    assert synced == [] and j.status(T0)["fsync"] is False and len(read(tmp_path / "decisions-2026-11-06.jsonl")[0]) == 2


def test_a_sync_that_fails_does_not_write_the_line_twice_or_hold_the_journal_back(tmp_path, monkeypatch, caplog):
    def broken(fd):
        raise OSError(5, "I/O error (test)")

    monkeypatch.setattr("app.journal.os.fsync", broken)
    j = Journal(tmp_path)
    with caplog.at_level(logging.WARNING):
        j.observe(stub_out("WAIT: one"), A, [], T0)
        j.observe(stub_out("WAIT: two"), A, [], T0 + 5)
    recs = read(tmp_path / "decisions-2026-11-06.jsonl")[0]
    assert [r["decision"]["headline"] for r in recs] == ["WAIT: one", "WAIT: two"] and j.status(T0)["fsync_errors"] == 2 and j.status(T0)["ok"] and not j._pending
    assert sum("could not be synced" in m for m in caplog.messages) == 1  # said once


# ====================================================================================================================
# 3. Where the journal is
# ====================================================================================================================
def test_the_journal_folder_is_absolute_and_beside_the_app_whatever_folder_prism_starts_in(tmp_path, monkeypatch):
    monkeypatch.delenv("JOURNAL_DIR", raising=False)
    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    d = app_main.journal_dir()
    assert d.is_absolute() and d == Path(app_main.__file__).resolve().parent.parent / "logs" and "somewhere else" not in str(d)
    monkeypatch.setenv("JOURNAL_DIR", "mine")  # a relative JOURNAL_DIR still means relative to where Prism was started: but it is made absolute at once
    assert app_main.journal_dir() == (elsewhere / "mine").resolve() and app_main.journal_dir().is_absolute()
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path / "abs"))
    assert app_main.journal_dir() == (tmp_path / "abs").resolve()


def test_a_journal_given_a_relative_folder_reports_the_absolute_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    j = Journal("some-logs")
    assert j.status(T0)["directory"] == str((tmp_path / "some-logs").resolve()) and Path(j.status(T0)["directory"]).is_absolute()


def test_the_real_startup_puts_the_journal_and_the_one_copy_lock_in_that_folder_and_says_so(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("JOURNAL_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    guarded = []

    class FakeFeed:
        def __init__(self, cfg, session, md):
            self.cfg, self.session, self.md = cfg, session, md

        def start(self):
            pass

        def stop(self):
            pass

        def watchdog(self):
            self.md.watchdog()

    monkeypatch.setattr(app_main.instance, "guard", lambda directory, *a, **k: guarded.append(directory) or {"pid": 1, "port": None, "started_at": 0})
    monkeypatch.setattr(app_main.instance, "release", lambda: None)
    monkeypatch.setattr(app_main, "load_config", lambda: CFG)
    monkeypatch.setattr(app_main, "Feed", FakeFeed)
    monkeypatch.setattr(app_main, "AngelSession", lambda cfg, limiter=None: None)
    monkeypatch.setattr(app_main, "run_history", lambda *a, **k: None)
    monkeypatch.setattr(app_main, "run_validation", lambda *a, **k: None)

    async def scenario():
        app = app_main.create_app()
        async with app.router.lifespan_context(app):
            return app.state.md.journal.dir, app.state.md.journal.sync

    with caplog.at_level(logging.INFO):
        directory, sync = asyncio.run(scenario())
    assert directory == app_main.JOURNAL_DEFAULT and guarded == [str(directory)] and sync is True  # the lock and the journal share one folder, and it is not the one Prism was started in
    assert any(str(directory) in m and "decision journal" in m for m in caplog.messages)  # and the log says which


def test_the_health_page_and_the_journal_route_say_where_the_journal_is(tmp_path):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    w.step(md, clock, 17)
    app = create_app(md, feed=None)
    app.state.md = md
    health = route(app, "/api/health").endpoint()["journal"]
    api = route(app, "/api/journal").endpoint()
    for j in (health, api["status"]):
        assert j["configured"] and j["ok"] and Path(j["directory"]) == tmp_path.resolve() and j["pending"] == 0 and j["lost"] == 0 and j["written"] == 1 and j["fsync"] is True
    assert health["file"] == api["file"] == "decisions-2026-11-06.jsonl" and health["last_error"] is None
    md.journal = None
    assert md.health(clock.t)["journal"] == {"configured": False, "ok": True} and not any("journal" in x for x in md.health(clock.t)["warnings"])


# ====================================================================================================================
# 4. An event is never confused with another
# ====================================================================================================================
def test_a_run_whose_restore_failed_numbers_its_events_after_those_on_record_and_nothing_is_hidden(tmp_path, monkeypatch):
    w = World()
    md1, clock1 = w.engine()
    md1.journal = Journal(tmp_path)
    for slot in (16, 17, 18, 19):
        w.step(md1, clock1, slot)
    first = [e["seq"] for r in read(md1.journal.path(clock1.t))[0] for e in r["events"]]
    assert first == [0, 1, 2]
    md2, clock2 = World().engine()
    clock2.t = clock1.t
    md2.journal = Journal(tmp_path)
    monkeypatch.setattr(tr.SetupBook, "restore", lambda self, records, now: (_ for _ in ()).throw(ValueError("restore failed (test)")))
    assert md2.resume() == 0 and md2.book.seq == 3 and md2.journal._restarted and md2.journal._seq == 2  # no setups carried over, but the numbering goes on
    monkeypatch.undo()
    w.step(md2, clock2, 20)  # (the new run's own events)
    recs = read(md2.journal.path(clock2.t))[0]
    seqs = [e["seq"] for r in recs for e in r["events"]]
    assert len(seqs) == len(set(seqs)) and seqs[:3] == [0, 1, 2] and all(s >= 3 for s in seqs[3:]) and recs[-1]["reason"] != "start"
    app = create_app(md2, feed=None)
    app.state.md = md2
    assert len(route(app, "/api/journal").endpoint()["events"]) == len(seqs)  # the API shows every one


def test_an_event_is_identified_by_its_number_and_what_it_is_so_two_runs_numbered_from_zero_do_not_hide_each_other(tmp_path):
    def event(seq, sid, status):
        return {"seq": seq, "time": T0 + seq, "id": sid, "strategy": "trend_pullback", "strategy_name": "Trend Pullback", "direction": "CE", "status": status, "text": f"{status}: x"}

    def rec(t, *events):
        return {"time": "t", "t": t, "reason": "changed", "price": 1.0, "decision": {"action": "WAIT", "headline": "WAIT: x", "reasons": [], "against": [], "setup_id": None}, "market": {}, "events": list(events)}

    w = World()
    md, _ = w.engine()
    md.journal = Journal(tmp_path)
    path = md.journal.path(md.clock())
    lines = [rec(1, event(0, "a", "DETECTED"), event(1, "a", "WAITING_FOR_CONFIRMATION")), rec(2, event(0, "b", "DETECTED")),  # a second run, numbered from 0 again
             rec(3, event(1, "a", "WAITING_FOR_CONFIRMATION"))]  # and one event written twice (the same number, the same setup, the same status)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    app = create_app(md, feed=None)
    app.state.md = md
    got = route(app, "/api/journal").endpoint()["events"]
    assert sorted((e["seq"], e["id"], e["status"]) for e in got) == [(0, "a", "DETECTED"), (0, "b", "DETECTED"), (1, "a", "WAITING_FOR_CONFIRMATION")]  # three events, the repeat once


# ====================================================================================================================
# 5. A number that is not finite
# ====================================================================================================================
def test_a_nan_costs_a_number_not_the_record(tmp_path, caplog):
    j = Journal(tmp_path)
    out = stub_out()
    out["price"] = float("nan")
    out["setups"] = [{"id": "s", "status": "WAITING_FOR_CONFIRMATION", "price_state": None, "actionable": False, "blockers": [], "zone": [float("inf"), 1.0]}]
    with caplog.at_level(logging.WARNING):
        j.observe(out, A, [], T0)
        out["price"] = float("nan")
        j.observe(stub_out("WAIT: next"), A, [], T0 + 10)
    recs, bad = read(tmp_path / "decisions-2026-11-06.jsonl")
    assert bad == [] and len(recs) == 2 and recs[0]["price"] is None and recs[0]["setups"][0]["zone"] == [None, 1.0] and j.status(T0)["ok"]
    assert recs[0]["decision"]["headline"] == "WAIT: nothing yet"  # everything else of the record is there
    assert sum("not finite" in m for m in caplog.messages) <= 1  # said once at most


# ====================================================================================================================
# 6. The journal still never disturbs trading
# ====================================================================================================================
def test_nothing_the_journal_can_do_wrong_reaches_the_decision(tmp_path, monkeypatch):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    for name in ("_append", "_drain", "_emit", "_dump", "_queue", "_fail", "_ok"):  # every step that touches the disk, or builds the record, failing: even a bug in the journal itself
        monkeypatch.setattr(Journal, name, lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("a bug (test)")))
        out = w.step(md, clock, 17)
        assert out["decision"]["action"] == "WAIT", name  # the decision is what it is, whatever the journal does
        monkeypatch.undo()
