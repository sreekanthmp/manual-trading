"""Hand-defined market scenarios for the Phase 3 tests.

Every price below is chosen by hand so the expected outcome can be worked out from the trading rules
without running the code. NIFTY is around 24,500; ATR(5M) = 10 and ATR(15M) = 25 points throughout.
`mirror` turns a bullish scenario into its bearish twin with its own, deliberately independent
transformation (prices reflected around a fixed number; highs and lows swapped)."""

from dataclasses import replace

from helpers import FRI, ist

from app.candles import Candle
from app.strategy import Context, Level, Params

M = 50000.0  # the mirror pivot
P = Params()
DAY = FRI
OPEN = ist(9, 15, day=DAY)


def t5(i):
    """Start of the i-th 5M slot of the day (0 = 09:15)."""
    return OPEN + 300 * i


def t15(j):
    return OPEN + 900 * j


def c5(i, o, h, low, c):
    return Candle(t5(i), t5(i) + 300, o, h, low, c, 0, source="db")


def c15(j, o, h, low, c):
    return Candle(t15(j), t15(j) + 900, o, h, low, c, 0, source="db")


def rise(i0, n, start, step):
    """n quiet rising 5M candles beginning at slot i0: the run-up to a swing high."""
    out = []
    for k in range(n):
        o = start + step * k
        out.append(c5(i0 + k, o, o + step + 2, o - 2, o + step))
    return out


def bull(**over):
    """A bullish context with sensible numbers; candles and swings are supplied by each scenario."""
    base = dict(
        now=ist(10, 55, day=DAY), price=24596.0, phase="morning", can_confirm=True,
        trend={"1h": "bullish", "15m": "bullish", "5m": "sideways", "1m": "sideways"},
        adx={"1h": 30.0, "15m": 28.0, "5m": 20.0, "1m": 15.0},
        atr={"1h": 60.0, "15m": 25.0, "5m": 10.0, "1m": 4.0},
        rsi={"1h": 60.0, "15m": 55.0, "5m": 50.0, "1m": 50.0},
        ema={"1h": (24400.0, 24300.0), "15m": (24570.0, 24520.0), "5m": (24595.0, 24585.0), "1m": (24596.0, 24594.0)},
        pattern={"1h": "HH_HL", "15m": "HH_HL", "5m": "mixed", "1m": "mixed"},
        swings={"15m": [("L", t15(0), 24500.0), ("H", t15(3), 24650.0)]},
        consolidation={"15m": None}, breakout={"15m": None, "1h": None},
        volatility="normal", vol_ratio=1.0, alignment="aligned_bullish",
        levels=[Level(24565.0, "swing level x2", 2)],
    )  # fmt: skip
    base.update(over)
    return Context(**base)


def mirror_candle(c):
    return replace(c, open=M - c.open, high=M - c.low, low=M - c.high, close=M - c.close)


MIRROR_TREND = {"bullish": "bearish", "bearish": "bullish", "sideways": "sideways", "unknown": "unknown"}
MIRROR_PATTERN = {"HH_HL": "LH_LL", "LH_LL": "HH_HL", "mixed": "mixed", "insufficient": "insufficient"}


STATE_FLIP = {"bullish": "bearish", "bearish": "bullish", "up": "down", "down": "up"}


def mirror_regime(regime):
    """The regime facts of the bearish twin: every directional state turned round, nothing else changed (written here, not borrowed from the code under test)."""
    if not regime or not regime.get("facts"):
        return regime
    return {**regime, "facts": {name: {**f, "state": STATE_FLIP.get(f["state"], f["state"])} for name, f in regime["facts"].items()}}


