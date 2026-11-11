"""The trend-continuation family (Stage 2.1): Momentum Continuation, EMA Trend Continuation and Volatility Expansion.

Written like the three originals: once, for the bullish case, run on a `View` for the bearish case, and every setup goes through `strategy.build`, which is
where the data, session, volatility, risk-distance and room checks are applied: nothing here can skip them.

The three strategies that follow a trend and could describe the same dip are kept apart by what the dip looks like, so one movement is never announced twice.
Measured from the prior high (the leg's high) down to the lowest low since it, in ATR15 (min_pullback_atr15 is the line Trend Pullback already draws):

    deeper than that line, at a value zone ............ Trend Pullback (unchanged, in strategy.py)
    shallower, and it reached the 5M EMA20 zone ....... EMA Trend Continuation
    shallower, and it stayed above the 5M EMA20 zone .. Momentum Continuation

All three need a trend that is already established (1H and 15M both in the side's direction, the 15M EMA20 moving that way), and the two continuation
strategies trigger on a completed 5M candle closing THROUGH the prior high, not merely above the pause: a trigger below the prior high would sit under resistance
that the room rule (rightly) counts. As a second guard, setups of the two continuation strategies on the same leg carry one `group`, and the lifecycle keeps only
one live setup per group.

Volatility Expansion is a different kind of move: a wide-range candle closing out of a quiet 5M compression box. It overlaps Breakout and Retest only where a
15M range edge is the box edge; the decision then lists the second one as "also matches" in the usual way."""

from typing import NamedTuple

from ..registry import WITH_TREND, StrategyDef, register
from ..strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Base, Bar, Params, Rev, Scan, View, build, dead, hm, rejection, tracked_extreme


class Leg(NamedTuple):
    bar: Bar  # the candle that made the leg's high
    high: float
    floor: float  # the leg's origin: the latest 15M swing low before it
    after: list  # the completed 5M candles after the high


def trend_leg(v: View, anchor: float | None = None) -> "Leg | str":
    """The leg a pause or dip belongs to, found exactly as Trend Pullback finds it: the latest 15M swing high, the highest 5M candle since, and the 15M swing
    low before it as the origin. `anchor` (the start of that high candle, remembered by a setup that is already being tracked) keeps a tracked setup on its
    own leg after the price has gone on to make new highs. Returns the reason there is no usable leg instead of a Leg."""
    hi_bar = None if anchor is None else next((b for b in v.b5 if b.start == anchor), None)
    if hi_bar is None:
        highs = [s for s in v.swings15 if s[0] == "H"]
        if not highs:
            return f"no confirmed 15M swing {v.peak} yet"
        since = [b for b in v.b5 if b.start >= highs[-1][1]]
        if not since:
            return f"no completed 5M candles since the latest 15M swing {v.peak}"
        hi_bar = max(since, key=lambda b: (b.h, b.start))
    lows = [s for s in v.swings15 if s[0] == "L" and s[1] < hi_bar.start]
    if not lows:
        return f"no 15M swing {v.trough} before the {v.peak}: the leg's origin is unknown"
    return Leg(hi_bar, hi_bar.h, lows[-1][2], [b for b in v.b5 if b.start > hi_bar.start])


