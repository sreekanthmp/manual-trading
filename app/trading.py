"""Phase 3 glue. Turns the Phase 2 analysis into a strategy context, keeps the lifecycle of every setup
(detected -> waiting -> confirmed, or invalidated / expired) and makes the overall decision.

Decision vocabulary
    BUY CE / BUY PE  a setup has been confirmed by a completed 5M candle and the price is still in its entry area
    WAIT             the market is tradable, but there is nothing to act on yet (or setups disagree)
    NO TRADE         stay out: the market is closed, the data is unreliable, volatility is extreme, the
                     timeframes conflict, or no strategy suits the market

Lifecycle states: DETECTED, WAITING_FOR_CONFIRMATION, CONFIRMED, INVALIDATED, EXPIRED. A setup only ever
moves forward; the same setup is never announced twice."""

import logging
from collections import deque
from datetime import datetime

from . import analysis as an
from .candles import IST, day_start
from .history import ist_date
from . import regime as rg
from .registry import COUNTER_TREND, PRIORITY  # PRIORITY: tie-break only, a documented convention, not a ranking of merit. It lives in the registry, so a strategy can never be scanned and not ranked
from .strategy import (
    CONFIRMED, DETECTED, EXPIRED, INVALIDATED, NAMES, OPTION, RANK, WAITING, Context, Level, Params, Scan, Setup, clock_seconds, hm, num, scan_all,
)

log = logging.getLogger(__name__)
LIVE = (DETECTED, WAITING, CONFIRMED)
SESSION_NOTES = {
    "pre_open": "before the market opens (09:15)",
    "opening": "opening range forming: no entries before {observe_until}",
    "morning": "",
    "midday": "midday low-activity window: stricter confirmation",
    "afternoon": "",
    "late": "late session: no new entries after {last_entry}",
    "closed": "the market is closed",
}


# -- session ------------------------------------------------------------------------------------------------------
def session_info(now: float, p: Params) -> dict:
    dt = datetime.fromtimestamp(now, IST)
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second
    t = {k: clock_seconds(getattr(p, k)) for k in ("open_time", "observe_until", "midday_start", "midday_end", "last_entry", "close_time")}
    # the end of the session wins over everything else, so any ordering of the configured times behaves sensibly
    if dt.weekday() >= 5 or secs >= t["close_time"]:
        phase = "closed"
    elif secs < t["open_time"]:
        phase = "pre_open"
    elif secs >= t["last_entry"]:
        phase = "late"
    elif secs < t["observe_until"]:
        phase = "opening"
    elif t["midday_start"] <= secs < t["midday_end"]:
        phase = "midday"
    elif secs < t["midday_start"]:
        phase = "morning"
    else:
        phase = "afternoon"
    return {"phase": phase, "entries_allowed": phase in ("morning", "midday", "afternoon"), "strict": phase == "midday",
            "note": SESSION_NOTES[phase].format(**{k: getattr(p, k) for k in ("observe_until", "last_entry")})}  # fmt: skip


def opening_range(c5: list, now: float, p: Params) -> dict | None:
    start = day_start(now) + clock_seconds(p.open_time)
    bars = [c for c in c5 if start <= c.start < start + 900]
    if not bars:
        return None
    return {"high": max(c.high for c in bars), "low": min(c.low for c in bars), "complete": len(bars) == 3 and now >= start + 900 + 10}


# -- the strategy context ---------------------------------------------------------------------------------------------
def data_issues(a: dict, now: float, p: Params) -> list:
    """Reasons the candle data cannot be trusted for a confirmation right now (today's session only)."""
    out = []
    if a.get("history", {}).get("stale"):
        out.append("historical data is stale: today's refresh has not completed")
    tfs = a["timeframes"]
    for tf, bars in (("5m", p.completeness_bars_5m), ("15m", p.completeness_bars_15m)):
        start = max(day_start(now) + clock_seconds(p.open_time), now - bars * an.SECS[tf])
        for run in tfs[tf]["missing"]["runs"]:
            if run["end"] > start and run["start"] < now:
                out.append(f"{an.LABEL[tf]} candles missing {hm(run['start'])}-{hm(run['end'])} (inside the last {bars} candles)")
    for tf in ("1h", "15m", "5m"):
        if not tfs[tf]["warm"]:
            out.append(f"{an.LABEL[tf]}: not enough completed candles to classify yet")
    return out


