"""Checking a trading day against the rules Prism is supposed to keep: the automatic half of the validation runbook. READ-ONLY.

    python -m app.session_check logs/decisions-2026-11-11.jsonl                   # one day: what passed, what failed, what only you can check
    python -m app.session_check logs/decisions-*.jsonl --coverage                 # many days: which kinds of day have been checked, which have not
    python -m app.session_check logs/decisions-2026-11-11.jsonl --json

The README's "Validating during market hours" lists what to look at after a session. Much of it can be checked by a program from the journal alone, so a person does not have to scroll
through hours of records to find a BUY in the opening range or a setup that came back from the dead. This does that part: every BUY the journal holds is tested against the rules (not in the
opening range, not in the late session, on trusted data, after the confirming 5M candle completed, with the price in the entry area, with the stop on the right side, with the 1H / 15M trend
rule kept, never with a CE and a PE setup both ready), nothing ended is revived and nothing is announced twice, and the journal covers the session. It cannot see the chart: whether the
trigger sat at the highest high of the base and whether the price matched your broker is still yours to check, and it prints those as the manual list.

A PASS means the journal holds no counter-example, not that the decisions were good: the outcome review (`python -m app.outcomes`) is where that is looked at, and a few days are a small sample.
A day with no record inside the session (a weekend, a holiday, a Prism that was off) is said to be NOT A SESSION and checks nothing."""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from .candles import CLOSE_S, IST, OPEN_S
from .journal import read
from .journal_check import verify

OR_END_S = 9 * 3600 + 30 * 60  # the opening range is complete at 09:30
LATE_S = 14 * 3600 + 45 * 60  # no new entry from 14:45
BUYS = ("BUY CE", "BUY PE")
SIDE = {"BUY CE": "CE", "BUY PE": "PE"}
TERMINAL = ("INVALIDATED", "EXPIRED", "MISSED")
SILENCE_S = 600  # a stretch of the session with no record at all (a heartbeat is written every 5 minutes)
SHOWN = 3  # examples listed per finding
GAP_TAG_PERCENT = 0.5  # the opening gap that makes a day a "gap" day for the coverage table (a convention of this tool, not a Prism threshold)
MANUAL = (
    "09:15-09:30: the decision said WAIT (opening range forming), and the price and the last completed 5M / 15M candles match your broker's chart",
    "for every setup event and every BUY: the trigger is where you would put it on the chart (the highest high of the base), the invalidation is beyond the pullback low / broken level, and the BUY came after the confirming candle closed",
    "a BUY turned into WAIT (do not chase) once price left the entry area",
    "12:00-13:30: the stricter-confirmation note was on the page",
    "was it an expiry day? (the journal does not say)",
    "for each decision: do you agree with it? (judge it on whether it was explainable and matched the chart, not on profit)",
)  # fmt: skip


def tod(t: float) -> float:
    """Seconds since midnight, IST."""
    d = datetime.fromtimestamp(t, IST)
    return d.hour * 3600 + d.minute * 60 + d.second + d.microsecond / 1e6


def hms(t: float) -> str:
    return datetime.fromtimestamp(t, IST).strftime("%H:%M:%S")


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def action(r: dict) -> str | None:
    return (r.get("decision") or {}).get("action")


def in_session(r: dict) -> bool:
    ph = (r.get("session") or {}).get("phase")
    t = _num(r.get("t"))
    return ph not in (None, "pre_open", "closed") if ph is not None else (t is not None and OPEN_S <= tod(t) < CLOSE_S)


def buys(records: list) -> list:
    return [r for r in records if action(r) in BUYS and _num(r.get("t")) is not None]


def result(cid: str, title: str, status: str, detail: str = "", examples=()) -> dict:
    return {"id": cid, "title": title, "status": status, "detail": detail, "examples": list(examples)[:SHOWN]}


