"""The trades you made yourself, kept next to the journal: what you did, at what price, and what Prism was saying at that moment.

Prism places no orders and reads no account. This is a notebook you fill in by hand (the Trades tab, or `POST /api/trades`), one JSON line per trade in `trades-<date>.jsonl` beside the decision
journal. It is append-only: a mistake is VOIDED (a line that names the trade it cancels), never edited or deleted. The realised profit and loss it shows is arithmetic on the prices you typed, first
in first out per instrument, for long option trades (a SELL needs an open quantity to sell); it knows nothing of brokerage, taxes or slippage, and is not an account statement.

Each BUY is stamped with what Prism's decision said when you logged it (the action, the setup it was about, the spot price), so the day's trades can be set beside the decisions afterwards."""

import json
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .candles import IST

MAX_QTY = 1_000_000
MAX_PRICE = 1_000_000.0
SIDES = ("BUY", "SELL")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
BUY_ACTIONS = ("BUY CE", "BUY PE")


def _text(payload: dict, key: str, limit: int, required: bool = False) -> str | None:
    v = payload.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text")
    v = CONTROL.sub(" ", v).strip()
    if len(v) > limit:
        raise ValueError(f"{key} is longer than {limit} characters")
    return v


def _number(payload: dict, key: str, whole: bool, top: float):
    v = payload.get(key)
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            raise ValueError(f"{key} must be a number") from None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
        raise ValueError(f"{key} must be a positive number")
    if v > top:
        raise ValueError(f"{key} is over {top:g}: a typing mistake?")
    if whole:
        if v != int(v):
            raise ValueError(f"{key} must be a whole number")
        return int(v)
    return float(v)


def clean(payload) -> dict:
    """The fields of a trade, checked: instrument, side (BUY / SELL), qty (units, a whole number), price (premium), and optionally setup_id and note. Raises ValueError that says which field and why."""
    if not isinstance(payload, dict):
        raise ValueError("a trade is an object with instrument, side, qty and price")
    side = _text(payload, "side", 8, required=True).upper()
    if side not in SIDES:
        raise ValueError("side must be BUY or SELL")
    return {"instrument": _text(payload, "instrument", 60, required=True), "side": side, "qty": _number(payload, "qty", True, MAX_QTY), "price": _number(payload, "price", False, MAX_PRICE),
            "setup_id": _text(payload, "setup_id", 120), "note": _text(payload, "note", 300)}  # fmt: skip


def key(instrument: str) -> str:
    """Instruments are matched ignoring case and spacing: `NIFTY 24500 CE` and `nifty  24500 ce` are one."""
    return re.sub(r"\s+", " ", instrument).strip().upper()


def live(records: list) -> list:
    """The trade records that have not been voided, in the order they were made."""
    voided = {r.get("void_of") for r in records if r.get("kind") == "void"}
    return [r for r in records if r.get("kind") == "trade" and r.get("id") not in voided]


def positions(trades: list) -> dict:
    """First in, first out, per instrument: {"realised": rupees, "closed": [...], "open": {instrument: [[qty, price, id], ...]}, "by_trade": {id: {...}}}. Raises ValueError when a SELL has no quantity
    to sell (which is how a sell is refused, and how a void that would leave one is)."""
    book: dict = {}
    realised, closed, by_trade = 0.0, [], {}
    for r in trades:
        k = key(r["instrument"])
        if r["side"] == "BUY":
            book.setdefault(k, []).append([r["qty"], r["price"], r["id"]])
            by_trade[r["id"]] = {"matched": 0, "pnl": 0.0}
            continue
        left, lots = r["qty"], book.get(k, [])
        have = sum(q for q, _, _ in lots)
        if have < left:
            raise ValueError(f"nothing to sell: {r['instrument']} has {have} unit(s) open and the sell is for {r['qty']}")
        pnl = 0.0
        while left:
            q, p, bid = lots[0]
            take = min(q, left)
            gain = (r["price"] - p) * take
            pnl += gain
            by_trade[bid]["matched"] += take
            by_trade[bid]["pnl"] += gain
            closed.append({"instrument": r["instrument"], "qty": take, "buy": p, "sell": r["price"], "pnl": gain, "buy_id": bid, "sell_id": r["id"]})
            lots[0][0] -= take
            left -= take
            if lots[0][0] == 0:
                lots.pop(0)
        realised += pnl
        by_trade[r["id"]] = {"matched": r["qty"], "pnl": pnl}
    return {"realised": realised, "closed": closed, "open": {k: v for k, v in book.items() if v}, "by_trade": by_trade}


