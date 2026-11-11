"""Trend Exhaustion Reversal (Stage 3): a possible reversal after an established trend loses structural control, and the one strategy allowed to trade against the 1H / 15M direction.

Written like the others: once, for the bullish TRADE (calls, after a bearish exhaustion), run on a `View` for the bearish trade (puts, after a bullish exhaustion). In the words below
"down" is the exhausted trend in the view's terms; for a put it is a bullish trend. Every setup goes through `strategy.build`, where the data, session, volatility, risk-distance and
room checks are applied: nothing here can skip them.

PERMISSION (the exception to the higher-timeframe direction gate). It is granted when ALL of these hold, and the decision is recorded whether it is granted or denied:
    1. an established trend to reverse: the 1H trend is the exhausted one, or the regime says the market is extended that way, or the 15M EMAs have been stacked that way for
       rg_stack_bars candles after a leg of at least ter_min_leg_atr15 ATR15
    2. weakening continuation: the regime fact `weakening_trend` names that trend (at least rg_weak_min_checks of its six checks)
    3. a confirmed 15M structural change: the confirmed swings went from lower highs and lower lows to a higher low and a higher high, recently (ter_reversal_max_bars_15m 15M candles
       since it was confirmed), with the exhaustion low still intact
RSI never enters it, and nothing else grants it: not an extended 1H, not an overbought or oversold reading, not a falling ADX. The decision engine checks the record again
(trading.decide): a setup that opposes the 1H or 15M trend is eligible only if it comes from a counter-trend strategy and its permission is granted.

THE SETUP, after the permission:
    DETECTED   the permission is granted but the 5M has formed no confirmed swing high to break yet
    WAITING    a confirmed 5M swing high (formed after the 15M higher low) is the trigger; no completed 5M candle has closed through it
    CONFIRMED  a completed 5M candle (rising, body of at least confirm_body_atr5 x ATR5, closing near its high; in stricter mode the next one too) closes through the trigger, and it
               completed after the 15M change was confirmed
    trigger ....... the latest confirmed 5M swing high since the 15M higher low
    entry zone .... the trigger up to entry_chase_atr5 x ATR5 beyond it (further is "do not chase")
    invalidation .. below the 5M pullback low that preceded the break (the lowest low since that swing high), less the usual buffer. A 5M close under the 15M higher low, or under the
                    exhaustion low, ends it at any stage: the structure the reversal rests on is gone
    expiry ........ the usual entry window after the confirmation, the usual do-not-chase limit, and no 5M confirmation within ter_max_wait_bars candles of the 15M change
    duplicates .... the key is anchored to the exhaustion extreme (a 15M swing): one structural break can only ever produce one setup, and an ended one is never revived
    the clock ..... the book freezes a CONFIRMED setup, so its entry window is ALSO kept by the clock, from the confirming candle the setup recorded: a restart, or a hole in the data that
                    hides the break, cannot stretch it (and a confirmation whose record is incomplete is not trusted)
    holes ......... a 5M candle missing between the 15M swing and now blocks a confirmation (the swing highs and lows are read across it) until the data is whole
    the record .... a tracked setup goes on from its own stored permission only if that is granted, made for THIS exhausted trend, complete and the right way round; otherwise it is
                    decided afresh from the facts of now. The decision engine holds the same direction check (trading.permitted)

It reads only completed candles. Its thresholds are the existing ones plus three of its own (`ter_*`) and the regime layer's (`rg_*`)."""

from copy import copy
from dataclasses import replace
from typing import NamedTuple

from ..regime import chk
from ..registry import REVERSAL, StrategyDef, register
from ..strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Params, Rev, Scan, View, build, hm

