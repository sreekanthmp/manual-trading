"""Phase 4: which option contract could I consider, and what is wrong with it? Pure functions over the live quotes.

The Phase 1-3 engine decides the underlying's direction and the setup. This only looks at the option contracts themselves: is the quote fresh, is
there a bid and an ask, how wide is the spread, is anything traded, how far from the money is the strike, how close is the expiry. It never
predicts a price, never scores a contract, and never places an order: Prism is a manual decision-support tool.

Every finding is a FLAG with a severity:
  avoid  the contract cannot be judged or traded sensibly on the data: no quote, a stale quote, no bid or ask, a crossed book, a spread wider than
         the maximum, a price below the option's intrinsic value, no liquidity information, volume under the minimum. An "avoid" contract is
         never selected.
  warn   something to know before trading it: a wide-ish spread, thin liquidity next to its neighbours, missing OI, far from the money, an inconsistent
         premium next to its neighbours, an expiry that is today or close.
A contract is offered only when Phase 3's decision is BUY CE / BUY PE, from the contracts of that side with no "avoid" flag, in this documented order:
fewest warnings, then the tightest spread, then the nearest to the money. That order is a convention, not a ranking of merit and not a score.

All thresholds are initial assumptions about what is "wide" or "thin": they are configurable (OPTIONS_* environment variables) and are meant to be
tuned to what you see on your own screen. A value of 0 switches a minimum off."""

import os
from dataclasses import dataclass, fields
from datetime import date, datetime

from .candles import IST, in_session


@dataclass(frozen=True)
class OptionParams:
    """Every option threshold in one place. Override with environment variables OPTIONS_<NAME>. Purposes are in the comments."""

    # which contracts are watched
    strikes_each_side: int = 3  # strikes either side of ATM that are subscribed, for each expiry watched
    expiries: int = 2  # how many of the nearest expiries are watched, so that they can be compared
    recentre_hysteresis: float = 0.75  # re-centre the strikes once the spot is this many strike steps from the centre strike (above 0.5: no flapping)
    # data quality
    quote_stale_seconds: float = 30.0  # RECEIPT age: a quote Prism received longer ago than this is shown as stale, never as live, and the contract is not selected
    exchange_stale_seconds: float = 30.0  # EXCHANGE-TIME age: if the exchange's own time for the quote is older than this, the quote is stale however recently Prism received it, and the
    # contract is not selected. An operational placeholder (set equal to the receipt limit): it closes the gap where a message received just now, for example the snapshot sent when a
    # contract is first subscribed, carries the time of an exchange update that is minutes or hours old. Distinct from the last-trade time, which is shown but never used for freshness
    # spread (the ask minus the bid, as a percentage of the mid price)
    warn_spread_pct: float = 1.0  # wider than this is flagged
    max_spread_pct: float = 3.0  # wider than this: the contract is not selected
    # liquidity
    min_volume: int = 100_000  # day volume (units) below this: not selected. 0 = no minimum
    min_open_interest: int = 100_000  # open interest (units) below this is flagged. 0 = no minimum
    min_top_quantity_lots: float = 1.0  # the quantity at the best bid / ask, in lots, below this is flagged. 0 = no minimum
    min_relative_volume: float = 0.10  # volume below this share of the busiest contract of the same side and expiry is flagged. 0 = off
    # strike and expiry
    far_from_atm_steps: int = 2  # more strikes than this from ATM is flagged (far OTM or deep ITM)
    decay_warning_days: int = 1  # this many days to expiry or fewer: warn about fast time decay (0 = expiry day only)
    intrinsic_tolerance: float = 1.0  # index points a premium may sit under the intrinsic value before it is called inconsistent

    @classmethod
    def from_env(cls, prefix: str = "OPTIONS_", env=None) -> "OptionParams":
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            raw = env.get(prefix + f.name.upper())
            if raw is None:
                continue
            kind = {"int": int, "float": float}[f.type if isinstance(f.type, str) else f.type.__name__]
            try:
                values[f.name] = kind(raw)
            except ValueError as e:
                raise ValueError(f"{prefix}{f.name.upper()}={raw!r} is not a valid {kind.__name__}") from e
        p = cls(**values)
        if p.strikes_each_side < 1 or p.expiries < 1 or not 0.5 <= p.recentre_hysteresis <= 3.0 or min(
                p.quote_stale_seconds, p.exchange_stale_seconds, p.warn_spread_pct, p.max_spread_pct, p.min_volume, p.min_open_interest, p.min_top_quantity_lots,
                p.min_relative_volume, p.far_from_atm_steps, p.decay_warning_days, p.intrinsic_tolerance) < 0 or p.warn_spread_pct > p.max_spread_pct:
            raise ValueError("OPTIONS_* settings out of range (strikes_each_side and expiries >= 1, recentre_hysteresis 0.5 to 3, the rest >= 0, warn_spread_pct <= max_spread_pct)")
        return p


