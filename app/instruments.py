"""Instrument master loading and NIFTY contract selection. Nothing is hardcoded: tokens, expiry,
strikes and lot size all come from the master file."""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

from .candles import CLOSE_S, IST

log = logging.getLogger(__name__)

MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
EXCH_TYPE = {"NSE": 1, "NFO": 2}  # WebSocket V2 exchangeType codes


@dataclass(frozen=True)
class Contract:
    role: str  # SPOT, ATM_CE, ATM_PE, ITM_CE, ITM_PE
    token: str
    symbol: str
    exch_seg: str
    expiry: date | None = None
    strike: float | None = None
    lot_size: int | None = None

    @property
    def exch_type(self) -> int:
        return EXCH_TYPE[self.exch_seg]

    @property
    def has_volume(self) -> bool:
        return self.role != "SPOT"  # an index has no traded volume

    @property
    def kind(self) -> str | None:
        """'CE' or 'PE' for an option contract, None for the spot."""
        return self.symbol[-2:] if self.exch_seg == "NFO" and self.symbol[-2:] in ("CE", "PE") else None


def _is_nifty_row(r: dict) -> bool:
    if r.get("name") != "NIFTY":
        return False
    return (r.get("exch_seg") == "NFO" and r.get("instrumenttype") == "OPTIDX") or (
        r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX"
    )


def load_master(cache_dir: str = ".cache", today: date | None = None, url: str = MASTER_URL) -> list[dict]:
    """NIFTY rows of the instrument master. The (small) filtered result is cached once per day."""
    today = today or datetime.now(IST).date()
    cache = Path(cache_dir) / f"nifty-master-{today.isoformat()}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    rows = [r for r in resp.json() if _is_nifty_row(r)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows), encoding="utf-8")
    log.info("instrument master loaded: %d NIFTY rows", len(rows))
    return rows


def find_spot(rows: list[dict]) -> Contract:
    spots = [r for r in rows if _is_nifty_row(r) and r["exch_seg"] == "NSE"]
    if not spots:
        raise ValueError("NIFTY spot not found in instrument master")
    row = next((r for r in spots if r["symbol"].lower() == "nifty 50"), spots[0])
    return Contract("SPOT", row["token"], row["symbol"], "NSE")


def _parse_expiry(s: str) -> date:
    return datetime.strptime(s, "%d%b%Y").date()


def select_contracts(rows: list[dict], spot_price: float, now: datetime | None = None) -> list[Contract]:
    """Spot + ATM CE/PE + one-strike ITM CE/PE for the nearest expiry that has all of them."""
    now = now or datetime.now(IST)
    today = now.date()
    after_close = now.hour * 3600 + now.minute * 60 + now.second >= CLOSE_S
    spot = find_spot(rows)

    # expiry -> strike -> {"CE": row, "PE": row}
    chain: dict[date, dict[float, dict[str, dict]]] = {}
    for r in rows:
        if not _is_nifty_row(r) or r["exch_seg"] != "NFO" or not r.get("token"):
            continue
        kind = r["symbol"][-2:]
        if kind not in ("CE", "PE"):
            continue
        try:
            exp, strike = _parse_expiry(r["expiry"]), float(r["strike"]) / 100.0
        except (KeyError, ValueError):
            continue
        if exp < today or (exp == today and after_close):
            continue
        chain.setdefault(exp, {}).setdefault(strike, {})[kind] = r

    for exp in sorted(chain):
        strikes = sorted(s for s, legs in chain[exp].items() if len(legs) == 2)
        if len(strikes) < 3:
            continue
        i = min(range(len(strikes)), key=lambda k: (abs(strikes[k] - spot_price), strikes[k]))
        if i == 0 or i == len(strikes) - 1:
            continue  # no strike on one side of ATM: this expiry is not usable
        atm, itm_ce, itm_pe = strikes[i], strikes[i - 1], strikes[i + 1]  # lower call / higher put are ITM
        out = [spot]
        for role, strike, kind in (
            ("ATM_CE", atm, "CE"),
            ("ATM_PE", atm, "PE"),
            ("ITM_CE", itm_ce, "CE"),
            ("ITM_PE", itm_pe, "PE"),
        ):
            r = chain[exp][strike][kind]
            out.append(Contract(role, r["token"], r["symbol"], r["exch_seg"], exp, strike, int(float(r["lotsize"]))))
        if len({c.token for c in out}) != len(out):
            continue
        return out
    raise ValueError("no valid NIFTY expiry with ATM and ITM CE/PE contracts found")


# -- the option chain, parsed once, and the window of strikes around the money --------------------------------------------------------------
@dataclass(frozen=True)
class Window:
    """The contracts being watched: the spot and the strikes around ATM of the first expiries."""

    contracts: list  # spot first
    primary: date  # the first expiry in the window
    center: float  # the ATM strike of the primary expiry when the window was made
    spot: float  # the spot it was made at