NAME = "trend_exhaustion_reversal"
SWING_K = 2  # a pivot needs this many candles each side, as analysis.SWING_K
CONFIRM_BARS = SWING_K + 1  # a 15M pivot is confirmed when the second candle after it has completed: three 15M candles after its start
FIVE = 300  # seconds in a 5M candle
PRICES = ("prior_high", "lower_high", "exhaustion_extreme", "higher_low", "higher_high")  # the prices of the stored structure (real prices; the names are the call's: see find_reversal)
STRUCTURE_KEYS = PRICES + ("exhaustion_start", "higher_low_start", "higher_high_start", "confirmed_at", "leg")


class Reversal(NamedTuple):
    """The 15M structural change, in the view's prices (bullish view: a down structure that turned up)."""

    prior_high: float  # H_a: the high before the lower high
    lower_high: float  # H_b
    low_b: float  # the exhaustion extreme: the lowest swing low of the exhausted leg
    low_b_start: float
    low_c: float  # the higher low
    low_c_start: float
    high_c: float  # the higher high
    high_c_start: float
    confirmed_at: float  # when the later of the two newest pivots was confirmed: from here a 5M break counts
    leg: float


def pivots_high(bars: list) -> list:
    """Indices of the confirmed 5M swing highs in `bars` (the analysis's own pivot rule: above the SWING_K bars before it, not below the SWING_K bars after it)."""
    k = SWING_K
    return [i for i in range(k, len(bars) - k) if all(bars[i].h > b.h for b in bars[i - k : i]) and all(bars[i].h >= b.h for b in bars[i + 1 : i + 1 + k])]


# -- the permission --------------------------------------------------------------------------------------------------------------------------------
def find_reversal(v: View, p: Params, atr15: float) -> tuple[Reversal | None, list]:
    """The 15M structural change from the confirmed swings (the three latest highs and lows the analysis keeps), with a named check for each requirement."""
    sw = v.swings15
    highs, lows = [(t, x) for k, t, x in sw if k == "H"], [(t, x) for k, t, x in sw if k == "L"]
    # The words the trader reads are in REAL terms. The view's "high" swings are real lows in a put, so a call follows a down structure that turned up (lower highs and lower lows,
    # then a higher low and a higher high) and a put an up structure that turned down (higher lows and higher highs, then a lower high and a lower low).
    hi, lo = ("high", "low") if v.sgn > 0 else ("low", "high")
    was, now_w = ("lower", "higher") if v.sgn > 0 else ("higher", "lower")
    was_rel, now_rel = ("under", "over") if v.sgn > 0 else ("over", "under")
    if len(highs) < 3 or len(lows) < 3:
        n_hi, n_lo = (len(highs), len(lows)) if v.sgn > 0 else (len(lows), len(highs))
        return None, [chk("15M swing structure", None, f"only {n_hi} swing highs and {n_lo} swing lows are confirmed: a structural change needs 3 of each")]
    (_, ha), (_, hb), (tc, hc) = highs[-3:]
    (_, la), (tlb, lb), (tlc, lc) = lows[-3:]
    px = v.px
    before = hb < ha and lb < la
    now = hc > hb and lc > lb
    leg = ha - lb
    confirmed_at = max(tc, tlc) + CONFIRM_BARS * 900
    age = (v.ctx.now - confirmed_at) / 900
    after = [b for b in v.b15 if b.start >= tlb]
    intact = bool(after) and min(b.l for b in after) >= lb
    checks = [
        chk(f"{'down' if v.sgn > 0 else 'up'} structure before", before, f"15M {was} {hi} {px(hb)} {was_rel} {px(ha)} and {was} {lo} {px(lb)} {was_rel} {px(la)}"),
        chk("structure turned", now, f"then a {now_w} {lo} {px(lc)} {now_rel} {px(lb)} and a {now_w} {hi} {px(hc)} {now_rel} {px(hb)}"),
        chk(f"the exhausted leg was at least {p.ter_min_leg_atr15:g} ATR15", leg >= p.ter_min_leg_atr15 * atr15, f"the leg from {px(ha)} to {px(lb)} was {leg:.1f} pts = {leg / atr15:.1f}x ATR15"),
        chk(f"confirmed within the last {p.ter_reversal_max_bars_15m} 15M candles", 0 <= age <= p.ter_reversal_max_bars_15m, f"confirmed {hm(confirmed_at)}, {age:.1f} 15M candles ago"),
        chk("the exhaustion extreme still holds", intact, f"no 15M candle has traded {v.under} {px(lb)} since it formed"),
    ]  # fmt: skip
    if not all(c["ok"] is True for c in checks):
        return None, checks
    return Reversal(ha, hb, lb, tlb, lc, tlc, hc, tc, confirmed_at, leg), checks


