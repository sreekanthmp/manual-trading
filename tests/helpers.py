from datetime import datetime
from types import SimpleNamespace

from app.candles import IST
from app.instruments import select_contracts
from app.ticks import Tick

SPOT_TOKEN = "99926000"
CFG = SimpleNamespace(gap_seconds=15.0, stale_seconds=45.0, cache_dir=".cache")


def ist(h, m, s=0, day=(2026, 11, 6)):
    """Epoch seconds for a wall-clock time in IST (2026-11-06 is a Friday)."""
    return datetime(*day, h, m, s, tzinfo=IST).timestamp()


def tick(token, price, ts, recv=None, volume=None, ltq=None, exch=1):
    return Tick(token, exch, price, ts, ts if recv is None else recv, volume, ltq)


def msg(token=SPOT_TOKEN, price=24500.0, ts=None, exch=1, volume=None):
    """A message as produced by SmartWebSocketV2's parser: prices in paise, timestamp in ms."""
    m = {
        "subscription_mode": 2,
        "exchange_type": exch,
        "token": token,
        "last_traded_price": int(round(price * 100)),
        "exchange_timestamp": int(ts * 1000),
    }
    if volume is not None:
        m["volume_trade_for_the_day"] = volume
    return m


def master_rows(expiries=("10NOV2026", "17NOV2026"), lo=24000, hi=25000, step=50):
    rows = [
        {"token": SPOT_TOKEN, "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "-1.000000",
         "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE"},
        {"token": "1", "symbol": "Nifty Bank", "name": "BANKNIFTY", "expiry": "", "strike": "-1.000000",
         "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE"},
    ]  # fmt: skip
    n = 1000
    for exp in expiries:
        for strike in range(lo, hi + 1, step):
            for kind in ("CE", "PE"):
                n += 1
                rows.append(
                    {"token": str(n), "symbol": f"NIFTY{exp}{strike}{kind}", "name": "NIFTY", "expiry": exp,
                     "strike": f"{strike * 100:.6f}", "lotsize": "75", "instrumenttype": "OPTIDX",
                     "exch_seg": "NFO"}
                )  # fmt: skip
    return rows


def sample_contracts(spot=24510.0):
    return select_contracts(master_rows(), spot, datetime(2026, 11, 6, 10, 0, tzinfo=IST))


# -- Phase 2 builders ----------------------------------------------------------------------------------------
from datetime import date, timedelta  # noqa: E402

from app.candles import Candle  # noqa: E402

THU, FRI, MON, TUE, WED = (2026, 11, 5), (2026, 11, 6), (2026, 11, 9), (2026, 11, 10), (2026, 11, 11)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def hhmm(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def session_candles(day, secs=60, drop=(), price=100.0, source="db", volume=0):
    """Every interval of one trading day (09:15-15:30) except those whose HH:MM start is in `drop`."""
    out, t, close = [], ist(9, 15, day=day), ist(15, 30, day=day)
    while t < close:
        if hhmm(t) not in drop:
            out.append(Candle(t, min(t + secs, close), price, price + 1, price - 1, price, volume, source=source))
        t += secs
    return out


def ramp(secs, days, step, end_day=FRI, source="db"):
    """A steadily rising series: one candle per interval on each of the `days` weekdays before end_day."""
    weekdays, d = [], date(*end_day)
    while len(weekdays) < days:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            weekdays.append(d)
    out, px = [], 24000.0
    for d in reversed(weekdays):
        for c in session_candles((d.year, d.month, d.day), secs):
            px += step
            out.append(Candle(c.start, c.end, px - step, px + 0.3, px - step - 0.3, px, 0, source=source))
    return out