# -- resumption through the prior high (Momentum Continuation and EMA Trend Continuation) -------------------------------------
def resumption(after: list, atr5: float, p: Params, strict: bool, confirm_after: float, floor_high: float, dirn: str, use_rejection: bool, hint: str | None = None) -> Base:
    """Like strategy.base_break, but the trigger is never below `floor_high` (the prior high): a completed 5M candle must close above BOTH the highest high since the
    pause low and the prior high. Evidence of buyers is a strong candle (body of at least momentum_body_atr5 x ATR5) or, for the EMA dip, a rejection candle at the
    low. `after` starts at the pause low (index 0), bullish view. A `hint` is evidence the caller already has (the failed breakout's own story): it stands in for
    both, so the confirming candle only has to be a solid one."""
    if not after:
        return Base(None, None, None)
    ev = hint
    if use_rejection and hint is None:
        for b in after[: p.rejection_window]:
            if rejection(b, p):
                ev = f"{dirn} rejection candle at {hm(b.start)} ({'lower' if dirn == 'bullish' else 'upper'} wick {b.lower_wick / b.rng:.0%} of its range)"
                break
    top, early = max(floor_high, after[0].h), None
    for k in range(1, len(after)):
        b, base_high = after[k], top
        top = max(top, b.h)
        if not (b.bull and b.c > base_high and b.body >= p.confirm_body_atr5 * atr5 and b.close_loc >= p.confirm_close_loc):
            continue
        evidence = ev
        if b.body >= p.momentum_body_atr5 * atr5 and ev is None:
            evidence = f"strong {dirn} candle at {hm(b.start)} (body {b.body / atr5:.1f}x ATR5) through the prior high"
        if evidence is None:
            continue  # closing through the high without any sign of strength is not enough
        if b.end <= confirm_after:  # made of opening-range trading: observed, not traded
            early = early or b
            continue
        if strict:
            nxt = after[k + 1] if k + 1 < len(after) else None
            if nxt is None:
                return Base(evidence, base_high, None, True, None, k)
            if not nxt.c > base_high:
                continue  # no follow-through
            return Base(evidence, base_high, k + 1, False, None, k)
        return Base(evidence, base_high, k, False, None, k)
    return Base(ev, top, None, False, early)


def continuation_state(v: View, p: Params, bars: list, floor_high: float, ok, *, min_pause: int, max_bars: int, use_rejection: bool, keep: float | None = None,
                       hint: str | None = None) -> Rev | None:  # fmt: skip
    """The lifecycle of a dip or pause that has not broken the leg's high, replayed in time order over `bars` (the completed 5M candles after the high, bullish view).
    The same rules as strategy.reversal_state (the lowest low is the base, a new low restarts it, the first completed candle that confirms fixes the setup, later
    candles can only invalidate or expire it), with the trigger placed through the prior high. `ok(low)` says whether the low is at an acceptable place (None = it
    is not: no setup); `keep` is the low a tracked setup already rests on, which stays acceptable although the EMAs it was judged against have moved. With a `hint`
    (evidence that already exists, see `resumption`) a setup that has not confirmed yet is WAITING, not DETECTED: there is something to wait on."""
    atr5 = v.atr["5m"]
    chase, buf = p.entry_chase_atr5 * atr5, p.inv_buffer_atr5 * atr5

    def good(x):
        return x == keep or ok(x)

    def base_of(after):
        return resumption(after, atr5, p, v.ctx.strict, v.ctx.confirm_after, floor_high, v.dirn, use_rejection, hint)

    low, done = 0, None
    for n in range(len(bars)):
        if bars[n].l <= bars[low].l:
            low = n  # a new low restarts the base
        if n > low and n >= min_pause and good(bars[low].l):
            after = bars[low : n + 1]
            base = base_of(after)
            if base.confirmed == len(after) - 1:
                done = (n, low, base)
                break
    if done is None:
        extreme = bars[low].l
        if not good(extreme):
            return None
        after = bars[low:]
        base = base_of(after)
        inv = extreme - buf
        if len(after) > max_bars:
            return Rev(EXPIRED, f"no resumption within {max_bars} candles of the {v.trough}", base.trigger, inv, None, base.evidence, None, extreme, bars[low].start)
        zone = None if base.trigger is None else (base.trigger, base.trigger + chase)
        note = "" if base.early is None else (f"the break candle at {hm(base.early.start)} completed inside the opening range (until {p.observe_until}): "
                                              "the opening range is observed, not traded, so a candle completed after it must confirm")  # fmt: skip
        return Rev(WAITING if base.pending_follow or hint else DETECTED, note, base.trigger, inv, zone, base.evidence, None, extreme, bars[low].start)
    n_c, low_c, base = done
    extreme = bars[low_c].l
    inv, trig = extreme - buf, base.trigger
    zone, cb = (trig, trig + chase), bars[n_c]

    def result(status, note):
        return Rev(status, note, trig, inv, zone, base.evidence, cb, extreme, bars[low_c].start, bars[low_c + base.broke])

    for b in bars[n_c + 1 :]:
        if b.c < inv:
            return result(INVALIDATED, f"a 5M candle closed {v.under} the invalidation level {v.px(inv)}")
        if b.c < trig - p.failed_follow_atr5 * atr5:
            return result(INVALIDATED, f"failed follow-through: a 5M candle closed back {v.under} the trigger {v.px(trig)}")
    top = max(b.c for b in bars[n_c:])
    if top - trig > chase:
        return result(EXPIRED, f"price closed {top - trig:.1f} pts ({(top - trig) / atr5:.1f}x ATR5) beyond the trigger: the entry opportunity has passed")
    if len(bars) - 1 - n_c >= p.confirm_valid_bars:
        return result(EXPIRED, "the entry window after the confirmation has passed")
    return result(CONFIRMED, "")