def build_context(a: dict, series: dict, now: float, p: Params, health: list | None = None) -> tuple[Context, dict]:
    """The strategy context in real prices, plus the session / data facts the decision needs. `health`: reasons the live feed or the
    clock cannot be trusted right now (MarketData.health_issues); they hold every setup back like any other data problem."""
    tfs, sess = a["timeframes"], session_info(now, p)

    def st(tf):
        return tfs[tf].get("structure") or {}

    c = Context(now=now, price=a.get("price"), phase=sess["phase"])
    c.trend = {tf: tfs[tf]["trend"] for tf in tfs}
    c.adx = {tf: tfs[tf].get("adx") for tf in tfs}
    c.atr = {tf: tfs[tf].get("atr14_intraday") for tf in tfs}  # the size of one candle: the overnight gap is left out
    c.rsi = {tf: tfs[tf].get("rsi14") for tf in tfs}
    c.ema = {tf: (tfs[tf].get("ema20"), tfs[tf].get("ema50")) for tf in tfs}
    c.pattern = {tf: st(tf).get("pattern", "insufficient") for tf in tfs}
    c.swings = {tf: [(s["kind"], s["start"], s["price"]) for s in st(tf).get("recent_swings", [])] for tf in tfs}
    c.consolidation = {tf: (tfs[tf]["consolidation"]["low"], tfs[tf]["consolidation"]["high"]) if tfs[tf].get("consolidation") else None for tf in tfs}
    c.breakout = {tf: tfs[tf].get("breakout") for tf in tfs}
    cond = a["condition"]
    c.volatility, c.alignment = cond["volatility"], cond["alignment"]["label"]
    c.vol_ratio = tfs["15m"].get("atr_ratio_intraday")
    c.atr_ratio = {tf: tfs[tf].get("atr_ratio_intraday") for tf in tfs}
    c.ema_slope = {tf: {"ema20": tfs[tf].get("ema20_slope"), "ema50": tfs[tf].get("ema50_slope"), "stack_bars": tfs[tf].get("ema_stack_bars")} for tf in tfs}
    c.c5, c.c15 = series["5m"][-120:], series["15m"][-60:]
    levels = [Level(lv["price"], f"swing level x{lv['touches']}", lv["touches"]) for lv in a.get("level_clusters", [])]
    pd = a["reference"].get("previous_day")
    if pd:
        levels += [Level(pd["pdh"], "previous day high"), Level(pd["pdl"], "previous day low")]
    if c.consolidation["15m"]:
        lo, hi = c.consolidation["15m"]
        levels += [Level(hi, "15M range high"), Level(lo, "15M range low")]
    orng = opening_range(c.c5, now, p)
    c.opening_range = orng
    if orng and orng["complete"] and p.opening_range_levels:
        levels += [Level(orng["high"], "opening range high"), Level(orng["low"], "opening range low")]
    c.levels = levels

    issues = data_issues(a, now, p) + list(health or [])
    vol_extreme = c.vol_ratio is not None and c.vol_ratio >= p.vol_extreme
    c.blockers = list(issues)
    if not sess["entries_allowed"]:
        c.blockers.append(sess["note"])
    if vol_extreme:
        c.blockers.append(f"volatility is unusually high (15M ATR {c.vol_ratio:.1f}x its average)")
    c.can_confirm = not c.blockers
    why = []
    if c.volatility == "high":
        why.append("volatility is high")
    if sess["strict"]:
        why.append("midday low-activity window")
    c.strict, c.strict_why = bool(why), " and ".join(why)
    c.confirm_after = day_start(now) + clock_seconds(p.observe_until)  # candles made of opening-range trading cannot confirm
    c.regime = rg.regime_facts(a, series, p, data_ok=not issues, data_why="; ".join(issues[:2]))  # facts beside the classification, never instead of it; unknown, not guessed, when the data is not trusted
    env = {"session": sess, "data": {"ok": not issues, "issues": issues}, "vol_extreme": vol_extreme, "vol_ratio": c.vol_ratio,
           "opening_range": orng, "gap": opening_gap(a, now), "regime": c.regime}  # fmt: skip
    return c, env


def opening_gap(a: dict, now: float) -> dict | None:
    """How far today's session opened from the previous close. A fact for the trader, not a volatility estimate."""
    ref = a.get("reference") or {}
    pd = ref.get("previous_day")
    if not pd or pd.get("stale") or ref.get("session_open") is None or ref.get("session_date") != ist_date(now).isoformat():
        return None
    points = ref["session_open"] - pd["pdc"]
    return {"points": points, "percent": 100 * points / pd["pdc"], "open": ref["session_open"], "previous_close": pd["pdc"]}


# -- the lifecycle ---------------------------------------------------------------------------------------------------------
def confirmed_note(s: Setup) -> str:
    """When the confirming candle completed, so a confirmation that was held back for a while can be told apart from a fresh one."""
    return f" (confirming candle completed {hm(s.confirmed_bar_end)})" if s.status == CONFIRMED and s.confirmed_bar_end else ""


MISSED_NOTES = ("opportunity has passed", "entry window after the confirmation has passed")


def end_code(status: str, note: str) -> str:
    """Why a setup ended, as a code, when a strategy's own scan ended it (the other ways of ending have a code of their own, given where they happen). The note stays the words."""
    if status == INVALIDATED:
        return "invalidated"
    return "missed" if any(m in note for m in MISSED_NOTES) or note.startswith("missed") else "expired"  # the same test as the stage a trader sees (setup_stage)


