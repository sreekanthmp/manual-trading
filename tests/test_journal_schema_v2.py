"""Stage 6.2: what the journal says about a setup's life, completed, without changing anything it already said.

Version 2 of a record only ADDS (a record without `v` is version 1 and reads exactly as before):
  * `v`, and `decision_setup`: the setup the decision is about, with its trigger, entry zone, invalidation, targets and stage, next to the decision that names it
  * `decision.also_setups`: the agreeing strategies as strategy and setup ids, beside the words of `decision.also`
  * on every lifecycle event `code`, `superseded_by` and `final`; the events that END a setup say why, as a code (the words stay in `text`), which structure replaced it, and what it stood at

Held here: each way a setup can end has its own code; `final` is what the setup held when it ended, and agrees with the record that last showed it live; nothing was renamed or removed; four
real version-1 records (tests/golden/journal_v1_sample.jsonl) and a file that mixes the two versions still read, restore, show in the API and in the review command; and what makes a
line get written is unchanged (see test_journal.py)."""

import json
from pathlib import Path

import pytest
from helpers import FRI, ist
from test_counter_trend_decision import GRANTED, decide, market, sd
from test_exhaustion import TER
from test_options_integration import FLOWS, NAMES, SIDES, flow
from test_phase3_integration import World, create_app, route

from app import registry
from app import trading as tr
from app.journal import SCHEMA, SETUP_KEYS, Journal, decision_setup, read, timeline
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Params, Scan, Setup
from app.trading import SetupBook, end_code, ist_date

FIXTURE = Path(__file__).parent / "golden" / "journal_v1_sample.jsonl"
NOW = ist(10, 30, day=FRI)

V1_EVENT_KEYS = {"seq", "time", "id", "strategy", "strategy_name", "direction", "status", "text"}
V1_DECISION_KEYS = {"action", "headline", "reasons", "against", "setup_id", "strategy", "strategy_name", "also"}
V2_TOP = {"v", "time", "t", "reason", "price", "spot", "decision", "decision_setup", "market", "session", "levels", "counter_trend", "data", "feed", "strategies", "setups", "events",
          "timeframes", "analysis"}  # fmt: skip


def setup(status=WAITING, name="trend_pullback", side="bull", anchor=1000.0, now=NOW):
    return Setup(name, side, f"{name}:{side}:{anchor}", status, anchor, level=24249.45, level_label="leg origin", trigger=24281.0, zone=(24281.0, 24287.06), invalidation=24270.48,
                 extreme=24249.45, plan={"entry_reference": 24281.0, "risk_points": 10.5}, confirmed_bar_end=now - 120 if status == CONFIRMED else None,
                 detected_at=now - 600, status_since=now - 300, confirmed_at=now - 60 if status == CONFIRMED else None)  # fmt: skip


def book_with(s, now=NOW):
    book = SetupBook(Params())
    book.day = ist_date(now)
    book.active[(s.strategy, s.side)] = s
    return book


# ====================================================================================================================
# 1. Each way a setup can end has a code, and `final` is what it stood at
# ====================================================================================================================
def ended(book, status=None):
    return next(e for e in book.events if e["status"] in (INVALIDATED, EXPIRED))


@pytest.fixture
def no_scans(monkeypatch):
    monkeypatch.setattr(tr, "scan_all", lambda ctx, p, active: {})


def case_session_ended(book, s, no_scans):
    book.day = ist_date(NOW - 86400)
    book.update(None, {"session": {"phase": "morning"}}, NOW)


def case_market_closed(book, s, no_scans):
    book.update(None, {"session": {"phase": "closed"}}, NOW)


def case_late_session(book, s, no_scans):
    book.update(None, {"session": {"phase": "late"}}, NOW)


def case_unconfirmed_timeout(book, s, no_scans):
    book.update(None, {"session": {"phase": "morning"}}, NOW)  # (the setup was detected long ago: see PREPARE)


def case_no_longer_suits(book, s, no_scans):
    book._apply((s.strategy, s.side), Scan(False, "1H is bearish: pullbacks are only taken with a bullish 1H trend", None), NOW, "morning")


def case_no_longer_present(book, s, no_scans):
    book._apply((s.strategy, s.side), Scan(True, "the pullback leg is gone", None), NOW, "morning")


def case_superseded(book, s, no_scans):
    book._apply((s.strategy, s.side), Scan(True, "", setup(WAITING, anchor=2000.0)), NOW, "morning")