def _rule(cid: str, title: str, bad: list, n: int, none_detail: str) -> dict:
    """pass / fail for a rule about BUYs: `bad` are the offending (record, why) pairs, `n` how many BUYs were tested."""
    if not n:
        return result(cid, title, "na", none_detail)
    if not bad:
        return result(cid, title, "pass", f"{n} BUY decision(s) checked")
    return result(cid, title, "fail", f"{len(bad)} of {n} BUY decision(s)", [f"{hms(r['t'])} {why}" for r, why in bad])


def check_records(records: list) -> list:
    """The checks that need only the records of one day, as result dicts (id, title, status pass / fail / warn / info / na, detail, examples)."""
    out, bs = [], buys(records)
    none = "the journal holds no BUY decision (nothing to test)"
    bad = [(r, f"{action(r)} at {r.get('price')} outside the session (phase {(r.get('session') or {}).get('phase')})") for r in bs if tod(r["t"]) < OPEN_S or tod(r["t"]) >= CLOSE_S or (r.get("session") or {}).get("phase") in ("pre_open", "closed")]
    out.append(_rule("buy_in_session", "No BUY before the open or after the close", bad, len(bs), none))
    bad = [(r, f"{action(r)}: the opening range was still forming") for r in bs if OPEN_S <= tod(r["t"]) < OR_END_S]
    out.append(_rule("buy_not_in_opening_range", "No BUY from 09:15 to 09:30 (the opening range is forming)", bad, len(bs), none))
    bad = [(r, f"{action(r)} in the late session") for r in bs if tod(r["t"]) >= LATE_S and tod(r["t"]) < CLOSE_S]
    out.append(_rule("buy_not_late", "No BUY from 14:45 (late session)", bad, len(bs), none))

    def untrusted(r):
        d, f = r.get("data") or {}, r.get("feed") or {}
        why = []
        if d.get("ok") is not True:
            why.append("data not trusted: " + "; ".join(map(str, (d.get("issues") or ["no data section"])[:2])))
        if f.get("state") != "connected":
            why.append(f"feed {f.get('state')}")
        if (f.get("spot") or {}).get("stale"):
            why.append("spot price stale")
        return "; ".join(why)

    bad = [(r, f"{action(r)}: {w}") for r in bs if (w := untrusted(r))]
    out.append(_rule("buy_on_trusted_data", "Every BUY was made on trusted data and a live feed", bad, len(bs), none))

    def unconfirmed(r):
        s = r.get("decision_setup")
        if not isinstance(s, dict):
            return "names no setup"
        end = _num(s.get("confirmed_bar_end"))
        if s.get("status") != "CONFIRMED" or end is None:
            return f"its setup is {s.get('status')}, not confirmed"
        if end > r["t"] + 1:
            return f"made {end - r['t']:.0f} s before its confirming 5M candle completed"
        return ""

    bad = [(r, f"{action(r)}: {w}") for r in bs if (w := unconfirmed(r))]
    out.append(_rule("buy_after_confirmation", "Every BUY came after its confirming 5M candle completed", bad, len(bs), none))

    def outside_area(r):
        s = r.get("decision_setup") or {}
        return "" if s.get("stage") == "READY_TO_ACT" and s.get("price_state") == "in_entry_area" else f"stage {s.get('stage')}, price {s.get('price_state')}: not in the entry area"

    bad = [(r, f"{action(r)}: {w}") for r in bs if (w := outside_area(r))]
    out.append(_rule("buy_in_entry_area", "Every BUY had the price in the entry area and nothing blocking", bad, len(bs), none))

    def stop_wrong(r):
        s, p = r.get("decision_setup") or {}, _num(r.get("price"))
        inv, side = _num(s.get("invalidation")), SIDE[action(r)]
        if inv is None or p is None:
            return "no invalidation level or price on the record"
        return "" if (inv < p if side == "CE" else inv > p) else f"invalidation {inv:g} is on the wrong side of the price {p:g}"

    bad = [(r, f"{action(r)}: {w}") for r in bs if (w := stop_wrong(r))]
    out.append(_rule("buy_stop_on_right_side", "Every BUY had its invalidation on the right side of the price", bad, len(bs), none))

    def against_trend(r):
        side, trends = SIDE[action(r)], (r.get("market") or {}).get("trends") or {}
        opposed = [tf for tf in ("1h", "15m") if trends.get(tf) == ("bearish" if side == "CE" else "bullish")]
        if not opposed:
            return ""
        strat = (r.get("decision") or {}).get("strategy")
        granted = any(c.get("granted") and c.get("strategy") in (strat, None) and c.get("side") in (side, None) for c in r.get("counter_trend") or [] if isinstance(c, dict))
        return "" if granted else f"{action(r)} with the {' and '.join(t.upper() for t in opposed)} trend against it and no counter-trend permission"

    bad = [(r, w) for r in bs if (w := against_trend(r))]
    out.append(_rule("buy_respects_trend", "No BUY against the 1H / 15M trend without a granted counter-trend permission", bad, len(bs), none))

    both = [r for r in records if _num(r.get("t")) is not None and {s.get("direction") for s in r.get("setups") or [] if isinstance(s, dict) and s.get("stage") == "READY_TO_ACT"} == {"CE", "PE"}]
    bad = [(r, f"{action(r)} with a CE and a PE setup both ready") for r in both if action(r) in BUYS]
    out.append(result("conflict_is_wait", "A CE and a PE setup both ready never gives a BUY", "fail" if bad else "pass" if both else "na",
                      f"{len(bad)} of {len(both)} record(s)" if bad else f"{len(both)} record(s) had both sides ready" if both else "no record had both sides ready", [f"{hms(r['t'])} {w}" for r, w in bad]))

    ended, revived, seen, twice = {}, [], set(), []
    for r in records:
        t = _num(r.get("t"))
        if t is None:
            continue
        for s in r.get("setups") or []:
            if isinstance(s, dict) and s.get("id") in ended and s.get("stage") not in TERMINAL:
                revived.append((r, f"setup {s['id']} is live again after it ended at {hms(ended[s['id']])}"))
        for e in r.get("events") or []:
            if not isinstance(e, dict):
                continue
            k = (e.get("seq"), e.get("id"), e.get("status"))
            if k in seen:
                twice.append((r, f"event {e.get('seq')} ({e.get('status')} {e.get('id')}) written again"))
            seen.add(k)
            if e.get("code") is not None and e.get("id"):
                ended.setdefault(e["id"], t)
    out.append(result("nothing_revived", "No setup that ended was live again", "fail" if revived else "pass", f"{len(revived)} record(s)" if revived else f"{len(ended)} ended setup(s) stayed ended", [f"{hms(r['t'])} {w}" for r, w in revived]))
    out.append(result("nothing_twice", "No event was announced twice", "fail" if twice else "pass", f"{len(twice)} repeat(s)" if twice else f"{len(seen)} event(s), each once", [f"{hms(r['t'])} {w}" for r, w in twice]))
    return out + coverage_checks(records)