class SetupBook:
    """Remembers each live setup so that it is announced once, only moves forward, and ends as
    INVALIDATED (its premise failed) or EXPIRED (its opportunity passed)."""

    def __init__(self, p: Params):
        self.p = p
        self.day = None
        self.active: dict = {}  # (strategy, side) -> Setup, at most one per strategy and side
        self.terminal: set = set()  # keys that have ended: never revived
        self.recent: deque = deque(maxlen=20)
        self.events: deque = deque(maxlen=60)
        self.seq = 0  # numbers the events so the journal can tell which ones it has written

    def _event(self, now: float, s: Setup, text: str, code: str | None = None, superseded_by: str | None = None, final: dict | None = None) -> None:
        """One lifecycle event. `code`, `superseded_by` and `final` are for the events that END a setup (None on the others): why it ended as a code (the words stay in `text`), the id of the
        structure that replaced it, and what it stood at when it ended."""
        self.events.appendleft({"seq": self.seq, "time": now, "id": s.id, "strategy": s.strategy, "strategy_name": NAMES[s.strategy],
                                "direction": s.direction, "status": s.status, "text": text, "code": code, "superseded_by": superseded_by, "final": final})  # fmt: skip
        self.seq += 1
        log.info("setup %s %s %s", s.direction, NAMES[s.strategy], text)

    def _finish(self, s: Setup, status: str, note: str, now: float, code: str | None = None, superseded_by: str | None = None) -> None:
        before, before_since = s.status, s.status_since
        s.status, s.note, s.status_since = status, note, now
        self.active.pop((s.strategy, s.side), None)
        self.terminal.add(s.key)
        self.recent.appendleft(s)
        final = {"status_before": before, "status_before_since": before_since, "detected_at": s.detected_at, "confirmed_at": s.confirmed_at,
                 "lifetime_seconds": now - s.detected_at if s.detected_at else None, "level": s.level, "level_label": s.level_label, "trigger": s.trigger,
                 "zone": list(s.zone) if s.zone else None, "invalidation": s.invalidation, "extreme": s.extreme, "confirmed_bar_end": s.confirmed_bar_end,
                 "resumed": s.resumed, "plan": s.plan}  # fmt: skip
        self._event(now, s, f"{status}: {note}", code or end_code(status, note), superseded_by, final)

    def update(self, ctx: Context, env: dict, now: float) -> dict:
        today, phase = ist_date(now), env["session"]["phase"]
        if self.day != today:
            for s in list(self.active.values()):
                self._finish(s, EXPIRED, "the session ended", now, "session_ended")
            self.terminal.clear()
            self.day = today
        scans = {}
        if phase not in ("closed", "pre_open"):
            scans = scan_all(ctx, self.p, dict(self.active))
            for slot, scan in scans.items():
                covered = self._apply(slot, scan, now, phase)
                if covered is not None:
                    scans[slot] = covered  # the monitor then says why this strategy shows nothing (assigning to an existing key while iterating is fine)
        for slot, s in list(self.active.items()):
            if phase in ("closed", "pre_open"):
                self._finish(s, EXPIRED, "the market is closed", now, "market_closed")
            elif phase == "late" and s.status != CONFIRMED:
                self._finish(s, EXPIRED, f"late session: no new entries after {self.p.last_entry}", now, "late_session")
            elif s.status != CONFIRMED and now - s.detected_at > self.p.detected_max_minutes * 60:
                self._finish(s, EXPIRED, f"no confirmation within {self.p.detected_max_minutes} minutes", now, "unconfirmed_timeout")
        return {"setups": list(self.active.values()), "recent": list(self.recent), "events": list(self.events), "scans": scans}

    def _apply(self, slot, scan, now: float, phase: str) -> Scan | None:
        """Advance one strategy's setup. Returns a replacement Scan only when a new setup is held back because another strategy already has a live setup for
        the same market movement (see Setup.group): one move is announced once, by whichever strategy saw it first."""
        prior = self.active.get(slot)
        if not scan.applicable:
            if prior:
                self._finish(prior, INVALIDATED, scan.note or "the market no longer suits this strategy", now, "no_longer_suits")
            return
        s = scan.setup
        if s is None:
            if prior:
                self._finish(prior, EXPIRED, f"no longer present: {scan.note}" if scan.note else "the conditions are no longer present", now, "no_longer_present")
            return
        if s.key in self.terminal:
            return
        if prior and prior.key != s.key:
            self._finish(prior, EXPIRED, "superseded by a newer structure", now, "superseded", s.key)
            prior = None
        if s.status in (INVALIDATED, EXPIRED):
            if prior:
                self._finish(prior, s.status, s.note, now)
            else:
                self.terminal.add(s.key)  # it was never announced; do not announce it now
            return
        if prior is None:
            if phase == "late":
                return
            twin = None if s.group is None else next((o for o in self.active.values() if o.group == s.group and o.key != s.key), None)
            if twin is not None:
                return Scan(True, f"the same move is already covered by {NAMES[twin.strategy]} ({twin.status.lower().replace('_', ' ')})", None)
            s.detected_at = s.status_since = now
            s.confirmed_at = now if s.status == CONFIRMED else None
            self.active[slot] = s
            self._event(now, s, f"{s.status}: {s.level_label or 'setup'} {num(s.level) if s.level is not None else ''}".strip() + confirmed_note(s))
            return
        self._merge(prior, s, now)

    def restore(self, records: list, now: float) -> int:
        """Pick today up where the journal left it, so a restart announces nothing twice. `records` are the journal's
        records of today, oldest first. Setups that had ended stay ended (their keys are never revived). The setups
        that were live in the last evaluated record carry on with their original detection and confirmation times,
        are marked `resumed`, and merge with what the candles say now exactly as if the process had never stopped:
        nothing is announced again, and what changed in the meantime ends them in the usual way. The event list and
        its numbering continue. Only a book that has not started yet can be restored; returns the live setups
        picked up. Entries that cannot be read are skipped, never fatal."""
        if not records or self.active or self.events or self.terminal:
            return 0
        events = [e for r in records for e in (r.get("events") or []) if isinstance(e, dict) and "id" in e and "status" in e]
        last = {e["id"]: e["status"] for e in events}  # where each setup's story stood at its last announcement
        ended = {key for key, status in last.items() if status in (INVALIDATED, EXPIRED)}
        evaluated = next((r for r in reversed(records) if r.get("market") is not None), None)  # a record that carries real setups
        live = {}
        for x in (evaluated or {}).get("setups") or []:
            try:
                s = setup_from_dict(x)
            except (KeyError, TypeError, ValueError, IndexError):
                log.warning("a setup in the journal could not be restored: %.120s", x)
                continue
            if s.key not in ended:
                live[(s.strategy, s.side)] = s
        self.terminal |= ended
        self.active.update(live)
        for e in events[-self.events.maxlen:]:  # oldest first, so the newest ends up in front
            self.events.appendleft(e)
        numbered = [e["seq"] for e in events if isinstance(e.get("seq"), int)]
        self.seq = max(numbered) + 1 if numbered else 0
        self.day = ist_date(now)
        return len(live)

    def _merge(self, prior: Setup, new: Setup, now: float) -> None:
        advanced = RANK[new.status] > RANK[prior.status]
        if prior.status == CONFIRMED:  # frozen once confirmed: only the conditions around it can change
            prior.blockers, prior.against = new.blockers, new.against
            return
        for f in ("level", "level_label", "trigger", "zone", "invalidation", "extreme", "confirm_text", "reasons", "against", "blockers", "quality",
                  "support", "resistance", "confirmed_bar_end", "confirm_candle", "break_candle", "candle_confirmed", "plan", "counter_trend"):  # fmt: skip
            setattr(prior, f, getattr(new, f))
        if advanced:
            prior.status, prior.status_since = new.status, now
            if new.status == CONFIRMED:
                prior.confirmed_at = now
            self._event(now, prior, f"{new.status}" + (f": trigger {num(new.trigger)}" if new.trigger is not None else "") + confirmed_note(prior))


