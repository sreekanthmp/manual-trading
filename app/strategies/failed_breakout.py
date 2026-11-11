"""Failed Breakout / Trap Reversal (Stage 2.2).

A level breaks (a completed 5M candle closes beyond it), the break does not hold (a later candle closes back through the level within a few candles), and the
trapped traders' stops fuel the move back. Written like the other strategies: once, for the bullish case (a failed BREAKDOWN, long), run on a `View` for the bearish
case (a failed BREAKOUT, short), and every setup goes through `strategy.build`, which is where the data, session, volatility, risk-distance and room checks are
applied: nothing here can skip them.

    level ......... any level the engine already knows (swing levels, previous day high / low, the 15M range edges, the opening range edges once it is complete),
                    that today's trading has been on the other side of: a level price has only ever been under is not "broken" by being under it
    the trap ...... a completed 5M candle closes beyond the level by break_margin_atr15 x ATR15, and the first candle that closes back through it does so within
                    fbtr_max_bars candles (a slower recovery is just a recovery); the push beyond the level never exceeds fail_depth_atr15 x ATR15 (further is a
                    genuine break and ends the setup, the same line Range Reversal draws)
    trigger ....... the highest high of the base since the low, and never below the reclaimed level: the confirming candle must close above both, with a real body
                    near its extreme (strategy.build's confirming-candle rules; stricter mode needs the follow-through candle as well)
    invalidation .. the low of the failed break, less the usual buffer. A close back under it, or back under the trigger (failed follow-through), ends the setup.

Which side may be taken is the original rule, with no exception: 1H and 15M must not be against the side (so a failed breakdown is only a long where the higher
timeframes are not bearish). In a market that is range-bound by Range Reversal's own definition (1H and 15M both sideways, alignment range_bound) this strategy
stands aside: the failed breaks of a range's edges are that strategy's, and one movement is never announced by both.

A failed breakdown that is also a Trend Pullback or EMA Trend Continuation dip is not the same entry (different trigger, different evidence): if two of them are ready
at once at the same place, the decision lists the second as "also matches" in the usual way."""

from ..candles import day_start
from ..registry import WITH_TREND, StrategyDef, register
from ..strategy import EXPIRED, INVALIDATED, Level, Params, Scan, View, build, clock_seconds, dead, hm, tracked_extreme
from .trend_family import continuation_state


def find_traps(v: View, p: Params, b5: list, first: int, margin: float) -> tuple:
    """Every failed break of a known level whose first close beyond it is at or after candle index `first` (bullish view): [(reclaim index, level price, break
    index, Level)], plus (break index, note) for the breaks that are not a failed one (yet). Several candles beyond the level before the one that takes it back
    are one trap, counted from the earliest of them that is still close enough to the reclaim."""
    traps, notes = [], []
    for lv in v.levels:
        above = next((i for i, b in enumerate(b5) if b.c >= lv.price), None)  # a level can only be broken downward after today's trading has been at or above it
        if above is None:
            continue
        taken = set()
        for g in (i for i in range(max(first, above + 1), len(b5)) if b5[i].c < lv.price - margin):
            r = next((j for j in range(g + 1, len(b5)) if b5[j].c > lv.price), None)
            if r is None:
                notes.append((g, f"a 5M candle closed {v.under} {lv.label} {v.px(lv.price)} at {hm(b5[g].start)} and has not closed back {v.over} it: a break in progress, not a failed one"))
            elif r - g > p.fbtr_max_bars:
                notes.append((g, f"{lv.label} {v.px(lv.price)} broke at {hm(b5[g].start)} and was only taken back {r - g} candles later (over {p.fbtr_max_bars}): a slow recovery, not a trap"))
            elif r not in taken:
                taken.add(r)
                traps.append((r, lv.price, g, lv))
    return traps, notes