def strategy_ends(s, status, note):
    """The same structure, as the strategy's own scan now reports it: ended."""
    out = setup(status, s.strategy, s.side, float(s.key.rsplit(":", 1)[1]))
    out.note = note
    return out


def case_invalidated(book, s, no_scans):
    book._apply((s.strategy, s.side), Scan(True, "", strategy_ends(s, INVALIDATED, "a 5M candle closed below the invalidation")), NOW, "morning")


def case_expired(book, s, no_scans):
    book._apply((s.strategy, s.side), Scan(True, "", strategy_ends(s, EXPIRED, "the structure aged out")), NOW, "morning")


def case_missed(book, s, no_scans):
    book._apply((s.strategy, s.side), Scan(True, "", strategy_ends(s, EXPIRED, "entry opportunity has passed: price ran 6 points past the trigger")), NOW, "morning")


CASES = {
    "session_ended": (case_session_ended, EXPIRED, WAITING), "market_closed": (case_market_closed, EXPIRED, CONFIRMED), "late_session": (case_late_session, EXPIRED, WAITING),
    "unconfirmed_timeout": (case_unconfirmed_timeout, EXPIRED, DETECTED), "no_longer_suits": (case_no_longer_suits, INVALIDATED, WAITING),
    "no_longer_present": (case_no_longer_present, EXPIRED, WAITING), "superseded": (case_superseded, EXPIRED, WAITING), "invalidated": (case_invalidated, INVALIDATED, CONFIRMED),
    "expired": (case_expired, EXPIRED, WAITING), "missed": (case_missed, EXPIRED, CONFIRMED),
}  # fmt: skip


PREPARE = {"unconfirmed_timeout": lambda s: setattr(s, "detected_at", NOW - (Params().detected_max_minutes + 5) * 60)}  # a setup detected longer ago than it may wait for its confirmation


@pytest.mark.parametrize("code", list(CASES))
def test_every_way_a_setup_ends_has_its_own_code_and_says_what_it_stood_at(code, no_scans):
    case, status, before = CASES[code]
    s = setup(before)
    PREPARE.get(code, lambda s: None)(s)
    book = book_with(s)
    stood = {"trigger": s.trigger, "zone": list(s.zone), "invalidation": s.invalidation, "extreme": s.extreme, "level": s.level, "level_label": s.level_label, "plan": s.plan,
             "detected_at": s.detected_at, "confirmed_at": s.confirmed_at, "confirmed_bar_end": s.confirmed_bar_end, "status_before_since": s.status_since}  # fmt: skip
    case(book, s, no_scans)
    e = next(x for x in book.events if x["id"] == s.id and x["status"] in (INVALIDATED, EXPIRED))
    assert e["code"] == code and e["status"] == status and e["text"] == f"{status}: {s.note}"  # the words are exactly what they were
    f = e["final"]
    assert f["status_before"] == before and {k: f[k] for k in stood} == stood and f["lifetime_seconds"] == e["time"] - stood["detected_at"] and f["resumed"] is False
    assert (e["superseded_by"] == f"{s.strategy}:{s.side}:2000.0") == (code == "superseded") and (code == "superseded" or e["superseded_by"] is None)
    json.dumps(e, allow_nan=False)  # and it is plain JSON
    assert book.recent[0] is s and (code == "session_ended" or s.key in book.terminal)  # (the lifecycle itself is as it was: the day's ended keys are cleared when a new session starts)
    assert code == "superseded" or (s.strategy, s.side) not in book.active


def test_the_events_that_do_not_end_a_setup_carry_the_keys_with_nothing_in_them(no_scans):
    book = SetupBook(Params())
    book.day = ist_date(NOW)
    book._apply(("trend_pullback", "bull"), Scan(True, "", setup(DETECTED)), NOW, "morning")  # announced
    book._apply(("trend_pullback", "bull"), Scan(True, "", setup(WAITING)), NOW + 60, "morning")  # advanced
    book._apply(("trend_pullback", "bull"), Scan(True, "", setup(CONFIRMED)), NOW + 120, "morning")
    assert [e["status"] for e in reversed(book.events)] == [DETECTED, WAITING, CONFIRMED]
    assert all(e["code"] is None and e["superseded_by"] is None and e["final"] is None and V1_EVENT_KEYS | {"code", "superseded_by", "final"} == set(e) for e in book.events)