# -- presenting a setup, and the decision ------------------------------------------------------------------------------------
def price_state(s: Setup, price: float | None) -> str | None:
    if s.status != CONFIRMED or price is None or s.zone is None or s.invalidation is None:
        return None
    lo, hi = s.zone
    if s.side == "bull":
        return "through_invalidation" if price <= s.invalidation else "extended" if price > hi else "in_entry_area" if price >= lo else "below_trigger"
    return "through_invalidation" if price >= s.invalidation else "extended" if price < lo else "in_entry_area" if price <= hi else "above_trigger"


def why_not(s: dict, price) -> list:
    """Why a confirmed setup is not actionable right now."""
    out = list(s["blockers"])
    ps = s["price_state"]
    if ps == "extended":
        out.append(f"price {num(price)} is already beyond the entry area ({num(s['zone'][0])}-{num(s['zone'][1])}): do not chase")
    elif ps in ("below_trigger", "above_trigger"):
        out.append(f"price {num(price)} has moved back through the trigger {num(s['trigger'])}; the setup is valid while price holds {num(s['invalidation'])}, but it is not in the entry area")
    elif ps == "through_invalidation":
        out.append(f"price {num(price)} is already through the invalidation level {num(s['invalidation'])}")
    return out


def setup_stage(s: Setup, actionable: bool, entries_allowed: bool, tick_ok: bool) -> tuple[str, str]:
    """What a trader should call this setup right now, derived from the lifecycle state (which is unchanged) and what holds it back.
    DETECTED, WAITING_FOR_CONFIRMATION, CONFIRMED, INVALIDATED and EXPIRED are the lifecycle states. BLOCKED (the confirming candle has
    completed but something else holds entry back), READY_TO_ACT (confirmed, in the entry area, nothing blocking) and MISSED (it was
    confirmed, and price ran away before an entry) are the same states told more precisely. There is no separate DEVELOPING: a setup
    that is still forming is DETECTED or WAITING_FOR_CONFIRMATION."""
    if s.status == INVALIDATED:
        return "INVALIDATED", s.note
    if s.status == EXPIRED:
        missed = any(m in s.note for m in MISSED_NOTES) or s.note.startswith("missed")
        return ("MISSED" if missed else "EXPIRED"), s.note
    if s.status == CONFIRMED:
        if actionable:
            return "READY_TO_ACT", "confirmed, price is in the entry area and nothing blocks entry"
        if s.blockers or not entries_allowed or not tick_ok:
            return "BLOCKED", "the setup is confirmed, but something blocks entry right now"
        return "CONFIRMED", "the setup is confirmed, but price is not in the entry area"
    if s.candle_confirmed:
        return "BLOCKED", "the confirming 5M candle has completed: confirmation is not what is missing, a blocker holds the setup back"
    if s.status == WAITING:
        return "WAITING_FOR_CONFIRMATION", "price is at the zone and buyers have shown up; a completed 5M candle must still close beyond the trigger"
    return "DETECTED", "price is at a meaningful zone, but there is no sign yet that buyers (or sellers) have stepped in"


