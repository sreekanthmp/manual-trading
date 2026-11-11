"""Trading setup detection (Phase 3): three strategies that read the Phase 2 analysis and the completed
candles. Pure functions: a setup is re-derived from the data on every call (the lifecycle bookkeeping lives
in trading.py). The only memory a scan takes is the setup it is already tracking, and only for facts that
must not move after the fact: the level that broke, the edge of the range, the low a setup rests on.

Every strategy is written once, for the bullish case, and run on a *view* of the market with prices
negated for the bearish case, so CE and PE rules are symmetrical by construction.

Rules in one paragraph. A setup needs a market regime that suits the strategy (Phase 2 trend and
alignment), a structure that is still intact, a price location that is meaningful (a value zone, a
broken level, a range edge) and then evidence of buyers (or sellers) in the form of a *completed* 5M
candle that closes beyond the base of the move. The forming candle never confirms anything. Location
(how far the invalidation is, how much room there is to the next level), data completeness and the
trading session can hold a setup back; they never turn it into a signal. None of the numbers below was
tuned against data; they are initial, documented assumptions and can be changed in `Params`."""

import os
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import NamedTuple

from . import registry
from .candles import IST
from .registry import NAMES, RANGE, WITH_TREND, StrategyDef  # noqa: F401  (NAMES: id -> title of every registered strategy, kept importable from here; see registry.py)

DETECTED, WAITING, CONFIRMED = "DETECTED", "WAITING_FOR_CONFIRMATION", "CONFIRMED"
INVALIDATED, EXPIRED = "INVALIDATED", "EXPIRED"
RANK = {DETECTED: 0, WAITING: 1, CONFIRMED: 2}
OPTION = {"bull": "CE", "bear": "PE"}
DIRECTION = {"bull": "bullish", "bear": "bearish"}


