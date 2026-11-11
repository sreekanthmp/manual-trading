"""Stage 5.2: the Options tab as a trader reads it, run through the dashboard's own script under node (see dashboard_harness.py) on the engine's real snapshot.

Nothing here changes how a contract is chosen: the selection, the suitability checks and the thresholds are Phase 4's and are tested in test_options.py / test_options_integration.py. What
is held here is the PRESENTATION:

  * a BUY's contract card carries everything the 5.2 list asks for: the contract, its side, strike and expiry, the strategy that made the decision (name and ID), the setup ID, the
    strategies that agree, the price, the bid and the ask, the spread, volume, open interest, the two quote ages (received and exchange time), every finding, and the alternatives
  * WAIT and NO TRADE say "No contract selected" and offer nothing as actionable
  * a contract is shown as selected ONLY when the final decision is the BUY it was selected for: an options payload that says otherwise, alone, shows nothing as selected
  * an unknown value is shown as unknown, never as 0; a stale page marks the contract as the last one, not current"""

import copy
import re

import pytest
from dashboard_harness import base_snapshot, run_page, shown, variant, words
from test_dashboard_stage5 import tight

TABS = ("overview", "options", "analysis", "journal")
NOT_SELECTED = {"actionable": False, "kind": None, "contract": None, "alternatives": [], "rejected": [], "why": [], "notice": ""}


@pytest.fixture(scope="module")
def base():
    return base_snapshot()