def setup_dict(s: Setup, price, entries_allowed: bool, tick_ok: bool) -> dict:
    ps = price_state(s, price)
    d = {
        "id": s.id, "strategy": s.strategy, "strategy_name": NAMES[s.strategy], "direction": s.direction, "side": s.side,
        "status": s.status, "detected_at": s.detected_at, "status_since": s.status_since, "confirmed_at": s.confirmed_at,
        "level": s.level, "level_label": s.level_label, "trigger": s.trigger, "zone": list(s.zone) if s.zone else None,
        "invalidation": s.invalidation, "extreme": s.extreme, "confirmed_bar_end": s.confirmed_bar_end, "resumed": s.resumed,
        "confirm_candle": s.confirm_candle, "break_candle": s.break_candle,
        "confirm_text": s.confirm_text, "note": s.note, "reasons": s.reasons, "against": s.against,
        "blockers": s.blockers, "quality": s.quality, "support": s.support, "resistance": s.resistance, "price_state": ps,
    }  # fmt: skip
    d["actionable"] = s.status == CONFIRMED and ps == "in_entry_area" and not s.blockers and entries_allowed and tick_ok
    d["candle_confirmed"], d["plan"] = s.candle_confirmed, s.plan
    if s.group is not None:  # only the strategies that use it: the three originals' setups look exactly as they always did
        d["group"] = s.group
    if s.counter_trend is not None and s.strategy in COUNTER_TREND:  # likewise: only a counter-trend strategy's setup carries its permission record
        d["counter_trend"] = s.counter_trend
    d["stage"], d["stage_text"] = setup_stage(s, d["actionable"], entries_allowed, tick_ok)
    return d


def setup_from_dict(x: dict) -> Setup:
    """A live setup as the journal wrote it (`setup_dict`), back into a Setup. Raises if it is not a usable live setup."""
    strategy, side, key, status = x["strategy"], x["side"], x["id"], x["status"]
    if strategy not in NAMES or side not in OPTION or status not in LIVE or not key.startswith(f"{strategy}:{side}:"):
        raise ValueError(f"not a live setup: {key}")
    return Setup(
        strategy, side, key, status, float(key.rsplit(":", 1)[1]),  # the key ends with the anchor candle's start time
        level=x.get("level"), level_label=x.get("level_label") or "", trigger=x.get("trigger"), zone=tuple(x["zone"]) if x.get("zone") else None,
        invalidation=x.get("invalidation"), extreme=x.get("extreme"), confirm_text=x.get("confirm_text") or "", note=x.get("note") or "",
        reasons=list(x.get("reasons") or []), against=list(x.get("against") or []), blockers=list(x.get("blockers") or []),
        quality=list(x.get("quality") or []), support=list(x.get("support") or []), resistance=list(x.get("resistance") or []),
        confirmed_bar_end=x.get("confirmed_bar_end"), confirm_candle=x.get("confirm_candle"), break_candle=x.get("break_candle"),
        candle_confirmed=bool(x.get("candle_confirmed")), plan=x.get("plan") or {},
        detected_at=float(x["detected_at"]), status_since=float(x["status_since"]), confirmed_at=x.get("confirmed_at"), resumed=True, group=x.get("group"),
        counter_trend=x.get("counter_trend") if strategy in COUNTER_TREND else None,  # (an older journal has none; and a record on any other strategy's setup is dropped: it never had the exception)
    )  # fmt: skip


def price_change(a: dict) -> dict:
    """How far NIFTY is from the previous session's close, worked out here from the backend's own numbers: the dashboard only formats it. Unknown (never zero) when the price or
    the previous close is missing, or the previous close is stale (the history has not been refreshed for today's session: it may not be yesterday's)."""
    price, pd = a.get("price"), (a.get("reference") or {}).get("previous_day")
    if price is None:
        return {"available": False, "why": "no NIFTY price yet"}
    if not pd or not pd.get("pdc"):
        return {"available": False, "why": "the previous close is not available"}
    if pd.get("stale"):
        return {"available": False, "why": "the previous close is stale (the history has not been refreshed for today's session)"}
    points = price - pd["pdc"]
    return {"available": True, "points": points, "percent": 100 * points / pd["pdc"], "price": price, "previous_close": pd["pdc"], "previous_close_date": pd.get("date"),
            "basis": "the previous session's close"}  # fmt: skip


def market_summary(a: dict, env: dict) -> dict:
    cond, tfs = a["condition"], a["timeframes"]
    t1, t15, label = tfs["1h"]["trend"], tfs["15m"]["trend"], cond["alignment"]["label"]
    adx = tfs["1h"].get("adx")
    adx_t = f" (1H ADX {adx:.1f})" if adx is not None else ""
    if "unknown" in (t1, t15):
        regime, text = "unclear", "Not enough completed candles to classify the market"
    elif label == "conflicting":
        regime, text = "conflicting", cond["alignment"]["text"]
    elif cond["trend"] == "bullish":
        regime, text = "trend_up", f"Bullish trend{adx_t}: 1H bullish, 15M {t15}"
    elif cond["trend"] == "bearish":
        regime, text = "trend_down", f"Bearish trend{adx_t}: 1H bearish, 15M {t15}"
    elif label == "range_bound":
        regime, text = "range", "Sideways: 1H and 15M are both sideways (possible range)"
    elif label == "developing":
        regime, text = "developing", cond["alignment"]["text"]
    else:
        regime, text = "unclear", cond["alignment"]["text"]
    return {"direction": cond["trend"], "regime": regime, "text": text, "volatility": cond["volatility"],
            "alignment": label, "vol_ratio": env["vol_ratio"], "gap": env.get("gap"),
            "atr": {tf: {"used": tfs[tf].get("atr14_intraday"), "with_gap": tfs[tf].get("atr14")} for tf in ("5m", "15m")},
            "trends": {"1h": t1, "15m": t15},  # what the allowed-side rule reads: the decision checks every setup against these
            "regime_facts": env.get("regime"), "change": price_change(a)}  # fmt: skip