@dataclass(frozen=True)
class Params:
    """Every threshold in one place. Override with environment variables STRATEGY_<NAME> (see from_env)."""

    # session (IST, HH:MM)
    open_time: str = "09:15"
    observe_until: str = "09:30"  # opening volatility: the opening range forms, no entries yet
    midday_start: str = "12:00"  # thin participation: stricter confirmation
    midday_end: str = "13:30"
    last_entry: str = "14:45"  # no new entries from here on
    close_time: str = "15:30"
    opening_range_levels: bool = True  # the opening range high/low count as levels once it is complete
    # data
    max_price_age: float = 20.0  # seconds without a tick before the live price is considered stale
    completeness_bars_5m: int = 12  # recent candles that must be complete (today's session only)
    completeness_bars_15m: int = 8
    # volatility
    vol_extreme: float = 2.0  # 15M ATR this many times its average: stay out
    # geometry, in ATR multiples
    level_tol_atr15: float = 0.25  # how close counts as "at" a level
    inv_buffer_atr5: float = 0.25  # invalidation sits this far beyond the structural extreme
    entry_chase_atr5: float = 1.0  # depth of the entry area beyond the trigger; further = extended
    min_risk_atr5: float = 0.75  # trigger-to-invalidation distance bounds
    max_risk_atr15: float = 1.5
    min_room_risk: float = 1.0  # room to the next level, in multiples of that distance
    rsi_caution: float = 70.0  # 15M RSI (in the trade's direction) at or above this adds a caution; it never blocks. Equals analysis.RSI_STRETCHED[0]
    # the confirming candle (5M)
    rejection_wick: float = 0.4  # lower wick as a share of the candle's range
    rejection_window: int = 3  # a rejection candle must be one of the first N candles at the low
    confirm_body_atr5: float = 0.4
    momentum_body_atr5: float = 0.7  # a candle this strong counts as renewed momentum by itself
    confirm_close_loc: float = 0.6  # must close in the upper 40% of its range
    confirm_valid_bars: int = 4  # a confirmation stays actionable for this many completed 5M candles
    failed_follow_atr5: float = 0.25  # closing this far back below the trigger = failed follow-through
    max_bars_after_touch: int = 18  # 5M candles allowed between the low/retest and a confirmation
    # lifecycle
    detected_max_minutes: int = 120
    # trend pullback
    min_adx_1h: float = 20.0
    min_pullback_atr15: float = 0.5
    # breakout and retest
    break_margin_atr15: float = 0.15  # the close must clear the level by this much
    break_body_atr15: float = 0.5
    break_close_loc: float = 0.6
    break_vs_recent: float = 1.0  # body at least this multiple of the previous candles' average body
    breakout_lookback_15m: int = 8
    retest_max_bars: int = 24  # 5M candles to wait for a retest
    missed_atr15: float = 1.0  # price this far beyond the breakout candle's extreme without a retest: missed
    # range reversal
    range_max_adx: float = 25.0
    min_range_atr15: float = 1.5
    max_range_atr15: float = 6.0
    range_edge_fraction: float = 0.25  # no recent visit to the edge and price beyond this share of the range = the middle of it
    approach_zone_atr15: float = 1.0
    approach_gap_bars: int = 2
    approach_stale_bars: int = 10
    fail_depth_atr15: float = 1.0  # a push this far beyond the edge is a genuine break, not a failed one
    # momentum continuation (Stage 2.1): a strong trend that only pauses, and resumes through its prior high
    mc_min_adx_15m: float = 25.0
    mc_min_leg_atr15: float = 1.0  # the impulse before the pause: from the leg's origin to its high
    mc_min_pause_bars: int = 2  # completed 5M candles between the high and the confirming candle
    mc_max_bars: int = 12  # 5M candles after the pause low within which it must resume
    # EMA trend continuation: a dip (shallower than a Trend Pullback) to the 5M EMA20 in a trend whose EMAs are stacked and rising
    etc_min_adx_15m: float = 20.0
    etc_min_stack_bars: int = 8  # 15M candles the EMA20 has been above the EMA50 (below it, for the bearish side)
    etc_min_leg_atr15: float = 0.75
    etc_max_bars: int = 12
    # volatility expansion: a wide-range candle closing out of a 5M compression box
    ve_box_bars: int = 12
    ve_box_max_atr5: float = 3.5  # the box's height, in ATR5, at most
    ve_range_atr5: float = 1.5  # the expansion candle's range, in ATR5, at least
    ve_max_bars: int = 6  # 5M candles after the box within which the expansion candle must appear to start a setup
    # opening range breakout (Stage 2.2): a completed 5M candle closing out of the 09:15-09:30 range
    orb_end: str = "11:00"  # the breakout must be confirmed by a candle completed by this time (IST); a setup still unconfirmed then ends
    orb_probe_atr5: float = 0.25  # a completed candle whose high comes this close to the range edge counts as testing it (what makes the setup DETECTED)
    # failed breakout / trap reversal: a close beyond a level that is reclaimed, then the reversal confirmed
    fbtr_window_bars: int = 12  # a failed break is only picked up while the close beyond the level is this recent (5M candles)
    fbtr_max_bars: int = 6  # 5M candles within which the level must be reclaimed after the first close beyond it
    # market regime facts (Stage 3, app/regime.py): the thresholds of the NAMED checks behind each fact. Initial assumptions like every other; none was tuned on data
    rg_strong_adx15: float = 25.0  # strong trend: 15M ADX at least this, and not falling
    rg_adx_fall: float = 2.0  # ADX points lost over analysis.ADX_SLOPE_BARS candles = "falling"
    rg_stack_bars: int = 8  # 15M candles the EMA20 has been on the trend side of the EMA50
    rg_persist_bars: int = 12  # 15M closes looked at for "persistence"
    rg_persist_share: float = 0.75  # share of them on the trend side of the 15M EMA20
    rg_window: int = 6  # candles in each half of every "the last N against the N before" comparison
    rg_body_fade: float = 0.5  # directional candle bodies are this fraction of what they were, or less
    rg_stretch_growth_atr15: float = 0.5  # distance from the EMA20 grew by this many ATR15 over rg_window candles
    rg_wick_share: float = 0.5  # the candle that made the latest extreme gave back this share of its range as a wick
    rg_weak_min_checks: int = 2  # a trend is "weakening" when at least this many of the six listed checks hold
    rg_compress_atr_ratio: float = 0.8  # compression: the 15M ATR is at most this fraction of its recent average
    rg_compress_narrow: float = 0.75  # ... the latest candles' ranges are at most this fraction of the ones before
    rg_compress_adx: float = 20.0  # ... 15M ADX is below this, or falling
    rg_compress_min_checks: int = 3  # compression: at least this many of its four checks hold
    rg_expand_atr_ratio: float = 1.3  # expansion: the 15M ATR is at least this multiple of its recent average
    rg_expand_body_atr15: float = 1.0  # ... with a directional candle whose body is at least this many ATR15
    rg_expand_close_loc: float = 0.7  # ... closing in this share of its range, in its direction
    rg_expand_box_back: int = 4  # ... out of a compression box that ended at most this many 15M candles ago
    rg_ext_atr20: float = 2.0  # extended: the 15M close is this many ATR15 from its EMA20 ...
    rg_ext_atr50: float = 3.0  # ... and this many from its EMA50
    rg_ext_run: int = 5  # ... and either this many consecutive 15M closes in one direction
    rg_ext_pullback_atr15: float = 1.0  # ... or no retracement bigger than this many ATR15 over rg_ext_pullback_bars candles
    rg_ext_pullback_bars: int = 12
    # trend exhaustion reversal (Stage 3): the only strategy allowed to trade against the 1H / 15M direction, and only through its structural permission
    ter_min_leg_atr15: float = 2.0  # the trend leg that ended at the exhaustion extreme was at least this many ATR15
    ter_reversal_max_bars_15m: int = 12  # the 15M structural change must be this recent (15M candles since it was confirmed)
    ter_max_wait_bars: int = 24  # 5M candles after the 15M change is confirmed within which the 5M break must confirm

    @classmethod
    def from_env(cls, prefix: str = "STRATEGY_", env=None) -> "Params":
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            raw = env.get(prefix + f.name.upper())
            if raw is None:
                continue
            try:
                values[f.name] = (raw.strip().lower() in ("1", "true", "yes", "on")) if f.type is bool else f.type(raw)
            except ValueError as e:
                raise ValueError(f"{prefix}{f.name.upper()}={raw!r} is not a valid {f.type.__name__}") from e
        return cls(**values)


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def clock_seconds(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


def num(x: float) -> str:
    return f"{x:,.2f}"


# -- data types ---------------------------------------------------------------------------------------------------
class Bar(NamedTuple):
    """A candle in the strategy's own view (prices negated for the bearish side)."""

    start: float
    end: float
    o: float
    h: float
    l: float
    c: float

    @property
    def body(self):
        return abs(self.c - self.o)

    @property
    def rng(self):
        return self.h - self.l

    @property
    def bull(self):
        return self.c > self.o

    @property
    def lower_wick(self):
        return min(self.o, self.c) - self.l

    @property
    def close_loc(self):
        return (self.c - self.l) / self.rng if self.rng > 0 else 0.5


class Level(NamedTuple):
    price: float
    label: str
    touches: int = 0


@dataclass
class Context:
    """Everything the strategies need, in real prices. Built from the Phase 2 analysis by trading.py."""

    now: float = 0.0
    price: float | None = None
    phase: str = "morning"
    can_confirm: bool = True  # the session and the data allow a CONFIRMED status right now
    blockers: list = field(default_factory=list)  # ...and if not, why
    strict: bool = False  # stricter confirmation: high volatility or the midday window
    strict_why: str = ""
    confirm_after: float = 0.0  # a candle that completed at or before this time cannot confirm: the opening range is observed, not traded
    trend: dict = field(default_factory=lambda: {"1h": "unknown", "15m": "unknown", "5m": "unknown", "1m": "unknown"})
    adx: dict = field(default_factory=dict)
    atr: dict = field(default_factory=dict)  # the ATR without the overnight gap (Phase 2's atr14_intraday): one candle's size
    rsi: dict = field(default_factory=dict)
    ema: dict = field(default_factory=dict)  # tf -> (ema20, ema50)
    pattern: dict = field(default_factory=dict)
    swings: dict = field(default_factory=dict)  # tf -> [(kind, start, price)]
    consolidation: dict = field(default_factory=dict)  # tf -> (low, high) | None
    breakout: dict = field(default_factory=dict)  # tf -> "breakout" | "breakdown" | None
    volatility: str = "unknown"
    vol_ratio: float | None = None
    alignment: str = "unknown"
    levels: list = field(default_factory=list)  # [Level]
    c5: list = field(default_factory=list)  # completed candles
    c15: list = field(default_factory=list)
    # Facts the later strategies read (Stage 2.0). All of them come straight from the analysis or the session, and none is read by the three original
    # strategies, so adding them changes nothing those strategies do.
    opening_range: dict | None = None  # {"high", "low", "complete"} of the 09:15-09:30 range, real prices; None until its first candle exists
    ema_slope: dict = field(default_factory=dict)  # tf -> {"ema20": points over the last 5 candles, "ema50": ..., "stack_bars": +n bars EMA20 above EMA50, -n below} (None where unknown)
    atr_ratio: dict = field(default_factory=dict)  # tf -> the intraday ATR against its own recent average (the 15M one is vol_ratio)
    # The market regime facts (Stage 3, app/regime.py), in real directions: {"available", "why", "facts": {name: {"state", "checks", ...}}}. Empty = not computed. The nine strategies
    # of Stages 1-2 never read this; Trend Exhaustion Reversal does (through View.regime).
    regime: dict = field(default_factory=dict)


@dataclass
class Setup:
    strategy: str
    side: str
    key: str
    status: str
    anchor_time: float
    level: float | None = None
    level_label: str = ""
    trigger: float | None = None
    zone: tuple | None = None  # entry area, real prices (low, high)
    invalidation: float | None = None
    extreme: float | None = None  # the pullback low (CE) / high (PE) the setup rests on, real prices
    confirm_text: str = ""
    note: str = ""  # why it was invalidated / expired
    reasons: list = field(default_factory=list)
    against: list = field(default_factory=list)
    blockers: list = field(default_factory=list)  # what keeps it from CONFIRMED right now
    quality: list = field(default_factory=list)
    support: list = field(default_factory=list)
    resistance: list = field(default_factory=list)
    confirmed_bar_end: float | None = None
    candle_confirmed: bool = False  # the confirming 5M candle HAS completed, whatever else still holds the setup back (it is then BLOCKED, not waiting)
    plan: dict = field(default_factory=dict)  # underlying risk and the targets taken from the levels actually detected (see target_plan)
    confirm_candle: dict | None = None  # the completed 5M candle that confirmed it, real prices: start, end, open, high, low, close
    break_candle: dict | None = None  # stricter mode only: the candle that broke the base before the confirming one
    detected_at: float = 0.0  # set by the lifecycle book
    status_since: float = 0.0
    confirmed_at: float | None = None
    resumed: bool = False  # picked up from the journal after a restart: it was announced by an earlier run
    group: str | None = None  # the market movement this setup is about, when several strategies could describe the same one (None for the three originals): the lifecycle never keeps two live setups of one group
    counter_trend: dict | None = None  # only a setup of a counter-trend strategy (registry.COUNTER_TREND): the permission record that lets it trade against the 1H / 15M direction. None for every other strategy

    @property
    def id(self) -> str:
        return self.key

    @property
    def direction(self) -> str:
        return OPTION[self.side]


class Scan(NamedTuple):
    applicable: bool  # does the market regime suit this strategy and side?
    note: str  # why it is inactive, or why there is no setup
    setup: Setup | None = None
    info: dict | None = None  # structured facts the strategy wants recorded beside its note (a counter-trend strategy's permission decision); None for the nine strategies of Stages 1-2


# -- the view: one bullish rule set for both sides ------------------------------------------------------------------
MIRROR_TREND = {"bullish": "bearish", "bearish": "bullish", "sideways": "sideways", "unknown": "unknown"}
MIRROR_PATTERN = {"HH_HL": "LH_LL", "LH_LL": "HH_HL", "mixed": "mixed", "insufficient": "insufficient"}
MIRROR_STATE = {"bullish": "bearish", "bearish": "bullish", "up": "down", "down": "up"}  # the directional states of the regime facts


def mirror_regime(regime: dict) -> dict:
    """The regime facts as the bearish view sees them: every directional state turned round (bullish <-> bearish, up <-> down). Nothing else changes."""
    if not regime or not regime.get("facts"):
        return regime or {}
    return {**regime, "facts": {name: {**f, "state": MIRROR_STATE.get(f.get("state"), f.get("state"))} for name, f in regime["facts"].items()}}


class View:
    def __init__(self, ctx: Context, side: str):
        s = 1 if side == "bull" else -1
        self.ctx, self.side, self.sgn = ctx, side, s
        self.live: dict = {}  # (strategy, side) -> the live Setup (real prices) of every strategy, set by scan_all: a strategy that must not describe a move another one already holds can ask
        self.dirn = DIRECTION[side]
        self.option = OPTION[side]
        self.over, self.under = ("above", "below") if s > 0 else ("below", "above")  # in real prices, for the messages
        self.peak, self.trough = ("high", "low") if s > 0 else ("low", "high")  # the leg's extreme and the pullback's extreme, as words
        self.price = None if ctx.price is None else s * ctx.price
        self.trend = {tf: t if s > 0 else MIRROR_TREND.get(t, t) for tf, t in ctx.trend.items()}
        self.pattern = {tf: p if s > 0 else MIRROR_PATTERN.get(p, p) for tf, p in ctx.pattern.items()}
        self.adx, self.atr = ctx.adx, ctx.atr
        self.regime = ctx.regime if s > 0 else mirror_regime(ctx.regime)  # the regime facts in the view's direction
        rsi = ctx.rsi.get("15m")
        self.rsi15 = None if rsi is None else (rsi if s > 0 else 100 - rsi)
        e20, e50 = ctx.ema.get("15m", (None, None))
        self.ema15 = tuple(None if e is None else s * e for e in (e20, e50))
        self.ema5 = tuple(None if e is None else s * e for e in ctx.ema.get("5m", (None, None)))
        e1h = ctx.ema.get("1h", (None, None))[0]
        self.ema1h20 = None if e1h is None else s * e1h
        flip = {"H": "L", "L": "H"}
        self.swings15 = [(k if s > 0 else flip[k], start, s * price) for k, start, price in ctx.swings.get("15m", [])]
        cons = ctx.consolidation.get("15m")
        self.cons15 = None if cons is None else ((cons[0], cons[1]) if s > 0 else (-cons[1], -cons[0]))
        self.levels = sorted((Level(s * lv.price, lv.label, lv.touches) for lv in ctx.levels), key=lambda lv: lv.price)
        self.b5 = [self._bar(c) for c in ctx.c5]
        self.b15 = [self._bar(c) for c in ctx.c15]

    def _bar(self, c) -> Bar:
        if self.sgn > 0:
            return Bar(c.start, c.end, c.open, c.high, c.low, c.close)
        return Bar(c.start, c.end, -c.open, -c.low, -c.high, -c.close)

    def real(self, x: float) -> float:
        return self.sgn * x

    def px(self, x: float) -> str:
        return num(self.sgn * x)


# -- the 5M confirmation: rejection, base, break -----------------------------------------------------------------------
def rejection(b: Bar, p: Params) -> bool:
    """A candle that dipped into the zone and was pushed back out: a long lower wick, closing in its upper half."""
    return b.rng > 0 and b.lower_wick / b.rng >= p.rejection_wick and b.close_loc >= 0.5


class Base(NamedTuple):
    evidence: str | None  # why the buyers look active
    trigger: float | None  # a completed 5M close beyond this confirms (the highest high of the base)
    confirmed: int | None  # index (in `after`) of the candle that completes the confirmation
    pending_follow: bool = False  # stricter mode: the break candle is in, its follow-through is not
    early: "Bar | None" = None  # a break that would have confirmed, but completed inside the opening range
    broke: int | None = None  # index of the candle that broke the base (the confirmation is the next one in stricter mode)


def base_break(after: list, atr5: float, p: Params, strict: bool, hint: str | None = None, confirm_after: float = 0.0,
               dirn: str = "bullish") -> Base:  # fmt: skip
    """`after`: completed 5M bars from the low (or retest) bar, which is index 0, in the bullish view.
    Confirmation = the first bullish candle that closes above the highest high of the bars before it,
    with a real body, closing near its high, and with some evidence that buyers stepped in: a rejection
    candle at the low, a failed breakdown (hint), or that candle being a strong momentum candle itself."""
    if not after:
        return Base(hint, None, None)
    ev = hint
    for b in after[: p.rejection_window]:
        if rejection(b, p):
            ev = f"{dirn} rejection candle at {hm(b.start)} ({'lower' if dirn == 'bullish' else 'upper'} wick {b.lower_wick / b.rng:.0%} of its range)"
            break
    top = after[0].h  # highest high of the bars before the one being examined
    early = None
    for k in range(1, len(after)):
        b, base_high = after[k], top
        top = max(top, b.h)
        if not (b.bull and b.c > base_high and b.body >= p.confirm_body_atr5 * atr5 and b.close_loc >= p.confirm_close_loc):
            continue
        evidence = ev
        if b.body >= p.momentum_body_atr5 * atr5 and ev is None:
            evidence = f"strong {dirn} candle at {hm(b.start)} (body {b.body / atr5:.1f}x ATR5)"
        if evidence is None:
            continue  # closing above a base without any sign of buyers is not enough
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


class Rev(NamedTuple):
    status: str
    note: str
    trigger: float | None
    inv: float
    zone: tuple | None
    evidence: str | None
    confirm_bar: Bar | None
    extreme: float  # the structural low the setup rests on (as it was when the setup confirmed)
    low_start: float
    break_bar: Bar | None = None  # the candle that broke the base (differs from confirm_bar in stricter mode)


def reversal_state(v: View, p: Params, bars: list, ok=None, hint_fn=None, cap: float | None = None, keep: float | None = None) -> Rev | None:
    """The lifecycle of a move that has come down to its zone, replayed in time order over `bars` (all the
    completed 5M bars since the reference event, bullish view).

    The pullback low is the lowest low so far, and every new low restarts the base. The first moment a
    completed candle confirms (see base_break) fixes the setup: its low, trigger and invalidation no longer
    move, and only later candles can invalidate or expire it. `ok(extreme)` says whether that low is at a
    meaningful location (None = the low is not at a valid place: no setup). `hint_fn(bars_so_far, low)`
    supplies evidence of its own (a failed breakdown). `cap` limits the structural low from above. `keep` is the
    low a tracked setup already rests on: it stays acceptable although the EMAs and levels it was judged against
    have moved since (the zone is where the low was made, not where those lines are now)."""
    atr5 = v.atr["5m"]
    chase, buf = p.entry_chase_atr5 * atr5, p.inv_buffer_atr5 * atr5
    judge = ok or (lambda x: True)

    def good(x):
        return x == keep or judge(x)

    def eff(x):
        return x if cap is None else min(x, cap)

    low, done = 0, None
    for n in range(len(bars)):
        if bars[n].l <= bars[low].l:
            low = n  # a new low restarts the base
        if n > low and good(eff(bars[low].l)):
            after = bars[low : n + 1]
            base = base_break(after, atr5, p, v.ctx.strict, hint_fn(bars[: n + 1], low) if hint_fn else None, v.ctx.confirm_after, v.dirn)
            if base.confirmed == len(after) - 1:
                done = (n, low, base)
                break
    if done is None:
        extreme = eff(bars[low].l)
        if not good(extreme):
            return None
        after = bars[low:]
        base = base_break(after, atr5, p, v.ctx.strict, hint_fn(bars, low) if hint_fn else None, v.ctx.confirm_after, v.dirn)
        inv = extreme - buf
        if len(after) > p.max_bars_after_touch:
            return Rev(EXPIRED, f"no confirmation within {p.max_bars_after_touch} candles of the {v.trough}", base.trigger, inv, None, base.evidence, None, extreme, bars[low].start)
        zone = None if base.trigger is None else (base.trigger, base.trigger + chase)
        note = "" if base.early is None else (f"the break candle at {hm(base.early.start)} completed inside the opening range (until {p.observe_until}): "
                                              "the opening range is observed, not traded, so a candle completed after it must confirm")  # fmt: skip
        return Rev(WAITING if base.evidence else DETECTED, note, base.trigger, inv, zone, base.evidence, None, extreme, bars[low].start)
    n_c, low_c, base = done
    extreme = eff(bars[low_c].l)
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


def location_notes(v: View, p: Params, trigger: float, inv: float, resist: list) -> tuple[float, float | None, list]:
    """Is this a reasonable place to enter? Distance to the invalidation and room to the next level."""
    atr5, atr15 = v.atr["5m"], v.atr["15m"]
    risk = trigger - inv
    notes = []
    if risk < p.min_risk_atr5 * atr5:
        notes.append(f"invalidation only {risk:.1f} pts away (under {p.min_risk_atr5:g}x ATR5): inside normal noise")
    if risk > p.max_risk_atr15 * atr15:
        notes.append(f"invalidation {risk:.1f} pts away (over {p.max_risk_atr15:g}x ATR15): the entry is too far from the structure")
    above = [x for x in resist if x > trigger]
    nxt = min(above) if above else None
    if nxt is not None and nxt - trigger < p.min_room_risk * risk:
        notes.append(f"only {nxt - trigger:.1f} pts to the next level {v.px(nxt)} (under {p.min_room_risk:g}x the {risk:.1f} pts at risk)")
    return risk, nxt, notes


def target_plan(v: View, p: Params, trigger: float, inv: float, resist: list, labels: dict | None = None) -> dict:
    """Where the move could be taken to, from the levels that were actually detected beyond the trigger, and what the underlying risks
    and gains are in INDEX POINTS measured from the trigger. Target 1 is the nearest level (the one the room rule already measures),
    target 2 the next distinct level, only if there is one. No target is invented: with no level beyond the trigger there is none. These
    are index points, not option premium: how an option moves with the index (delta, time decay, spread) is a separate question."""
    labels, tol, risk = labels or {}, p.level_tol_atr15 * v.atr["15m"], trigger - inv
    picked: list = []
    for x in sorted({x for x in resist if x > trigger}):
        if picked and x - picked[-1] <= tol:
            continue  # the same level seen twice (a swing level and the prior high): one target, not two
        picked.append(x)
        if len(picked) == 2:
            break
    targets = [{"n": i + 1, "price": v.real(x), "label": labels.get(x, "level"), "distance": x - trigger,
                "reward_risk": (x - trigger) / risk if risk > 0 else None} for i, x in enumerate(picked)]  # fmt: skip
    note = ("no level was detected beyond the trigger, so no target is named (that is not a promise that price has no room)" if not targets else
            "only one level was detected beyond the trigger, so there is no second target" if len(targets) == 1 else "")  # fmt: skip
    return {"entry_reference": v.real(trigger), "risk_points": risk, "targets": targets, "note": note,
            "basis": "index points from the trigger, not option premium"}  # fmt: skip


def cautions(v: View, p: Params) -> list:
    """Things that argue against entering without ruling it out."""
    c, out = v.ctx, []
    if v.rsi15 is not None and v.rsi15 >= p.rsi_caution:
        out.append(f"15M RSI {c.rsi['15m']:.1f}: momentum is already stretched in this direction")
    if v.trend["1m"] == "bearish":
        out.append(f"1M is {c.trend['1m']}, against the {v.dirn} side (not a reason to skip the setup by itself)")
    if c.strict:
        out.append(f"stricter confirmation applies ({c.strict_why})")
    return out


def build(v: View, p: Params, prior, strategy: str, key: str, anchor: float, level: float | None, label: str,
          rev: Rev, reasons: list, against: list, resist: list, level_text: str, labels: dict | None = None) -> Scan:  # fmt: skip
    """Turn a reversal state into a Setup: apply the location, data and session gates, fill in the explanation."""
    c = v.ctx
    if rev.status in (INVALIDATED, EXPIRED):
        return Scan(True, rev.note, Setup(strategy, v.side, key, rev.status, anchor, note=rev.note))
    status, blockers, quality, plan = rev.status, [], [], {}
    accepted = prior is not None and prior.key == key and prior.status == CONFIRMED
    if rev.trigger is not None:
        plan = target_plan(v, p, rev.trigger, rev.inv, resist, labels)
        risk, nxt, notes = location_notes(v, p, rev.trigger, rev.inv, resist)
        atr5, atr15 = v.atr["5m"], v.atr["15m"]
        quality.append({"name": "Entry location", "ok": not any("invalidation" in n for n in notes),
                        "text": f"invalidation {risk:.1f} pts away = {risk / atr5:.1f}x ATR5 / {risk / atr15:.2f}x ATR15"})  # fmt: skip
        quality.append({"name": "Room", "ok": not any("next level" in n for n in notes),
                        "text": "no level ahead" if nxt is None else f"{nxt - rev.trigger:.1f} pts to {v.px(nxt)} = {(nxt - rev.trigger) / risk:.1f}x the risk"})  # fmt: skip
        if not accepted:
            (blockers if status == CONFIRMED else against).extend(notes)
    confirm_candle = break_candle = None
    if rev.confirm_bar is not None:
        cb, bb = rev.confirm_bar, rev.break_bar

        def ohlc(b):  # a candle in real prices
            return {"start": b.start, "end": b.end, "open": v.real(b.o), "high": max(v.real(b.h), v.real(b.l)),
                    "low": min(v.real(b.h), v.real(b.l)), "close": v.real(b.c)}  # fmt: skip

        def candle(b):
            x = ohlc(b)
            return f"{hm(b.start)}-{hm(b.end)} 5M, O {num(x['open'])} H {num(x['high'])} L {num(x['low'])} C {num(x['close'])}"

        def shape(b):
            return f"body {b.body / v.atr['5m']:.1f}x ATR5, closed {b.close_loc:.0%} of the way {'up' if v.sgn > 0 else 'down'} its range"

        confirm_candle = ohlc(cb)
        if bb is not None and bb.start != cb.start:  # stricter confirmation: the candle that broke the base, then one that held it
            break_candle = ohlc(bb)
            quality.append({"name": "Break candle", "ok": True, "text": f"{candle(bb)}: {shape(bb)}"})
            quality.append({"name": "Follow-through candle", "ok": True,
                            "text": f"{candle(cb)}: closed {v.over} the trigger {v.px(rev.trigger)} (stricter confirmation)"})  # fmt: skip
        else:
            quality.append({"name": "Trigger candle", "ok": True, "text": f"{candle(cb)}: {shape(cb)}"})
    quality.append({"name": "Higher timeframes (1H and 15M)", "ok": v.trend["1h"] != "bearish" and v.trend["15m"] != "bearish",
                    "text": f"1H {c.trend['1h']}, 15M {c.trend['15m']}: the two that decide whether this side is allowed"})  # fmt: skip
    quality.append({"name": "Entry timeframes (5M and 1M): information only", "ok": v.trend["5m"] != "bearish",
                    "text": f"5M {c.trend['5m']}, 1M {c.trend['1m']}: shown for context; they never block a setup"})  # fmt: skip
    if status == CONFIRMED and not c.can_confirm:
        blockers.extend(c.blockers)
    if status == CONFIRMED and blockers and not accepted:
        status = WAITING
    word = "above" if v.sgn > 0 else "below"
    if rev.trigger is not None:
        trig_text = f"a completed 5M {v.dirn} candle closes {word} {v.px(rev.trigger)} (body of at least {p.confirm_body_atr5:g}x ATR5, closing near its extreme)"
        if c.strict:
            trig_text += "; the next completed 5M candle must also close beyond it (stricter confirmation)"
    else:
        trig_text = level_text
    real = v.real
    zone = None if rev.zone is None else tuple(sorted(real(x) for x in rev.zone))
    trig_real = None if rev.trigger is None else real(rev.trigger)
    ref = trig_real if trig_real is not None else (real(level) if level is not None else c.price)
    prices = sorted({lv.price for lv in c.levels})
    sup = [x for x in prices if ref is not None and x < ref][-2:][::-1]
    res = [x for x in prices if ref is not None and x > ref][:2]
    setup = Setup(
        strategy, v.side, key, status, anchor,
        level=None if level is None else real(level), level_label=label,
        trigger=trig_real, zone=zone, invalidation=real(rev.inv), extreme=real(rev.extreme), confirm_text=trig_text,
        reasons=list(reasons) + ([rev.evidence] if rev.evidence else []), against=list(against) + cautions(v, p) + ([rev.note] if rev.note else []),
        blockers=blockers, quality=quality, support=sup, resistance=res, candle_confirmed=rev.status == CONFIRMED, plan=plan,
        confirmed_bar_end=None if rev.confirm_bar is None else rev.confirm_bar.end, confirm_candle=confirm_candle, break_candle=break_candle,
    )  # fmt: skip
    return Scan(True, "", setup)


def dead(v: View, strategy: str, key: str, anchor: float, status: str, note: str) -> Scan:
    return Scan(True, note, Setup(strategy, v.side, key, status, anchor, note=note))


def tracked_extreme(v: View, prior, key: str) -> float | None:
    """The low (in the view's prices) that the setup already tracked under this key rests on."""
    if prior is None or prior.key != key or prior.extreme is None:
        return None
    return v.sgn * prior.extreme


# -- strategy A: trend pullback ----------------------------------------------------------------------------------------
def scan_pullback(v: View, p: Params, prior) -> Scan:
    c, name = v.ctx, "trend_pullback"
    if v.trend["1h"] != "bullish":
        return Scan(False, f"1H is {c.trend['1h']}: pullbacks are only taken with a {v.dirn} 1H trend")
    if v.trend["15m"] not in ("bullish", "sideways"):
        return Scan(False, f"15M is {c.trend['15m']}, which conflicts with the {v.dirn} 1H trend")
    adx1 = v.adx.get("1h")
    if adx1 is None or adx1 < p.min_adx_1h:
        return Scan(False, f"1H ADX {'n/a' if adx1 is None else format(adx1, '.1f')} is below {p.min_adx_1h:g}: the trend is not strong enough")
    if v.pattern.get("15m") == "LH_LL":
        return Scan(False, f"15M structure ({c.pattern['15m']}) does not support a {v.dirn} move")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    tol = p.level_tol_atr15 * atr15
    highs = [s for s in v.swings15 if s[0] == "H"]
    if not highs:
        return Scan(True, f"no confirmed 15M swing {v.peak} yet")
    since = [b for b in v.b5 if b.start >= highs[-1][1]]
    if not since:
        return Scan(True, f"no completed 5M candles since the latest 15M swing {v.peak}")
    hi_bar = max(since, key=lambda b: (b.h, b.start))  # the leg's true high, even if its 15M pivot is not confirmed yet
    leg_high = hi_bar.h
    lows = [s for s in v.swings15 if s[0] == "L" and s[1] < hi_bar.start]
    if not lows:
        return Scan(True, f"no 15M swing {v.trough} before the {v.peak}: the leg's origin is unknown")
    floor = lows[-1][2]
    after_high = [b for b in v.b5 if b.start > hi_bar.start]
    if not after_high:
        if prior is not None:  # a pullback was being tracked: the trend resumed without it being confirmed
            return Scan(True, f"the trend made a new {v.peak} at {v.px(leg_high)} before this pullback was confirmed: it is over, and no pullback yet from the new {v.peak}")
        return Scan(True, f"price is at its {v.px(leg_high)} {v.peak}: no pullback yet")
    key = f"{name}:{v.side}:{int(hi_bar.start)}"
    if any(b.c < floor - tol for b in v.b15 if b.start >= hi_bar.start) or any(b.c < floor - tol for b in after_high):
        return dead(v, name, key, hi_bar.start, INVALIDATED, f"a candle closed {v.under} the leg's origin {v.px(floor)}: the {v.dirn} structure is broken")
    e20, e50 = v.ema15
    f20, f50 = v.ema5
    refs = [("15M EMA20", e20), ("15M EMA50", e50), ("5M EMA20", f20), ("5M EMA50", f50), ("1H EMA20", v.ema1h20)] + [(lv.label, lv.price) for lv in v.levels]
    bands = [("15M", e20, e50), ("5M", f20, f50)]

    def near_refs(x):
        return [f"{n} {v.px(r)}" for n, r in refs if r is not None and abs(x - r) <= tol]

    def in_band(x):  # between a pair of EMAs (the value zone), allowing the tolerance
        return [f"between the {n} EMA20 {v.px(a)} and EMA50 {v.px(b)}" for n, a, b in bands
                if a is not None and b is not None and min(a, b) - tol <= x <= max(a, b) + tol]  # fmt: skip

    def at_zone(x):  # deep enough to be a pullback, and at a meaningful level
        return leg_high - x >= p.min_pullback_atr15 * atr15 and bool(near_refs(x) or in_band(x))

    rev = reversal_state(v, p, after_high, at_zone, keep=tracked_extreme(v, prior, key))
    if rev is None:
        low = min(b.l for b in after_high)
        if leg_high - low < p.min_pullback_atr15 * atr15:
            return Scan(True, f"pullback so far {leg_high - low:.1f} pts ({(leg_high - low) / atr15:.2f}x ATR15): too shallow to count as a pullback")
        emas = ", ".join(f"{n} EMA20 {'n/a' if a is None else v.px(a)} / EMA50 {'n/a' if b is None else v.px(b)}" for n, a, b in bands)
        return Scan(True, f"pullback {v.trough} {v.px(low)} is not at a meaningful level ({emas}; no level within {tol:.1f} pts)")
    extreme = rev.extreme
    depth = leg_high - extreme
    where = near_refs(extreme) or in_band(extreme)
    reasons = [
        f"1H {c.trend['1h']} (ADX {adx1:.1f}); 15M {c.trend['15m']} with {c.pattern.get('15m', 'n/a')} structure",
        f"pulled back {depth:.1f} pts ({depth / atr15:.1f}x ATR15) from the {v.px(leg_high)} {v.peak} to {v.px(extreme)}, at " + ", ".join(where),
        f"the pullback holds {v.over} the leg's origin {v.px(floor)}, so the {v.dirn} structure is intact",
    ]  # fmt: skip
    # The leg high is where this pullback started: the nearest supply above it and the natural first target. It is counted as a level for
    # the room rule (as before) and named for what it is, so the trader sees "prior high" and not an anonymous level.
    resist = [lv.price for lv in v.levels] + [leg_high]
    labels = {**{lv.price: lv.label for lv in v.levels}, leg_high: "prior high (where this pullback started)"}
    return build(v, p, prior, name, key, hi_bar.start, floor, "leg origin", rev, reasons, [], resist,
                 f"price pulls back to the zone and a completed 5M {v.dirn} candle closes beyond the base of the pullback", labels)  # fmt: skip


# -- strategy B: breakout and retest ----------------------------------------------------------------------------------
def scan_breakout(v: View, p: Params, prior) -> Scan:
    c, name = v.ctx, "breakout_retest"
    if "unknown" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, "not enough completed 1H / 15M candles to judge the direction")
    if "bearish" in (v.trend["1h"], v.trend["15m"]):
        return Scan(False, f"1H {c.trend['1h']} / 15M {c.trend['15m']}: a {v.dirn} breakout would go against the higher-timeframe direction")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15:
        return Scan(True, "ATR is not available yet")
    b15, b5 = v.b15, v.b5
    if len(b15) < 8:
        return Scan(True, "not enough completed 15M candles")
    tol, margin = p.level_tol_atr15 * atr15, p.break_margin_atr15 * atr15
    k = lvl = None
    if prior is not None and prior.strategy == name and prior.side == v.side and prior.level is not None:
        k = next((i for i, b in enumerate(b15) if b.start == prior.anchor_time), None)
        lvl = None if k is None else next((x for x in v.levels if abs(x.price - v.sgn * prior.level) < 1e-6), Level(v.sgn * prior.level, prior.level_label))
    if k is None:
        for i in range(len(b15) - 1, max(0, len(b15) - 1 - p.breakout_lookback_15m), -1):
            crossed = [x for x in v.levels if b15[i - 1].c <= x.price and b15[i].c >= x.price + margin]
            if crossed:
                k, lvl = i, min(crossed, key=lambda x: x.price)  # the first resistance above the previous close is the one that broke
                break
    if k is None:
        return Scan(True, f"no 15M close {v.over} a meaningful level in the last {p.breakout_lookback_15m} candles")
    bar, L = b15[k], lvl.price
    key = f"{name}:{v.side}:{int(bar.start)}"
    if not (prior is not None and prior.key == key):  # an accepted breakout is not judged again
        recent = [b.body for b in b15[max(0, k - 6) : k]]
        weak = []
        if not bar.bull or bar.body < p.break_body_atr15 * atr15:
            weak.append(f"body {bar.body:.1f} pts is only {bar.body / atr15:.2f}x ATR15 (needs {p.break_body_atr15:g}x)")
        if bar.close_loc < p.break_close_loc:
            weak.append("it closed in the weak part of its range (a long wick back = rejection)")
        if recent and bar.body < p.break_vs_recent * (sum(recent) / len(recent)):
            weak.append("its body is no bigger than the recent candles")
        if weak:
            return Scan(True, f"{hm(bar.start)} 15M close {v.over} {lvl.label} {v.px(L)} rejected as a weak {"breakout" if v.sgn > 0 else "breakdown"}: " + "; ".join(weak))
    after = [b for b in b5 if b.start >= bar.end]
    later15 = b15[k + 1 :]
    if any(b.c < L - tol for b in after) or any(x.c < L - tol for x in later15):
        return dead(v, name, key, bar.start, INVALIDATED, f"false {"breakout" if v.sgn > 0 else "breakdown"}: price closed back {v.under} {lvl.label} {v.px(L)}")
    ri = next((i for i, b in enumerate(after) if b.l <= L + tol), None)
    before = after if ri is None else after[:ri]
    if any(b.c > bar.h + p.missed_atr15 * atr15 for b in before):
        return dead(v, name, key, bar.start, EXPIRED, f"missed: price ran more than {p.missed_atr15:g}x ATR15 beyond the breakout candle without retesting {v.px(L)}")
    reasons = [
        f"{hm(bar.start)} 15M candle closed {v.over} {lvl.label} {v.px(L)} at {v.px(bar.c)}: body {bar.body / atr15:.1f}x ATR15, closed {bar.close_loc:.0%} of the way {'up' if v.sgn > 0 else 'down'} its range",
        f"1H {c.trend['1h']}, 15M {c.trend['15m']}: the breakout is not against the higher timeframes",
    ]  # fmt: skip
    if c.breakout.get("15m") == ("breakout" if v.sgn > 0 else "breakdown"):
        reasons.append("Phase 2 also flags a 15M break of the latest swing level")
    resist = [x.price for x in v.levels if x.price > L + tol]
    if ri is None:
        if len(after) > p.retest_max_bars:
            return dead(v, name, key, bar.start, EXPIRED, f"no retest of {v.px(L)} within {p.retest_max_bars} candles")
        inv = L - tol - p.inv_buffer_atr5 * atr5
        rev = Rev(DETECTED, "", None, inv, (L - tol, L + tol), None, None, L, bar.start)
        scan = build(v, p, prior, name, key, bar.start, L, lvl.label, rev, reasons,
                     [f"unconfirmed: no retest of {v.px(L)} yet. A breakout without a successful retest is not an entry"], resist,
                     f"price retests {lvl.label} {v.px(L)} and holds, then a completed 5M {v.dirn} candle closes beyond the retest {v.peak}",
                     {lv.price: lv.label for lv in v.levels})  # fmt: skip
        return scan
    rs = after[ri:]
    rev = reversal_state(v, p, rs, cap=L)
    reasons.append(f"retested {lvl.label} {v.px(L)} at {hm(rs[0].start)} and held (no 5M close beyond {v.px(L - tol)})")
    return build(v, p, prior, name, key, bar.start, L, lvl.label, rev, reasons, [], resist, "", {lv.price: lv.label for lv in v.levels})


