"""The three original strategies must not change while the others are added (a Stage 2 requirement): same scans, same lifecycle, same decisions.

The expected values in `golden/existing_strategies.json` were recorded from the code as it stood BEFORE the strategy registry work (Stage 2.0). Every entry holds a
readable summary and a hash of the complete result, so a change in ANY field of ANY setup is caught, and the message says which scenario moved.

Two layers, because later stages add strategies:
  1. With ONLY the three originals registered, everything (scans, lifecycle, the whole decision pipeline) must equal the recording exactly. This is the proof that
     the shared code (the registry, Setup, Params, the setup book) did not change what the originals do.
  2. With every strategy registered, the originals' own results must still be identical (their setups, events and monitor rows, the market and data facts, the
     decision's action, headline, strategy and setup), and every reason line the originals contributed to a decision must survive. What may differ is only what the
     new strategies add: their own rows, extra reason lines, and the explanation when it picks a more advanced setup of a new strategy.

Re-record (only after deciding that a change is intended):  PYTHONPATH=. python tests/test_existing_strategies_golden.py --write"""

import hashlib
import json
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from scenarios import P, bull_breakout, bull_pullback, bull_range, mirror

from app import registry
from app import strategy as st
from app import trading as tr

GOLDEN = Path(__file__).resolve().parent / "golden" / "existing_strategies.json"
ORIGINAL = ("trend_pullback", "breakout_retest", "range_reversal")
SCENARIOS = {
    "pullback": (bull_pullback, range(11, 20)),
    "breakout": (bull_breakout, range(24, 29)),
    "range": (bull_range, range(30, 36)),
}


def canon(x) -> str:
    return json.dumps(x, sort_keys=True, default=str, allow_nan=False)


def digest(x) -> str:
    return hashlib.sha256(canon(x).encode()).hexdigest()[:16]


def only_original(rows, key="strategy"):
    return [r for r in rows if r[key] in ORIGINAL]


@contextmanager
def originals_only():
    """Only the three original strategies registered, exactly as in Stage 2.0."""
    saved = list(registry.REGISTRY)
    registry.REGISTRY[:] = [d for d in saved if d.name in ORIGINAL]
    registry._refresh()
    try:
        yield
    finally:
        registry.REGISTRY[:] = saved
        registry._refresh()


def scan_entry(s) -> dict:
    # `group` (Stage 2.1) and `counter_trend` (Stage 3) are fields added after the recording was made; both are None for the three originals (asserted below), so the recording
    # predates them and they are left out of the hash. Every field that existed when it was recorded is still in it, and still has to match.
    assert s.setup is None or (s.setup.group is None and s.setup.counter_trend is None), "an original strategy produced a setup with a group or a counter-trend permission"
    assert s.info is None, "an original strategy produced scan info"
    full = {"applicable": s.applicable, "note": s.note, "setup": {k: v for k, v in asdict(s.setup).items() if k not in ("group", "counter_trend")} if s.setup else None}
    su = s.setup
    return {"applicable": s.applicable, "status": su.status if su else None, "trigger": su.trigger if su else None, "zone": list(su.zone) if su and su.zone else None,
            "invalidation": su.invalidation if su else None, "note": s.note[:70], "hash": digest(full)}  # fmt: skip


def scans_snapshot() -> dict:
    out = {}
    for name, (build, through) in SCENARIOS.items():
        for n in through:
            for orient in ("bull", "bear"):
                ctx = build(n) if orient == "bull" else mirror(build(n))
                scans = st.scan_all(ctx, P, {})
                for (strategy, side), s in sorted(scans.items()):
                    if strategy in ORIGINAL:
                        out[f"{name}/{n}/{orient}/{strategy}/{side}"] = scan_entry(s)
    return out


def bare(row: dict) -> dict:
    """A strategy monitor row as the recording knew it. The rows gained one key in Stage 5.1 (`sides`: the CE and the PE note of each strategy, kept apart). It is additive: the recording
    predates it, so it is left out of the hash (and must be there: see lifecycle_snapshot), while every key the recording did hold is still compared."""
    assert [x["side"] for x in row["sides"]] == ["CE", "PE"], "the monitor row lost its Stage 5.1 addition"
    return {k: v for k, v in row.items() if k != "sides"}