def permission(v: View, p: Params, atr15: float) -> tuple[dict | None, Reversal | None, str]:
    """(the permission record, the reversal, "" or why this strategy does not apply). The record is in REAL prices and says what was checked and what the result was; None when
    there is no established trend to reverse (the strategy is then inactive and there is nothing to decide)."""
    facts = v.regime.get("facts") or {}
    down = "bearish" if v.sgn > 0 else "bullish"  # the exhausted trend, in real terms
    est_checks = []
    one_h = v.trend["1h"] == "bearish"
    est_checks.append(chk(f"the 1H trend is {down}", one_h, f"1H is {v.ctx.trend['1h']}"))
    ext = (facts.get("extended") or {}).get("state") == "bearish"
    est_checks.append(chk(f"the market is extended {down}", ext, "regime fact: " + ((facts.get("extended") or {}).get("summary") or "unknown")))
    stack = (v.ctx.ema_slope.get("15m") or {}).get("stack_bars")
    stacked = stack is not None and v.sgn * stack <= -p.rg_stack_bars
    reversal, rchecks = find_reversal(v, p, atr15)
    leg_ok = any(c["name"].startswith("the exhausted leg") and c["ok"] is True for c in rchecks)
    est_checks.append(chk(f"15M EMAs stacked {down} for {p.rg_stack_bars}+ candles after a big leg", stacked and leg_ok, f"stacked {stack if stack is not None else 'n/a'} candles"))
    if not any(c["ok"] is True for c in est_checks):
        return None, None, f"no established {down} trend or extension to reverse (" + "; ".join(c["text"] for c in est_checks) + ")"
    weak = facts.get("weakening_trend") or {}
    weak_ok = weak.get("state") == "bearish"
    checks = [
        {**chk("an established trend", True, "; ".join(c["name"] for c in est_checks if c["ok"] is True)), "group": "established"},
        {**chk("weakening continuation", weak_ok, "regime fact: " + (weak.get("summary") or "unknown")), "group": "weakening"},
        *({**c, "group": "structure"} for c in rchecks),
    ]  # fmt: skip
    granted = weak_ok and reversal is not None
    against = v.trend["1h"] == "bearish" or v.trend["15m"] == "bearish"
    failing = next((c for c in checks if c["ok"] is not True), None)
    reason = (f"granted: the {down} trend is weakening and the 15M structure has turned" if granted else
              f"denied: {failing['name']} ({failing['text']})" if failing else "denied")  # fmt: skip
    real = v.real
    structure = None if reversal is None else {
        "exhaustion_extreme": real(reversal.low_b), "exhaustion_start": reversal.low_b_start, "higher_low": real(reversal.low_c), "higher_low_start": reversal.low_c_start,
        "lower_high": real(reversal.lower_high), "higher_high": real(reversal.high_c), "higher_high_start": reversal.high_c_start, "prior_high": real(reversal.prior_high),
        "confirmed_at": reversal.confirmed_at, "leg": reversal.leg,
    }  # fmt: skip
    return {"needed": against, "granted": granted, "reason": reason, "checks": checks, "structure": structure, "exhausted_trend": down}, reversal, ""