def flag(severity: str, area: str, code: str, text: str) -> dict:
    return {"severity": severity, "area": area, "code": code, "text": text}


def fmt_age(seconds: float) -> str:
    s = max(0.0, seconds)
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h"


def exchange_freshness(t, now: float, p: OptionParams) -> tuple[float | None, str, str | None]:
    """(exchange-time age, status, reason) of a quote. The age is measured from the exchange's own timestamp for the message, never from the time Prism
    received it. Status: 'fresh' within the limit, 'stale' beyond it, 'unknown' when there is no quote or the message had no usable exchange time (the reason
    says why). An unknown time is never turned into a fresh one."""
    if t is None:
        return None, "unknown", "no quote has been received"
    if t.exch_ts is None:
        return None, "unknown", t.exch_note or "the exchange timestamp is not available"
    age = max(0.0, now - t.exch_ts)  # an exchange time a moment ahead of the PC clock (within the tolerance the decoder allows) is age 0, not negative
    if age > p.exchange_stale_seconds:
        return age, "stale", f"the exchange's last update was {fmt_age(age)} ago (limit {p.exchange_stale_seconds:g} s): a message that arrives now can carry old data"
    return age, "fresh", None


def days_to_expiry(expiry: date, now: float) -> int:
    return (expiry - datetime.fromtimestamp(now, IST).date()).days


def moneyness(kind: str, strike: float, spot: float, steps: int) -> str:
    if steps == 0:
        return "ATM"
    return "ITM" if (strike < spot if kind == "CE" else strike > spot) else "OTM"


def intrinsic(kind: str, strike: float, spot: float) -> float:
    return max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)


