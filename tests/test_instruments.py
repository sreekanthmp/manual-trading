from datetime import date, datetime

import pytest
from helpers import SPOT_TOKEN, master_rows

from app.candles import IST
from app.instruments import find_spot, select_contracts

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=IST)


def by_role(contracts):
    return {c.role: c for c in contracts}


def test_selects_spot_atm_and_one_strike_itm():
    c = by_role(select_contracts(master_rows(), 24510.0, NOW))
    assert c["SPOT"].token == SPOT_TOKEN and c["SPOT"].exch_type == 1
    assert c["ATM_CE"].strike == c["ATM_PE"].strike == 24500.0
    assert c["ITM_CE"].strike == 24450.0  # a call is ITM below spot
    assert c["ITM_PE"].strike == 24550.0  # a put is ITM above spot
    assert c["ATM_CE"].symbol.endswith("CE") and c["ATM_PE"].symbol.endswith("PE")
    assert all(x.expiry == date(2026, 11, 10) and x.exch_type == 2 for x in c.values() if x.role != "SPOT")
    assert c["ATM_CE"].lot_size == 75  # taken from the master, not hardcoded
    assert len({x.token for x in c.values()}) == 5


def test_strike_interval_comes_from_the_data():
    c = by_role(select_contracts(master_rows(lo=24000, hi=25000, step=100), 24540.0, NOW))
    assert c["ATM_CE"].strike == 24500.0 and c["ITM_CE"].strike == 24400.0 and c["ITM_PE"].strike == 24600.0


def test_skips_expired_expiry_and_expiry_day_after_close():
    rows = master_rows(expiries=("05NOV2026", "06NOV2026", "13NOV2026"))
    assert select_contracts(rows, 24500, NOW)[1].expiry == date(2026, 11, 6)  # expiry today, still open
    after_close = datetime(2026, 11, 6, 15, 45, tzinfo=IST)
    assert select_contracts(rows, 24500, after_close)[1].expiry == date(2026, 11, 13)


def test_expiry_missing_a_leg_is_skipped():
    rows = [r for r in master_rows() if not (r["expiry"] == "10NOV2026" and r["symbol"].endswith("24500PE"))]
    c = by_role(select_contracts(rows, 24510.0, NOW))
    assert c["ATM_CE"].expiry == date(2026, 11, 10)  # still the nearest expiry...
    assert c["ATM_CE"].strike == 24550.0  # ...but 24500 is unusable without its put


def test_spot_at_edge_of_chain_or_no_chain_raises():
    with pytest.raises(ValueError):
        select_contracts(master_rows(), 23000.0, NOW)  # ATM is the lowest strike: no ITM call
    with pytest.raises(ValueError):
        select_contracts([r for r in master_rows() if r["exch_seg"] == "NSE"], 24500.0, NOW)


def test_find_spot_ignores_other_indices():
    assert find_spot(master_rows()).symbol == "Nifty 50"
    with pytest.raises(ValueError):
        find_spot([])