def reversal_from_record(v: View, s: dict) -> Reversal:
    """The reversal a tracked setup was permitted by, from the record it carries (real prices) back into the view: the permission is decided once, at detection."""
    k = v.sgn
    return Reversal(k * s["prior_high"], k * s["lower_high"], k * s["exhaustion_extreme"], s["exhaustion_start"], k * s["higher_low"], s["higher_low_start"], k * s["higher_high"],
                    s["higher_high_start"], s["confirmed_at"], s["leg"])


def carried(perm: dict | None, down: str) -> bool:
    """Can a tracked setup go on from this stored permission? It must be granted, be for THIS exhausted trend (a call follows a bearish one, a put a bullish one: a record written for
    the other direction is never used), and hold every number of the structure it rests on. Anything less is not trusted: the permission is decided afresh, from the facts of now."""
    st = (perm or {}).get("structure")
    if not perm or not perm.get("granted") or perm.get("exhausted_trend", down) != down or not isinstance(st, dict):
        return False
    return all(isinstance(st.get(k), (int, float)) and not isinstance(st.get(k), bool) for k in STRUCTURE_KEYS)


def oriented(rv: Reversal) -> bool:
    """Is the stored structure the right way round in the view's prices? The same relations find_reversal demanded: a lower high under the prior high, then a higher low over the
    exhaustion extreme and a higher high over the lower high. A record whose numbers are the wrong way round (its direction inverted) is not trusted."""
    return rv.lower_high < rv.prior_high and rv.low_c > rv.low_b and rv.high_c > rv.lower_high and rv.low_c_start > rv.low_b_start


def higher_low(v: View) -> str:
    """The 15M swing the reversal rests on, in real words: the higher low of a call, the lower high of a put."""
    return "higher low" if v.sgn > 0 else "lower high"


def hole(bars: list) -> str | None:
    """A 5M candle missing inside the window the setup is read from (from the 15M swing it rests on to now): the swing highs and lows of that window cannot be trusted across it."""
    for a, b in zip(bars, bars[1:]):
        if b.start - a.start > FIVE:
            return f"5M candles are missing between {hm(a.end)} and {hm(b.start)}, inside the structure this setup is read from: its swings cannot be trusted"
    return None


def lapsed(v: View, p: Params, prior) -> str | None:
    """Why a CONFIRMED setup's entry window is over, from the CLOCK and the confirming candle it recorded (not from the candles that happen to be in the data now: a hole there must
    never stretch the window). None while the window is open."""
    cc = prior.confirm_candle or {}
    end = cc.get("end", prior.confirmed_bar_end)
    if not isinstance(end, (int, float)):
        return "its record of the confirming candle is incomplete: the entry window cannot be checked"
    if v.ctx.now - end >= p.confirm_valid_bars * FIVE:
        return "the entry window after the confirmation has passed"
    return None