def scan_continuation(v: View, p: Params, prior, kind: str) -> Scan:
    """Momentum Continuation (`kind == "momentum"`) and EMA Trend Continuation (`"ema"`): the same trend, leg and resumption rules, told apart by whether the dip
    reached the 5M EMA20 zone, and both kept apart from Trend Pullback by its depth (see the module docstring)."""
    mc = kind == "momentum"
    name = "momentum_continuation" if mc else "ema_trend_continuation"
    title = "momentum continuation" if mc else "EMA trend continuation"
    c = v.ctx
    if v.trend["1h"] != "bullish" or v.trend["15m"] != "bullish":
        return Scan(False, f"1H is {c.trend['1h']} and 15M is {c.trend['15m']}: {title} needs both {v.dirn}")
    min_adx = p.mc_min_adx_15m if mc else p.etc_min_adx_15m
    adx15 = v.adx.get("15m")
    if adx15 is None or adx15 < min_adx:
        return Scan(False, f"15M ADX {'n/a' if adx15 is None else format(adx15, '.1f')} is below {min_adx:g}: the trend is not strong enough for {title}")
    if v.pattern.get("15m") == "LH_LL":
        return Scan(False, f"15M structure ({c.pattern['15m']}) does not support a {v.dirn} move")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    sl15, sl5 = (c.ema_slope or {}).get("15m") or {}, (c.ema_slope or {}).get("5m") or {}
    e5_20, e5_50 = v.ema5
    if sl15.get("ema20") is None or sl15.get("stack_bars") is None or sl5.get("ema20") is None or e5_20 is None or e5_50 is None:
        return Scan(True, "the 15M / 5M EMAs and their slopes are not available yet")
    word = "rising" if v.sgn > 0 else "falling"
    rise15, stack15, rise5 = v.sgn * sl15["ema20"], v.sgn * sl15["stack_bars"], v.sgn * sl5["ema20"]
    if rise15 <= 0 or rise5 <= 0:
        return Scan(False, f"the EMA20 is not {word} (15M {sl15['ema20']:+.1f} pts, 5M {sl5['ema20']:+.1f} pts over 5 candles): there is no trend to continue")
    if not mc and stack15 < p.etc_min_stack_bars:
        return Scan(False, f"the 15M EMA20 has been {v.over} the EMA50 for {max(stack15, 0):.0f} candles, under {p.etc_min_stack_bars}: the EMAs are not yet stacked")
    anchor = prior.anchor_time if prior is not None and prior.strategy == name and prior.side == v.side else None
    leg = trend_leg(v, anchor)
    if isinstance(leg, str):
        return Scan(True, leg)
    bar, leg_high, floor, after = leg
    key = f"{name}:{v.side}:{int(bar.start)}"
    tol = p.level_tol_atr15 * atr15
    min_leg = (p.mc_min_leg_atr15 if mc else p.etc_min_leg_atr15) * atr15
    if leg_high - floor < min_leg:
        return Scan(True, f"the leg is only {leg_high - floor:.1f} pts ({(leg_high - floor) / atr15:.1f}x ATR15) from its origin: too small to continue")
    if not after:
        return Scan(True, f"price is at its {v.px(leg_high)} {v.peak}: no {'pause' if mc else 'dip'} yet")
    if mc and anchor is None and len(after) < p.mc_min_pause_bars:  # a new high followed by one candle is not yet a pause: announcing it would be noise in a strong trend
        return Scan(True, f"price is {len(after)} candle(s) under its {v.px(leg_high)} {v.peak}: a pause needs at least {p.mc_min_pause_bars}")
    if any(b.c < floor - tol for b in v.b15 if b.start >= bar.start) or any(b.c < floor - tol for b in after):
        return dead(v, name, key, bar.start, INVALIDATED, f"a candle closed {v.under} the leg's origin {v.px(floor)}: the {v.dirn} structure is broken")
    min_depth = p.min_pullback_atr15 * atr15

    def ok(x):  # the pause or dip must be shallower than a Trend Pullback, and on the right side of the 5M EMA20 zone
        if leg_high - x >= min_depth:
            return False
        return x > e5_20 + tol if mc else (e5_50 - tol <= x <= e5_20 + tol)

    rev = continuation_state(v, p, after, leg_high, ok, min_pause=p.mc_min_pause_bars if mc else 1, max_bars=p.mc_max_bars if mc else p.etc_max_bars,
                             use_rejection=not mc, keep=tracked_extreme(v, prior, key))  # fmt: skip
    if rev is None:
        low = min(b.l for b in after)
        depth = leg_high - low
        if depth >= min_depth:
            return Scan(True, f"the {'pause' if mc else 'dip'} reached {depth:.1f} pts ({depth / atr15:.2f}x ATR15) below the {v.peak} {v.px(leg_high)}: a pullback that deep is "
                              "Trend Pullback's, not a continuation")  # fmt: skip
        if mc:
            return Scan(True, f"the pause low {v.px(low)} reached the 5M EMA20 zone ({v.px(e5_20)}): a dip to the EMA is EMA Trend Continuation's, not a momentum pause")
        if low > e5_20 + tol:
            return Scan(True, f"the dip low {v.px(low)} has not reached the 5M EMA20 zone ({v.px(e5_20)}, within {tol:.1f} pts) yet")
        return Scan(True, f"the dip low {v.px(low)} went through the 5M EMA50 ({v.px(e5_50)}): that is not a continuation")
    depth = leg_high - rev.extreme
    reasons = [
        f"1H {c.trend['1h']} and 15M {c.trend['15m']} (15M ADX {adx15:.1f}); the 15M EMA20 is {word} ({sl15['ema20']:+.1f} pts over 5 candles), {stack15:.0f} candles {v.over} the EMA50",
        f"a {leg_high - floor:.1f} pt leg ({(leg_high - floor) / atr15:.1f}x ATR15) from {v.px(floor)} to {v.px(leg_high)}",
    ]  # fmt: skip
    if mc:
        reasons.append(f"paused {depth:.1f} pts ({depth / atr15:.2f}x ATR15) below the {v.peak}, low {v.px(rev.extreme)}, {v.over} the 5M EMA20 zone ({v.px(e5_20)}): a pause, not a pullback")
    else:
        reasons.append(f"dipped {depth:.1f} pts ({depth / atr15:.2f}x ATR15) to the 5M EMA20 {v.px(e5_20)} (low {v.px(rev.extreme)}) and held {v.over} the EMA50 {v.px(e5_50)}")
    resist = [lv.price for lv in v.levels]
    scan = build(v, p, prior, name, key, bar.start, floor, "leg origin", rev, reasons, [], resist,
                 f"price {'pauses' if mc else 'dips to the 5M EMA20'} and a completed 5M {v.dirn} candle closes through the prior {v.peak} {v.px(leg_high)}",
                 {lv.price: lv.label for lv in v.levels})  # fmt: skip
    s = scan.setup
    if s is not None:
        s.group = f"leg:{v.side}:{int(bar.start)}"  # the same leg is described once, whichever of the two continuation strategies saw it first
        if rev.trigger is not None and s.status not in (INVALIDATED, EXPIRED):
            need = f"a body of at least {p.momentum_body_atr5:g}x ATR5" if mc else f"a body of at least {p.confirm_body_atr5:g}x ATR5 (a rejection candle at the low, or {p.momentum_body_atr5:g}x ATR5)"
            s.confirm_text = (f"a completed 5M {v.dirn} candle closes {'above' if v.sgn > 0 else 'below'} {v.px(rev.trigger)} (through the prior {v.peak}) with {need}, "
                              "closing near its extreme" + ("; the next completed 5M candle must also close beyond it (stricter confirmation)" if c.strict else ""))  # fmt: skip
    return scan