def coverage_checks(records: list) -> list:
    """Did the journal cover the session, and was the data trusted for most of it: warnings, not failures (a Prism that was off is not a wrong decision)."""
    ts = sorted(t for r in records if (t := _num(r.get("t"))) is not None and OPEN_S <= tod(t) < CLOSE_S)
    out = []
    if not ts:
        return [result("session_covered", "The journal covers the session", "na", "no record falls inside 09:15-15:30")]
    problems = []
    if tod(ts[0]) > OPEN_S + 120:
        problems.append(f"the journal starts at {hms(ts[0])}: the open is not covered")
    if tod(ts[-1]) < CLOSE_S - 600:
        problems.append(f"the journal ends at {hms(ts[-1])}: the close is not covered")
    silent = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > SILENCE_S]
    problems += [f"no record between {hms(a)} and {hms(b)}" for a, b in silent[:SHOWN]]
    out.append(result("session_covered", "The journal covers the session without a silent stretch", "warn" if problems else "pass", "; ".join(problems) if problems else f"{len(ts)} records from {hms(ts[0])} to {hms(ts[-1])}", problems))
    rows = [r for r in records if (t := _num(r.get("t"))) is not None and OPEN_S <= tod(t) < CLOSE_S]
    bad = [r for r in rows if (r.get("data") or {}).get("ok") is not True]
    share = 100.0 * len(bad) / len(rows)
    out.append(result("data_trusted_share", "The data was trusted for most of the session", "warn" if share > 10 else "info", f"{len(rows) - len(bad)} of {len(rows)} session record(s) on trusted data ({100 - share:.0f}%)",
                      sorted({str(i)[:80] for r in bad for i in ((r.get("data") or {}).get("issues") or [])})[:SHOWN]))
    restarts = [r for r in records if r.get("reason") == "restart"]
    failed = [r for r in restarts if isinstance(r.get("restart"), dict) and r["restart"].get("ok") is False]
    out.append(result("restarts", "Restarts", "warn" if failed else "info", f"{len(restarts)} restart(s)" + (f", {len(failed)} could not restore the setups" if failed else ""), [f"{hms(r['t'])} restart did not restore" for r in failed]))
    return out


