"""Stage 3 regression protection for the six strategies of Stages 2.1-2.3 (Momentum Continuation, EMA Trend Continuation, Volatility Expansion, Opening Range Breakout, Failed
Breakout, Range Bounce), beside test_existing_strategies_golden.py, which protects the three originals.

`tests/golden/stage2_strategies.json` was recorded from the code as it stood BEFORE Stage 3 (see tests/stage2_snapshot.py): every scanner result of all nine strategies on every fixture
their own tests use, in both orientations, step by step through the real SetupBook, and the decision (action, headline, strategy, setup id) at every step. These tests recompute all
of it with Stage 3 in place (ten strategies registered, the regime facts, the counter-trend rule in the decision) and require it to be IDENTICAL, and require the source files of the
six strategies to be byte-for-byte what they were."""

import json

from stage2_snapshot import GOLDEN, NINE, SCENARIOS, SIX, snapshot, source_hashes

from app import registry


def recorded() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_baseline_covers_all_nine_strategies_on_a_real_spread_of_fixtures():
    g = recorded()
    entries = g["scenarios"]
    assert len(entries) == 2 * (len(SCENARIOS) + 3) and all(len(e["steps"]) >= 1 for e in entries.values())  # every fixture, both orientations
    seen = {k.split(":")[0] for e in entries.values() for st in e["steps"] for k, r in st["scans"].items() if r["setup"]}
    assert seen == set(NINE), seen  # each of the nine produced a setup somewhere in the recording
    actions = {st["decision"]["action"] for e in entries.values() for st in e["steps"]}
    assert {"BUY CE", "BUY PE", "WAIT"} <= actions  # and the recording includes real BUY decisions in both directions


def test_the_six_strategies_scan_and_decide_exactly_as_before_stage_3():
    expected, got = recorded()["scenarios"], snapshot()["scenarios"]
    assert expected.keys() == got.keys()
    moved = [k for k in expected if expected[k] != got[k]]
    assert moved == [], "these fixtures changed: " + ", ".join(moved[:8])


def test_the_scan_rows_the_lifecycle_and_the_decision_are_each_unchanged_not_only_in_total():
    """The same comparison, split, so a failure says WHAT moved: a scanner's own output, the book's live setups, the events, or the decision."""
    expected, got = recorded()["scenarios"], snapshot()["scenarios"]
    for part in ("scans", "live_now", "decision"):
        moved = [f"{k}@{e['through']}" for k in expected for e, g in zip(expected[k]["steps"], got[k]["steps"]) if e[part] != g[part]]
        assert moved == [], f"{part} changed at: " + ", ".join(moved[:6])
    assert [k for k in expected if expected[k]["events"] != got[k]["events"]] == []


def test_the_source_of_the_six_strategies_is_byte_for_byte_what_it_was():
    expected = recorded()["sources"]
    assert source_hashes() == expected, "a strategy module of Stages 2.1-2.3 was edited: that needs the user's approval, then --write on tests/stage2_snapshot.py"
    assert set(SIX) <= set(registry.NAMES) and len(registry.NAMES) == 10  # and they are all still registered, now beside the tenth
