"""Stage 5.4: what the page says, held without a browser (the page's own script under node, see dashboard_harness.py), for the three things the real-browser measurement of this stage
found; the layout itself is held in test_dashboard_stage54_browser.py.

  1. the header's one-line summary ("last tick ... ticks ... rejected ...") is cut with an ellipsis when the header is short of width: its whole text is also its tooltip
  2. when a BUY has no suitable contract, the contracts ruled out are listed ONCE, nearest the money first, each with its own findings (they used to be listed twice: the six
     nearest, then all of them again)
  3. no text is set smaller than 10 px"""

import re
from pathlib import Path

import pytest
from dashboard_harness import base_snapshot, run_page, shown, variant, words
from test_dashboard_stage5 import tight

PAGE = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
NOT_SELECTED = {"actionable": False, "kind": None, "contract": None, "alternatives": [], "rejected": [], "why": [], "notice": ""}


@pytest.fixture(scope="module")
def base():
    return base_snapshot()


# ====================================================================================================================
# 1. The header summary is readable in full
# ====================================================================================================================
def test_the_header_summary_carries_its_whole_text_as_a_tooltip(base, tmp_path):
    states = run_page(tmp_path, [{"op": "capture", "label": "loading"}, {"op": "message", "snap": base}, {"op": "capture", "label": "live"},
                                 {"op": "close"}, {"op": "capture", "label": "stale"}])  # fmt: skip
    loading, live, stale = (s["els"]["hsum"] for s in states)
    assert loading["title"] == "" and words(loading["html"]) == "" and loading["text"] == ""  # nothing to say yet: nothing to hover
    assert live["text"].startswith("last tick ") and " ticks " in live["text"] and live["title"] == live["text"]  # whatever the width cuts off, hovering gives all of it
    assert stale["text"].startswith("last snapshot ") and "not current" in stale["text"] and stale["title"] == stale["text"]


def test_the_summary_with_data_issues_names_them_in_the_tooltip_too(base, tmp_path):
    snap = variant(base)
    snap["health"]["issues"] = ["historical data is stale: today's refresh has not completed"]
    live = run_page(tmp_path, [{"op": "message", "snap": snap}, {"op": "capture", "label": "live"}])[0]["els"]["hsum"]
    assert "1 data issue(s)" in live["text"] and live["title"] == live["text"]


# ====================================================================================================================
# 2. The contracts ruled out are listed once
# ====================================================================================================================
def no_suitable(base):
    snap = variant(base)
    for c in snap["options"]["contracts"]:
        if c["kind"] == "CE":
            c.update({"blocking": True, "flags": [{"severity": "avoid", "area": "spread", "code": "no_bid_ask", "text": f"the bid or the ask is not provided for {c['symbol']}"}]})
    ce = sorted((c for c in snap["options"]["contracts"] if c["kind"] == "CE"), key=lambda c: (c["steps_from_atm"], c["expiry"]))
    rejected = [{"symbol": c["symbol"], "strike": c["strike"], "expiry": c["expiry"], "reasons": [c["flags"][0]["text"]]} for c in ce[:6]]  # what the backend hands over: the six nearest
    snap["options"]["selection"] = {**NOT_SELECTED, "kind": "CE", "notice": "no suitable CE contract is available: every one has a blocking data or liquidity problem", "rejected": rejected}
    return snap, ce


def test_a_buy_with_no_suitable_contract_lists_each_contract_ruled_out_once_nearest_the_money_first(base, tmp_path):
    snap, ce = no_suitable(base)
    s = run_page(tmp_path, [{"op": "message", "snap": snap}, {"op": "tab", "name": "options"}, {"op": "capture", "label": "options"}])[0]
    html = s["els"]["opt-sel"]["html"]
    card = tight(shown(s, "opt-sel"))
    assert card.startswith("No contract selected") and "no suitable CE contract is available" in card and "14 of 14 CE contracts ruled out (Avoid): why" in card
    assert html.count('class="why sev-avoid"') == 14  # one line for each of the fourteen: none twice
    for c in ce:
        assert card.count(f"the bid or the ask is not provided for {c['symbol']}") == 1, c["symbol"]
    order = [m.group(1) for m in re.finditer(r"⛔ (NIFTY\w+)", card)]
    assert order == [c["symbol"] for c in ce]  # nearest the money first, then by expiry: the order the backend ranks them in
    assert '<details class="more" data-key="opt-ruled" open>' in html  # and the list is open: it is the answer to "why nothing"


def test_the_contracts_ruled_out_under_a_selected_contract_are_nearest_first_and_folded(base, tmp_path):
    snap = variant(base)
    ce = [c for c in snap["options"]["contracts"] if c["kind"] == "CE"]
    for c in ce[:3]:
        c.update({"blocking": True, "flags": [{"severity": "avoid", "area": "quote", "code": "stale", "text": f"the last quote of {c['symbol']} is old"}]})
    s = run_page(tmp_path, [{"op": "message", "snap": snap}, {"op": "tab", "name": "options"}, {"op": "capture", "label": "options"}])[0]
    html = s["els"]["opt-sel"]["html"]
    shown_order = [m.group(1) for m in re.finditer(r"⛔ (NIFTY\w+)", tight(words(html)))]
    expected = [c["symbol"] for c in sorted(ce[:3], key=lambda c: (c["steps_from_atm"], c["expiry"]))]
    assert shown_order == expected and '<details class="more" data-key="opt-ruled">' in html  # (folded here: the contract is the answer)


# ====================================================================================================================
# 3. No small print
# ====================================================================================================================
def test_no_text_is_set_below_ten_pixels():
    css = PAGE.read_text(encoding="utf-8")
    sizes = [float(x) for x in re.findall(r"font-size:\s*([0-9.]+)px", css)]
    assert sizes and min(sizes) >= 10, sorted(set(sizes))[:5]
    shorthand = [float(x) for x in re.findall(r"[^-]font:\s*([0-9.]+)px", css)]  # (`font: 13px/1.35 ...`)
    assert shorthand and min(shorthand) >= 10, shorthand