# -- strategy C: range reversal -----------------------------------------------------------------------------------------
def approach_run(b5: list, zone_top: float, gap: int):
    """First and last index of the latest cluster of bars that came down to the zone (allowing short excursions)."""
    last = next((i for i in range(len(b5) - 1, -1, -1) if b5[i].l <= zone_top), None)
    if last is None:
        return None
    first, misses = last, 0
    for i in range(last - 1, -1, -1):
        if b5[i].l <= zone_top:
            first, misses = i, 0
        else:
            misses += 1
            if misses > gap:
                break
    return first, last


def scan_range(v: View, p: Params, prior) -> Scan:
    c, name = v.ctx, "range_reversal"
    if c.alignment != "range_bound" or c.trend["1h"] != "sideways" or c.trend["15m"] != "sideways":
        return Scan(False, f"the market is not range-bound (1H {c.trend['1h']}, 15M {c.trend['15m']}): range reversals are only taken in sideways markets")
    hot = [tf for tf in ("1h", "15m") if (c.adx.get(tf) or 0) >= p.range_max_adx]
    if hot:
        return Scan(False, f"{hot[0].upper()} ADX {c.adx[hot[0]]:.1f} is at or above {p.range_max_adx:g}: too much trend strength for a range reversal")
    live = [tf for tf in ("1h", "15m") if c.breakout.get(tf)]
    if live:
        return Scan(False, f"Phase 2 flags a {live[0].upper()} {c.breakout[live[0]]}: a {c.breakout[live[0]]} is in progress, not a range")
    atr5, atr15 = v.atr.get("5m"), v.atr.get("15m")
    if not atr5 or not atr15 or v.price is None:
        return Scan(True, "ATR or price is not available yet")
    tol = p.level_tol_atr15 * atr15
    cands = sorted({x.price for x in v.levels} | ({v.cons15[0], v.cons15[1]} if v.cons15 else set()))
    side_word = "support" if v.sgn > 0 else "resistance"
    if prior is not None and prior.level is not None:
        S = v.sgn * prior.level  # a setup that is being tracked keeps the edge it was found at, wherever price has moved since
    else:
        sup = [x for x in cands if x <= v.price + tol]
        if not sup:
            just_above = [x for x in cands if v.price + tol < x <= v.price + p.fail_depth_atr15 * atr15]
            if just_above:
                return Scan(True, f"price {v.px(v.price)} is beyond the range {side_word} {v.px(min(just_above))}: a break in progress, not a reversal")
            return Scan(True, f"no known level at or {v.under} the price: no range {side_word}")
        S = max(sup)
    far = [x for x in cands if x >= S + p.min_range_atr15 * atr15]
    if not far:
        return Scan(True, f"no level at least {p.min_range_atr15:g}x ATR15 {v.over} {v.px(S)}: there is no range to trade")
    R = min(far)
    width = R - S
    if width > p.max_range_atr15 * atr15:
        return Scan(True, f"the range {v.px(S)}-{v.px(R)} is wider than {p.max_range_atr15:g}x ATR15: no clear boundary")
    run = approach_run(v.b5, S + p.approach_zone_atr15 * atr15, p.approach_gap_bars)
    if run is None or len(v.b5) - 1 - run[1] > p.approach_stale_bars:
        # The location is judged by the visit to the edge, never by where price is now: after a bounce it has moved away.
        loc = (v.price - S) / width
        if loc > p.range_edge_fraction:
            return Scan(True, f"price is {loc:.0%} of the way across the range: the middle of a range offers no edge")
        return Scan(True, f"price has not come back to the range {side_word} {v.px(S)} recently")
    bars = v.b5[run[0] :]
    key = f"{name}:{v.side}:{int(bars[0].start)}"
    extreme = min(b.l for b in bars)
    if any(x.c < S - tol for x in v.b15 if x.start >= bars[0].start) or extreme < S - p.fail_depth_atr15 * atr15:
        return dead(v, name, key, bars[0].start, INVALIDATED, f"price broke through the range edge {v.px(S)}: a genuine break, not a reversal")
    beyond = [i for i, b in enumerate(bars) if b.c < S - tol]
    if beyond and not any(b.c > S for b in bars[beyond[-1] + 1 :]):
        return Scan(True, f"price closed beyond the range {side_word} {v.px(S)} and has not reclaimed it: a break in progress")

    def failed_break(prefix, low):
        gone = [i for i, b in enumerate(prefix) if b.c < S - tol]
        back = next((j for j in range(gone[-1] + 1, len(prefix)) if prefix[j].c > S), None) if gone else None
        if back is None:
            return None
        return f"failed {'breakdown' if v.sgn > 0 else 'breakout'}: closed beyond {v.px(S)} at {hm(prefix[gone[-1]].start)} and reclaimed it at {hm(prefix[back].start)}"

    rev = reversal_state(v, p, bars, lambda x: x <= S + tol, failed_break, keep=tracked_extreme(v, prior, key))
    if rev is None:
        return Scan(True, f"price is near the range {side_word} {v.px(S)} but has not touched it ({v.trough} {v.px(extreme)})")
    gap = rev.extreme - S
    reasons = [
        f"1H and 15M are both sideways (ADX {c.adx.get('1h') or 0:.1f} / {c.adx.get('15m') or 0:.1f}): a range between {num(min(v.real(S), v.real(R)))} and {num(max(v.real(S), v.real(R)))} ({width / atr15:.1f}x ATR15 wide)",
        f"price came back to the range {side_word} {v.px(S)} at {hm(rev.low_start)} ({v.trough} {v.px(rev.extreme)}, {abs(gap):.1f} pts {v.over if gap >= 0 else v.under} it)",
    ]  # fmt: skip
    labels = {**{x: "15M range edge" for x in cands}, **{lv.price: lv.label for lv in v.levels}, R: "top of the range"}
    return build(v, p, prior, name, key, bars[0].start, S, f"range {side_word}", rev, reasons, [], [R] + [x for x in cands if x > S],
                 f"price rejects the range {side_word} {v.px(S)} (or fails to break it) and a completed 5M {v.dirn} candle closes beyond the base", labels)  # fmt: skip