def mirror(ctx):
    """The bearish twin of a bullish context, built without using the strategy code's own view logic."""
    flip = {"H": "L", "L": "H"}
    m = lambda x: None if x is None else M - x  # noqa: E731
    return replace(
        ctx,
        price=m(ctx.price),
        trend={k: MIRROR_TREND[v] for k, v in ctx.trend.items()},
        rsi={k: m(v) if False else (None if v is None else 100 - v) for k, v in ctx.rsi.items()},
        ema={k: (m(a), m(b)) for k, (a, b) in ctx.ema.items()},
        pattern={k: MIRROR_PATTERN[v] for k, v in ctx.pattern.items()},
        swings={k: [(flip[kind], start, M - price) for kind, start, price in v] for k, v in ctx.swings.items()},
        consolidation={k: None if v is None else (M - v[1], M - v[0]) for k, v in ctx.consolidation.items()},
        breakout={k: {"breakout": "breakdown", "breakdown": "breakout", None: None}[v] for k, v in ctx.breakout.items()},
        levels=[Level(M - lv.price, lv.label, lv.touches) for lv in ctx.levels],
        opening_range=None if ctx.opening_range is None else {"high": M - ctx.opening_range["low"], "low": M - ctx.opening_range["high"], "complete": ctx.opening_range["complete"]},
        ema_slope={k: {f: (None if x is None else -x) for f, x in v.items()} for k, v in ctx.ema_slope.items()},  # rising becomes falling, a bullish stack a bearish one
        regime=mirror_regime(ctx.regime),
        c5=[mirror_candle(c) for c in ctx.c5],
        c15=[mirror_candle(c) for c in ctx.c15],
    )  # fmt: skip


# -- scenario A: trend pullback (bullish) ---------------------------------------------------------------------------------
def pullback_5m(through=19):
    """A run-up to 24,650 at 10:10, a pullback to a hammer at 24,562 (10:40), then a base and a break.

    slot  time   open   high   low    close   what it is
     11   10:10  24630  24650  24626  24640   the leg's high (24,650)
     12-16 10:15-10:35 a steady pullback
     17   10:40  24575  24583  24562  24580   hammer: lower wick 13 of a 21-point range (62%), closes in its upper 86%
     18   10:45  24580  24590  24578  24585   small bullish candle; closes at 58% of its range (< 60%): not a confirmation
     19   10:50  24585  24598  24584  24596   bullish, body 11 (1.1x ATR5), closes above the base high 24,590 at 86% of its range
    """
    rows = {
        11: (24630, 24650, 24626, 24640), 12: (24640, 24642, 24620, 24624), 13: (24624, 24628, 24600, 24604),
        14: (24604, 24608, 24588, 24592), 15: (24592, 24596, 24576, 24580), 16: (24580, 24584, 24566, 24570),
        17: (24575, 24583, 24562, 24580), 18: (24580, 24590, 24578, 24585), 19: (24585, 24598, 24584, 24596),
    }  # fmt: skip
    return rise(0, 11, 24560, 7) + [c5(i, *rows[i]) for i in range(11, through + 1)]


def pullback_15m():
    """Closes stay above the leg's origin (24,500) throughout."""
    return [
        c15(0, 24510, 24530, 24500, 24525), c15(1, 24525, 24560, 24520, 24555), c15(2, 24555, 24610, 24550, 24605),
        c15(3, 24610, 24650, 24605, 24640), c15(4, 24640, 24642, 24600, 24604), c15(5, 24604, 24608, 24562, 24580),
    ]  # fmt: skip


def bull_pullback(through=19, **over):
    """The context at the close of 5M slot `through` (the next slot is forming)."""
    bars = pullback_5m(through)
    d = dict(c5=bars, c15=pullback_15m(), now=bars[-1].end + 3, price=bars[-1].close)
    d.update(over)
    return bull(**d)


# -- scenario B: breakout and retest (bullish) -----------------------------------------------------------------------------
LEVEL = 24600.0


def breakout_15m():
    """Seven quiet 15M candles under 24,600, then a strong breakout candle (14:00..)."""
    quiet = [c15(j, 24570 + 2 * (j % 3), 24592 + (j % 2), 24560 + 2 * (j % 3), 24580 + 2 * (j % 3) + 3) for j in range(7)]
    # j=7 (11:00): open 24590, high 24640, low 24588, close 24635: body 45 (1.8x ATR15), closes at 90% of its range, 35 above 24,600
    return quiet + [c15(7, 24590, 24640, 24588, 24635)]


