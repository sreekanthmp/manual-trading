"""Opening Range Breakout (Stage 2.2).

The opening range is the 09:15-09:30 range (three 5M candles, `trading.opening_range`); it is observed, not traded. After it, a completed 5M candle closing OUT of it
with a real body, near its extreme, is the breakout. Written like the other strategies: once, for the bullish case, run on a `View` for the bearish case, and every
setup goes through `strategy.build`, which is where the data, session, volatility, risk-distance and room checks are applied: nothing here can skip them.

    trigger ....... the range edge (a completed 5M close beyond it, body of at least confirm_body_atr5 x ATR5, closing in the top 40% of its range). In stricter
                    confirmation (high volatility) the next completed candle must also close beyond the edge.
    invalidation .. the MIDDLE of the opening range, less the usual quarter of ATR5. Before the breakout is confirmed, a close beyond the OPPOSITE edge ends it
                    (the range broke the other way first); wandering inside the range does not, because nothing has been entered yet. Once it is confirmed, a close
                    back through the invalidation, or back under the trigger (failed follow-through), ends it, like every other strategy.
    too risky ..... a new setup whose trigger-to-invalidation distance breaks the existing risk limits (under min_risk_atr5 x ATR5: noise; over max_risk_atr15 x ATR15:
                    too far from the structure; both from `strategy.location_notes`) is not started at all, and says why. Room to the next level is judged, as always, by `build`.
    window ........ the confirming candle must be complete by `orb_end` (11:00). A setup that has not confirmed by then ends; one that has carries on through its
                    ordinary entry window.
    one per side .. a setup is anchored to the range (the key carries the 09:15 start), so there is at most one opening range breakout per side per day: a failed
                    break is not retried; the opposite side's trap is Failed Breakout's, not this strategy's.

It follows the same side rule as Breakout and Retest: 1H and 15M must not be against the side. Where the 15M closes through the same range edge (the opening range
high is also a level Breakout and Retest watches), the two are different entries into one move (the immediate break, then the retest); if both are ready at once the
decision lists the second as "also matches" in the usual way."""

from ..candles import day_start
from ..registry import SESSION, StrategyDef, register
from ..strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Params, Rev, Scan, View, build, clock_seconds, hm, location_notes, num

OR_SECONDS = 900  # three 5M candles: the window trading.opening_range measures


def orb_state(v: View, p: Params, bars: list, trig: float, inv: float, low: float, anchor: float, end_ts: float) -> "Rev | None":
    """The lifecycle of an opening range breakout, replayed in time order over `bars` (the completed 5M candles after the opening range, bullish view). `low` is
    the opposite edge of the range. None = nothing to track yet (price has not come to the edge)."""
    atr5, c = v.atr["5m"], v.ctx
    chase = p.entry_chase_atr5 * atr5
    zone, over = (trig, trig + chase), c.now >= end_ts
    elig = [b for b in bars if b.end <= end_ts]  # candles that may start or confirm a breakout (a prefix of `bars`)
    edge = "high" if v.sgn > 0 else "low"

    def solid(b):
        return b.bull and b.c > trig and b.body >= p.confirm_body_atr5 * atr5 and b.close_loc >= p.confirm_close_loc

    def ended(status, note):
        return Rev(status, note, trig, inv, None, None, None, low, anchor)

    def evidence(b):
        return (f"breakout candle {hm(b.start)}: closed {b.c - trig:.1f} pts {v.over} the opening range {edge} {v.px(trig)}, body {b.body / atr5:.1f}x ATR5, "
                f"closed {b.close_loc:.0%} of the way {'up' if v.sgn > 0 else 'down'} its range")  # fmt: skip

    n_c = brk = pending = weak = held = None
    for i, b in enumerate(elig):
        if b.c < low:
            return ended(INVALIDATED, f"a 5M candle closed {v.under} the opposite edge of the opening range {v.px(low)}: the range broke the other way first")
        if b.c > trig + chase and not solid(b):
            return ended(EXPIRED, f"missed: a 5M candle closed {b.c - trig:.1f} pts ({(b.c - trig) / atr5:.1f}x ATR5) {v.over} the opening range {edge} without being a "
                                  "confirming candle: the entry opportunity has passed")  # fmt: skip
        if not solid(b):
            weak = weak or (b if b.c > trig else None)
            continue
        if not c.strict:
            n_c, brk = i, b
            break
        nxt = elig[i + 1] if i + 1 < len(elig) else None
        if nxt is None:
            pending = b  # the break candle is in, its follow-through is not
        elif nxt.c > trig:
            n_c, brk = i + 1, b
            break
        else:
            held = b  # ... and the next candle did not hold it
    if n_c is None:
        if over:
            return ended(EXPIRED, f"the opening range breakout was not confirmed by {p.orb_end}: it is not an opening range trade any more")
        if pending is not None:
            return Rev(WAITING, "", trig, inv, zone, evidence(pending), None, low, anchor)
        note = ""
        if held is not None:
            note = f"the {hm(held.start)} candle broke out but the next candle did not close {v.over} {v.px(trig)}: no follow-through (stricter confirmation applies: {c.strict_why})"
        elif weak is not None:
            note = (f"the {hm(weak.start)} candle closed {v.over} the opening range {edge} but is not a confirming candle (body {weak.body / atr5:.1f}x ATR5, needs "
                    f"{p.confirm_body_atr5:g}x; closed {weak.close_loc:.0%} of its range, needs {p.confirm_close_loc:.0%})")  # fmt: skip
        elif not any(b.h >= trig - p.orb_probe_atr5 * atr5 for b in elig):
            return None
        return Rev(DETECTED, note, trig, inv, zone, None, None, low, anchor)
    cb = elig[n_c]

    def result(status, note):
        return Rev(status, note, trig, inv, zone, evidence(brk), cb, low, anchor, brk)

    for b in bars[n_c + 1 :]:
        if b.c < inv:
            return result(INVALIDATED, f"a 5M candle closed {v.under} the invalidation level {v.px(inv)} (the middle of the opening range)")
        if b.c < trig - p.failed_follow_atr5 * atr5:
            return result(INVALIDATED, f"failed follow-through: a 5M candle closed back {v.under} the trigger {v.px(trig)}")
    top = max(b.c for b in bars[n_c:])
    if top - trig > chase:
        return result(EXPIRED, f"price closed {top - trig:.1f} pts ({(top - trig) / atr5:.1f}x ATR5) beyond the trigger: the entry opportunity has passed")
    if len(bars) - 1 - n_c >= p.confirm_valid_bars:
        return result(EXPIRED, "the entry window after the confirmation has passed")
    return result(CONFIRMED, "")