def monitor(scans: dict, setups: list, market: dict, session: dict | None = None) -> list:
    """The state of each strategy, and why it is inactive or has no setup."""
    out = []
    for name, title in NAMES.items():
        sides = [(side, scans.get((name, side))) for side in ("bull", "bear")]
        active = [(side, sc) for side, sc in sides if sc and sc.applicable]
        mine = [s for s in setups if s["strategy"] == name]
        if mine:
            lead = max(mine, key=lambda s: RANK.get(s["status"], -1))
            state = "BLOCKED" if lead.get("stage") == "BLOCKED" else lead["status"]  # a confirmed-but-blocked setup is not "waiting for confirmation"
        else:
            state = "NO SETUP" if active else "INACTIVE"
        if active:
            why = f"suits the market ({'/'.join(OPTION[s] for s, _ in active)} side): {market['text']}"
            live = {x["direction"] for x in mine}  # a side with a live setup is explained by the setup, not by a note about the other side
            note = " · ".join(f"{OPTION[side]}: {sc.note}" for side, sc in active if sc.note and OPTION[side] not in live)
        else:
            closed = session["note"] if session and session["phase"] in ("closed", "pre_open") else ""
            why = next((sc.note for _, sc in sides if sc and sc.note), closed or "not enough data")
            note = ""
        per_side = []  # each side on its own: the CE and the PE reasons are never merged into one line
        for side, sc in sides:
            held = next((s for s in mine if s["side"] == side), None)
            per_side.append({"side": OPTION[side], "scanned": sc is not None, "applicable": bool(sc and sc.applicable), "note": sc.note if sc else "",
                             "state": (("BLOCKED" if held.get("stage") == "BLOCKED" else held["status"]) if held else "NO SETUP" if sc and sc.applicable else "INACTIVE"),
                             "setup_id": held["id"] if held else None})  # fmt: skip
        out.append({"id": name, "name": title, "active": bool(active), "why": why, "state": state, "note": note, "setups": [s["id"] for s in mine], "sides": per_side})
    return out


def overlaps(a: dict, b: dict, tol: float) -> bool:
    if a["direction"] != b["direction"]:
        return False
    if a["zone"] and b["zone"] and a["zone"][0] <= b["zone"][1] + tol and b["zone"][0] <= a["zone"][1] + tol:
        return True
    return a["trigger"] is not None and b["trigger"] is not None and abs(a["trigger"] - b["trigger"]) <= tol


def opposes(direction: str, trends: dict) -> bool:
    """Does the 1H or the 15M trend run against this side? The allowed-side rule: a call needs neither to be bearish, a put neither to be bullish. (Every strategy's own
    scanner already applies it; this is the same rule, held once more by the decision, so that no strategy can get round it.)"""
    return ("bearish" if direction == "CE" else "bullish") in (trends.get("1h"), trends.get("15m"))


def permitted(s: dict) -> bool:
    """Does this setup hold a granted counter-trend permission? Only a strategy registered as counter-trend can: no other strategy has the exception, whatever its setup says."""
    ct = s.get("counter_trend")
    if not (ct and ct.get("granted") and s["strategy"] in COUNTER_TREND):
        return False
    exhausted = ct.get("exhausted_trend")  # the trend the reversal is after: a call follows a bearish one, a put a bullish one. A record made for the other direction grants nothing here
    return exhausted is None or exhausted == ("bearish" if s["direction"] == "CE" else "bullish")  # (a hand-built record without it behaves as it always did)