def breakout_5m(through=None):
    """After the breakout candle ends (11:15): a run to 24,650, a retest of 24,600 and a continuation.

    slot  time   open   high   low    close
     24   11:15  24636  24650  24634  24648   (the 11:00 slot is 21: the breakout candle spans slots 21-23)
     25   11:20  24648  24650  24622  24626
     26   11:25  24626  24630  24608  24612
     27   11:30  24612  24615  24601  24611   retest: low 24,601 is within 6.25 of 24,600; lower wick 10 of 14 (71%)
     28   11:35  24611  24622  24609  24620   bullish, closes above the retest high 24,615 at 85% of its range
    """
    rows = {24: (24636, 24650, 24634, 24648), 25: (24648, 24650, 24622, 24626), 26: (24626, 24630, 24608, 24612),
            27: (24612, 24615, 24601, 24611), 28: (24611, 24622, 24609, 24620)}  # fmt: skip
    last = through or 28
    base = rise(0, 21, 24580, 1)  # quiet earlier 5M candles, well below the level
    return base + [c5(i, *rows[i]) for i in range(24, last + 1)]


def bull_breakout(through=28, **over):
    c5s = breakout_5m(through)
    d = dict(
        trend={"1h": "bullish", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment="pausing",
        pattern={"1h": "HH_HL", "15m": "mixed", "5m": "mixed", "1m": "mixed"}, swings={"15m": []},
        levels=[Level(LEVEL, "swing level x3", 3), Level(24700.0, "swing level x2", 2)],
        c5=c5s, c15=breakout_15m(), now=c5s[-1].end + 3, price=c5s[-1].close,
    )  # fmt: skip
    d.update(over)
    return bull(**d)


# -- scenario C: range reversal (bullish at the range support) ----------------------------------------------------------------
SUPPORT, RESISTANCE = 24500.0, 24620.0


def range_5m(through=None):
    """Price grinds down to the 24,500 support, closes under it (a failed breakdown) and reclaims it.

    slot  time   open   high   low    close
     30   11:45  24520  24524  24508  24510
     31   11:50  24510  24512  24496  24498   closes within the 6.25 tolerance of 24,500 (not beyond it)
     32   11:55  24498  24500  24486  24490   closes 10 below the support: beyond the tolerance (a failed breakdown candidate)
     33   12:00  24490  24508  24484  24505   the low (24,484); lower wick 6 of 24 = 25%; closes above 24,500: the reclaim
     34   12:05  24505  24509  24503  24507   small: closes below the base high 24,508, so no confirmation
     35   12:10  24509  24522  24507  24518   bullish, body 9 (0.9x ATR5), closes above the base high 24,509, at 73% of its range
    """
    rows = {30: (24520, 24524, 24508, 24510), 31: (24510, 24512, 24496, 24498), 32: (24498, 24500, 24486, 24490),
            33: (24490, 24508, 24484, 24505), 34: (24505, 24509, 24503, 24507), 35: (24509, 24522, 24507, 24518)}  # fmt: skip
    last = through or 35
    base = [c5(i, 24540 - i, 24545 - i, 24535 - i, 24538 - i) for i in range(30)]  # a gentle drift lower from 24,540
    return base + [c5(i, *rows[i]) for i in range(30, last + 1)]


def range_15m():
    return [c15(j, 24560, 24580, 24520, 24550) for j in range(8)] + [c15(8, 24540, 24545, 24505, 24512)]


def bull_range(through=35, **over):
    c5s = range_5m(through)
    d = dict(
        trend={"1h": "sideways", "15m": "sideways", "5m": "sideways", "1m": "sideways"}, alignment="range_bound",
        adx={"1h": 15.0, "15m": 14.0, "5m": 12.0, "1m": 10.0}, pattern={"1h": "mixed", "15m": "mixed", "5m": "mixed", "1m": "mixed"},
        swings={"15m": []}, ema={"1h": (24550.0, 24545.0), "15m": (24552.0, 24550.0), "5m": (24520, 24525), "1m": (24518, 24520)},
        levels=[Level(SUPPORT, "swing level x3", 3), Level(RESISTANCE, "swing level x3", 3)],
        c5=c5s, c15=range_15m(), now=c5s[-1].end + 3, price=c5s[-1].close,
    )  # fmt: skip
    d.update(over)
    return bull(**d)
