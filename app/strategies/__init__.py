"""Strategies beyond the three original ones in strategy.py.

Each module here defines scanners written like the originals (once, for the bullish case, run on a `View` for the bearish case; the lifecycle, the safety
checks and the decision are not repeated here: a scanner returns a `Scan` built through `strategy.build`, so none of them can be bypassed) and registers them
with `registry.register`. Importing this package registers everything; strategy.py imports it last."""

from . import trend_family  # noqa: F401  (Stage 2.1: Momentum Continuation, EMA Trend Continuation, Volatility Expansion)
from . import opening_range  # noqa: F401  (Stage 2.2: Opening Range Breakout)
from . import failed_breakout  # noqa: F401  (Stage 2.2: Failed Breakout / Trap Reversal)
from . import range_bounce  # noqa: F401  (Stage 2.3: Range Bounce)
from . import exhaustion  # noqa: F401  (Stage 3: Trend Exhaustion Reversal, the one counter-trend strategy)
