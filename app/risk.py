"""Position-size arithmetic for a manual option trade. A calculator: it places nothing, chooses nothing and says nothing about whether to trade.

You give it the premium you would buy at, the premium at which you would get out if you are wrong (your own stop: Prism does not know an option's stop, only the index's invalidation level) and
the lot size. It gives back how many lots keep the loss at that stop inside your own limits:

    RISK_CAPITAL            rupees you trade with
    RISK_PER_TRADE_PCT      percent of that capital you accept to lose on one trade (e.g. 1)
    RISK_MAX_DAILY_LOSS     rupees you accept to lose in a day; what the day has already lost (realised, from the trade log) is taken off it

None of them has a default: a limit nobody set is not invented. The loss at the stop ignores slippage, a gap through the stop, brokerage and taxes, so it is a floor under the real loss."""

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    capital: float | None = None
    per_trade_pct: float | None = None
    max_daily_loss: float | None = None

    @property
    def per_trade_loss(self) -> float | None:
        """Rupees one trade may lose: the percentage of the capital (None when either is not set)."""
        return None if self.capital is None or self.per_trade_pct is None else self.capital * self.per_trade_pct / 100.0

    def as_dict(self) -> dict:
        return {"capital": self.capital, "per_trade_pct": self.per_trade_pct, "per_trade_loss": self.per_trade_loss, "max_daily_loss": self.max_daily_loss}


NAMES = {"capital": "RISK_CAPITAL", "per_trade_pct": "RISK_PER_TRADE_PCT", "max_daily_loss": "RISK_MAX_DAILY_LOSS"}


def _positive(name: str, text) -> float | None:
    if text is None or str(text).strip() == "":
        return None
    try:
        v = float(str(text).strip())
    except ValueError:
        raise ValueError(f"{name} must be a number, not {str(text).strip()[:30]!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be a positive number, not {v}")
    return v


def limits_from_env(env=None) -> Limits:
    """The limits in the environment (or in the .env file, which the app has already loaded into it). A value that is not a positive number is an error that names the variable."""
    env = os.environ if env is None else env
    got = {k: _positive(n, env.get(n)) for k, n in NAMES.items()}
    if got["per_trade_pct"] is not None and got["per_trade_pct"] > 100:
        raise ValueError(f"{NAMES['per_trade_pct']} is a percentage and cannot be over 100, not {got['per_trade_pct']:g}")
    return Limits(**got)


def size(entry, stop, lot_size, limits: Limits, lost_today: float = 0.0) -> dict:
    """How many lots keep the loss at the stop inside the limits. `entry` and `stop` are option premiums (the stop is below the entry: you buy options), `lot_size` is in units, `lost_today` is
    the realised loss so far today in rupees (a positive number). Raises ValueError for numbers that cannot be a trade."""
    for name, v in (("entry premium", entry), ("stop premium", stop), ("lot size", lot_size)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"the {name} must be a positive number")
    if stop >= entry:
        raise ValueError("the stop premium must be below the entry premium: Prism's trades are buys of options, and the loss is what the premium falls by")
    if lot_size != int(lot_size):
        raise ValueError("the lot size must be a whole number of units")
    lot_size = int(lot_size)
    per_unit = entry - stop
    per_lot = per_unit * lot_size
    out = {"entry": entry, "stop": stop, "lot_size": lot_size, "loss_per_unit": per_unit, "loss_per_lot": per_lot, "outlay_per_lot": entry * lot_size,
           "per_trade_loss": limits.per_trade_loss, "max_daily_loss": limits.max_daily_loss, "lost_today": lost_today, "left_today": None,
           "lots_by_trade_limit": None, "lots_by_daily_limit": None, "lots": None, "quantity": None, "loss_at_stop": None, "outlay": None, "outlay_pct_of_capital": None,
           "binding": None, "warnings": []}  # fmt: skip
    if limits.per_trade_loss is not None:
        out["lots_by_trade_limit"] = int(limits.per_trade_loss // per_lot)
    if limits.max_daily_loss is not None:
        out["left_today"] = max(limits.max_daily_loss - lost_today, 0.0)
        out["lots_by_daily_limit"] = int(out["left_today"] // per_lot)
    caps = {k: out[k] for k in ("lots_by_trade_limit", "lots_by_daily_limit") if out[k] is not None}
    if not caps:
        out["warnings"].append("no limit is set: set RISK_CAPITAL and RISK_PER_TRADE_PCT (and RISK_MAX_DAILY_LOSS) in the .env file and restart Prism, and this will say how many lots your own limits allow")
        return out
    out["binding"] = min(caps, key=caps.get)
    lots = caps[out["binding"]]
    out["lots"], out["quantity"] = lots, lots * lot_size
    out["loss_at_stop"], out["outlay"] = lots * per_lot, lots * lot_size * entry
    if limits.capital is not None:
        out["outlay_pct_of_capital"] = 100.0 * out["outlay"] / limits.capital
        if out["outlay"] > limits.capital:
            out["warnings"].append("the premium you would pay for these lots is more than your capital")
    if lots == 0:
        why = "your per-trade limit" if out["binding"] == "lots_by_trade_limit" else "what is left of today's loss limit"
        out["warnings"].append(f"not even one lot fits: one lot loses Rs {per_lot:,.0f} at that stop, more than {why} allows. A nearer stop, or no trade")
    if out["lots_by_daily_limit"] is not None and out["left_today"] == 0:
        out["warnings"].append("today's loss limit is already used up")
    return out
