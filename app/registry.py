"""The one list of strategies.

A strategy used to be spread over three hand-kept places (the name table, the scanner list and the tie-break order), and a fourth strategy added to only
two of them failed at run time. Everything now reads this list instead: the scanners run from it, the lifecycle and the decision rank from it, the strategy
monitor shows what is in it, and a journal is restored against it. A strategy is added by registering it once.

Nothing here knows how a strategy works, and it imports nothing from the strategy code, so a strategy module can register itself without an import cycle.

Families say what kind of market a strategy is for, and which rule decides whether its side is allowed:
    with_trend   follows a higher-timeframe trend: 1H and 15M must not be against the side (the rule the three original strategies already use)
    range        trades a boundary in a sideways market
    session      anchored to the trading session (the opening range)
    reversal     trades against the move: the only family that may be `counter_trend`, i.e. may qualify against the 1H / 15M direction, and only through a permission record
                 that the decision engine checks (trading.decide); no other strategy ever gets that exception
Priority is a documented tie-break between setups that are both ready in the same direction (lower wins). It is a convention, not a ranking of merit, and
it never makes a setup pass a check that it would otherwise fail."""

from dataclasses import dataclass
from typing import Callable

WITH_TREND, RANGE, SESSION, REVERSAL = "with_trend", "range", "session", "reversal"
FAMILIES = (WITH_TREND, RANGE, SESSION, REVERSAL)
SPOT_CANDLES = "NIFTY spot candles"


@dataclass(frozen=True)
class StrategyDef:
    name: str  # the id used in setup keys, the journal and the API: "trend_pullback"
    title: str  # what the dashboard calls it
    scanner: Callable  # scan(view, params, prior) -> Scan
    family: str
    priority: int  # unique; lower wins a tie between ready setups in the same direction
    needs: tuple = (SPOT_CANDLES,)  # the data it depends on. A strategy whose own data fails is switched off by itself: it never blocks the others
    counter_trend: bool = False  # may trade against the 1H / 15M direction, but ONLY through the permission record its own setups carry (see trading.decide). Only the reversal family may


REGISTRY: list[StrategyDef] = []  # in the order the strategies are scanned and shown
NAMES: dict[str, str] = {}  # id -> title (the same dict object for the life of the process: modules import it)
FAMILY: dict[str, str] = {}
PRIORITY: list[str] = []  # ids, best first: the same list object for the life of the process
COUNTER_TREND: set[str] = set()  # ids of the strategies with counter_trend: the one exception to the higher-timeframe direction gate (the same set object for the life of the process)


def register(defn: StrategyDef) -> StrategyDef:
    """Add a strategy. A duplicate id or priority, or an unknown family, is a programming error and stops the import: a half-registered strategy would
    be scanned but never ranked, which is the failure this module exists to prevent."""
    if defn.name in NAMES:
        raise ValueError(f"strategy {defn.name!r} is already registered")
    if defn.family not in FAMILIES:
        raise ValueError(f"strategy {defn.name!r}: unknown family {defn.family!r} (one of {', '.join(FAMILIES)})")
    if defn.counter_trend and defn.family != REVERSAL:
        raise ValueError(f"strategy {defn.name!r}: only the {REVERSAL} family may be counter-trend (it is the one exception to the higher-timeframe direction gate)")
    if any(d.priority == defn.priority for d in REGISTRY):
        raise ValueError(f"strategy {defn.name!r}: priority {defn.priority} is already taken")
    if not defn.name.replace("_", "").isalnum() or ":" in defn.name:
        raise ValueError(f"strategy id {defn.name!r} must be letters, digits and underscores: it is part of every setup key")
    REGISTRY.append(defn)
    _refresh()
    return defn


def _refresh() -> None:
    NAMES.clear()
    NAMES.update({d.name: d.title for d in REGISTRY})
    FAMILY.clear()
    FAMILY.update({d.name: d.family for d in REGISTRY})
    PRIORITY[:] = [d.name for d in sorted(REGISTRY, key=lambda d: d.priority)]
    COUNTER_TREND.clear()
    COUNTER_TREND.update(d.name for d in REGISTRY if d.counter_trend)


def require() -> None:
    """Raise unless strategies are registered. They register themselves when `app.strategy` is imported (it imports the strategy modules last): importing this module alone leaves the
    list empty, and an engine run like that would answer NO TRADE for ever with nothing to say why."""
    if not REGISTRY:
        raise RuntimeError("no strategy is registered: import app.strategy (or app.trading), which registers the strategies, before using the registry")


def scanners() -> tuple:
    """((id, scanner), ...) in scan order."""
    require()
    return tuple((d.name, d.scanner) for d in REGISTRY)


def definition(name: str) -> StrategyDef:
    return next(d for d in REGISTRY if d.name == name)
