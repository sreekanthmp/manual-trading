"""Decision journal: what the engine said, and why, so a session can be reviewed after it ends.

One JSON line is appended to logs/decisions-<date>.jsonl whenever the decision, a setup, the session phase, the data
state or the connection changes, whenever a lifecycle event happens, and once per completed 5M candle (so "nothing
changed" is on record too). Each line carries the full picture at that moment: the decision with its reasons for and
against, the market condition, every timeframe's trend, every strategy's state, every live setup (trigger, entry area,
invalidation, confirmation candle, blockers), the lifecycle events since the previous line (detected, confirmed,
invalidated, expired, with the reason) and the feed (connection state, reconnects, data gaps, history state, errors).

A restart is on record too: the first line of the new run says `restart`, and the setups it carried over from the
journal are marked `resumed` (see SetupBook.restore). A line cut short by a crash costs only that line: the reader
skips it and the next run starts on a fresh line.

Review a session:
    python -m app.journal logs/decisions-2026-11-06.jsonl                # what changed, in time order
    python -m app.journal logs/decisions-2026-11-06.jsonl --detail       # ...with the reasons for and against
    python -m app.journal logs/decisions-2026-11-06.jsonl --all          # ...including the once-per-candle lines
"""

import argparse
import json
import logging
import math
import os
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from .candles import IST

log = logging.getLogger(__name__)
SLOT = 300  # a heartbeat line per 5M candle
SCHEMA = 2  # the version of a record, in its `v` key. A record with no `v` is version 1 (everything written before Stage 6.2) and is read exactly as it always was.
# Version 2 only ADDS: `v`; `decision_setup` (the setup the decision is about, in full where it matters: trigger, entry zone, invalidation, targets, stage); `decision.also_setups` (the
# agreeing strategies as ids, beside the words in `decision.also`); and on every lifecycle event `code`, `superseded_by` and `final` (set on the events that end a setup: why, in a code;
# which structure replaced it; what it stood at). Nothing was renamed or removed, and what makes a line get written is unchanged. (Stage 6.3 adds, only when it applies, `outage` on the first
# record after the storage was unavailable; Stage 6.5 adds, on the first record of a run that read an earlier run's journal, `restart`: which setups it carried over, which had ended, how
# many events it carried and where its numbering goes on, or why it could not restore.)
SETUP_KEYS = ("id", "strategy", "strategy_name", "direction", "status", "stage", "actionable", "price_state", "level", "level_label", "trigger", "zone", "invalidation", "extreme",
              "detected_at", "status_since", "confirmed_at", "confirmed_bar_end", "resumed", "plan", "blockers", "note")  # fmt: skip


def decision_setup(out: dict) -> dict | None:
    """The setup the decision is about, picked out of the live setups by the decision's own setup id: None when the decision names none (or names one that is not live)."""
    sid = (out.get("decision") or {}).get("setup_id")
    s = next((x for x in out.get("setups") or [] if x.get("id") == sid), None) if sid else None
    return None if s is None else {k: s.get(k) for k in SETUP_KEYS}


def feed_signature(feed: dict | None) -> tuple | None:
    """The discrete facts about the connection. A change in any of them is worth a line; a tick age that moves is not."""
    if not feed:
        return None
    gap, last, history = feed.get("open_gap") or {}, feed.get("last_gap") or {}, feed.get("history") or {}
    return (feed.get("state"), feed.get("reconnects"), gap.get("start"), last.get("start"), last.get("end"), history.get("state"), history.get("error"),
            (feed.get("spot") or {}).get("stale"), tuple(feed.get("health_issues") or ()))  # a stale spot or a data-health problem starting or ending is worth a line


def signature(out: dict, feed: dict | None = None) -> tuple:
    """What a person reading the dashboard would notice changing."""
    d = out["decision"]
    return (
        d["action"], d["headline"], d["setup_id"], (out.get("session") or {}).get("phase"), (out.get("data") or {}).get("ok"),
        tuple((s["id"], s["status"], s["price_state"], s["actionable"], bool(s["blockers"])) for s in out.get("setups", [])),
        tuple((m["id"], m["state"], m["active"]) for m in out.get("strategies", [])), feed_signature(feed),
        tuple((c["strategy"], c["side"], c["needed"], c["granted"]) for c in out.get("counter_trend") or []),  # a counter-trend permission being granted, or denied, is worth a line
    )  # fmt: skip