def inr(n):
    """The Indian digit grouping the page uses for volume and open interest (20,00,000)."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail, parts = s[:-3], s[-3:], []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + parts + [tail])


def options_tab(tmp_path, snap, tab="options"):
    return run_page(tmp_path, [{"op": "message", "snap": snap}, {"op": "tab", "name": tab}, {"op": "capture", "label": tab}])[0]


def decided(base, action, headline, **over):
    """The snapshot as it is when the FINAL decision is `action` and the options layer, told the same, selects nothing (what the backend does for anything but a BUY)."""
    snap = copy.deepcopy(base)
    for part in (snap["trading"]["decision"], snap["options"]["decision"]):
        part.update({"action": action, "headline": headline, **over})
    snap["options"]["selection"] = {**NOT_SELECTED, "notice": f"no options selection: Phase 3 says {action}. The table below is for information only"}
    return snap


# ====================================================================================================================
# 1. The contract card of a BUY
# ====================================================================================================================
def test_a_buy_shows_the_contract_with_everything_the_list_asks_for(base, tmp_path):
    s = options_tab(tmp_path, base)
    c, d = base["options"]["selection"]["contract"], base["options"]["decision"]
    card = tight(shown(s, "opt-sel"))
    assert card.startswith("Selected contract") and f"{c['kind']} {c['strike']:.0f}" in card and c["symbol"] in card  # the contract, its side and strike
    assert f"expiry {c['expiry']} ({c['days_to_expiry']} day(s))" in card and f"{c['moneyness']} · {c['steps_from_atm']} strike(s) from ATM" in card
    assert 'class="big up"' in s["els"]["opt-sel"]["html"]  # a call is drawn as one
    assert f"Chosen for {d['action']} from {d['strategy_name']} {d['strategy']} · setup {d['setup_id']} · also matches: none" in card  # who made the decision, its ID, the setup, who agrees
    for label, value in (("LTP", f"{c['ltp']:.2f}"), ("Bid × units", f"{c['bid']:.2f} × {c['bid_qty']}"), ("Ask × units", f"{c['ask']:.2f} × {c['ask_qty']}"),
                         ("Spread", f"{c['spread']:.2f} ({c['spread_pct']:.2f}% of mid)"), ("Day volume (units)", inr(c["volume"])), ("Open interest (units)", inr(c["oi"])),
                         ("Freshness", "FRESH"), ("Quote age: received", f"{c['receipt_age']:.0f} s"), ("Quote age: exchange time", f"{c['exchange_age']:.0f} s")):
        assert f"{label} {value}" in card, (label, value)
    assert "quote FRESH · receipt age 1 s · exchange-time age 3 s" in card  # the two clocks, side by side again in the strip
    assert "No suitability warnings." in card and "6 of 14" not in card and s["errors"] == []


def test_the_agreeing_strategies_are_on_the_card_beside_the_one_that_was_picked(base, tmp_path):
    snap = variant(base)
    also = ["Range Bounce (same CE opportunity)", "Momentum Continuation (same CE opportunity)"]
    snap["trading"]["decision"]["also"] = also
    snap["options"]["decision"]["also"] = also
    s = options_tab(tmp_path, snap)
    card = tight(shown(s, "opt-sel"))
    assert "from Trend Pullback trend_pullback" in card and "also matches: Range Bounce (same CE opportunity), Momentum Continuation (same CE opportunity)" in card
    assert card.count("Chosen for") == 1 and card.count("Selected contract") == 1  # one selection: the one picked, not one per agreeing strategy
    assert "also matches: Range Bounce (same CE opportunity), Momentum Continuation (same CE opportunity)" in tight(shown(s, "opt-head"))


def test_a_put_is_drawn_as_a_put_for_a_bearish_decision(base, tmp_path):
    snap = variant(base)
    put = next(c for c in snap["options"]["contracts"] if c["kind"] == "PE" and not c["blocking"])
    snap["trading"]["decision"].update({"action": "BUY PE", "headline": "BUY PE: Trend Pullback setup confirmed", "strategy": "trend_pullback", "setup_id": "trend_pullback:bear:1793940000"})
    snap["options"]["decision"].update({"action": "BUY PE", "headline": "BUY PE: Trend Pullback setup confirmed", "setup_id": "trend_pullback:bear:1793940000"})
    snap["options"]["selection"] = {**NOT_SELECTED, "actionable": True, "kind": "PE", "contract": put, "why": ["a PE contract"], "notice": "n"}
    s = options_tab(tmp_path, snap)
    html = s["els"]["opt-sel"]["html"]
    assert f"PE {put['strike']:.0f}" in tight(shown(s, "opt-sel")) and 'class="big down"' in html and 'class="panel card pe"' in html and "Chosen for BUY PE" in tight(shown(s, "opt-sel"))
    assert "Option side PE" in shown(s, "opt-head")
    assert {m for m in re.findall(r'<td class="l">(CE|PE)</td><td>\d', s["els"]["opt-table"]["html"])} == {"PE"}  # only the puts are compared


def test_the_alternatives_show_the_price_the_book_the_liquidity_both_ages_and_their_warnings(base, tmp_path):
    snap = variant(base)
    alts = snap["options"]["selection"]["alternatives"]
    alts[0].update({"warnings": 1, "flags": [{"severity": "warn", "area": "liquidity", "code": "low_oi", "text": "open interest 50,000 units is under 100,000 units"}]})
    s = options_tab(tmp_path, snap)
    html = s["els"]["opt-sel"]["html"]
    table = re.search(r"<b>Alternatives</b>.*?</table>", html, re.S).group(0)
    header = words(re.search(r"<tr>(<th.*?)</tr>", table, re.S).group(1))
    assert header == "Contract Expiry LTP Bid × Ask Spread Volume (units) OI (units) Quote age: received / exchange Warnings"
    rows = [words(r) for r in re.findall(r"<tr><td class=\"l\">.*?</tr>", table, re.S)]
    assert len(rows) == 2
    for row, a in zip(rows, alts):
        assert row.startswith(f"{a['kind']} {a['strike']:.0f} {a['expiry']} {a['ltp']:.2f} {a['bid']:.2f} × {a['ask']:.2f} {a['spread_pct']:.2f}% {inr(a['volume'])} {inr(a['oi'])} {a['receipt_age']:.0f} s / {a['exchange_age']:.0f} s")
    assert "low oi" in rows[0] and 'title="open interest 50,000 units is under 100,000 units"' in table and rows[1].endswith("none")  # a warning is on the alternative it belongs to
    assert not any("blocking" in r for r in rows) and "sev-avoid" not in table  # an alternative has no Avoid finding: it could not have been offered


def test_the_warn_findings_of_the_selected_contract_are_given_in_full_and_the_avoid_ones_of_the_others_are_listed(base, tmp_path):
    snap = variant(base)
    sel = snap["options"]["selection"]["contract"]
    sel["flags"] = [{"severity": "warn", "area": "liquidity", "code": "no_oi", "text": "the open interest was not provided"},
                    {"severity": "warn", "area": "expiry", "code": "near_expiry", "text": "1 day(s) to expiry: time decay accelerates close to expiry"}]  # fmt: skip
    for row in snap["options"]["contracts"]:
        if row["token"] == sel["token"]:
            row["flags"] = sel["flags"]
    bad = [c for c in snap["options"]["contracts"] if c["kind"] == "CE" and c["token"] != sel["token"]][:2]
    for c, text in zip(bad, ("the last quote is 60 s old (limit 30 s): it is not shown as live", "the bid or the ask is not provided: the spread cannot be judged")):
        c.update({"blocking": True, "flags": [{"severity": "avoid", "area": "freshness", "code": "stale_quote", "text": text}]})
    s = options_tab(tmp_path, snap)
    card = tight(shown(s, "opt-sel"))
    assert "Suitability findings" in card and "⚠ the open interest was not provided" in card and "⚠ 1 day(s) to expiry: time decay accelerates close to expiry" in card
    assert "a Warn is shown and does not block" in card and "No suitability warnings." not in card
    ruled = tight(words(re.search(r'<details class="more" data-key="opt-ruled".*?</details>', s["els"]["opt-sel"]["html"], re.S).group(0)))
    assert ruled.startswith("2 of 14 CE contracts ruled out (Avoid): why") and "⛔ " + bad[0]["symbol"] in ruled and "the last quote is 60 s old (limit 30 s)" in ruled
    assert "the bid or the ask is not provided" in ruled and sel["symbol"] not in ruled  # the one that was chosen is not among those ruled out


def test_the_selected_contract_is_the_one_row_highlighted_in_the_table_which_lists_only_its_side(base, tmp_path):
    s = options_tab(tmp_path, base)
    table = s["els"]["opt-table"]["html"]
    sel = base["options"]["selection"]["contract"]
    assert table.count('<tr class="opt-sel">') == 1 and re.search(rf'<tr class="opt-sel"><td class="l">{sel["expiry"]} \({sel["days_to_expiry"]}d\)</td><td class="l">CE</td><td>{sel["strike"]:.0f}</td>', table)
    assert table.count("<tr class=") == 14 and '<td class="l">PE</td>' not in table and shown(s, "opt-info") == ""


# ====================================================================================================================
# 2. WAIT and NO TRADE
# ====================================================================================================================
@pytest.mark.parametrize("action,headline", [("WAIT", "WAIT: the market is tradable but there is no setup yet"), ("NO TRADE", "NO TRADE: the market data is not reliable enough")])
def test_wait_and_no_trade_say_no_contract_selected_and_offer_nothing_as_actionable(base, tmp_path, action, headline):
    s = options_tab(tmp_path, decided(base, action, headline, strategy=None, strategy_name=None, setup_id=None, also=[]))
    card = tight(shown(s, "opt-sel"))
    assert card.startswith("No contract selected informational: only a BUY CE / BUY PE decision selects a contract")
    assert f"Final decision: {action} — {headline}. Everything below is for information only: nothing in it is selected, and none of it is actionable." in card
    assert "Selected contract" not in card and "Chosen for" not in card and "Alternatives" not in card and 'class="big' not in s["els"]["opt-sel"]["html"] and "ruled out" not in card
    assert "Option side none" in shown(s, "opt-head") and f"Phase 3 decision {action}" in shown(s, "opt-head") and "From " not in shown(s, "opt-head")
    table = s["els"]["opt-table"]["html"]
    assert "opt-sel" not in table and table.count("<tr class=") == 28 and '<td class="l">CE</td>' in table and '<td class="l">PE</td>' in table  # both sides to read, none picked
    assert "informational: no contract is selected" in shown(s, "opt-info")
    assert not re.search(r"actionable now|eligible|you could consider", shown(s, "opt-sel", "opt-head"), re.I) or "none of it is actionable" in card  # nothing says a contract can be acted on


def test_a_wait_about_a_setup_names_it_as_a_setup_in_view_never_as_the_source_of_a_contract(base, tmp_path):
    s = options_tab(tmp_path, decided(base, "WAIT", "WAIT: Trend Pullback (CE) is confirmed but not actionable right now"))  # (the decision still names its setup)
    head = shown(s, "opt-head")
    assert "Setup in view (not a BUY, so no contract): Trend Pullback setup trend_pullback:bull:1793940000" in head and "From Trend Pullback" not in head
    assert "No contract selected" in shown(s, "opt-sel")


def test_a_buy_with_no_suitable_contract_still_names_its_strategy_and_side_and_says_why_nothing_is_offered(base, tmp_path):
    snap = variant(base)
    for c in snap["options"]["contracts"]:
        if c["kind"] == "CE":
            c.update({"blocking": True, "flags": [{"severity": "avoid", "area": "spread", "code": "no_bid_ask", "text": "the bid or the ask is not provided: the spread cannot be judged"}]})
    rejected = [{"symbol": c["symbol"], "strike": c["strike"], "expiry": c["expiry"], "reasons": [c["flags"][0]["text"]]} for c in snap["options"]["contracts"] if c["kind"] == "CE"][:6]
    snap["options"]["selection"] = {**NOT_SELECTED, "kind": "CE", "notice": "no suitable CE contract is available: every one has a blocking data or liquidity problem", "rejected": rejected}
    s = options_tab(tmp_path, snap)
    head, card = shown(s, "opt-head"), tight(shown(s, "opt-sel"))
    assert "From Trend Pullback setup trend_pullback:bull:1793940000" in head and "Option side CE" in head and "Setup in view" not in head  # it IS a BUY: only the contract is missing
    assert card.startswith("No contract selected") and "no suitable CE contract is available" in card and card.count("the bid or the ask is not provided") >= 6
    assert "14 of 14 CE contracts ruled out (Avoid): why" in card and "Selected contract" not in card and "opt-sel" not in s["els"]["opt-table"]["html"]


# ====================================================================================================================
# 3. Never selected on the options payload's word alone
# ====================================================================================================================
def refused(s):
    card = tight(shown(s, "opt-sel"))
    return ("No contract selected" in card and "Selected contract" not in card and "Chosen for" not in card and 'class="big' not in s["els"]["opt-sel"]["html"]
            and "opt-sel" not in s["els"]["opt-table"]["html"])  # fmt: skip


def test_a_contract_is_not_shown_as_selected_when_the_final_decision_is_not_a_buy(base, tmp_path):
    snap = variant(base)
    snap["trading"]["decision"].update({"action": "WAIT", "headline": "WAIT: bullish and bearish setups disagree", "setup_id": None, "strategy": None, "strategy_name": None})  # the options layer still says BUY CE
    s = options_tab(tmp_path, snap)
    assert refused(s) and "The options data offers a contract, but the final decision is WAIT (the options payload says BUY CE): no contract is shown as selected." in tight(shown(s, "opt-sel"))
    assert "Option side none" in shown(s, "opt-head") and s["els"]["opt-table"]["html"].count("<tr class=") == 28  # both sides, none highlighted
    assert "selected CE" not in shown(options_tab(tmp_path, snap, "overview"), "dhs")  # nor named on the Overview's option feed


def test_a_contract_is_not_shown_when_the_two_payloads_name_different_decisions_or_sides(base, tmp_path):
    other_side = variant(base)
    other_side["trading"]["decision"].update({"action": "BUY PE", "headline": "BUY PE: Trend Pullback setup confirmed"})  # the decision says PE, the options layer chose a CE
    wrong_kind = variant(base)
    for part in (wrong_kind["trading"]["decision"], wrong_kind["options"]["decision"]):
        part["action"] = "BUY PE"
    gone = variant(base, trading=None)
    no_contract = variant(base)
    no_contract["options"]["selection"]["contract"] = None
    for snap in (other_side, wrong_kind, gone, no_contract):
        s = options_tab(tmp_path, snap)
        assert refused(s), snap["options"]["decision"]
        assert "selected C" not in shown(options_tab(tmp_path, snap, "overview"), "dhs") and "selected P" not in shown(options_tab(tmp_path, snap, "overview"), "dhs")
    assert "final decision is not available" in tight(shown(options_tab(tmp_path, gone), "opt-sel"))  # no decision at all: nothing is offered


def test_the_overviews_option_feed_names_a_selected_contract_only_for_a_consistent_buy(base, tmp_path):
    assert "selected CE 24200: FRESH" in shown(options_tab(tmp_path, base, "overview"), "dhs")
    assert "selected" not in shown(options_tab(tmp_path, decided(base, "WAIT", "WAIT: nothing yet"), "overview"), "dhs")


# ====================================================================================================================
# 4. Unknown, stale, console
# ====================================================================================================================
def test_unknown_values_on_the_card_are_unknown_never_zero(base, tmp_path):
    snap = variant(base)
    c = snap["options"]["selection"]["contract"]
    c.update({"bid": None, "ask": None, "bid_qty": None, "ask_qty": None, "spread": None, "spread_pct": None, "volume": None, "oi": None, "prev_close": None, "change": None, "ltp": None,
              "exchange_age": None, "exchange_status": "unknown", "exchange_reason": "the exchange timestamp is not available", "receipt_age": None, "last_trade_age": None, "distance_points": None})  # fmt: skip
    s = options_tab(tmp_path, snap)
    card = tight(shown(s, "opt-sel"))
    for label in ("LTP –", "Bid × units – × –", "Ask × units – × –", "Spread –", "Prev close –", "Day volume (units) –", "Open interest (units) –", "Quote age: received –", "Quote age: exchange time unknown"):
        assert label in card, label
    assert "–  pts from the spot" not in card and not re.search(r"\bundefined\b|\bNaN\b|\bnull\b", card) and s["errors"] == [] and s["els"]["jserr"]["text"] == ""
    assert "exchange-time age unknown (the exchange timestamp is not available)" in card  # and the reason, in the freshness strip


def test_a_stale_page_marks_the_contract_as_the_last_one_not_current_and_not_actionable(base, tmp_path):
    steps = [{"op": "message", "snap": base}, {"op": "tab", "name": "options"}, {"op": "capture", "label": "live"}, {"op": "close"}, {"op": "capture", "label": "stale"}]
    live, stale = run_page(tmp_path, steps)
    assert shown(live, "opt-sel").startswith("Selected contract the market data does not rule it out") and "Chosen for" in shown(live, "opt-sel") and "Last selected" not in shown(live, "opt-sel") and "Last chosen" not in shown(live, "opt-sel")
    text = tight(shown(stale, "opt-sel"))
    assert text.startswith("Last selected contract STALE since 10:55: not current, not actionable") and "Last chosen for BUY CE" in text and "does not rule it out" not in text
    assert "staled" in stale["els"]["main"]["cls"].split() and "STALE since 10:55" in shown(stale, "stale")


@pytest.mark.parametrize("name", TABS)
def test_every_tab_still_draws_without_an_error_when_options_are_missing_or_partial(base, tmp_path, name):
    for snap in (variant(base, options={"available": False, "reason": "no option contracts are selected yet"}), variant(base, options=None),
                 variant(base, options={**base["options"], "contracts": []})):  # fmt: skip
        s = options_tab(tmp_path, snap, name)
        assert s["errors"] == [] and s["els"]["jserr"]["text"] == "", (name, snap["options"])
    gone = options_tab(tmp_path, variant(base, options={"available": False, "reason": "no option contracts are selected yet"}))
    assert shown(gone, "opt-head") == "no option contracts are selected yet" and shown(gone, "opt-sel") == "" and shown(gone, "opt-table") == ""  # nothing offered, and it says why