class Chain:
    """Every NIFTY option of the instrument master that can still be traded, organised as expiry -> strike -> {CE, PE}. Built once per day (the
    master itself is cached on disk once per day); choosing and re-centring a window afterwards is a lookup in memory, never another read of the
    master. Strikes and expiries are whatever the master has: no strike interval and no expiry date is assumed."""

    def __init__(self, rows: list[dict], now: datetime | None = None):
        now = now or datetime.now(IST)
        self.today = now.date()
        after_close = now.hour * 3600 + now.minute * 60 + now.second >= CLOSE_S
        self.spot_contract = find_spot(rows)
        by: dict[date, dict[float, dict[str, dict]]] = {}
        for r in rows:
            if not _is_nifty_row(r) or r["exch_seg"] != "NFO" or not r.get("token"):
                continue
            kind = r["symbol"][-2:]
            if kind not in ("CE", "PE"):
                continue
            try:
                exp, strike = _parse_expiry(r["expiry"]), float(r["strike"]) / 100.0
            except (KeyError, ValueError):
                continue
            if exp < self.today or (exp == self.today and after_close):
                continue  # expired, or expiring today and the market is over
            by.setdefault(exp, {}).setdefault(strike, {})[kind] = r
        self._by: dict[date, dict[float, dict[str, dict]]] = {}
        for exp in sorted(by):
            strikes = {s: legs for s, legs in sorted(by[exp].items()) if len(legs) == 2}  # a strike needs both a call and a put
            if len(strikes) >= 3:
                self._by[exp] = strikes
        self.expiries: list[date] = list(self._by)

    def strikes(self, expiry: date) -> list[float]:
        return list(self._by[expiry])

    @staticmethod
    def atm_index(strikes: list[float], spot: float) -> int:
        """The position of the strike nearest the spot (the lower one on a tie)."""
        return min(range(len(strikes)), key=lambda k: (abs(strikes[k] - spot), strikes[k]))

    def contract(self, expiry: date, strike: float, kind: str, role: str = "OPT") -> Contract:
        r = self._by[expiry][strike][kind]
        return Contract(role, r["token"], r["symbol"], r["exch_seg"], expiry, strike, int(float(r["lotsize"])))

    def window(self, spot: float, each_side: int = 3, n_expiries: int = 2) -> Window:
        """`each_side` strikes either side of ATM (by position in the real strike list), for the first `n_expiries` expiries that have a strike on
        both sides of ATM. The first expiry keeps the roles Phases 1-3 know (ATM_CE, ATM_PE, one strike ITM_CE / ITM_PE); everything else is OPT."""
        contracts, expiries, center = [self.spot_contract], [], None
        for exp in self.expiries:
            strikes = self.strikes(exp)
            i = self.atm_index(strikes, spot)
            if i == 0 or i == len(strikes) - 1:
                continue  # no strike on one side of ATM: this expiry is not usable
            first = not expiries
            for j in range(max(0, i - each_side), min(len(strikes), i + each_side + 1)):
                for kind in ("CE", "PE"):
                    role = "OPT"
                    if first and (j == i or (kind == "CE" and j == i - 1) or (kind == "PE" and j == i + 1)):
                        role = f"ATM_{kind}" if j == i else f"ITM_{kind}"
                    contracts.append(self.contract(exp, strikes[j], kind, role))
            expiries.append(exp)
            center = strikes[i] if first else center
            if len(expiries) == n_expiries:
                break
        if not expiries or len({c.token for c in contracts}) != len(contracts):
            raise ValueError("no valid NIFTY expiry with strikes on both sides of the money found")
        return Window(contracts, expiries[0], center, spot)

    def adopt(self, contracts: list, spot: float) -> Window | None:
        """The window an existing list of contracts amounts to (None if it has no ATM call to centre on)."""
        atm = next((c for c in contracts if c.role == "ATM_CE" and c.expiry in self._by), None)
        return None if atm is None else Window(list(contracts), atm.expiry, atm.strike, spot)

    def needs_recentre(self, window: Window, spot: float, hysteresis: float) -> bool:
        """True once the spot is more than `hysteresis` strike steps away from the strike the window is centred on, the step being the real gap to
        the next strike in the spot's direction. Above 0.5 (the midpoint) so that a spot hovering between two strikes does not flip the window."""
        strikes = self._by.get(window.primary)
        if not strikes or window.center not in strikes:
            return True  # the data the window was made from is gone
        ordered = list(strikes)
        i = ordered.index(window.center)
        j = i + 1 if spot > window.center else i - 1
        if not 0 <= j < len(ordered):
            return False  # nothing further out to centre on
        return abs(spot - window.center) > hysteresis * abs(ordered[j] - window.center)