# The three original strategies, registered in the order they have always been scanned and shown. The tie-break priority is the one they have always had
# (breakout and retest, then trend pullback, then range reversal): it is a convention for two ready setups in the same direction, nothing more.
registry.register(StrategyDef("trend_pullback", "Trend Pullback", scan_pullback, WITH_TREND, priority=1))
registry.register(StrategyDef("breakout_retest", "Breakout and Retest", scan_breakout, WITH_TREND, priority=0))
registry.register(StrategyDef("range_reversal", "Range Reversal", scan_range, RANGE, priority=2))


def scan_all(ctx: Context, p: Params, priors: dict) -> dict:
    """Every registered strategy on both sides: {(strategy, side): Scan}. A bug in one scanner cannot silence the others."""
    out = {}
    for name, fn in registry.scanners():
        for side in ("bull", "bear"):
            try:
                view = View(ctx, side)
                view.live = priors
                out[(name, side)] = fn(view, p, priors.get((name, side)))
            except Exception as e:  # reported, never swallowed silently
                import logging

                logging.getLogger(__name__).exception("scanner %s/%s failed", name, side)
                out[(name, side)] = Scan(False, f"scanner error: {type(e).__name__}: {e}")
    return out


from . import strategies  # noqa: E402,F401  (the later strategies register themselves here, after everything they build on above is defined)
