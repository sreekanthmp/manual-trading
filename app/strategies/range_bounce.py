"""Range Bounce (Stage 2.3).

A bounce off the edge of the 15M compression box (`Context.consolidation["15m"]`: the last 12 completed 15M candles inside 3x ATR15) while the 1H trend pauses
(alignment `pausing`: the 1H trending, the 15M sideways), and only in the trend's direction: the lower edge in an uptrend (calls), the upper edge in a downtrend (puts).
Written like the other strategies: once, for the bullish case, run on a `View` for the bearish case, and every setup goes through `strategy.build`, which is where the
data, session, volatility, risk-distance and room checks are applied: nothing here can skip them.

    the edge ...... the box low (bullish view), and the first level the room rule counts is the box high
    the approach .. the same one Range Reversal uses (`approach_run`: price came within approach_zone_atr15 x ATR15 of the edge, and not longer ago than approach_stale_bars),
                    with a low no higher than level_tol_atr15 x ATR15 above the edge. It may pierce the edge with a wick, never by more than fail_depth_atr15 x ATR15
    a hold ........ no completed 5M candle closes beyond the edge by break_margin_atr15 x ATR15. A close beyond it is a break, or a failed break, and a failed
                    breakdown that is taken back is Failed Breakout's, so this strategy stands aside; a candle that does that to a bounce that was already being tracked
                    ends it
    confirmation .. strategy.reversal_state's: a completed 5M candle closing above the base of the bounce, with a rejection candle at the low or a strong candle,
                    body of at least confirm_body_atr5 x ATR5, closing near its extreme (stricter mode: the next candle must also close beyond it)
    invalidation .. the low of the bounce, less the usual buffer

Its thresholds are the existing ones (nothing new in `Params`): the approach, the minimum box height (min_range_atr15) and the push depth are Range Reversal's, because both
trade the edge of a box.

What it is NOT, and how one movement is kept to one strategy:
* Range Reversal needs both timeframes sideways; this needs the 1H trending. The regimes never overlap.
* Failed Breakout needs a close beyond the level; this needs none (above).
* Trend Pullback already takes a pullback to a level in a trend, and the box low is a level it knows: a bounce at the box low in an uptrend with a 15M swing structure
  is usually ITS setup (the audit that proposed this strategy missed that, and a scenario in tests/ shows it). Trend Pullback is not changed, so this strategy defers to it:
  it starts nothing while Trend Pullback holds the dip (any setup of its own except one that failed), and ends a tracked bounce when Trend Pullback takes the same low.
  What is left is what Trend Pullback cannot see: a dip that went under the leg's origin (the last 15M swing low, which in a box is usually higher than the box low,
  so Trend Pullback calls the structure broken while the box still holds), a wick through the edge by more than the touch tolerance (the low is then "not at a
  meaningful level" for it), and a market with no usable leg."""

from ..candles import day_start
from ..registry import RANGE, StrategyDef, register
from ..strategy import (
    CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Params, Scan, View, approach_run, build, clock_seconds, dead, hm, num, reversal_state, scan_pullback, tracked_extreme,
)

PULLBACK = "trend_pullback"
LIVE_STATUS = (DETECTED, WAITING, CONFIRMED)