def test_the_code_a_strategys_own_scan_gets_follows_the_stage_a_trader_sees():
    assert end_code(INVALIDATED, "anything") == "invalidated" and end_code(EXPIRED, "the structure aged out") == "expired"
    for note in tr.MISSED_NOTES:
        assert end_code(EXPIRED, f"x: {note}") == "missed" and tr.setup_stage(strategy_ends(setup(), EXPIRED, f"x: {note}"), False, True, True)[0] == "MISSED"
    assert end_code(EXPIRED, "missed the entry") == "missed"


def test_a_setup_carried_over_from_the_journal_says_so_when_it_ends():
    s = setup(CONFIRMED)
    s.resumed = True
    book = book_with(s)
    book._finish(s, EXPIRED, "the market is closed", NOW)  # (no code given: the words are what is known)
    e = book.events[0]
    assert e["final"]["resumed"] is True and e["code"] == "expired"


# ====================================================================================================================
# 2. The agreeing strategies, as ids
# ====================================================================================================================
def test_the_agreeing_strategies_are_listed_as_ids_beside_the_words_in_the_same_order():
    mkt = market("developing", "sideways", "bullish")
    d = decide([sd(TER, "CE", counter={**GRANTED, "needed": False}, zone=(24592.0, 24602.0)), sd("trend_pullback", "CE")], mkt)
    assert d["also"] == ["Trend Exhaustion Reversal (same CE opportunity)"]  # the words: unchanged
    assert d["also_setups"] == [{"strategy": TER, "strategy_name": registry.NAMES[TER], "setup_id": f"{TER}:CE", "direction": "CE"}]  # the ids: new
    assert (d["action"], d["strategy"], d["setup_id"]) == ("BUY CE", "trend_pullback", "trend_pullback:CE")  # and the decision itself is what it was


def test_an_actionable_setup_that_is_not_the_same_opportunity_is_neither_in_the_words_nor_in_the_ids():
    mkt = market("developing", "sideways", "bullish")
    d = decide([sd("trend_pullback", "CE"), sd(TER, "CE", counter={**GRANTED, "needed": False}, zone=(24592.0, 24602.0)), sd("range_bounce", "CE", zone=(25000.0, 25010.0))], mkt)
    assert d["also"] == ["Trend Exhaustion Reversal (same CE opportunity)"] and [x["strategy"] for x in d["also_setups"]] == [TER]  # the far one is a different opportunity: in neither


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", SIDES)
def test_every_decision_has_as_many_agreeing_ids_as_agreeing_words(name, side):
    d = flow(name, side)["decision"]
    assert isinstance(d["also_setups"], list) and len(d["also_setups"]) == len(d["also"]) == 0 and V1_DECISION_KEYS | {"also_setups"} == set(d)
    assert tr.unavailable("x", NOW)["decision"]["also_setups"] == []


def test_a_decision_that_is_not_a_buy_has_no_agreeing_ids():
    assert decide([sd("trend_pullback", "CE", actionable=False, price_state="extended")])["also_setups"] == []


# ====================================================================================================================
# 3. A real run through the journal file
# ====================================================================================================================
def run(tmp_path, slots=(16, 17, 18, 19, 20)):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    for slot in slots:
        w.step(md, clock, slot)
    return md, read(md.journal.path(md.clock()))[0]


def test_a_new_record_is_version_2_and_only_adds_to_version_1(tmp_path):
    md, recs = run(tmp_path, (16, 17, 18, 19))
    assert SCHEMA == 2 and all(r["v"] == 2 and set(r) == V2_TOP for r in recs)  # (no options window on this engine: no `options` key)
    assert all(V1_DECISION_KEYS | {"also_setups"} == set(r["decision"]) for r in recs)
    assert all(V1_EVENT_KEYS | {"code", "superseded_by", "final"} == set(e) for r in recs for e in r["events"])
    v1 = read(FIXTURE)[0]
    assert all("v" not in r for r in v1) and all(set(r) <= V2_TOP | {"options"} for r in v1)  # every key version 1 ever had is still a key of version 2: nothing was renamed or removed