def levels_brief(a: dict) -> dict:
    """The levels the decision was made against, compactly: the nearest supports and resistances ([price, touches]) and the previous day's."""
    lv, ref = a.get("levels") or {}, a.get("reference") or {}
    pd = ref.get("previous_day")
    return {"support": [[x["price"], x["touches"]] for x in lv.get("support", [])], "resistance": [[x["price"], x["touches"]] for x in lv.get("resistance", [])],
            "previous_day": None if not pd else {k: pd.get(k) for k in ("date", "pdh", "pdl", "pdc", "stale")},
            "session_open": ref.get("session_open"), "session_avg_price": ref.get("session_avg_price")}  # fmt: skip


def record(out: dict, a: dict, events: list, now: float, reason: str, feed: dict | None = None, options: dict | None = None) -> dict:
    tfs = a.get("timeframes", {})
    sp = (feed or {}).get("spot") or {}
    keep = ("trend", "adx", "rsi14", "ema20", "ema50", "atr14_intraday", "atr14", "extension")
    return {
        "v": SCHEMA, "time": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"), "t": now, "reason": reason,
        "price": out.get("price"), "spot": {"price": out.get("price"), "age": sp.get("age"), "exchange_time": sp.get("exchange_time"), "received_time": sp.get("received_time")},
        "decision": out["decision"], "decision_setup": decision_setup(out), "market": out.get("market"), "session": out.get("session"), "levels": levels_brief(a),
        "counter_trend": out.get("counter_trend") or [],  # every permission decision of a counter-trend strategy at this moment, granted or denied, with its checks and structure
        **({"options": options} if options is not None else {}),
        "data": out.get("data"), "feed": feed, "strategies": out.get("strategies"), "setups": out.get("setups"), "events": events,
        "timeframes": {tf: {**{k: t.get(k) for k in keep}, "last_candle_end": (t.get("last_completed") or {}).get("end")} for tf, t in tfs.items()},
        "analysis": {"reasons": (a.get("condition") or {}).get("reasons"), "warnings": a.get("warnings")},
    }  # fmt: skip


def read(path) -> tuple[list[dict], list[int]]:
    """Every readable record of a journal, and the numbers of the lines that were not (a record cut short by a crash)."""
    records, bad = [], []
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
            if not isinstance(r, dict) or not isinstance(r.get("decision"), dict):
                raise ValueError("not a journal record")
            records.append(r)
        except ValueError:  # json.JSONDecodeError is one
            bad.append(n)
    return records, bad


