"""A snapshot of what the six strategies added in Stage 2 (Momentum Continuation, EMA Trend Continuation, Volatility Expansion, Opening Range Breakout, Failed Breakout, Range
Bounce) do on every fixture their own tests use, in both orientations, step by step through the real SetupBook: every scanner result (applicable, note, setup fields), every live
setup as the dashboard receives it, and the decision (action, headline, strategy, setup id).

`tests/golden/stage2_strategies.json` was written from the code as it stood BEFORE Stage 3, and test_stage2_strategies_golden.py recomputes this and compares it with that file,
so that nothing Stage 3 adds can change what those six strategies say. It lives beside the original golden (test_existing_strategies_golden.py, which covers the three originals).

    python tests/stage2_snapshot.py --write      # only for a deliberate, documented change of one of these strategies"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_range_bounce as RB  # noqa: E402
import test_session_and_trap as ST  # noqa: E402
import test_trend_family as TF  # noqa: E402
from scenarios import bull_breakout, bull_pullback, bull_range  # noqa: E402
from test_trend_family import decision_after, play  # noqa: E402

NINE = ["trend_pullback", "breakout_retest", "range_reversal", "momentum_continuation", "ema_trend_continuation", "volatility_expansion", "opening_range_breakout", "failed_breakout",
        "range_bounce"]  # fmt: skip
SIX = NINE[3:]
GOLDEN = ROOT / "tests" / "golden" / "stage2_strategies.json"
MODULES = ["app/strategies/trend_family.py", "app/strategies/opening_range.py", "app/strategies/failed_breakout.py", "app/strategies/range_bounce.py"]

T = TF.PAUSE
MC_ROWS = {**T, 17: (24660, 24661, 24630, 24634)}
SCENARIOS = {  # name -> (builder, throughs, kwargs)
    "mc_flow": (TF.trend_ctx, range(12, 17), {}),
    "mc_invalidated": (TF.trend_ctx, range(12, 18), {"rows": MC_ROWS}),
    "mc_hold": (TF.trend_ctx, range(12, 18), {"rows": {**T, 17: (24660, 24662, 24656, 24659)}}),
    "mc_chase": (TF.trend_ctx, range(12, 18), {"rows": {**T, 17: (24660, 24664, 24659, 24661)}}),
    "mc_strict": (TF.trend_ctx, range(12, 17), {"strict": True, "strict_why": "midday low-activity window"}),
    "mc_blocked": (TF.trend_ctx, range(12, 17), {"can_confirm": False, "blockers": ["historical data is stale: today's refresh has not completed"]}),
    "etc_flow": (TF.trend_ctx, range(12, 17), {"ema5": TF.EMA_TOUCH}),
    "tp_deep": (TF.trend_ctx, range(12, 16), {"rows": {**T, 15: (24644, 24648, 24628, 24647)}}),
    "ve_flow": (TF.expansion_ctx, [32], {}),
    "ve_end": (TF.expansion_ctx, [32, 33], {"after": {33: (24610, 24611, 24585, 24588)}}),
    "ve_strict": (TF.expansion_ctx, [32, 33], {"strict": True, "strict_why": "volatility is high", "after": {33: (24609, 24611, 24604, 24606)}}),
    "orb_flow": (ST.orb_ctx, range(2, 6), {}),
    "orb_weak_then_solid": (ST.orb_ctx, range(2, 7), {"rows": {5: (24621, 24630, 24620, 24624), 6: (24624, 24631, 24623, 24629)}}),
    "orb_chase": (ST.orb_ctx, range(2, 6), {"rows": {5: (24616, 24634, 24615, 24632)}}),
    "orb_invalidated": (ST.orb_ctx, range(2, 7), {"rows": {6: (24626, 24627, 24598, 24600)}}),
    "orb_strict": (ST.orb_ctx, range(2, 7), {"rows": {6: ST.HOLD}, "strict": True, "strict_why": "volatility is high"}),
    "orb_window": (ST.orb_ctx, [4, 19, 20, 21], {"rows": {**ST.fill(5, 21), 21: ST.BREAK}}),
    "orb_blocked": (ST.orb_ctx, range(2, 6), {"levels": [ST.Level(24630.0, "swing level x2", 2)]}),
    "fb_flow": (ST.fb_ctx, range(21, 25), {}),
    "fb_strong_reclaim": (ST.fb_ctx, range(21, 24), {"rows": {23: (24590, 24603, 24589, 24602)}}),
    "fb_deep": (ST.fb_ctx, range(21, 25), {"rows": {22: (24594, 24597, 24570, 24590)}}),
    "fb_end": (ST.fb_ctx, range(21, 26), {"rows": {25: (24608, 24609, 24580, 24584)}}),
    "fb_strict": (ST.fb_ctx, range(21, 26), {"rows": {25: (24608, 24614, 24607, 24612)}, "strict": True, "strict_why": "volatility is high"}),
    "fb_tracked_old": (ST.fb_ctx, range(21, 35), {"rows": {**{i: ST.QUIET for i in range(24, 45)}}}),
    "rb_flow": (RB.box_ctx, range(33, 37), {}),
    "rb_wick": (RB.box_ctx, range(33, 36), {"dip": RB.DIP_B, "floor": RB.ORIGIN_FAR}),
    "rb_tp_holds": (RB.box_ctx, range(33, 37), {"floor": RB.ORIGIN_FAR}),
    "rb_trap": (RB.box_ctx, range(33, 36), {"dip": {34: (24578, 24580, 24550, 24555), 35: (24556, 24572, 24554, 24570)}, "floor": RB.ORIGIN_FAR}),
    "rb_strict": (RB.box_ctx, range(33, 37), {"dip": RB.DIP_B, "floor": RB.ORIGIN_FAR, "rows": {36: (24582, 24590, 24580, 24586)}, "strict": True, "strict_why": "volatility is high"}),
}
BUILTIN = {  # the three original scenarios of scenarios.py, at the steps their tests use
    "tp_pullback": (lambda n, **kw: bull_pullback(n, **kw), range(14, 20), {}),
    "br_breakout": (lambda n, **kw: bull_breakout(n, **kw), range(24, 29), {}),
    "rr_range": (lambda n, **kw: bull_range(n, **kw), range(31, 36), {}),
}
SIDES = ("bull", "bear")


def scan_row(sc) -> dict:
    s = sc.setup
    return {"applicable": sc.applicable, "note": sc.note,
            "setup": None if s is None else {"key": s.key, "status": s.status, "anchor": s.anchor_time, "level": s.level, "level_label": s.level_label, "trigger": s.trigger, "zone": s.zone,
                                             "invalidation": s.invalidation, "extreme": s.extreme, "confirm_text": s.confirm_text, "note": s.note, "reasons": s.reasons, "against": s.against,
                                             "blockers": s.blockers, "quality": s.quality, "support": s.support, "resistance": s.resistance, "plan": s.plan, "group": s.group,
                                             "candle_confirmed": s.candle_confirmed, "confirm_candle": s.confirm_candle, "break_candle": s.break_candle}}  # fmt: skip


def record(builders: dict) -> dict:
    out = {}
    for name, (build, throughs, kw) in builders.items():
        throughs = list(throughs)
        for side in SIDES:
            book, steps = play(build, throughs, side, **kw)
            rows = []
            for k, (_, res) in enumerate(steps):
                live = {f"{n}:{sd}": [st, trig] for (n, sd), (st, trig) in res["live_now"].items() if n in NINE}
                scans = {f"{n}:{sd}": scan_row(sc) for (n, sd), sc in sorted(res["scans"].items()) if n in NINE}
                dec = decision_after(build, throughs[: k + 1], side, **kw)["decision"]
                rows.append({"through": throughs[k], "scans": scans, "live_now": live, "decision": {x: dec[x] for x in ("action", "headline", "strategy", "setup_id")}})
            ended = [[e["strategy"], e["status"], e["text"]] for e in reversed(book.events) if e["strategy"] in NINE]
            out[f"{name}/{side}"] = {"steps": rows, "events": ended}
    return out


def source_hashes() -> dict:
    return {m: hashlib.sha256((ROOT / m).read_bytes()).hexdigest() for m in MODULES}


def snapshot() -> dict:
    tr_scenarios = {**SCENARIOS, **BUILTIN}
    return json.loads(json.dumps({"scenarios": record(tr_scenarios), "sources": source_hashes()}, default=str))


if __name__ == "__main__":
    if "--write" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(snapshot(), indent=1, sort_keys=True), encoding="utf-8")
        print("wrote", GOLDEN, GOLDEN.stat().st_size, "bytes")
    else:
        print("use --write")
