"""Checking a decision journal file, READ-ONLY.

    python -m app.journal logs/decisions-2026-11-06.jsonl --verify          # what is wrong with it, and what is in it
    python -m app.journal logs/decisions-2026-11-06.jsonl --verify --json   # the same, for a program

It opens the file for reading only: nothing is repaired, rewritten, truncated or deleted. It says, for every problem, how serious it is and where:

  error  the file cannot be trusted for that: a time that goes backwards, a decision that is not one of the four, an event after its setup ended, a status that goes backwards
  warn   something was lost or odd, and the journal is still usable: a line that could not be read (a write cut short), an event number missing or used by two events, an event written twice,
         a record that belongs to another day, a time that disagrees with itself, an outage that cost records

and it summarises what the file holds (records and bytes by kind, the share that is once-per-candle heartbeats, and how many of those were written while the market was closed), which is what
a decision about the file's size needs. The exit status is 1 when there is an error, else 0 (2: the file cannot be opened)."""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .candles import IST
from .journal import SCHEMA

REASONS = ("start", "restart", "changed", "candle")
ACTIONS = ("BUY CE", "BUY PE", "WAIT", "NO TRADE")
LIVE = ("DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED")
TERMINAL = ("INVALIDATED", "EXPIRED")
RANK = {s: i for i, s in enumerate(LIVE)}
CLOSED = ("closed", "pre_open")  # the session phases with nothing to trade
NAME = re.compile(r"decisions-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def verify(path) -> dict:
    """The problems found in a journal file and a summary of what it holds. Raises OSError when the file cannot be read; touches nothing."""
    p = Path(path)
    raw = p.read_bytes()
    lines = raw.decode("utf-8", errors="replace").splitlines()  # (the same lines, numbered the same way, as the reader's)
    problems: list[dict] = []

    def problem(level, code, message, where=()):
        problems.append({"level": level, "code": code, "message": message, "lines": sorted(set(where))[:20], "count": len(set(where)) or 1})

    # -- the lines ----------------------------------------------------------------------------------------------------------------------------
    recs, bad = [], []  # (line, record, bytes)
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad.append(n)
            continue
        if not isinstance(r, dict) or not isinstance(r.get("decision"), dict):
            bad.append(n)
            continue
        recs.append((n, r, len(line.encode("utf-8")) + 1))
    if bad:
        problem("warn", "unreadable_line", f"{len(bad)} line(s) could not be read as a record and a reader skips them (a write cut short by a crash?)", bad)
    if raw and not raw.endswith(b"\n"):
        problem("warn", "no_final_newline", "the last line has no line ending: a write was cut short there", [len(lines)])
    if raw.startswith(b"\xef\xbb\xbf"):
        problem("warn", "bom", "the file starts with a byte-order mark")
    m = NAME.search(p.name)
    file_date = m.group(1) if m else None

    # -- each record -------------------------------------------------------------------------------------------------------------------------
    versions, reasons, size_by = Counter(), Counter(), Counter()
    bad_reason, bad_action, no_time, backwards, other_day, disagree, newer, unreadable_event = [], [], [], [], [], [], [], []
    previous = None
    for n, r, size in recs:
        v = r.get("v", 1)
        versions[v if isinstance(v, int) else "?"] += 1
        if isinstance(v, int) and v > SCHEMA:
            newer.append(n)
        reason = r.get("reason")
        reasons[reason] += 1
        size_by[reason] += size
        if reason not in REASONS:
            bad_reason.append(n)
        if r["decision"].get("action") not in ACTIONS:
            bad_action.append(n)
        t = _num(r.get("t"))
        if t is None:
            no_time.append(n)
            continue
        if previous is not None and t < previous - 1.0:
            backwards.append(n)
        previous = t if previous is None else max(previous, t)
        if file_date and datetime.fromtimestamp(t, IST).date().isoformat() != file_date:
            other_day.append(n)
        try:
            parsed = datetime.fromisoformat(str(r.get("time")))
            if abs(parsed.timestamp() - t) > 2 or parsed.utcoffset() != datetime.fromtimestamp(t, IST).utcoffset():
                disagree.append(n)
        except ValueError:
            disagree.append(n)
    if no_time:
        problem("error", "no_time", "a record has no usable time (`t`), so it cannot be placed", no_time)
    if backwards:
        problem("error", "time_backwards", "the time goes backwards (the clock was changed, or two runs wrote at once)", backwards)
    if bad_action:
        problem("error", "bad_action", f"the decision is not one of {', '.join(ACTIONS)}", bad_action)
    if bad_reason:
        problem("warn", "bad_reason", f"the reason is not one of {', '.join(REASONS)}", bad_reason)
    if other_day:
        problem("warn", "other_day", f"a record belongs to another day than the file's ({file_date})", other_day)
    if disagree:
        problem("warn", "time_disagrees", "`time` and `t` disagree, or the time is not in IST (+05:30)", disagree)
    if newer:
        problem("warn", "newer_version", f"a record is of a newer version than this tool knows ({SCHEMA})", newer)

    # -- the lifecycle events -----------------------------------------------------------------------------------------------------------------
    events = []  # (line, record version, event)
    for n, r, _ in recs:
        for e in r.get("events") or []:
            if isinstance(e, dict) and isinstance(e.get("seq"), int) and e.get("id") and e.get("status"):
                events.append((n, r.get("v", 1), e))
            else:
                unreadable_event.append(n)
    if unreadable_event:
        problem("warn", "malformed_event", "an event has no number, setup or status", unreadable_event)
    identity = Counter((e["seq"], e["id"], e["status"]) for _, _, e in events)
    repeated = sorted(k for k, c in identity.items() if c > 1)
    if repeated:
        problem("warn", "event_repeated", f"{len(repeated)} event(s) are written more than once (the same number, setup and status): {', '.join(str(k[0]) for k in repeated[:8])}",
                [n for n, _, e in events if (e["seq"], e["id"], e["status"]) in repeated])
    by_seq = defaultdict(set)
    for seq, sid, status in identity:
        by_seq[seq].add((sid, status))
    shared = sorted(s for s, who in by_seq.items() if len(who) > 1)
    if shared:
        problem("warn", "event_number_shared", f"event number(s) {', '.join(map(str, shared[:8]))} belong to more than one event (two runs numbered from 0?)", [n for n, _, e in events if e["seq"] in shared])
    gaps = [s for s in range(min(by_seq), max(by_seq) + 1) if s not in by_seq] if by_seq else []
    if gaps:
        problem("warn", "event_number_missing", f"event number(s) missing: {', '.join(map(str, gaps[:10]))}{' ...' if len(gaps) > 10 else ''} (an event was lost, or it was on a line that was cut)")
    story, seen = defaultdict(list), set()
    for n, v, e in events:
        key = (e["seq"], e["id"], e["status"])
        if key not in seen:
            seen.add(key)
            story[e["id"]].append((n, e["status"]))
    after_end, backward, twice, unknown, open_setups = [], [], [], [], []
    for sid, steps in story.items():
        ended, rank = False, -1
        for n, status in steps:
            if status not in RANK and status not in TERMINAL:
                unknown.append(n)
            elif ended:
                after_end.append(n)
            elif status in TERMINAL:
                ended = True
            elif RANK[status] < rank:
                backward.append(n)
            else:
                if RANK[status] == rank:
                    twice.append(n)
                rank = RANK[status]
        if not ended:
            open_setups.append(sid)
    if after_end:
        problem("error", "event_after_end", "an event for a setup that had already ended", after_end)
    if backward:
        problem("error", "status_backwards", "a setup's status goes backwards (e.g. CONFIRMED, then WAITING_FOR_CONFIRMATION)", backward)
    if twice:
        problem("warn", "status_twice", "a setup is announced twice with the same status", twice)
    if unknown:
        problem("warn", "unknown_status", "an event has a status this tool does not know", unknown)
    missing = [n for n, v, e in events if isinstance(v, int) and v >= 2 and e["status"] in TERMINAL and not (e.get("code") and e.get("final"))]
    if missing:
        problem("warn", "ending_without_code", "a version-2 event that ends a setup has no `code` or `final`", missing)

    # -- restarts and outages ------------------------------------------------------------------------------------------------------------------
    restarts = [(n, r.get("time"), r.get("restart") if isinstance(r.get("restart"), dict) else None) for n, r, _ in recs if r.get("reason") in ("start", "restart")]
    failed = [n for n, _, info in restarts if info is not None and info.get("ok") is False]
    if failed:
        problem("warn", "restore_failed", "a restart could not restore the day's setups (see `restart` on these records): they were re-derived from the candles", failed)
    outages = [(n, r["outage"]) for n, r, _ in recs if isinstance(r.get("outage"), dict)]
    lost = [n for n, o in outages if (_num(o.get("lost")) or 0) > 0]
    if lost:
        problem("warn", "records_lost", "the storage was unavailable for long enough that records were lost (see `outage` on these records)", lost)

    # -- what the file holds ------------------------------------------------------------------------------------------------------------------
    sizes = sorted(s for _, _, s in recs)
    beats = [(r, s) for _, r, s in recs if r.get("reason") == "candle"]
    closed = [(r, s) for r, s in beats if isinstance(r.get("session"), dict) and r["session"].get("phase") in CLOSED]  # (the session phase the record itself carries)
    total = sum(sizes)
    summary = {
        "file": str(p), "bytes": len(raw), "lines": sum(1 for x in lines if x.strip()), "records": len(recs), "unreadable_lines": len(bad), "versions": dict(versions),
        "first": recs[0][1].get("time") if recs else None, "last": recs[-1][1].get("time") if recs else None,
        "record_bytes": {"min": sizes[0], "median": sizes[len(sizes) // 2], "max": sizes[-1]} if sizes else None,
        "by_reason": {k: {"records": c, "bytes": size_by[k]} for k, c in reasons.items()},
        "heartbeats": {"records": len(beats), "bytes": sum(s for _, s in beats), "share_of_bytes": round(sum(s for _, s in beats) / total, 3) if total else 0.0,
                       "while_market_closed": len(closed), "closed_bytes": sum(s for _, s in closed)},
        "events": {"written": len(events), "distinct": len(identity), "setups": len(story), "setups_not_ended_in_this_file": len(open_setups)},
        "restarts": [{"line": n, "time": t, **({"restored": len(info.get("restored") or []), "ok": info.get("ok")} if info else {})} for n, t, info in restarts], "outages": [{"line": n, **o} for n, o in outages],
    }  # fmt: skip
    errors, warnings = sum(1 for x in problems if x["level"] == "error"), sum(1 for x in problems if x["level"] == "warn")
    return {"ok": errors == 0, "errors": errors, "warnings": warnings, "problems": problems, "summary": summary}


def format_report(rep: dict) -> list[str]:
    s = rep["summary"]
    out = [f"{s['file']}: {s['records']} record(s) in {s['lines']} line(s), {s['bytes']:,} bytes; versions {s['versions'] or 'none'}; {s['first']} to {s['last']}"]
    if not rep["problems"]:
        out.append("no problem found")
    for x in rep["problems"]:
        where = f" (line {', '.join(map(str, x['lines']))}{' ...' if x['count'] > len(x['lines']) else ''})" if x["lines"] else ""
        out.append(f"  {x['level'].upper():5s} {x['code']}: {x['message']}{where}")
    out.append("what it holds:")
    for k, v in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]["bytes"]):
        out.append(f"  {str(k):8s} {v['records']:6d} record(s) {v['bytes']:>12,} bytes")
    h = s["heartbeats"]
    out.append(f"  heartbeats (nothing changed, once per 5 minutes) are {h['share_of_bytes']:.0%} of the records' bytes; {h['while_market_closed']} of {h['records']} were written while the market was closed ({h['closed_bytes']:,} bytes)")
    if s["record_bytes"]:
        out.append(f"  a record is {s['record_bytes']['min']:,} to {s['record_bytes']['max']:,} bytes (median {s['record_bytes']['median']:,})")
    e = s["events"]
    out.append(f"  {e['written']} lifecycle event(s) ({e['distinct']} distinct) for {e['setups']} setup(s); {e['setups_not_ended_in_this_file']} not ended in this file")
    if s["restarts"]:
        out.append(f"  {len(s['restarts'])} start/restart record(s)")
    out.append(f"{rep['errors']} error(s), {rep['warnings']} warning(s)")
    return out