def quote_row(c, t, spot: float | None, spot_fresh: bool, steps: int, now: float, p: OptionParams, live: bool) -> dict:
    """One contract's quote and everything that can be said about it from that quote alone. `t` is the last Tick (None: nothing received)."""
    kind, dte = c.kind, days_to_expiry(c.expiry, now)
    age = None if t is None else now - t.recv_ts
    status = "market closed" if not live else "no data" if t is None else "stale" if age > p.quote_stale_seconds else "fresh"
    bid, ask = (t.bid, t.ask) if t is not None else (None, None)
    xage, xstatus, xreason = exchange_freshness(t, now, p)
    trade_age = None if t is None or t.last_trade_ts is None else max(0.0, now - t.last_trade_ts)
    book_ok = bid is not None and ask is not None and bid <= ask
    mid = (bid + ask) / 2 if book_ok else None
    spread = ask - bid if book_ok else None
    spread_pct = 100 * spread / mid if book_ok and mid > 0 else None
    ref = mid if book_ok else (None if t is None else t.ltp)  # the price a premium is judged by: the mid when the book is valid, else the last trade
    intr = None if spot is None else intrinsic(kind, c.strike, spot)
    row = {
        "token": c.token, "symbol": c.symbol, "kind": kind, "strike": c.strike, "expiry": c.expiry.isoformat(), "days_to_expiry": dte, "expiry_day": dte == 0,
        "lot_size": c.lot_size, "steps_from_atm": steps, "moneyness": None if spot is None else moneyness(kind, c.strike, spot, steps),
        "distance_points": None if spot is None else c.strike - spot, "intrinsic": intr,
        "ltp": None if t is None else t.ltp, "bid": bid, "ask": ask, "bid_qty": None if t is None else t.bid_qty, "ask_qty": None if t is None else t.ask_qty,
        "mid": mid, "spread": spread, "spread_pct": spread_pct, "volume": None if t is None else t.volume, "oi": None if t is None else t.oi,
        "prev_close": None if t is None else t.prev_close, "change": None if t is None or t.prev_close is None else round(t.ltp - t.prev_close, 2),
        "change_pct": None if t is None or not t.prev_close else round(100 * (t.ltp - t.prev_close) / t.prev_close, 2),
        "exchange_time": None if t is None else t.exch_ts, "received_time": None if t is None else t.recv_ts, "last_trade_time": None if t is None else t.last_trade_ts,
        "age": age, "status": status, "receipt_age": age,  # the time since Prism received the message (age and status are the receipt-based ones, unchanged)
        "exchange_age": xage, "exchange_status": xstatus, "exchange_reason": xreason, "last_trade_age": trade_age,  # since the exchange's own time; since the last trade (shown only)
        "freshness": ("market closed" if not live else "no data" if t is None else "stale" if "stale" in (status, xstatus) else "unknown" if xstatus == "unknown" else "fresh"),
        "ref_price": ref, "book": "no quote" if t is None else "valid" if book_ok else "crossed" if bid is not None and ask is not None else "missing",
        "flags": [],
    }  # fmt: skip
    f = row["flags"]
    # -- is it live? --
    if not live:
        f.append(flag("avoid", "freshness", "market_closed", "the market is closed: this is the last quote received, not a live one"))
    elif not spot_fresh:
        f.append(flag("avoid", "freshness", "spot_stale", "the NIFTY spot is stale: the distance from the money and the intrinsic value cannot be trusted"))
    if t is None:
        f.append(flag("avoid", "freshness", "no_quote", "no quote has been received for this contract"))
    elif live and status == "stale":
        f.append(flag("avoid", "freshness", "stale_quote", f"the last quote is {age:.0f} s old (limit {p.quote_stale_seconds:g} s): it is not shown as live"))
    if t is not None and live and xstatus == "stale":  # received recently, but the exchange data in it is old
        f.append(flag("avoid", "freshness", "exchange_time_stale", f"exchange-time age {fmt_age(xage)} is over the limit of {p.exchange_stale_seconds:g} s: the message was received "
                      f"{fmt_age(age)} ago, but the exchange's own time for the quote is {fmt_age(xage)} old. Recent receipt is not fresh data"))  # fmt: skip
    elif t is not None and live and xstatus == "unknown":
        f.append(flag("avoid", "freshness", "exchange_time_unknown", f"{xreason}: how fresh the exchange data is cannot be known, so the quote is not trusted"))
    # -- the book and the spread --
    if t is not None:
        if row["book"] == "missing":
            f.append(flag("avoid", "spread", "no_bid_ask", "the bid or the ask is not provided: the spread cannot be judged (an LTP alone does not make a contract tradable)"))
        elif row["book"] == "crossed":
            f.append(flag("avoid", "spread", "crossed_book", "the bid is above the ask: the book is invalid, or its two sides are reversed. It is not used"))
        elif spread_pct is not None and spread_pct > p.max_spread_pct:
            f.append(flag("avoid", "spread", "wide_spread", f"spread {spread:.2f} = {spread_pct:.2f}% of the mid price is over the maximum of {p.max_spread_pct:g}%"))
        elif spread_pct is not None and spread_pct > p.warn_spread_pct:
            f.append(flag("warn", "spread", "spread_high", f"spread {spread:.2f} = {spread_pct:.2f}% of the mid price is over {p.warn_spread_pct:g}%"))
    # -- the premium --
    if t is not None and ref is not None and intr is not None and spot_fresh and ref < intr - p.intrinsic_tolerance:
        f.append(flag("avoid", "premium", "below_intrinsic", f"the price {ref:.2f} is below the intrinsic value {intr:.2f}: a stale or wrong quote"))
    # -- liquidity: what was actually provided --
    if t is not None:
        qty = [q for q in (t.bid_qty, t.ask_qty) if q is not None]
        if t.volume is None and t.oi is None and not qty:
            f.append(flag("avoid", "liquidity", "no_liquidity_info", "no volume, open interest or quote quantity was provided: liquidity is unknown, and an LTP alone does not show it"))
        else:
            if t.volume is None:
                f.append(flag("warn", "liquidity", "no_volume", "the day volume was not provided"))
            elif p.min_volume and t.volume < p.min_volume:
                f.append(flag("avoid", "liquidity", "low_volume", f"day volume {t.volume:,} units is under the minimum of {p.min_volume:,} units"))
            if t.oi is None:
                f.append(flag("warn", "liquidity", "no_oi", "the open interest was not provided"))
            elif p.min_open_interest and t.oi < p.min_open_interest:
                f.append(flag("warn", "liquidity", "low_oi", f"open interest {t.oi:,} units is under {p.min_open_interest:,} units"))
            if len(qty) == 2 and p.min_top_quantity_lots and min(qty) < p.min_top_quantity_lots * (c.lot_size or 1):
                f.append(flag("warn", "liquidity", "thin_top_quantity", f"only {min(qty):,} units at the best bid or ask: under {p.min_top_quantity_lots:g} lot ({c.lot_size} units)"))
    # -- the strike --
    if steps > p.far_from_atm_steps and spot is not None:
        f.append(flag("warn", "strike", "far_from_atm", f"{steps} strikes from ATM ({row['moneyness']}, {abs(c.strike - spot):.0f} points from the spot)"
                      + (": NIFTY has to move a long way before this gains value" if row["moneyness"] == "OTM" else ": a deep in-the-money contract costs more premium")))  # fmt: skip
    # -- the expiry --
    if dte == 0:
        f.append(flag("warn", "expiry", "expiry_day", "this contract expires today: time decay is fastest and the premium can fall quickly even if NIFTY does not move"))
    elif dte <= p.decay_warning_days:
        f.append(flag("warn", "expiry", "near_expiry", f"{dte} day(s) to expiry: time decay accelerates close to expiry"))
    return row