# -- the lifecycle ----------------------------------------------------------------------------------------------------------------------------------
def ter_state(v: View, p: Params, rv: Reversal, bars: list) -> Rev:
    """The lifecycle of the 5M break after the 15M change, replayed in time order over `bars` (the completed 5M candles since the 15M higher low, bullish view)."""
    atr5, c = v.atr["5m"], v.ctx
    chase, buf = p.entry_chase_atr5 * atr5, p.inv_buffer_atr5 * atr5
    lc_floor, lb_floor = rv.low_c - buf, rv.low_b - buf
    low = min([rv.low_c] + [b.l for b in bars])  # the structural low the setup rests on

    def ended(status, note, inv=lc_floor, trig=None, cb=None, brk=None, ev=None):
        return Rev(status, note, trig, inv, None if trig is None else (trig, trig + chase), ev, cb, low, rv.low_b_start, brk)

    def solid(b, trig):
        return b.bull and b.c > trig and b.body >= p.confirm_body_atr5 * atr5 and b.close_loc >= p.confirm_close_loc and b.end > c.confirm_after

    def failed(b):
        if b.c < lb_floor:
            return f"a 5M candle closed {v.under} the exhaustion {v.trough} {v.px(rv.low_b)}: the reversal failed"
        if b.c < lc_floor:
            return f"a 5M candle closed {v.under} the 15M {higher_low(v)} {v.px(rv.low_c)}: the structure the reversal rests on is lost"
        return None

    n_c = brk = pending = piv = None
    trig = None
    for n, b in enumerate(bars):
        ps = pivots_high(bars[:n])  # only candles that had completed before this one
        if ps:
            piv, trig = ps[-1], bars[ps[-1]].h
        if why := failed(b):
            return ended(INVALIDATED, why)
        if b.start < rv.confirmed_at or trig is None or not solid(b, trig):
            continue
        if not c.strict:
            n_c, brk = n, b
            break
        nxt = bars[n + 1] if n + 1 < len(bars) else None
        if nxt is None:
            pending = b
        elif nxt.c > trig:
            n_c, brk = n + 1, b
            break
    if n_c is None:
        waited = sum(1 for b in bars if b.start >= rv.confirmed_at)
        if waited > p.ter_max_wait_bars:
            return ended(EXPIRED, f"no 5M break confirmed within {p.ter_max_wait_bars} candles of the 15M structural change")
        if trig is None:
            return ended(DETECTED, "", lc_floor)
        pull = min(b.l for b in bars[piv:])  # the pullback after the swing high that is the trigger
        ev = None if pending is None else f"5M break candle {hm(pending.start)} closed {pending.c - trig:.1f} pts {v.over} the 5M swing {v.peak} {v.px(trig)}"
        return ended(WAITING, "", pull - buf, trig, ev=ev)
    trig_cb = trig
    cb = bars[n_c]
    piv_brk = pivots_high(bars[:bars.index(brk)])[-1]  # the swing high the break candle closed through
    inv = min(b.l for b in bars[piv_brk : n_c + 1]) - buf
    zone_ev = (f"5M break: the {hm(brk.start)} candle closed {brk.c - trig_cb:.1f} pts ({(brk.c - trig_cb) / atr5:.1f}x ATR5) {v.over} the 5M swing {v.peak} {v.px(trig_cb)} "
               f"(formed {hm(bars[piv_brk].start)}), body {brk.body / atr5:.1f}x ATR5, closed {brk.close_loc:.0%} of its range")  # fmt: skip

    def result(status, note):
        return Rev(status, note, trig_cb, inv, (trig_cb, trig_cb + chase), zone_ev, cb, low, rv.low_b_start, brk)

    for b in bars[n_c + 1 :]:
        if why := failed(b):
            return result(INVALIDATED, why)
        if b.c < inv:
            return result(INVALIDATED, f"a 5M candle closed {v.under} the invalidation level {v.px(inv)} (the 5M pullback {v.trough} before the break)")
        if b.c < trig_cb - p.failed_follow_atr5 * atr5:
            return result(INVALIDATED, f"failed follow-through: a 5M candle closed back {v.under} the trigger {v.px(trig_cb)}")
    top = max(b.c for b in bars[n_c:])
    if top - trig_cb > chase:
        return result(EXPIRED, f"price closed {top - trig_cb:.1f} pts ({(top - trig_cb) / atr5:.1f}x ATR5) beyond the trigger: the entry opportunity has passed")
    if len(bars) - 1 - n_c >= p.confirm_valid_bars:
        return result(EXPIRED, "the entry window after the confirmation has passed")
    return result(CONFIRMED, "")