def test_the_setup_the_decision_is_about_sits_beside_it_with_its_trigger_zone_invalidation_and_targets(tmp_path):
    md, recs = run(tmp_path, (16, 17, 18, 19))
    waiting, buy = recs[-2], recs[-1]
    assert waiting["decision"]["action"] == "WAIT" and buy["decision"]["action"] == "BUY CE"
    for r in (waiting, buy):  # a decision that is only WAITING for a setup names it too
        s = next(x for x in r["setups"] if x["id"] == r["decision"]["setup_id"])
        assert r["decision_setup"] == {k: s.get(k) for k in SETUP_KEYS} and r["decision_setup"]["id"] == r["decision"]["setup_id"]
    ds = buy["decision_setup"]
    assert ds["status"] == "CONFIRMED" and ds["actionable"] is True and ds["stage"] == "READY_TO_ACT" and ds["trigger"] is not None and len(ds["zone"]) == 2 and ds["invalidation"] is not None
    assert ds["plan"]["targets"] and ds["plan"]["risk_points"] > 0 and ds["confirmed_at"] is not None
    assert set(ds) == {"id", "strategy", "strategy_name", "direction", "status", "stage", "actionable", "price_state", "level", "level_label", "trigger", "zone", "invalidation", "extreme",
                       "detected_at", "status_since", "confirmed_at", "confirmed_bar_end", "resumed", "plan", "blockers", "note"}  # fmt: skip


def test_the_setup_picked_is_the_one_the_decision_names_not_the_first_that_is_live():
    out = {"decision": {"setup_id": "b"}, "setups": [{"id": "a", "trigger": 1.0}, {"id": "b", "trigger": 2.0, "zone": [2.0, 3.0]}]}
    assert decision_setup(out)["id"] == "b" and decision_setup(out)["trigger"] == 2.0 and decision_setup(out)["zone"] == [2.0, 3.0]
    assert decision_setup({"decision": {"setup_id": "c"}, "setups": out["setups"]}) is None  # (one that is not live is not made up)


def test_a_decision_with_no_setup_has_none(tmp_path):
    md, clock = World().engine()
    md.journal = Journal(tmp_path)
    md.safe_trading()  # nothing is loaded: "no decision is possible" is on record, with no setup
    recs = read(md.journal.path(md.clock()))[0]
    assert recs and all(r["decision_setup"] is None and r["decision"]["setup_id"] is None for r in recs)
    assert decision_setup({"decision": {"setup_id": "gone"}, "setups": []}) is None and decision_setup({"decision": {}, "setups": [{"id": "x"}]}) is None


def test_the_final_values_of_an_ended_setup_are_those_its_last_live_record_showed(tmp_path):
    md, recs = run(tmp_path)  # slot 20: the entry opportunity has passed
    ends = [(r, e) for r in recs for e in r["events"] if e["status"] == "EXPIRED"]
    assert len(ends) == 1
    r, e = ends[0]
    assert e["code"] == "missed" and e["final"]["status_before"] == "CONFIRMED" and e["superseded_by"] is None and "opportunity has passed" in e["text"]
    last_live = next(x for x in reversed(recs) if any(s["id"] == e["id"] for s in x["setups"]))  # the last record that still held it
    s = next(s for s in last_live["setups"] if s["id"] == e["id"])
    f = e["final"]
    assert {k: f[k] for k in ("trigger", "zone", "invalidation", "extreme", "detected_at", "confirmed_at", "confirmed_bar_end", "level", "level_label", "plan")} == \
           {k: s[k] for k in ("trigger", "zone", "invalidation", "extreme", "detected_at", "confirmed_at", "confirmed_bar_end", "level", "level_label", "plan")}  # fmt: skip
    assert f["lifetime_seconds"] == pytest.approx(e["time"] - s["detected_at"]) and f["lifetime_seconds"] > 0
    assert r["decision_setup"] is None  # (and the decision of that moment no longer names it)


def test_the_review_says_why_a_setup_ended_and_who_agreed(tmp_path):
    md, recs = run(tmp_path)
    detail = "\n".join(timeline(md.journal.path(md.clock()), detail=True))
    assert "ended [missed]: was CONFIRMED, trigger " in detail and "entry " in detail and ", lived " in detail
    assert "ended [" not in "\n".join(timeline(md.journal.path(md.clock())))  # (the plain review is as it was)
    payload = flow("trend_pullback", "bull")
    payload["decision"]["also_setups"] = [{"strategy": "range_bounce", "strategy_name": "Range Bounce", "setup_id": "range_bounce:bull:1", "direction": "CE"}]
    payload["decision"]["also"] = ["Range Bounce (same CE opportunity)"]
    j = Journal(tmp_path / "also")
    j.observe(payload, {"timeframes": {}, "condition": {}, "warnings": []}, [], NOW)
    assert "also: Range Bounce (range_bounce:bull:1)" in "\n".join(timeline(j.path(NOW), detail=True))