def analyse(contracts: list, ticks: dict, spot: float | None, spot_age: float | None, spot_limit: float, now: float, p: OptionParams) -> dict:
    """Every watched option, analysed against its own quote and its neighbours. `ticks`: token -> last Tick."""
    live = in_session(now)
    spot_fresh = spot is not None and spot_age is not None and spot_age <= spot_limit
    options = [c for c in contracts if c.kind]
    by_expiry: dict = {}
    for c in options:
        by_expiry.setdefault(c.expiry, set()).add(c.strike)
    atm = {e: min(sorted(s), key=lambda x: (abs(x - spot), x)) if spot is not None else None for e, s in by_expiry.items()}
    pos = {e: sorted(s) for e, s in by_expiry.items()}
    rows = []
    for c in options:
        strikes = pos[c.expiry]
        steps = 0 if atm[c.expiry] is None else abs(strikes.index(c.strike) - strikes.index(atm[c.expiry]))
        rows.append(quote_row(c, ticks.get(c.token), spot, spot_fresh, steps, now, p, live))
    # next to its neighbours: relative liquidity, and a premium that does not fall (calls) or rise (puts) with the strike
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["kind"], r["expiry"]), []).append(r)
    for (kind, _), g in groups.items():
        g.sort(key=lambda r: r["strike"])
        top = max((r["volume"] for r in g if r["volume"] is not None), default=0)
        for r in g:
            if p.min_relative_volume and top > 0 and r["volume"] is not None and r["volume"] / top < p.min_relative_volume and r["status"] == "fresh":
                r["flags"].append(flag("warn", "liquidity", "thin_relative_volume", f"volume {r['volume']:,} units is {100 * r['volume'] / top:.0f}% of the busiest {kind} of this expiry (under {100 * p.min_relative_volume:.0f}%)"))
        priced = [r for r in g if r["status"] == "fresh" and r["ref_price"] is not None]
        for lo, hi in zip(priced, priced[1:]):
            wrong = lo["ref_price"] < hi["ref_price"] if kind == "CE" else lo["ref_price"] > hi["ref_price"]
            if wrong:
                text = (f"the {kind} premium {lo['ref_price']:.2f} at {lo['strike']:g} is below {hi['ref_price']:.2f} at {hi['strike']:g}" if kind == "CE" else
                        f"the {kind} premium {lo['ref_price']:.2f} at {lo['strike']:g} is above {hi['ref_price']:.2f} at {hi['strike']:g}") + ": inconsistent with its neighbour (a stale or bad quote)"
                for r in (lo, hi):
                    r["flags"].append(flag("warn", "premium", "premium_inconsistent", text))
    for r in rows:
        r["blocking"] = any(x["severity"] == "avoid" for x in r["flags"])
        r["warnings"] = sum(1 for x in r["flags"] if x["severity"] == "warn")
    rows.sort(key=lambda r: (r["expiry"], r["kind"], r["strike"]))
    expiries = [{"expiry": e.isoformat(), "days_to_expiry": days_to_expiry(e, now), "expiry_day": days_to_expiry(e, now) == 0,
                 "decay_warning": days_to_expiry(e, now) <= p.decay_warning_days} for e in sorted(by_expiry)]  # fmt: skip
    return {"spot": spot, "spot_age": spot_age, "spot_fresh": spot_fresh, "live": live, "expiries": expiries, "contracts": rows}