def scan_momentum(v: View, p: Params, prior) -> Scan:
    return scan_continuation(v, p, prior, "momentum")


def scan_ema_continuation(v: View, p: Params, prior) -> Scan:
    return scan_continuation(v, p, prior, "ema")


# -- Volatility Expansion ---------------------------------------------------------------------------------------------------------------
def scan_expansion(v: View, p: Params, prior) -> Scan:
    """A quiet 5M compression box (ve_box_bars candles inside ve_box_max_atr5 x ATR5), then a wide-range, strong, completed 5M candle closing out of it, in a
    direction the 1H and 15M do not oppose. The trigger is the box edge, the invalidation the middle of the box (a close back there means the break failed), and
    the entry area, the "do not chase" limit, the risk-distance and room checks are the ones every other strategy has (strategy.build). In stricter confirmation
    (high volatility, midday) the next completed candle must also close beyond the edge."""
    c, name = v.ctx, "volatility_expansion"
    if "unknown" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, "not enough completed 1H / 15M candles to judge the direction")
    if "bearish" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, f"1H {c.trend['1h']} / 15M {c.trend['15m']}: a {v.dirn} expansion would go against the higher-timeframe direction")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    n, b5 = p.ve_box_bars, v.b5
    if len(b5) < n + 1:
        return Scan(True, f"not enough completed 5M candles for a {n}-candle compression box")

    def box(k):
        bx = b5[k - n : k]
        return bx, max(b.h for b in bx), min(b.l for b in bx)

    def expansion(e, hi):
        return e.bull and e.c > hi and e.rng >= p.ve_range_atr5 * atr5 and e.body >= p.momentum_body_atr5 * atr5 and e.close_loc >= p.confirm_close_loc

    k = None
    if prior is not None and prior.strategy == name and prior.side == v.side:
        k = next((i for i, b in enumerate(b5) if b.start == prior.anchor_time and i >= n), None)
    weak = None
    if k is None:
        for i in range(max(n, len(b5) - 1 - p.ve_max_bars), len(b5)):
            bx, hi, lo = box(i)
            if hi - lo > p.ve_box_max_atr5 * atr5 or not b5[i].c > hi or b5[i].end <= c.confirm_after:
                continue
            if expansion(b5[i], hi):
                k = i
                break
            weak = weak or (i, hi)
    if k is None:
        bx, hi, lo = box(len(b5))
        if weak is not None:
            e = b5[weak[0]]
            return Scan(True, f"the {hm(e.start)} candle closed above the compression box {v.px(weak[1])} but is not an expansion candle (range {e.rng / atr5:.1f}x ATR5, needs "
                              f"{p.ve_range_atr5:g}x; body {e.body / atr5:.1f}x, needs {p.momentum_body_atr5:g}x)")  # fmt: skip
        if hi - lo <= p.ve_box_max_atr5 * atr5:
            return Scan(True, f"compression: the last {n} candles ({hm(bx[0].start)}-{hm(bx[-1].end)}) are inside {hi - lo:.1f} pts ({(hi - lo) / atr5:.1f}x ATR5): "
                              f"waiting for a wide-range candle to close {v.over} {v.px(hi)}")  # fmt: skip
        return Scan(True, f"no compression box: the last {n} candles span {hi - lo:.1f} pts ({(hi - lo) / atr5:.1f}x ATR5, a box is at most {p.ve_box_max_atr5:g}x)")
    bx, hi, lo = box(k)
    e = b5[k]
    key = f"{name}:{v.side}:{int(e.start)}"
    trig, mid, chase = hi, (hi + lo) / 2, p.entry_chase_atr5 * atr5
    inv, zone = mid - p.inv_buffer_atr5 * atr5, (trig, trig + chase)
    evidence = (f"expansion candle {hm(e.start)}: range {e.rng / atr5:.1f}x ATR5, body {e.body / atr5:.1f}x ATR5, closed {e.close_loc:.0%} of the way {'up' if v.sgn > 0 else 'down'} its range, "
                f"out of a {n}-candle compression box")  # fmt: skip
    n_c = k
    if c.strict:  # the next completed candle must hold the break too
        if k + 1 >= len(b5):
            n_c = None
        elif not b5[k + 1].c > trig:
            return Scan(True, f"no follow-through: the candle after the expansion closed back {v.under} {v.px(trig)} (stricter confirmation applies: {c.strict_why})")
        else:
            n_c = k + 1
    if n_c is None:
        rev = Rev(WAITING, "", trig, inv, zone, evidence, None, lo, bx[0].start)
    else:
        cb = b5[n_c]

        def result(status, note):
            return Rev(status, note, trig, inv, zone, evidence, cb, lo, bx[0].start, e)

        rev = None
        for b in b5[n_c + 1 :]:
            if b.c < inv:
                rev = result(INVALIDATED, f"a 5M candle closed {v.under} the invalidation level {v.px(inv)} (the middle of the compression box)")
                break
            if b.c < trig - p.failed_follow_atr5 * atr5:
                rev = result(INVALIDATED, f"failed follow-through: a 5M candle closed back {v.under} the trigger {v.px(trig)}")
                break
        if rev is None:
            top = max(b.c for b in b5[n_c:])
            if top - trig > chase:
                rev = result(EXPIRED, f"price closed {top - trig:.1f} pts ({(top - trig) / atr5:.1f}x ATR5) beyond the trigger: the entry opportunity has passed")
            elif len(b5) - 1 - n_c >= p.confirm_valid_bars:
                rev = result(EXPIRED, "the entry window after the confirmation has passed")
            else:
                rev = result(CONFIRMED, "")
    reasons = [
        f"1H {c.trend['1h']} and 15M {c.trend['15m']}: the expansion is not against the higher timeframes",
        f"compression: {n} candles {hm(bx[0].start)}-{hm(bx[-1].end)} inside {hi - lo:.1f} pts ({(hi - lo) / atr5:.1f}x ATR5; a box is at most {p.ve_box_max_atr5:g}x)",
    ]  # fmt: skip
    scan = build(v, p, prior, name, key, e.start, hi, "compression box high", rev, reasons, [], [lv.price for lv in v.levels],
                 f"a completed 5M {v.dirn} candle with a range of at least {p.ve_range_atr5:g}x ATR5 closes {v.over} the compression box {v.px(hi)}", {lv.price: lv.label for lv in v.levels})  # fmt: skip
    if scan.setup is not None and rev.status not in (INVALIDATED, EXPIRED):
        scan.setup.confirm_text += f"; the invalidation is the middle of the box, {v.px(inv)}"
    return scan


# Registration order is the scan and display order. Priority is the documented tie-break between two ready setups in the same direction (lower wins); the
# two continuation strategies can never both be ready on one dip, so their relative order only matters against the other strategies.
register(StrategyDef("momentum_continuation", "Momentum Continuation", scan_momentum, WITH_TREND, priority=11))
register(StrategyDef("ema_trend_continuation", "EMA Trend Continuation", scan_ema_continuation, WITH_TREND, priority=10))
register(StrategyDef("volatility_expansion", "Volatility Expansion", scan_expansion, WITH_TREND, priority=12))