EVENT_ADDITIONS = ("code", "superseded_by", "final")  # Stage 6.2: why a setup ended, as a code; what replaced it; what it stood at (None on the events that do not end one)


def bare_event(e: dict) -> dict:
    """A lifecycle event as the recording knew it. The events gained three keys in Stage 6.2. They are additive: the recording predates them, so they are left out of the hash (and must be
    there), while every key the recording did hold is still compared."""
    assert set(EVENT_ADDITIONS) <= set(e), "the lifecycle event lost its Stage 6.2 addition"
    return {k: v for k, v in e.items() if k not in EVENT_ADDITIONS}


def bare_decision(d: dict) -> dict:
    """The decision as the recording knew it (Stage 6.2 added `also_setups`, the agreeing strategies as ids beside the words in `also`: additive, so left out of the hash, and it must be there)."""
    assert isinstance(d["also_setups"], list) and len(d["also_setups"]) == len(d["also"]), "the decision lost its Stage 6.2 addition"
    return {k: v for k, v in d.items() if k != "also_setups"}


def lifecycle_snapshot() -> dict:
    """Each scenario played candle by candle through the real SetupBook (both orientations): what is announced, in what order, and every setup as the API shows it."""
    out = {}
    for name, (build, through) in SCENARIOS.items():
        for orient in ("bull", "bear"):
            book, steps = tr.SetupBook(P), []
            for n in through:
                ctx = build(n) if orient == "bull" else mirror(build(n))
                sess = tr.session_info(ctx.now, P)
                res = book.update(ctx, {"session": sess}, ctx.now)
                setups = [tr.setup_dict(s, ctx.price, True, True) for s in res["setups"]]
                recent = [tr.setup_dict(s, ctx.price, True, True) for s in res["recent"]]
                monitors = tr.monitor(res["scans"], setups, {"text": "m"}, sess)
                steps.append({"through": n, "live": [[s["id"], s["status"], s["stage"]] for s in setups], "ended": [[s["id"], s["status"]] for s in recent],
                              "events": [[e["id"], e["status"], e["text"]] for e in res["events"]], "monitor": [[m["id"], m["state"], m["active"]] for m in monitors if m["id"] in ORIGINAL],
                              "hash": digest([setups, recent, [bare_event(e) for e in res["events"]], [bare(m) for m in monitors if m["id"] in ORIGINAL]])})  # fmt: skip
            out[f"{name}/{orient}"] = steps
    return out


def pipeline_runs() -> dict:
    """The whole pipeline (real analysis, real scans, the lifecycle, the decision, the explanation) on the two integration worlds, slot by slot."""
    from test_phase3_integration import World

    out = {}
    for orient, world in (("bull", World()), ("bear", World(mirror=True))):
        md, clock = world.engine()
        for slot in range(12, 21):
            out[f"{orient}/{slot}"] = world.step(md, clock, slot)
    return out


def pipeline_snapshot() -> dict:
    out = {}
    for k, res in pipeline_runs().items():
        d = res["decision"]
        # The market payload gained two keys in Stage 3 (the 1H / 15M trends the decision checks every setup against, and the regime facts). They are additive: the recording predates
        # them, so they are left out of its hash (and must be there), while every key it did record is still compared.
        assert set(res["market"]) >= {"trends", "regime_facts"} and res["market"]["regime_facts"]["facts"], "the market payload lost its Stage 3 additions"
        assert "change" in res["market"], "the market payload lost its Stage 5.1 addition (the move from the previous close)"  # (also additive: left out of the recorded hash)
        market = {key: v for key, v in res["market"].items() if key not in ("trends", "regime_facts", "change")}
        rows = {"strategies": [bare(r) for r in only_original(res["strategies"], "id")], "setups": only_original(res["setups"]), "recent": only_original(res["recent"]),
                "events": [bare_event(e) for e in only_original(res["events"])], "decision": bare_decision(d), "explain": res["explain"], "market": market, "data": res["data"]}  # fmt: skip
        out[k] = {"action": d["action"], "headline": d["headline"], "strategy": d["strategy"], "setups": [[s["id"], s["status"], s["stage"]] for s in rows["setups"]],
                  "hash": {f: digest(v) for f, v in rows.items()}}  # fmt: skip
    return out