def order_key(r: dict) -> tuple:
    return (r["warnings"], r["spread_pct"] if r["spread_pct"] is not None else float("inf"), r["steps_from_atm"], abs(r["distance_points"] or 0.0))


def select(rows: list, action: str | None) -> dict:
    """The contract to consider, or why there is none. Only when Phase 3's decision is BUY CE / BUY PE: any other decision gives no selection, not
    even a tentative one."""
    if action not in ("BUY CE", "BUY PE"):
        return {"actionable": False, "kind": None, "contract": None, "alternatives": [], "rejected": [], "why": [],
                "notice": f"no options selection: Phase 3 says {action or 'nothing yet'}. The table below is for information only"}  # fmt: skip
    kind = action[-2:]
    cand = [r for r in rows if r["kind"] == kind]
    ok = sorted((r for r in cand if not r["blocking"]), key=order_key)
    if not ok:
        rejected = [{"symbol": r["symbol"], "strike": r["strike"], "expiry": r["expiry"], "reasons": [x["text"] for x in r["flags"] if x["severity"] == "avoid"]}
                    for r in sorted(cand, key=lambda r: (r["steps_from_atm"], r["expiry"]))[:6]]  # fmt: skip
        return {"actionable": False, "kind": kind, "contract": None, "alternatives": [], "rejected": rejected, "why": [],
                "notice": f"no suitable {kind} contract is available: every one has a blocking data or liquidity problem" if cand else f"no {kind} contract is being watched"}  # fmt: skip
    best = ok[0]
    same = [r for r in ok if r["warnings"] == best["warnings"]]
    why = [f"{len(ok)} of {len(cand)} {kind} contracts have no blocking problem",
           f"it has the fewest warnings ({best['warnings']})" + (f"; of the {len(same)} with that many, it has the tightest spread ({best['spread_pct']:.2f}% of the mid)" if len(same) > 1 else f" and a spread of {best['spread_pct']:.2f}% of the mid"),
           f"{best['moneyness']}, {best['steps_from_atm']} strike(s) from ATM, expiry {best['expiry']} ({best['days_to_expiry']} day(s))",
           "this order (fewest warnings, then tightest spread, then nearest the money) is a convention, not a score, and says nothing about profit"]  # fmt: skip
    return {"actionable": True, "kind": kind, "contract": best, "alternatives": ok[1:3], "rejected": [], "why": why,
            "notice": f"a {kind} contract you could consider: the market data does not rule it out. Prism does not place orders and does not predict that it will gain"}  # fmt: skip