def scan_failed_breakout(v: View, p: Params, prior) -> Scan:
    c, name = v.ctx, "failed_breakout"
    if "unknown" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, "not enough completed 1H / 15M candles to judge the direction")
    if "bearish" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, f"1H {c.trend['1h']} / 15M {c.trend['15m']}: a {v.dirn} reversal would go against the higher-timeframe direction")
    if c.alignment == "range_bound" and c.trend["1h"] == "sideways" and c.trend["15m"] == "sideways":
        return Scan(False, "the market is range-bound: the failed breaks of a range's edges are Range Reversal's")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    day_open = day_start(c.now) + clock_seconds(p.open_time)
    b5 = [b for b in v.b5 if b.start >= day_open]  # today's session only: a close "beyond a level" across the overnight gap is not a trap
    if len(b5) < 2:
        return Scan(True, "not enough completed 5M candles today")
    margin = p.break_margin_atr15 * atr15
    g = r = lvl = None
    if prior is not None and prior.strategy == name and prior.side == v.side and prior.level is not None:  # a tracked setup stays on the trap it was found at
        g = next((i for i, b in enumerate(b5) if b.start == prior.anchor_time), None)
        if g is not None:
            level = v.sgn * prior.level
            lvl = next((x for x in v.levels if abs(x.price - level) < 1e-6), Level(level, prior.level_label))
            r = next((j for j in range(g + 1, len(b5)) if b5[j].c > level), None)
    if g is None or r is None:
        g = r = lvl = None
        traps, notes = find_traps(v, p, b5, max(0, len(b5) - p.fbtr_window_bars), margin)
        if not traps:
            return Scan(True, max(notes)[1] if notes else f"no 5M close {v.under} a known level in the last {p.fbtr_window_bars} candles: nothing has been trapped")
        r, _, g, lvl = max(traps, key=lambda t: (t[0], t[1]))  # the most recent trap; the higher level if two were taken back by one candle
    level = lvl.price
    key = f"{name}:{v.side}:{int(b5[g].start)}"
    bars = b5[g:]
    word = "breakdown" if v.sgn > 0 else "breakout"
    story = (f"failed {word} of {lvl.label} {v.px(level)}: a 5M candle closed {v.under} it at {hm(b5[g].start)} and one closed back {v.over} it at {hm(b5[r].start)} "
             f"({r - g} candle{'s' if r - g != 1 else ''} later)")  # fmt: skip
    rev = continuation_state(v, p, bars, level, lambda x: level - x <= p.fail_depth_atr15 * atr15, min_pause=1, max_bars=p.max_bars_after_touch,
                             use_rejection=False, keep=tracked_extreme(v, prior, key), hint=story)  # fmt: skip
    if rev is None:
        low = min(b.l for b in bars)
        return dead(v, name, key, b5[g].start, INVALIDATED, f"price pushed {level - low:.1f} pts ({(level - low) / atr15:.1f}x ATR15) {v.under} {lvl.label} {v.px(level)}: "
                                                              f"over {p.fail_depth_atr15:g}x ATR15 is a genuine break, not a failed one")  # fmt: skip
    depth = level - rev.extreme
    reasons = [
        f"1H {c.trend['1h']} and 15M {c.trend['15m']}: the reversal is not against the higher timeframes",
        f"the {word} went {depth:.1f} pts ({depth / atr15:.2f}x ATR15) {v.under} the level (low {v.px(rev.extreme)}), no further than {p.fail_depth_atr15:g}x ATR15",
    ]  # fmt: skip
    scan = build(v, p, prior, name, key, b5[g].start, level, lvl.label, rev, reasons, [], [lv.price for lv in v.levels],
                 f"a completed 5M {v.dirn} candle closes {v.over} the base of the reversal and the reclaimed level {v.px(level)}", {lv.price: lv.label for lv in v.levels})  # fmt: skip
    s = scan.setup
    if s is not None and rev.trigger is not None and s.status not in (INVALIDATED, EXPIRED):
        s.confirm_text = (f"a completed 5M {v.dirn} candle closes {v.over} {v.px(rev.trigger)} (the base of the reversal, never below the reclaimed {lvl.label} {v.px(level)}) "
                          f"with a body of at least {p.confirm_body_atr5:g}x ATR5, closing near its extreme" + ("; the next completed 5M candle must also close beyond it (stricter confirmation)" if c.strict else ""))  # fmt: skip
    return scan


register(StrategyDef("failed_breakout", "Failed Breakout (Trap Reversal)", scan_failed_breakout, WITH_TREND, priority=14))