def decide(market: dict, env: dict, setups: list, monitors: list, price, tick_ok: bool, atr5: float | None) -> dict:
    sess, data = env["session"], env["data"]
    refused: list = []  # setups ignored for running against the 1H / 15M trend without a counter-trend permission (set below; empty unless there are some)

    def out(action, headline, reasons, against=(), setup=None, also=(), also_setups=()):
        return {"action": action, "headline": headline, "reasons": list(reasons), "against": list(against) + refused,
                "setup_id": setup["id"] if setup else None, "strategy": setup["strategy"] if setup else None,
                "strategy_name": setup["strategy_name"] if setup else None, "also": list(also),
                "also_setups": [{"strategy": s["strategy"], "strategy_name": s["strategy_name"], "setup_id": s["id"], "direction": s["direction"]} for s in also_setups]}  # fmt: skip

    phase = sess["phase"]
    if phase in ("closed", "pre_open"):
        return out("NO TRADE", f"NO TRADE: {sess['note']}", [sess["note"]])
    if not data["ok"]:
        return out("NO TRADE", "NO TRADE: the market data is not reliable enough", data["issues"])
    if phase == "late":
        return out("NO TRADE", f"NO TRADE: {sess['note']}", [sess["note"]])
    if env["vol_extreme"]:
        return out("NO TRADE", "NO TRADE: volatility is unusually high", [f"15M ATR is {env['vol_ratio']:.1f}x its recent average: wait for conditions to settle"])
    trends = market.get("trends") or {}
    live_all = [s for s in setups if s["status"] in LIVE]
    # The one exception to the direction gate. A market whose 1H and 15M disagree ("conflicting") is NO TRADE, unless a setup of a counter-trend strategy holds a granted permission (a
    # genuine 15M structural reversal: see strategies/exhaustion.py). An "unclear" market (not enough data to know the trends) never has an exception.
    if market["regime"] == "unclear" or (market["regime"] == "conflicting" and not any(permitted(s) for s in live_all)):
        return out("NO TRADE", f"NO TRADE: {market['text']}", [market["text"]])
    if not any(m["active"] for m in monitors):
        return out("NO TRADE", "NO TRADE: no strategy suits the current market", [m["why"] for m in monitors])
    if phase == "opening":
        return out("WAIT", f"WAIT: {sess['note']}", [sess["note"]], [m["note"] for m in monitors if m["note"]])
    # Whatever the regime, a setup that runs against the 1H or 15M trend counts only with a granted permission: for every strategy, held here once more as well as in each scanner.
    ignored = [s for s in live_all if opposes(s["direction"], trends) and not permitted(s)]
    live = [s for s in live_all if s not in ignored]
    refused.extend(f"ignored {s['strategy_name']} ({s['direction']}): it runs against the {' / '.join(tf.upper() for tf in ('1h', '15m') if trends.get(tf) == ('bearish' if s['direction'] == 'CE' else 'bullish'))} "
                   "trend and holds no counter-trend permission" for s in ignored)  # fmt: skip
    strong = [s for s in live if s["status"] in (WAITING, CONFIRMED)]
    if {s["direction"] for s in strong} == {"CE", "PE"}:
        lines = [f"{s['direction']}: {s['strategy_name']} is {s['status']}" for s in strong]
        return out("WAIT", "WAIT: bullish and bearish setups disagree", ["the setups point in opposite directions, so neither is taken: " + "; ".join(lines)])
    confirmed = [s for s in live if s["status"] == CONFIRMED]
    tol = (atr5 or 0.0) * 0.5
    actionable = sorted((s for s in confirmed if s["actionable"]), key=lambda s: PRIORITY.index(s["strategy"]))
    if actionable:
        pri = actionable[0]
        agreeing = [s for s in actionable[1:] if overlaps(pri, s, tol)]
        also = [f"{s['strategy_name']} (same {s['direction']} opportunity)" for s in agreeing]
        return out(f"BUY {pri['direction']}", f"BUY {pri['direction']}: {pri['strategy_name']} setup confirmed", pri["reasons"], pri["against"], pri, also, agreeing)
    if confirmed:
        s = confirmed[0]
        return out("WAIT", f"WAIT: {s['strategy_name']} ({s['direction']}) is confirmed but not actionable right now", why_not(s, price) or ["no live price"], s["against"], s)
    if not tick_ok:
        return out("WAIT", "WAIT: no recent price tick", ["the live price is stale; no entry is judged without it"])
    if live:
        s = max(live, key=lambda x: RANK[x["status"]])
        if s.get("stage") == "BLOCKED":  # the candle has confirmed it: say what blocks it, not that it waits for a candle
            when = f" (completed {hm(s['confirmed_bar_end'])})" if s.get("confirmed_bar_end") else ""
            reasons = [f"the confirming 5M candle{when} has closed beyond the trigger {num(s['trigger'])}: confirmation is not what is missing"] + s["blockers"] + s["reasons"][:1]
            return out("WAIT", f"WAIT: {s['strategy_name']} ({s['direction']}) is confirmed but blocked", reasons, s["against"], s)
        reasons = [f"{s['strategy_name']} ({s['direction']}) is {s['status'].replace('_', ' ').lower()}", s["confirm_text"]] + s["reasons"][:2]
        return out("WAIT", f"WAIT: {s['strategy_name']} ({s['direction']}) is developing", reasons, s["against"] + s["blockers"], s)
    return out("WAIT", "WAIT: the market is tradable but there is no setup yet", [m["note"] or m["why"] for m in monitors if m["active"]])


def counter_trend_report(scans: dict, setups: list) -> list:
    """The permission decision of every counter-trend strategy on each side it considered: whether it was needed (the 1H or 15M runs against the side), whether it was granted,
    and the checks and structure behind it. Granted or denied, it is on record: the dashboard shows it and the journal keeps it."""
    out = []
    for name in NAMES:
        if name not in COUNTER_TREND:
            continue
        for side in ("bull", "bear"):
            sc = scans.get((name, side))
            if sc is None or sc.info is None:
                continue
            live = next((s for s in setups if s["strategy"] == name and s["side"] == side), None)
            out.append({**sc.info, "strategy": name, "strategy_name": NAMES[name], "side": OPTION[side], "setup_id": live["id"] if live else None})
    return out


def timeframe_support(tfs: dict, direction: str) -> dict:
    """Which timeframes agree with a CE / PE setup, which disagree, which are neutral. Only 1H and 15M can stop a side; 5M and 1M are told apart."""
    want, against = ("bullish", "bearish") if direction == "CE" else ("bearish", "bullish")
    out = {"supports": [], "disagrees": [], "neutral": [], "note": "only the 1H and 15M decide whether a side is allowed; 5M and 1M are information"}
    for tf in ("1h", "15m", "5m", "1m"):
        t = tfs[tf]["trend"]
        text = f"{an.LABEL[tf]} {t}"
        (out["supports"] if t == want else out["disagrees"] if t == against else out["neutral"]).append(text)
    return out