def day_type(records: list) -> dict:
    """What kind of day it was, from the journal's own market facts: the opening gap, the regime and the volatility, weighted by how long each held."""
    rows = sorted((r for r in records if _num(r.get("t")) is not None and in_session(r)), key=lambda r: r["t"])
    gap = next((g for r in rows if isinstance(g := (r.get("market") or {}).get("gap"), dict) and _num(g.get("percent")) is not None), None)
    reg, vol = Counter(), Counter()
    for a, b in zip(rows, rows[1:] + [None]):
        w = min((b["t"] - a["t"]) if b else 60, SILENCE_S)
        m = a.get("market") or {}
        if m.get("regime"):
            reg[m["regime"]] += w
        if m.get("volatility"):
            vol[m["volatility"]] += w
    tot_r, tot_v = sum(reg.values()), sum(vol.values())
    pct = _num(gap["percent"]) if gap else None
    return {"gap_percent": pct, "gap": None if pct is None else "gap up" if pct >= GAP_TAG_PERCENT else "gap down" if pct <= -GAP_TAG_PERCENT else "flat open",
            "regime": reg.most_common(1)[0][0] if reg else None, "regime_share": (reg.most_common(1)[0][1] / tot_r) if reg else None,
            "high_volatility_share": (vol.get("high", 0) / tot_v) if tot_v else None,
            "volatile": bool(tot_v) and vol.get("high", 0) / tot_v >= 0.25, "session_records": len(rows), "buys": len(buys(rows))}  # fmt: skip


def check_file(path) -> dict:
    """Everything for one journal file: the journal's own integrity, the rule checks, the kind of day, the manual list."""
    records, bad = read(path)
    rep = verify(path)
    sess = [r for r in records if in_session(r)]
    checks = [result("journal_sound", "The journal file itself is sound (no error from --verify)", "pass" if rep["ok"] else "fail",
                     f"{len(records)} record(s)" + (f", {len(bad)} line(s) unreadable" if bad else ""), [f"{p.get('code')}: {p.get('message')}"[:120] for p in rep.get("problems", []) if p.get("level") == "error"])]
    checks += check_records(records) if sess else []
    failed = [c for c in checks if c["status"] == "fail"]
    return {"file": Path(path).name, "day": _day(path, records), "session": bool(sess), "day_type": day_type(records), "checks": checks, "manual": list(MANUAL),
            "passed": sum(1 for c in checks if c["status"] == "pass"), "failed": len(failed), "warned": sum(1 for c in checks if c["status"] == "warn"), "ok": not failed}  # fmt: skip


def _day(path, records):
    name = Path(path).name
    if name.startswith("decisions-") and name.endswith(".jsonl"):
        return name[len("decisions-"):-len(".jsonl")]
    t = next((_num(r.get("t")) for r in records if _num(r.get("t")) is not None), None)
    return datetime.fromtimestamp(t, IST).date().isoformat() if t is not None else None


MARK = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "info": "info", "na": "n/a"}