def snapshot() -> dict:
    return {"scans": scans_snapshot(), "lifecycle": lifecycle_snapshot(), "pipeline": pipeline_snapshot()}


def normalise(x):
    return json.loads(canon(x))


def moved(expected: dict, got: dict) -> list:
    keys = sorted(set(expected) | set(got))
    return [k for k in keys if canon(expected.get(k)) != canon(got.get(k))]


def recorded() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_original_strategies_produce_exactly_the_recorded_scans():
    expected, got = recorded()["scans"], normalise(scans_snapshot())
    assert len(expected) == 3 * 2 * 2 * sum(len(r) for _, r in SCENARIOS.values())  # three strategies, both sides, both orientations, every scenario step
    assert moved(expected, got) == [], "these scans changed: " + ", ".join(moved(expected, got)[:8])


def test_the_original_strategies_run_through_the_lifecycle_exactly_as_recorded():
    expected = recorded()["lifecycle"]
    with originals_only():
        got = normalise(lifecycle_snapshot())
    assert sorted(expected) == sorted(got) and len(expected) == 6
    assert moved(expected, got) == [], "these lifecycles changed: " + ", ".join(moved(expected, got))


def test_the_decision_pipeline_on_the_integration_worlds_is_exactly_as_recorded_when_only_the_originals_are_registered():
    expected = recorded()["pipeline"]
    with originals_only():
        got = normalise(pipeline_snapshot())
    assert len(expected) == 18
    assert moved(expected, got) == [], "the decision pipeline changed at: " + ", ".join(moved(expected, got)[:8])
    actions = {v["action"] for v in expected.values()}
    assert {"BUY CE", "BUY PE", "WAIT"} <= actions  # the recording really contains the interesting states: confirmed buys on both sides as well as waiting


def test_with_every_strategy_registered_the_originals_still_do_exactly_what_they_did():
    """The same scenarios with the new strategies present: the originals' setups, events and monitor rows are identical, step by step."""
    expected = recorded()["lifecycle"]
    got = normalise(lifecycle_snapshot())
    is_original = lambda row: str(row[0]).split(":")[0] in ORIGINAL  # noqa: E731
    for key, steps in expected.items():
        for want, have in zip(steps, got[key]):
            for field in ("live", "ended", "events"):
                assert [r for r in have[field] if is_original(r)] == want[field], f"{key} through {want['through']}: the originals' {field} changed"
            assert have["monitor"] == want["monitor"], f"{key} through {want['through']}: the originals' monitor rows changed"


def test_with_every_strategy_registered_the_decision_pipeline_keeps_every_fact_the_originals_contribute():
    """What the decision says about the market and the originals' setups does not change; the new strategies may add reason lines and rows of their own."""
    full = pipeline_runs()
    with originals_only():
        base = pipeline_runs()
    assert set(full) == set(base) and len(base) == 18
    for k in base:
        b, f = base[k], full[k]
        for section in ("market", "data"):
            assert canon(b[section]) == canon(f[section]), f"{k}: {section} changed"
        for section, key in (("strategies", "id"), ("setups", "strategy"), ("recent", "strategy"), ("events", "strategy")):
            assert canon(only_original(f[section], key)) == canon(b[section]), f"{k}: the originals' {section} changed"
        bd, fd = b["decision"], f["decision"]
        assert (fd["action"], fd["headline"], fd["strategy"], fd["setup_id"]) == (bd["action"], bd["headline"], bd["strategy"], bd["setup_id"]), f"{k}: the decision changed"
        if bd["action"].startswith("BUY"):
            assert canon(fd) == canon(bd) and canon(f["explain"]) == canon(b["explain"]), f"{k}: a BUY changed"  # a confirmed buy is explained exactly as before
        missing = [r for r in bd["reasons"] if r not in fd["reasons"]]
        assert not missing, f"{k}: reason lines the originals contributed disappeared: {missing}"


if __name__ == "__main__":
    if "--write" not in sys.argv:
        sys.exit("pass --write to record the current behaviour as the expected one")
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(normalise(snapshot()), indent=1, sort_keys=True), encoding="utf-8")
    print("recorded", GOLDEN)