# ====================================================================================================================
# 4. Version 1 is still read, restored and shown
# ====================================================================================================================
def served(directory, md=None):
    md = md or World().engine()[0]
    md.journal = Journal(directory)
    app = create_app(md, feed=None)
    app.state.md = md
    return md, route(app, "/api/journal").endpoint


def v1_file(tmp_path, extra=()):
    path = Journal(tmp_path).path(FRI_NOON)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8") + "".join(extra), encoding="utf-8")
    return path


FRI_NOON = ist(12, 0, day=FRI)


def test_four_real_version_1_records_still_read_and_review(tmp_path):
    recs, bad = read(FIXTURE)
    assert len(recs) == 4 and bad == [] and [r["reason"] for r in recs] == ["start", "changed", "changed", "candle"]
    assert all("v" not in r and "decision_setup" not in r for r in recs)
    lines = timeline(FIXTURE, everything=True, detail=True)  # the review (with every line, in detail) copes with records that have none of the new keys
    assert any("WAITING_FOR_CONFIRMATION" in x for x in lines) and not any("ended [" in x for x in lines)


def test_the_api_shows_a_version_1_row_with_the_new_fields_empty_and_says_it_is_version_1(tmp_path):
    md, endpoint = served(tmp_path)
    v1_file(tmp_path)
    out = endpoint()
    assert out["available"] and out["records"] == 4
    rows = out["decisions"]
    assert all(r["v"] == 1 and r["also_setups"] == [] and r["decision_setup"] is None for r in rows)
    assert all("code" not in e for e in out["events"]) and {e["status"] for e in out["events"]} == {"WAITING_FOR_CONFIRMATION", "EXPIRED"}  # (the events as they were written)


def test_a_file_that_mixes_the_two_versions_reads_as_one(tmp_path):
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp_path)
    path = md.journal.path(md.clock())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")  # an earlier run's lines, version 1
    for slot in (16, 17, 18, 19):
        w.step(md, clock, slot)  # this run's, version 2 (events carry their numbers on from the engine: it did not resume)
    app = create_app(md, feed=None)
    app.state.md = md
    out = route(app, "/api/journal").endpoint()
    assert sorted({r["v"] for r in out["decisions"]}) == [1, 2] and out["decisions"][0]["v"] == 2 and out["decisions"][-1]["v"] == 1  # newest first
    new = next(r for r in out["decisions"] if r["v"] == 2 and r["action"] == "BUY CE")
    assert new["decision_setup"]["id"] == new["setup_id"] and new["also_setups"] == []
    assert len(timeline(path, detail=True)) > 0


def test_a_restart_on_a_version_1_journal_restores_as_it_always_did_and_new_events_have_the_new_keys(tmp_path):
    md, endpoint = served(tmp_path)
    path = v1_file(tmp_path)
    n = md.resume()
    expected = sum(1 for _ in read(path)[0][-1]["setups"])  # the live setup of the last evaluated record
    assert n == expected == 1 and [s.resumed for s in md.book.active.values()] == [True]
    old = list(md.book.events)
    assert old and all("code" not in e for e in old) and md.book.seq == max(e["seq"] for e in old) + 1  # restored events as written; numbering continues
    s = next(iter(md.book.active.values()))
    md.book._finish(s, EXPIRED, "the market is closed", NOW)
    e = md.book.events[0]
    assert e["code"] == "expired" and e["final"]["resumed"] is True and e["final"]["trigger"] == s.trigger and e["seq"] == md.book.seq - 1


def test_a_restart_on_a_version_2_journal_keeps_what_the_events_said(tmp_path):
    md, recs = run(tmp_path)  # run 1 ends with the EXPIRED event (code, final)
    ended_events = [e for r in recs for e in r["events"] if e["status"] == "EXPIRED"]
    w = World()
    md2, clock2 = w.engine()
    clock2.t = md.clock()
    md2.journal = Journal(tmp_path)
    n = md2.resume()
    assert n == 0  # the only setup had ended: it stays ended
    restored = [e for e in md2.book.events if e["status"] == "EXPIRED"]
    assert restored == ended_events and restored[0]["code"] == "missed" and restored[0]["final"] == ended_events[0]["final"]
    assert md2.book.seq == max(e["seq"] for r in recs for e in r["events"]) + 1