def scan_orb(v: View, p: Params, prior) -> Scan:
    c, name = v.ctx, "opening_range_breakout"
    if "unknown" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, "not enough completed 1H / 15M candles to judge the direction")
    if "bearish" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, f"1H {c.trend['1h']} / 15M {c.trend['15m']}: a {v.dirn} opening range breakout would go against the higher-timeframe direction")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    day = day_start(c.now)
    day_open = day + clock_seconds(p.open_time)
    or_end, end_ts = day_open + OR_SECONDS, day + clock_seconds(p.orb_end)
    orng = c.opening_range
    if orng is None or not orng["complete"]:
        if c.now < or_end + 10:
            return Scan(True, f"the opening range ({hm(day_open)}-{hm(or_end)}) is still forming: it is observed, not traded")
        return Scan(True, f"the opening range ({hm(day_open)}-{hm(or_end)}) is incomplete (candles are missing): no opening range breakout today")
    hi, lo = (orng["high"], orng["low"]) if v.sgn > 0 else (-orng["low"], -orng["high"])  # the view's range: the edge to break and the opposite edge
    width, mid = hi - lo, (hi + lo) / 2
    edge = "high" if v.sgn > 0 else "low"
    trig, inv = hi, mid - p.inv_buffer_atr5 * atr5
    key = f"{name}:{v.side}:{int(day_open)}"
    span = f"{num(orng['low'])}-{num(orng['high'])}"
    if not (prior is not None and prior.key == key):  # a setup that is already being tracked keeps the risk it was started with
        notes = location_notes(v, p, trig, inv, [])[2]
        if notes:
            return Scan(True, f"opening range {span}, {width / atr15:.1f}x ATR15: the middle of the range as the invalidation is not a usable risk — " + "; ".join(notes))
    bars = [b for b in v.b5 if b.start >= or_end and b.end > c.confirm_after]
    rev = orb_state(v, p, bars, trig, inv, lo, day_open, end_ts)
    if rev is None:
        gap = None if v.price is None else trig - v.price
        where = "price is not available" if gap is None else f"price is {gap:.1f} pts {v.under} its {edge}" if gap > 0 else f"price is {v.over} its {edge}, but no completed candle has closed there yet"
        return Scan(True, f"{where} (opening range {span}): waiting for a completed 5M candle to close {v.over} {v.px(trig)}")
    reasons = [
        f"opening range {hm(day_open)}-{hm(or_end)}: {span} ({width:.1f} pts, {width / atr15:.1f}x ATR15)",
        f"1H {c.trend['1h']} and 15M {c.trend['15m']}: the breakout is not against the higher timeframes",
        f"initial invalidation: the middle of the opening range {v.px(mid)} less {p.inv_buffer_atr5:g}x ATR5 = {v.px(inv)}",
    ]  # fmt: skip
    scan = build(v, p, prior, name, key, day_open, trig, f"opening range {edge}", rev, reasons, [], [lv.price for lv in v.levels],
                 f"a completed 5M {v.dirn} candle closes {v.over} the opening range {edge} {v.px(trig)}", {lv.price: lv.label for lv in v.levels})  # fmt: skip
    if scan.setup is not None and rev.status not in (INVALIDATED, EXPIRED):
        scan.setup.confirm_text += f"; the invalidation is the middle of the opening range ({v.px(mid)}) less a buffer, {v.px(inv)}"
    return scan


register(StrategyDef("opening_range_breakout", "Opening Range Breakout", scan_orb, SESSION, priority=13))
