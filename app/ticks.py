"""Turn a decoded WebSocket V2 message into a Tick and validate it."""

import math
from dataclasses import dataclass

PRICE_DIVISOR = 100.0  # NSE cash and F&O prices arrive in paise
FUTURE_TOLERANCE = 5.0  # seconds an exchange timestamp may lead our clock


@dataclass(frozen=True)
class Tick:
    token: str
    exch_type: int
    ltp: float  # rupees
    exch_ts: float | None  # the exchange's own time for this message (Angel One's exchange_timestamp, which its quote API calls exchFeedTime), epoch seconds.
    # None only for an option whose message had no usable exchange timestamp: it is then never replaced by the receipt time (see exch_note)
    recv_ts: float  # local receipt time, epoch seconds
    volume: int | None = None  # cumulative traded volume for the day
    ltq: int | None = None  # last traded quantity
    # What the snap-quote mode adds. None means the broker did not provide it (or it failed validation): it is never guessed.
    prev_close: float | None = None  # previous session's close, rupees
    bid: float | None = None  # best bid price, rupees
    ask: float | None = None  # best ask price, rupees
    bid_qty: int | None = None  # quantity at the best bid
    ask_qty: int | None = None  # quantity at the best ask
    oi: int | None = None  # open interest
    last_trade_ts: float | None = None  # when the last trade happened, epoch seconds (a different clock from exch_ts: it says nothing about the order book)
    exch_note: str | None = None  # why exch_ts is None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _best(levels, pick) -> tuple[float | None, int | None]:
    """The best price (rupees) and its quantity from a list of depth levels `{price (paise), quantity}`. `pick` is max for bids, min for asks. Empty
    levels (no price or no quantity, which the feed sends for an unfilled book) are ignored; no valid level means (None, None)."""
    good = [(lv["price"], lv["quantity"]) for lv in levels or () if isinstance(lv, dict) and _is_int(lv.get("price")) and _is_int(lv.get("quantity"))
            and lv["price"] > 0 and lv["quantity"] > 0]  # fmt: skip
    if not good:
        return None, None
    price = pick(p for p, _ in good)
    return round(price / PRICE_DIVISOR, 2), next(q for p, q in good if p == price)


def option_time(ts_ms, recv_ts: float) -> tuple[float | None, str | None]:
    """The exchange time of an option message in epoch seconds, or (None, the reason it cannot be used). Missing, not a number, zero or negative, and
    in the future (more than FUTURE_TOLERANCE ahead of the receipt time) all give None: the receipt time is never substituted, so an unknown
    exchange time stays unknown and is treated as untrusted downstream."""
    if ts_ms is None:
        return None, "the exchange timestamp was not provided"
    if not _is_int(ts_ms):
        return None, "the exchange timestamp is not a number"
    if ts_ms <= 0:
        return None, "the exchange timestamp is zero or negative"
    ts = ts_ms / 1000.0  # the feed sends milliseconds
    if ts > recv_ts + FUTURE_TOLERANCE:
        return None, f"the exchange timestamp is {ts - recv_ts:.0f} s in the future (the PC clock is behind, or the timestamp is wrong)"
    return ts, None


def decode(msg: dict, recv_ts: float, strict_time: bool = True) -> Tick:
    """msg is the dict produced by SmartWebSocketV2's binary parser. Raises ValueError if malformed.

    `strict_time` (the default, used for the NIFTY spot, whose candles and gap detection need the exchange time): a missing or non-integer exchange
    timestamp is an error. With `strict_time=False` (the option contracts) the message is kept and the exchange time becomes None, with the reason
    in `exch_note`: a quote whose own time is unknown is shown as such, and is never trusted as fresh."""
    try:
        token, exch_type = msg["token"], msg["exchange_type"]
        raw_price = msg["last_traded_price"]
        ts_ms = msg["exchange_timestamp"] if strict_time else msg.get("exchange_timestamp")
    except (KeyError, TypeError) as e:
        raise ValueError(f"missing field {e}") from e
    if not token or not _is_int(exch_type) or not _is_int(raw_price) or (strict_time and not _is_int(ts_ms)):
        raise ValueError("malformed field types")
    exch_ts, exch_note = (ts_ms / 1000.0, None) if strict_time else option_time(ts_ms, recv_ts)
    volume, ltq = msg.get("volume_trade_for_the_day"), msg.get("last_traded_quantity")
    closed, oi, ltt = msg.get("closed_price"), msg.get("open_interest"), msg.get("last_traded_timestamp")
    bid, bid_qty = _best(msg.get("best_5_buy_data"), max)  # the SDK's own names: buy = bids, sell = asks. A crossed book is caught where it is used
    ask, ask_qty = _best(msg.get("best_5_sell_data"), min)
    if _is_int(ltt) and ltt > 0:
        ltt = ltt / 1000.0 if ltt > 1e11 else float(ltt)  # seconds, or milliseconds
        ltt = ltt if ltt <= recv_ts + FUTURE_TOLERANCE else None  # a last-trade time in the future is not believed
    else:
        ltt = None
    return Tick(
        token=str(token),
        exch_type=exch_type,
        ltp=round(raw_price / PRICE_DIVISOR, 2),
        exch_ts=exch_ts,
        recv_ts=recv_ts,
        volume=volume if _is_int(volume) else None,
        ltq=ltq if _is_int(ltq) else None,
        prev_close=round(closed / PRICE_DIVISOR, 2) if _is_int(closed) and closed > 0 else None,
        bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty,
        oi=oi if _is_int(oi) and oi >= 0 else None,
        last_trade_ts=ltt, exch_note=exch_note,
    )  # fmt: skip


class TickValidator:
    """check() returns None for a good tick, otherwise the reason it was rejected."""

    def __init__(self, tokens: dict[str, int]):
        self.tokens = tokens  # token -> expected exchange type
        self._last: dict[str, tuple[float | None, tuple, float | None]] = {}  # token -> (highest exchange time seen, last quote, that quote's own exchange time)

    def check(self, t: Tick) -> str | None:
        if self.tokens.get(t.token) != t.exch_type:
            return "unknown_instrument"
        if not (math.isfinite(t.ltp) and t.ltp > 0):
            return "bad_price"
        if t.exch_ts is not None and (t.exch_ts <= 0 or t.exch_ts > t.recv_ts + FUTURE_TOLERANCE):
            return "bad_timestamp"  # (an option's unusable time is already None: see decode)
        key = (t.ltp, t.volume, t.ltq, t.bid, t.ask, t.bid_qty, t.ask_qty, t.oi)  # a change of the book alone is a new quote, not a duplicate
        prev = self._last.get(t.token)
        if prev:
            if t.exch_ts is not None and prev[0] is not None and t.exch_ts < prev[0]:
                return "out_of_order"  # an older exchange time than one already accepted: the newer data is kept
            if t.exch_ts == prev[2] and key == prev[1]:
                return "duplicate"  # the same quote again (two messages with no exchange time and the same content count too)
        # the highest exchange time seen stays the mark for out-of-order, even across a message that had none
        self._last[t.token] = (t.exch_ts if t.exch_ts is not None else (prev[0] if prev else None), key, t.exch_ts)
        return None