def scan_bounce(v: View, p: Params, prior) -> Scan:
    c, name = v.ctx, "range_bounce"
    if c.alignment != "pausing" or v.trend["1h"] != "bullish" or v.trend["15m"] != "sideways":
        return Scan(False, f"1H {c.trend['1h']} / 15M {c.trend['15m']} ({c.alignment}): a range bounce needs a {v.dirn} 1H trend with the 15M sideways (alignment pausing)")
    if v.pattern.get("15m") == "LH_LL":
        return Scan(False, f"15M structure ({c.pattern['15m']}) does not support a {v.dirn} move")
    live = [tf for tf in ("1h", "15m") if c.breakout.get(tf)]
    if live:
        return Scan(False, f"Phase 2 flags a {live[0].upper()} {c.breakout[live[0]]}: a {c.breakout[live[0]]} is in progress, not a box")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15 or v.price is None:
        return Scan(True, "ATR or price is not available yet")
    tol, margin = p.level_tol_atr15 * atr15, p.break_margin_atr15 * atr15
    tracked = prior is not None and prior.strategy == name and prior.side == v.side and prior.level is not None
    box = v.cons15
    if tracked:
        S, R = v.sgn * prior.level, None if box is None else box[1]  # a tracked bounce keeps the edge it was found at, even if the box has since moved or dissolved
    else:
        if box is None:
            return Scan(True, "no 15M compression box (the last 12 candles are not inside 3x ATR15): there is no edge to bounce from")
        S, R = box
        if R - S < p.min_range_atr15 * atr15:
            return Scan(True, f"the 15M box {v.px(S)}-{v.px(R)} is only {(R - S) / atr15:.1f}x ATR15 high (under {p.min_range_atr15:g}x): too small for a bounce to have room")
    low_word, high_word = ("low", "high") if v.sgn > 0 else ("high", "low")  # the edge being bounced from, and the far one, in real prices
    # Trend Pullback owns a dip it can take. This asks it, with its own live setup as its prior, so the answer is exactly what it says this very update.
    tp = scan_pullback(v, p, v.live.get((PULLBACK, v.side))).setup
    if tracked:
        if tp is not None and tp.status in LIVE_STATUS and tp.extreme is not None and prior.extreme is not None and abs(v.sgn * (tp.extreme - prior.extreme)) <= tol:
            return dead(v, name, prior.key, prior.anchor_time, EXPIRED, f"Trend Pullback now holds the same low {num(tp.extreme)}: the dip is announced once, by it")
    elif tp is not None and tp.status != INVALIDATED:
        return Scan(True, f"Trend Pullback already has this dip ({tp.status.lower().replace('_', ' ')}): a bounce it takes is announced once, by it")
    day_open = day_start(c.now) + clock_seconds(p.open_time)
    b5 = [b for b in v.b5 if b.start >= day_open]  # today's session only
    run = approach_run(b5, S + p.approach_zone_atr15 * atr15, p.approach_gap_bars)
    if run is None or len(b5) - 1 - run[1] > p.approach_stale_bars:
        loc = None if R is None or R <= S else (v.price - S) / (R - S)
        if loc is not None and loc > p.range_edge_fraction:
            return Scan(True, f"price is {loc:.0%} of the way across the 15M box: the middle of a box offers no edge")
        return Scan(True, f"price has not come back to the 15M box {low_word} {v.px(S)} recently")
    bars = b5[run[0] :]
    key = f"{name}:{v.side}:{int(bars[0].start)}"
    extreme = min(b.l for b in bars)
    if any(x.c < S - tol for x in v.b15 if x.start >= bars[0].start) or extreme < S - p.fail_depth_atr15 * atr15:
        return dead(v, name, key, bars[0].start, INVALIDATED, f"price broke through the box {low_word} {v.px(S)}: a genuine break, not a bounce")
    beyond = [b for b in bars if b.c < S - margin]
    if beyond:
        if tracked and prior.key == key:
            return dead(v, name, key, bars[0].start, INVALIDATED, f"a 5M candle closed {S - beyond[-1].c:.1f} pts {v.under} the box {low_word} {v.px(S)} at {hm(beyond[-1].start)}: the bounce failed")
        return Scan(True, f"a 5M candle closed {v.under} the box {low_word} {v.px(S)} at {hm(beyond[-1].start)}: a close beyond the edge is a break or a failed break (Failed Breakout's), not a bounce")
    rev = reversal_state(v, p, bars, lambda x: x <= S + tol, keep=tracked_extreme(v, prior, key))
    if rev is None:
        return Scan(True, f"price is near the 15M box {low_word} {v.px(S)} but has not touched it ({v.trough} {v.px(extreme)})")
    gap, adx1 = rev.extreme - S, v.adx.get("1h")
    reasons = [
        f"1H {c.trend['1h']}{'' if adx1 is None else f' (ADX {adx1:.1f})'} while the 15M is sideways: the {v.dirn} trend is pausing, the bounce goes with it",
        f"15M compression box {num(min(v.real(S), v.real(R)))}-{num(max(v.real(S), v.real(R)))} ({(R - S) / atr15:.1f}x ATR15)" if R is not None else
        f"the 15M box {low_word} {v.px(S)}",  # fmt: skip
        f"price came back to the box {low_word} {v.px(S)} at {hm(rev.low_start)} ({v.trough} {v.px(rev.extreme)}, {abs(gap):.1f} pts {v.over if gap >= 0 else v.under} it) and did not close beyond it",
    ]  # fmt: skip
    resist = [lv.price for lv in v.levels] + ([] if R is None else [R])
    labels = {**{lv.price: lv.label for lv in v.levels}, **({} if R is None else {R: f"15M box {high_word}"})}
    return build(v, p, prior, name, key, bars[0].start, S, f"15M box {low_word}", rev, reasons, [], resist,
                 f"price bounces off the 15M box {low_word} {v.px(S)} and a completed 5M {v.dirn} candle closes beyond the base of the bounce", labels)  # fmt: skip


register(StrategyDef("range_bounce", "Range Bounce", scan_bounce, RANGE, priority=15))