def format_report(rep: dict) -> list:
    out = [f"Session check {rep['day']} ({rep['file']})"]
    if not rep["session"]:
        out.append("NOT A SESSION: no record falls inside the trading session (a weekend or holiday, or Prism was not running): nothing to check.")
    else:
        dt = rep["day_type"]
        kind = [dt["gap"] and f"{dt['gap']} ({dt['gap_percent']:+.2f}%)", dt["regime"] and f"{dt['regime']} for {100 * dt['regime_share']:.0f}% of the time", "volatile" if dt["volatile"] else "normal volatility"]
        out.append(f"Kind of day: {', '.join(k for k in kind if k)}; {dt['session_records']} session records, {dt['buys']} of them a BUY")
    for c in rep["checks"]:
        out.append(f"  {MARK[c['status']]:4} {c['title']}" + (f": {c['detail']}" if c["detail"] else ""))
        if c["status"] == "fail" or (c["status"] == "warn" and "; ".join(c["examples"]) != c["detail"]):
            out += [f"         {e}" for e in c["examples"]]
    if rep["session"]:
        verdict = "FAILED" if rep["failed"] else "no counter-example in the journal"
        out.append(f"Automatic checks: {rep['passed']} passed, {rep['failed']} failed, {rep['warned']} warning(s): {verdict}.")
        out.append("Only you can check (the journal cannot see the chart):")
        out += [f"  [ ] {m}" for m in rep["manual"]]
        out.append("A PASS is the absence of a counter-example, not a verdict on the decisions: `python -m app.outcomes FILE` looks at how the setups went. A few days are a small sample.")
    return out


def coverage_table(reports: list) -> list:
    """Many days side by side: which kinds of day have a clean automatic check and which have not been seen (the runbook's 'repeat on different kinds of sessions')."""
    wanted = {"gap up": 0, "gap down": 0, "flat open": 0, "trending (regime trend_up / trend_down)": 0, "range-bound": 0, "volatile": 0}
    out = ["Day                 session  BUYs  checks            kind of day"]
    for r in sorted(reports, key=lambda r: r["day"] or ""):
        dt = r["day_type"]
        if not r["session"]:
            out.append(f"{r['day']}  not a session")
            continue
        clean = r["ok"] and dt["buys"] > 0  # a day with no BUY passes the BUY rules by having nothing to test: it does not count as a kind of day checked
        for k, hit in (("gap up", dt["gap"] == "gap up"), ("gap down", dt["gap"] == "gap down"), ("flat open", dt["gap"] == "flat open"),
                       ("trending (regime trend_up / trend_down)", bool(dt["regime"]) and dt["regime"].startswith("trend")), ("range-bound", bool(dt["regime"]) and "range" in dt["regime"]), ("volatile", dt["volatile"])):
            wanted[k] += 1 if hit and clean else 0
        out.append(f"{r['day']}  yes      {dt['buys']:>4}  {'clean' if clean else 'FAILED' if not r['ok'] else 'no BUY':<7} ({r['passed']}p {r['failed']}f {r['warned']}w)  {dt['gap'] or 'gap unknown'}, {dt['regime']}, {'volatile' if dt['volatile'] else 'normal'}")
    out.append("Kinds of day with a clean automatic check so far (the runbook asks for each before Prism is trusted; 0 = not yet seen):")
    out += [f"  {k}: {n}" for k, n in wanted.items()]
    out.append("Expiry days cannot be told from the journal: note them yourself.")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check a trading day's decision journal against Prism's own rules (read-only).")
    ap.add_argument("files", nargs="+", help="logs/decisions-YYYY-MM-DD.jsonl (several with --coverage)")
    ap.add_argument("--coverage", action="store_true", help="a table of many days and the kinds of day checked")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    reports = []
    for f in args.files:
        try:
            reports.append(check_file(f))
        except OSError as e:
            print(f"{f}: cannot be read ({e.strerror or e})", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(reports if len(reports) > 1 or args.coverage else reports[0], indent=1, allow_nan=False, default=str))
    elif args.coverage:
        print("\n".join(coverage_table(reports)))
    else:
        for r in reports:
            print("\n".join(format_report(r)))
    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