class TradeLog:
    """The day's trades, appended to `trades-<date>.jsonl` in `directory`."""

    def __init__(self, directory, clock=time.time):
        self.dir, self.clock, self._lock, self._n = Path(directory), clock, threading.Lock(), 0

    def path(self, now: float) -> Path:
        return self.dir / f"trades-{datetime.fromtimestamp(now, IST).date().isoformat()}.jsonl"

    def load(self, now: float) -> list:
        """Every readable record of that day, in order. A line that is not a record is skipped (a write cut short by a crash costs that line)."""
        p, out = self.path(now), []
        if not p.exists():
            return out
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("kind") in ("trade", "void") and isinstance(r.get("id") or r.get("void_of"), str):
                out.append(r)
        return out

    def _append(self, now: float, rec: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, allow_nan=False, separators=(",", ":"))
        with open(self.path(now), "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def add(self, payload, prism: dict | None = None) -> dict:
        """Record a trade. Raises ValueError (the field and why) for anything that is not one, or a SELL with nothing open to sell. `prism`: what the decision said at this moment (kept on a BUY)."""
        t = clean(payload)
        with self._lock:
            now = self.clock()
            records = self.load(now)
            self._n += 1
            rec = {"v": 1, "kind": "trade", "id": f"t{int(now * 1000)}-{self._n}", "t": now, "time": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"), **t,
                   "prism": prism if t["side"] == "BUY" else None}  # fmt: skip
            positions(live(records) + [rec])  # raises when it is a sell with nothing to sell
            self._append(now, rec)
            return rec

    def void(self, trade_id: str) -> dict:
        """Cancel a trade by naming it (the file is never edited: a line that cancels is added). Refused when it is not there, already cancelled, or when cancelling it would leave a later sell with nothing to sell."""
        with self._lock:
            now = self.clock()
            records = self.load(now)
            target = next((r for r in live(records) if r["id"] == trade_id), None)
            if target is None:
                raise ValueError("no such trade today, or it is already voided")
            rest = [r for r in live(records) if r["id"] != trade_id]
            try:
                positions(rest)
            except ValueError:
                raise ValueError("a later sell depends on this buy: void the sell first") from None
            rec = {"v": 1, "kind": "void", "void_of": trade_id, "t": now, "time": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds")}
            self._append(now, rec)
            return rec

    def summary(self, limits=None) -> dict:
        """Today's trades, what is still open, the realised profit and loss, and where that stands against the daily loss limit."""
        now = self.clock()
        records = self.load(now)
        trades = live(records)
        try:
            book = positions(trades)
        except ValueError:  # only a hand-edited file can get here: say so instead of a wrong number
            return {"available": False, "reason": "the trade file does not add up (a sell with nothing to sell): it was edited by hand?", "trades": trades, "file": self.path(now).name}
        voided = {r["void_of"] for r in records if r.get("kind") == "void"}
        rows = []
        for r in trades:  # a BUY says how much of it has been sold and what that lot made (`lot_pnl`); only a SELL carries `pnl`, so a column of `pnl` adds up to the realised result and nothing is counted twice
            m = book["by_trade"].get(r["id"], {})
            rows.append({**r, "matched": m.get("matched", 0), "pnl": m.get("pnl") if r["side"] == "SELL" else None, "lot_pnl": m.get("pnl") if r["side"] == "BUY" else None, "followed": followed(r)})
        spelled = {r["id"]: r["instrument"] for r in trades}  # (the instrument as you typed it)
        open_ = [{"instrument": spelled[lots_[0][2]], "qty": sum(q for q, _, _ in lots_), "average_price": sum(q * p for q, p, _ in lots_) / sum(q for q, _, _ in lots_)} for lots_ in book["open"].values()]
        lost = max(-book["realised"], 0.0)
        out = {"available": True, "file": self.path(now).name, "trades": rows, "voided": sorted(voided), "open": open_, "realised": book["realised"], "lost_today": lost,
               "limits": limits.as_dict() if limits is not None else None, "daily_loss_used_pct": None, "daily_loss_reached": False,
               "basis": "arithmetic on the prices you typed, long option trades, first in first out; no brokerage, tax or slippage; not an account statement"}  # fmt: skip
        if limits is not None and limits.max_daily_loss:
            out["daily_loss_used_pct"] = 100.0 * lost / limits.max_daily_loss
            out["daily_loss_reached"] = lost >= limits.max_daily_loss
        return out


def followed(r: dict) -> str | None:
    """For a BUY: did Prism call a buy at that moment (`yes`), was it about the setup you named (`yes: setup`), or did you buy when it did not (`no`)? None for a sell or when Prism's answer was not recorded."""
    p = r.get("prism")
    if r.get("side") != "BUY" or not isinstance(p, dict) or p.get("action") is None:
        return None
    if p["action"] not in BUY_ACTIONS:
        return "no"
    return "yes: the setup you named" if r.get("setup_id") and r["setup_id"] == p.get("setup_id") else "yes"