def scan_exhaustion(v: View, p: Params, prior) -> Scan:
    c = v.ctx
    if "unknown" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, "not enough completed 1H / 15M candles to judge the direction")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    down = "bearish" if v.sgn > 0 else "bullish"
    tracked = prior is not None and prior.strategy == NAME and prior.side == v.side and carried(prior.counter_trend, down)
    if tracked:
        perm = prior.counter_trend  # decided once, when it was detected: a later change of the facts neither revokes nor invents it
        rv = reversal_from_record(v, perm["structure"])
        tracked = oriented(rv)
    if not tracked:
        if not v.regime.get("available"):
            return Scan(False, f"regime facts are not available ({v.regime.get('why') or 'not computed'}): nothing is assumed about the {down} trend")
        perm, rv, why = permission(v, p, atr15)
        if perm is None:
            return Scan(False, why)
        if not perm["granted"]:
            return Scan(True, f"counter-trend permission {perm['reason']}", None, perm)
    key = f"{NAME}:{v.side}:{int(rv.low_b_start)}"
    bars = [b for b in v.b5 if b.start >= rv.low_c_start]
    rev = ter_state(v, p, rv, bars)
    if tracked and prior.status == CONFIRMED and rev.status not in (INVALIDATED, EXPIRED) and (why := lapsed(v, p, prior)):
        rev = rev._replace(status=EXPIRED, note=why)  # a confirmed setup is frozen by the book, so its window is also kept by the clock: a restart or a hole in the data cannot stretch it
    if (gap := hole(bars)) and rev.status not in (INVALIDATED, EXPIRED):
        v = copy(v)  # (the book's own context is shared by every strategy: this one view carries the extra blocker)
        v.ctx = replace(v.ctx, can_confirm=False, blockers=[*v.ctx.blockers, gap])
    px = v.px
    moved, hl = ("down", "higher low") if v.sgn > 0 else ("up", "lower high")
    reasons = [
        f"counter-trend permission {perm['reason']}" if perm["needed"] else f"permission not needed (the 1H and 15M do not oppose this side); {perm['reason']}",
        *(f"{x['name']}: {x['text']}" for x in perm["checks"] if x["ok"] is True),
        f"the {down} leg ended at the exhaustion {v.trough} {px(rv.low_b)} ({rv.leg / atr15:.1f}x ATR15 {moved} from {px(rv.prior_high)})",
    ]  # fmt: skip
    against = [f"counter-trend: the {'1H' if v.trend['1h'] == 'bearish' else '15M'} trend is still {down}; this trade is allowed only by the 15M structural reversal"] if perm["needed"] else []
    resist = [lv.price for lv in v.levels] + [x for k, t, x in v.swings15 if k == "H" and x > rv.low_c]
    names = ("15M lower high", "15M prior high", "15M higher high") if v.sgn > 0 else ("15M higher low", "15M prior low", "15M lower low")  # (the swings a target can be, in real words)
    labels = {**{lv.price: lv.label for lv in v.levels}, rv.lower_high: names[0], rv.prior_high: names[1], rv.high_c: names[2]}
    scan = build(v, p, prior, NAME, key, rv.low_b_start, rv.low_b, f"exhaustion {v.trough}", rev, reasons, against, resist,
                 f"a 5M swing {v.peak} forms after the 15M {hl} and a completed 5M {v.dirn} candle closes {v.over} it", labels)  # fmt: skip
    s = scan.setup
    if s is not None:
        if gap and s.status not in (INVALIDATED, EXPIRED) and gap not in s.blockers and prior is not None and prior.status == CONFIRMED:
            s.blockers = [*s.blockers, gap]  # (a confirmed setup is frozen by the book: only its blockers move, so the hole must be among them)
        s.counter_trend = perm
        if rev.trigger is not None and s.status not in (INVALIDATED, EXPIRED):
            s.confirm_text += f"; the invalidation is the 5M pullback {v.trough} before the break, {px(rev.inv)}, and a close {v.under} the 15M {hl} {px(rv.low_c)} ends it at any stage"
    return Scan(scan.applicable, scan.note, s, perm)


register(StrategyDef(NAME, "Trend Exhaustion Reversal", scan_exhaustion, REVERSAL, priority=16, counter_trend=True))