def _finite(x):
    """A copy of `x` with every NaN and infinity turned into None: JSON has neither, and one stray number must not cost a whole record."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x


class Journal:
    """Writes the records and keeps them safe until they are on disk.

    A record is on disk when `_append` returns: it is flushed to the operating system, and synced to the disk (fsync) for every record but the once-per-candle heartbeat. When the storage cannot
    be written (a full disk, a locked file, a folder that is gone) the record is NOT lost: it is made as usual (the state machine moves on, the events it carries are not repeated) and kept in
    memory, at most `max_pending` of them, and written, in order and with its own time, as soon as the storage works again; the first record after that says how long the outage was and what it
    cost (`outage`). When the queue is full a heartbeat is dropped first, then the oldest record, and the loss is counted. It protects against a storage that comes back, not against the process
    ending during the outage."""

    def __init__(self, directory="logs", max_pending: int = 1000, sync: bool = True):
        self.dir = Path(directory)
        self.max_pending, self.sync = max_pending, sync
        self._sig = self._slot = None
        self._seq = -1  # the highest lifecycle event already written (or waiting to be)
        self._failed = False  # the last write could not be made
        self._logged = False  # the problem has been said in the log (once per outage)
        self._restarted = False  # the engine picked up an earlier run of today: the first line says so
        self._restart_info = None  # ...and says what it picked up (see resumed)
        self._checked: set = set()  # files whose last line has been looked at (marked only after a write that worked)
        self._pending: deque = deque()  # (path, line, heartbeat): made, not on disk yet, oldest first
        self._outage = None  # {"since", "failed", "queued"} while writes fail
        self._lost = 0  # records dropped from a full queue during this outage
        self._recovered = None  # what the next record says about the outage that has just ended
        self.last_ok = self.last_error = None
        self.unreadable: list = []  # the numbers of the lines the last load_day could not read
        self.written = self.sync_errors = 0
        self._warned_sync = self._warned_nan = False

    def path(self, now: float) -> Path:
        return self.dir / f"decisions-{datetime.fromtimestamp(now, IST).date().isoformat()}.jsonl"

    def load_day(self, now: float) -> list[dict]:
        """The records of today so far, for a restart and for the API. Lines that cannot be read are skipped; their numbers are kept in `unreadable` (of this read) for whoever wants to say so."""
        p = self.path(now)
        records, self.unreadable = read(p) if p.exists() else ([], [])
        return records

    def next_seq(self, now: float) -> int:
        """The number after the highest event already in today's file (0 when there is none): a run that could not restore its setups still numbers its events after those on record."""
        seqs = [e["seq"] for r in self.load_day(now) for e in (r.get("events") or []) if isinstance(e, dict) and isinstance(e.get("seq"), int)]
        return max(seqs) + 1 if seqs else 0

    def resumed(self, next_seq: int, info: dict | None = None) -> None:
        """The engine picked up an earlier run of today. Its events up to `next_seq - 1` are already in the file. `info` (what was restored, or why it could not be) goes on the first record."""
        self._seq, self._restarted, self._restart_info = next_seq - 1, True, info

    def status(self, now: float | None = None) -> dict:
        """Where the journal is and whether it is being written: for the health page. `ok` is False while any record is waiting for the storage."""
        return {"configured": True, "directory": str(self.dir.resolve()), "file": None if now is None else self.path(now).name, "ok": not self._failed and not self._pending,
                "pending": len(self._pending), "lost": self._lost, "written": self.written, "last_ok": self.last_ok, "last_error": self.last_error,
                "since": (self._outage or {}).get("since"), "fsync": self.sync, "fsync_errors": self.sync_errors}  # fmt: skip

    def observe(self, out: dict, a: dict, events: list, now: float, feed: dict | None = None, options: dict | None = None) -> None:
        """Called after every evaluation (once a second); writes only when there is something new to say.
        `events` are the lifecycle events, newest first. `options`: the contract Phase 4 selected for a BUY decision (a change of contract is
        written). Never raises: the journal must not disturb trading."""
        try:
            self._drain(now)  # records still waiting from a failed write go first, in order
        except Exception:
            log.exception("the decision journal's waiting records could not be written")
        try:
            sig, slot = signature(out, feed) + ((options or {}).get("symbol"),), int(now // SLOT)
            new = [e for e in reversed(events) if e.get("seq", -1) > self._seq]
            if sig == self._sig and slot == self._slot and not new:
                return
            first = self._sig is None
            reason = ("restart" if self._restarted else "start") if first else "changed" if sig != self._sig or new else "candle"
            rec = record(out, a, new, now, reason, feed, options)
            if self._recovered is not None:
                rec["outage"] = self._recovered  # storage was unavailable: from when to when, how many records were written late, how many were lost
            if first and self._restart_info is not None:
                rec["restart"] = self._restart_info  # what this run took over from the earlier one (the record itself is written while the history is still loading: it has no setups of its own)
            self._emit(rec, now, reason)  # (a storage problem queues the record: it does not raise)
            self._recovered = self._restart_info = None
            if reason != "candle" and (first or sig[:2] != self._sig[:2]):
                log.info("decision: %s", out["decision"]["headline"])
            self._sig, self._slot = sig, slot
            if new:
                self._seq = max(e["seq"] for e in new)
        except Exception as e:  # a bug, not a full disk: say it once, not every second
            if not self._logged:
                log.exception("the decision journal could not be written")
                self._logged = True
            self._failed, self.last_error = True, f"{type(e).__name__}: {e}"

    # -- getting a record to the disk -----------------------------------------------------------------------------------------------------
    def _dump(self, rec: dict) -> str:
        try:
            return json.dumps(rec, allow_nan=False, separators=(",", ":"), default=str) + "\n"
        except ValueError:  # a NaN or an infinity somewhere in the record
            if not self._warned_nan:
                log.warning("a number in a journal record was not finite (NaN or infinity): it is written as null")
                self._warned_nan = True
            return json.dumps(_finite(rec), allow_nan=False, separators=(",", ":"), default=str) + "\n"

    def _emit(self, rec: dict, now: float, reason: str) -> None:
        line, path, heartbeat = self._dump(rec), self.path(now), reason == "candle"
        if self._pending:  # older records are waiting: this one goes behind them (the next evaluation tries them all)
            self._queue(path, line, heartbeat)
            return
        try:
            self._append(path, line, sync=not heartbeat)
        except OSError as e:
            self._fail(e, now)
            self._queue(path, line, heartbeat)
        else:
            self._ok(now)

    def _drain(self, now: float) -> None:
        while self._pending:
            path, line, heartbeat = self._pending[0]
            try:
                self._append(path, line, sync=not heartbeat)
            except OSError as e:
                self._fail(e, now)
                return
            self._pending.popleft()
            self._ok(now)
        if self._outage is not None:  # everything that was waiting is on disk: the outage is over
            o = self._outage
            self._recovered = {"from": o["since"], "to": now, "failed_writes": o["failed"], "written_late": o["queued"], "lost": self._lost}
            log.info("the decision journal is being written again: %d record(s) were held back and written late, %d lost", o["queued"] - self._lost, self._lost)
            self._outage, self._lost = None, 0

    def _queue(self, path: Path, line: str, heartbeat: bool) -> None:
        if len(self._pending) >= self.max_pending:
            del self._pending[next((i for i, item in enumerate(self._pending) if item[2]), 0)]  # a heartbeat goes first, else the oldest
            self._lost += 1
        self._pending.append((path, line, heartbeat))
        if self._outage is not None:
            self._outage["queued"] += 1

    def _fail(self, e: OSError, now: float) -> None:
        self._failed, self.last_error = True, f"{type(e).__name__}: {e}"
        self._checked.clear()  # a write that fails may leave a cut line: every file's last byte is looked at again before the next write to it
        if self._outage is None:
            self._outage = {"since": now, "failed": 0, "queued": 0}
        self._outage["failed"] += 1
        if not self._logged:
            self._logged = True
            log.error("the decision journal could not be written (%s): records are kept in memory (up to %d) and written when the storage is back", self.last_error, self.max_pending)

    def _ok(self, now: float) -> None:
        self.written += 1
        self.last_ok, self._failed, self._logged = now, False, False

    def _append(self, path: Path, line: str, sync: bool = True) -> None:
        """Put one line at the end of the file. Raises OSError when the storage cannot be written. A write that fails halfway leaves a cut line: the file is looked at again before the next attempt, which starts on a fresh line."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if path not in self._checked and path.exists() and path.stat().st_size:
            with path.open("rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    line = "\n" + line  # the file ends in a record that was cut short: this one starts on a fresh line
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            if sync and self.sync:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as e:  # the line is written; only its reaching the disk is in doubt: not a reason to write it twice
                    self.sync_errors += 1
                    if not self._warned_sync:
                        self._warned_sync = True
                        log.warning("the decision journal could not be synced to disk (%s): the line is written, but a power cut could lose it", e)
        self._checked.add(path)


# -- reading a journal back ---------------------------------------------------------------------------------------------------------
def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _n(x) -> str:
    x = _num(x)
    return "–" if x is None else f"{x:,.2f}"


def _hm(ts) -> str:
    ts = _num(ts)
    if ts is None:
        return "–"
    try:
        return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "–"


def feed_text(f: dict) -> str:
    """The connection in one line."""
    parts = [f"{f.get('state')}" + (f" since {_hm(f['since'])}" if f.get("since") else ""), f"reconnects {f.get('reconnects')}"]
    gap, last, history = f.get("open_gap"), f.get("last_gap"), f.get("history") or {}
    if gap:
        parts.append(f"NO TICKS since {_hm(gap.get('start'))} ({gap.get('reason')})")
    elif last:
        parts.append(f"last gap {_hm(last.get('start'))} to {_hm(last.get('end'))} ({last.get('reason') or last.get('ended_by')})")
    if history.get("state") not in (None, "ready") or history.get("error"):
        parts.append(f"history {history.get('state')}" + (f": {history['error']}" if history.get("error") else ""))
    sp = f.get("spot") or {}
    if sp.get("stale"):
        parts.append("NIFTY SPOT STALE" + (f" (last tick {sp['age']:.0f} s ago)" if _num(sp.get("age")) is not None else " (no tick yet)"))
    if f.get("health_issues"):
        parts.append("DATA NOT TRUSTED: " + "; ".join(map(str, _list(f["health_issues"]))))
    if f.get("reason") and f.get("state") != "connected":
        parts.append(f"why: {f['reason']}" + (f" (attempt {f['attempt']})" if f.get("attempt") else ""))
    if f.get("errors"):
        parts.append(f"last error: {(_list(f['errors']) or [{}])[-1].get('message')}")
    return " · ".join(parts)


def _record_lines(r: dict, detail: bool, shown_feed):
    """One record as text (the lines, and the feed signature last shown). Every field is looked up, not assumed: a record that lacks one (an older or an odd one) shows a dash for it."""
    d = r["decision"]
    m = r["market"] if isinstance(r.get("market"), dict) else {}
    s = r["session"] if isinstance(r.get("session"), dict) else {}
    ctx = [f"price {_n(r.get('price'))}"] + ([f"{str(m.get('regime') or '–').replace('_', ' ')}, volatility {m.get('volatility') or '–'}"] if m else []) + ([str(s["phase"])] if s.get("phase") else [])
    out = [f"{_hm(r.get('t'))}  {d.get('headline') or d.get('action') or '–'}   [{'; '.join(ctx)}]"]
    if r.get("reason") == "restart":
        out.append("      the app was restarted: setups carried over from the earlier run are marked (resumed) and are not announced again")
        rs = r.get("restart")
        if isinstance(rs, dict) and rs.get("ok") is False:
            out.append(f"      the day's setups could NOT be restored ({rs.get('error') or '–'}): this run's events are numbered from {rs.get('next_event', '–')}")
        elif isinstance(rs, dict):
            ids = [str(x) for x in _list(rs.get("restored"))]
            out.append(f"      carried over {len(ids)} live setup(s){': ' + ', '.join(ids) if ids else ''}; {len(_list(rs.get('ended')))} that ended earlier today stay ended; "
                       f"{rs.get('events_carried', '–')} event(s) carried, the next is number {rs.get('next_event', '–')}")
    gap = m.get("gap")
    if isinstance(gap, dict) and _num(gap.get("points")) is not None and _num(gap.get("percent")) is not None:
        out.append(f"      opened {gap['points']:+,.0f} pts ({gap['percent']:+.2f}%) from the previous close")
    if isinstance(r.get("feed"), dict) and r["feed"] and feed_signature(r["feed"]) != shown_feed:
        out.append(f"      feed: {feed_text(r['feed'])}")
        shown_feed = feed_signature(r["feed"])
    for x in _list(r.get("setups")):
        if not isinstance(x, dict):
            continue
        when = f"detected {_hm(x.get('detected_at'))}" + (f", confirmed {_hm(x.get('confirmed_at'))}" if x.get("confirmed_at") else "")
        live = " ACTIONABLE" if x.get("actionable") else (f" ({str(x['price_state']).replace('_', ' ')})" if x.get("price_state") else "")
        lo, hi = (list(x["zone"]) + [None, None])[:2] if isinstance(x.get("zone"), (list, tuple)) else (None, None)
        out.append(f"      {x.get('direction') or '–'} {x.get('strategy_name') or '–'}: {x.get('status') or '–'}{live}{' (resumed)' if x.get('resumed') else ''}  [{when}]  trigger {_n(x.get('trigger'))}, "
                   f"entry {_n(lo)}-{_n(hi)}, invalidation {_n(x.get('invalidation'))}")  # fmt: skip
        if detail:
            out += [f"          + {t}" for t in _list(x.get("reasons"))] + [f"          ⏸ {t}" for t in _list(x.get("blockers"))] + [f"          - {t}" for t in _list(x.get("against"))]
            out += [f"          ✓ {q.get('name')}: {q.get('text')}" for q in _list(x.get("quality")) if isinstance(q, dict)]
    for c in _list(r.get("counter_trend")):  # (older records have none)
        if not isinstance(c, dict):
            continue
        out.append(f"      counter-trend {c.get('side') or '–'} {c.get('strategy_name') or '–'}: permission {'granted' if c.get('granted') else 'denied'}{'' if c.get('needed', True) else ' (not needed)'} — {c.get('reason') or '–'}")
        if detail:
            out += [f"          {'✓' if x.get('ok') else '✗' if x.get('ok') is False else '?'} {x.get('name')}: {x.get('text')}" for x in _list(c.get("checks")) if isinstance(x, dict)]
    rf = m.get("regime_facts") if isinstance(m.get("regime_facts"), dict) else {}
    if detail and rf.get("facts"):
        out.append("      regime facts: " + ("; ".join(map(str, _list(rf.get("active")))) or "none notable") + ("" if rf.get("available") else f" (not available: {rf.get('why')})"))
    events = [e for e in _list(r.get("events")) if isinstance(e, dict)]
    out += [f"      event: {_hm(e.get('time'))} {e.get('direction') or '–'} {e.get('strategy_name') or '–'} — {e.get('text') or '–'}" for e in events]
    if detail:
        for e in events:  # (version 2: why it ended, as a code, and what it stood at; older records have none of it)
            f = e.get("final")
            if e.get("code") and isinstance(f, dict):
                lo, hi = (list(f["zone"]) + [None, None])[:2] if isinstance(f.get("zone"), (list, tuple)) else (None, None)
                out.append(f"          ended [{e['code']}]{' by ' + str(e['superseded_by']) if e.get('superseded_by') else ''}: was {f.get('status_before')}, trigger {_n(f.get('trigger'))}, "
                           f"entry {_n(lo)}-{_n(hi)}, invalidation {_n(f.get('invalidation'))}"
                           + (f", lived {f['lifetime_seconds'] / 60:.0f} min" if _num(f.get("lifetime_seconds")) is not None else ""))
        out += [f"          also: {x.get('strategy_name')} ({x.get('setup_id')})" for x in _list(d.get("also_setups")) if isinstance(x, dict)]
        out += [f"          + {t}" for t in _list(d.get("reasons"))] + [f"          - {t}" for t in _list(d.get("against"))]
        out += [f"          strategy {x.get('name')}: {x.get('state')} — {x.get('note') or x.get('why') or ''}" for x in _list(r.get("strategies")) if isinstance(x, dict)]
    return out, shown_feed


def timeline(path, everything: bool = False, detail: bool = False) -> list[str]:
    """The journal as text, in time order. Heartbeat lines (nothing changed) are left out unless `everything`. A record that is odd or from an older version is shown as far as it goes (a dash
    for what it lacks), and one that cannot be shown at all costs one line saying so, never the review."""
    out, shown_feed = [], None
    records, bad = read(path)
    for n, r in enumerate(records, 1):
        if r.get("reason") == "candle" and not everything:
            continue
        try:
            lines, shown_feed = _record_lines(r, detail, shown_feed)
            out += lines
        except Exception as e:  # the review must go on
            out.append(f"(record {n} could not be shown: {type(e).__name__}: {e})")
    if bad:
        out.append(f"({len(bad)} line(s) could not be read and were skipped: line {', '.join(map(str, bad))}. A record cut short by a crash?)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Review (or, with --verify, check) a decision journal.")
    ap.add_argument("file")
    ap.add_argument("--all", action="store_true", help="include the once-per-candle lines")
    ap.add_argument("--detail", action="store_true", help="include reasons for and against")
    ap.add_argument("--verify", action="store_true", help="check the file, read-only, and say what is wrong with it and what is in it (exit status 1 on an error)")
    ap.add_argument("--json", action="store_true", help="with --verify: the report as JSON")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a console or file in a legacy encoding must not make the review crash on a tick mark
    try:
        if args.verify:
            from .journal_check import format_report, verify

            report = verify(args.file)
            print(json.dumps(report, indent=1, allow_nan=False, default=str) if args.json else "\n".join(format_report(report)))
            return 0 if report["ok"] else 1
        print("\n".join(timeline(args.file, args.all, args.detail)))
    except OSError as e:
        print(f"{args.file}: cannot be read ({e.strerror or e})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