def explain(d: dict, setups: list, a: dict, price, events: list, data: dict | None = None, tick_age: float | None = None, tick_ok: bool = True) -> dict:
    """The decision in the five things a manual trader needs: what happened, what is happening now, what has to happen next, what would end
    it, and what blocks entry. Built from the setup the decision is about (or the most advanced live one); with no setup it says why."""
    s = next((x for x in setups if x["id"] == d["setup_id"]), None) or max((x for x in setups if x["status"] in LIVE), key=lambda x: RANK[x["status"]], default=None)
    bad_data = [] if not data or data.get("ok", True) else list(data.get("issues") or [])  # untrusted data blocks everything, whatever the setup says
    if s is None:
        ended = [e for e in events if e["status"] in (INVALIDATED, EXPIRED)][:2]
        return {"setup": None, "stage": None, "actionable": False, "summary": d["headline"],
                "happened": [f"{hm(e['time'])} {e['direction']} {e['strategy_name']} {e['status'].lower()}: {e['text']}" for e in ended] or ["no setup has been announced yet"],
                "now": list(d["reasons"][:3]), "next": ["a strategy has to detect a setup at a meaningful level; the strategy monitor says why each one is inactive"],
                "invalidation": None, "blocking": bad_data or (list(d["reasons"][:3]) if d["action"] == "NO TRADE" else []), **timeframe_support(a["timeframes"], "CE"), "side": None}  # fmt: skip
    happened = [f"detected {hm(s['detected_at'])}"] + ([f"the confirming 5M candle completed {hm(s['confirmed_bar_end'])} and closed beyond {num(s['trigger'])}"] if s.get("confirmed_bar_end") else [])
    now_ = [s["stage_text"]]
    ct = s.get("counter_trend")
    if ct:  # a counter-trend setup says why it is allowed to run against the higher timeframes
        now_.append(("counter-trend permission " + ct["reason"]) if ct["needed"] else "no counter-trend permission was needed: the 1H and 15M do not oppose this side")
    ps = s["price_state"]
    if not tick_ok:  # an old price says nothing about where the market is now
        now_.append(f"the last NIFTY price {num(price)} is " + ("not available" if tick_age is None else f"{tick_age:.0f} s old") + ": it is not treated as live, so nothing is judged against it")
    elif ps == "in_entry_area":
        now_.append(f"price {num(price)} is inside the entry area {num(s['zone'][0])}-{num(s['zone'][1])}")
    elif s["status"] == CONFIRMED:
        now_ += why_not(s, price)[len(s["blockers"]):]
    nxt = {"DETECTED": [s["confirm_text"]], "WAITING_FOR_CONFIRMATION": [s["confirm_text"]],
           "BLOCKED": ["the blocker below has to clear; it is a condition around the setup (data, session, distance, room), not a missing candle"],
           "CONFIRMED": ["price has to come back into the entry area while the setup is still valid, or the opportunity passes"],
           "READY_TO_ACT": [f"the entry area is {num(s['zone'][0])}-{num(s['zone'][1])} while price holds {num(s['invalidation'])}; beyond the area it is chasing"] if s["zone"] else [],
           }.get(s["stage"], ["nothing: this setup has ended"])  # fmt: skip
    under = "below" if s["side"] == "bull" else "above"
    inv = None if s["invalidation"] is None else f"a completed 5M candle closing {under} {num(s['invalidation'])} ends this setup (other rules in the setup card)"
    blocking = list(s["blockers"]) + (why_not(s, price) if s["status"] == CONFIRMED else [])
    return {"setup": s["id"], "side": s["direction"], "stage": s["stage"], "actionable": s["actionable"], "summary": d["headline"], "happened": happened,
            "now": now_, "next": nxt, "invalidation": inv, "blocking": list(dict.fromkeys(bad_data + blocking)), **timeframe_support(a["timeframes"], s["direction"])}  # fmt: skip


def present(core: dict, a: dict, env: dict, now: float, p: Params, tick_age: float | None) -> dict:
    """The JSON the dashboard shows: lifecycle results from the last candle update, judged against the live price."""
    price = a.get("price")
    tick_ok = tick_age is not None and tick_age <= p.max_price_age
    allowed = env["session"]["entries_allowed"] and env["data"]["ok"]  # nothing is actionable while the data cannot be trusted
    setups = [setup_dict(s, price, allowed, tick_ok) for s in core["setups"]]
    market = market_summary(a, env)
    monitors = monitor(core["scans"], setups, market, env["session"])
    atr5 = a["timeframes"]["5m"].get("atr14_intraday")
    decision = decide(market, env, setups, monitors, price, tick_ok, atr5)
    return {
        "available": True, "as_of": now, "price": price,
        "decision": decision, "explain": explain(decision, setups, a, price, core["events"], env["data"], tick_age, tick_ok),
        "market": market, "session": {**env["session"], "opening_range": env["opening_range"]}, "counter_trend": counter_trend_report(core["scans"], setups),
        "strategies": monitors, "setups": setups,
        "recent": [setup_dict(s, price, allowed, tick_ok) for s in core["recent"][:8]],
        "events": core["events"][:12], "data": {**env["data"], "price_age": tick_age, "price_ok": tick_ok},
    }  # fmt: skip


def unavailable(reason: str, now: float) -> dict:
    return {"available": False, "as_of": now, "decision": {"action": "NO TRADE", "headline": f"NO TRADE: {reason}", "reasons": [reason],
            "against": [], "setup_id": None, "strategy": None, "strategy_name": None, "also": [], "also_setups": []},
            "market": None, "session": None, "counter_trend": [], "strategies": [], "setups": [], "recent": [], "events": [], "data": {"ok": False, "issues": [reason]}}  # fmt: skip
