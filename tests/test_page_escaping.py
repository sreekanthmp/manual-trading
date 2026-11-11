"""Stage 7.2: the dashboard escapes every string it is given.

The Stage 7 audit poisoned the strings of one real snapshot and found that four fields reached the page unescaped; probing key by key (and every state) found sixteen keys in thirty-odd places
(dates, ATM / ITM / OTM, a setup's stage, the status of an event, the side of a strategy ...). All were values Prism itself generates, so none was reachable from outside, but a page that prints a
backend string without escaping it only stays safe for as long as every producer does. Now every one is escaped, and this holds it:

  * every string leaf of the snapshot of each of the fourteen states the dashboard tests pin (a BUY on each side, WAIT, NO TRADE, closed, a lost feed, a stale spot, no option quote, no suitable
    contract, the reversal's states), and of a real /api/journal answer, gets a payload that would break out of text or of an attribute; the page's own script draws all four tabs (and the
    lost-link page) under node, and no payload may reach an element unescaped, whatever its case (a value the page lower-cases before it prints it is not escaped either)
  * the same, ONE key at a time with everything else intact: the page compares exact values to decide what to draw (the action, a contract's side, a strategy's id), so poisoning everything at once
    hides what is drawn only when they match (the findings of a selected contract, a strategy's "last ended today" line). A mutation check found exactly those two uncovered
  * the keys of the few dicts the page walks (the regime facts, the candle coverage, the candles missing, the ticks rejected), and what the states leave empty (findings, a breakout ...) filled in
  * the page does not log an error while it draws any of it (an unknown value is drawn, not fatal)"""

import copy
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from dashboard_harness import base_snapshot, run_page
from dashboard_states import STATES
from test_phase3_integration import World, create_app, route

from app.journal import Journal

RAW = re.compile(r"<img src=x onerror=PWNED(\d*)>", re.IGNORECASE)  # (case does not matter: a page that lower-cases or upper-cases a value before it prints it has not escaped it)
TABS = ("overview", "options", "analysis")
STATE_NAMES = list(STATES)  # all fourteen states the dashboard tests pin: a BUY on each side, WAIT, NO TRADE, closed (an ended setup), a lost feed, a stale spot, no option quote, no suitable contract, the reversal's states


def journal_answer(tmp):
    """A real /api/journal answer: a BUY, the setup it ended with, a restart."""
    w = World()
    md, clock = w.engine()
    md.journal = Journal(tmp)
    for slot in (16, 17, 18, 19, 20):
        w.step(md, clock, slot)
    app = create_app(md, feed=None)
    app.state.md = md
    return jsonable(route(app, "/api/journal").endpoint())


def jsonable(x):
    return json.loads(json.dumps(x, allow_nan=False, default=str))


KEY_DICTS = ("facts", "coverage", "missing_today", "missing_in_decision_window", "ticks_rejected")  # the dicts whose KEYS (not only values) the page draws


def payload(paths, where):
    paths.append(where)
    return f'"><img src=x onerror=PWNED{len(paths) - 1}>'


def poison(x, paths, path="$", only=None, key=None, keys=False):
    """Every string leaf (or, with `only`, only the leaves under those keys) gets a payload numbered by where it was; `paths` says which number is which field. With `keys`, the keys of the
    dicts the page draws keys of (KEY_DICTS) get one too."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            nk = k + payload(paths, f"{path}.<key {k}>") if keys and key in KEY_DICTS and isinstance(k, str) else k
            out[nk] = poison(v, paths, f"{path}.{k}", only, k, keys)
        return out
    if isinstance(x, list):
        return [poison(v, paths, f"{path}[{i}]", only, key, keys) for i, v in enumerate(x)]
    if isinstance(x, str) and x and (only is None or key in only):
        return x + payload(paths, path)
    return x


def enrich(snap):
    """The nine states leave some things empty that the page nevertheless draws when they are not: the flags of a selected contract and of its alternatives, a breakout, the candles missing today or
    in the decision window, the ticks the feed rejected. Fill them in (with the shapes the backend gives them), so that what they print is held too."""
    s = copy.deepcopy(snap)
    flags = [{"severity": "warn", "area": "spread", "code": "wide_spread", "text": "the spread is wide"}, {"severity": "avoid", "area": "quote", "code": "no_bid", "text": "there is no bid"}]
    sel = (s.get("options") or {}).get("selection") or {}
    for c in ([sel["contract"]] if isinstance(sel.get("contract"), dict) else []) + [a for a in sel.get("alternatives") or [] if isinstance(a, dict)]:
        c["flags"] = [dict(f) for f in flags]
        c["warnings"] = 1
    for t in ((s.get("analysis") or {}).get("timeframes") or {}).values():
        if isinstance(t, dict) and t.get("breakout") is None:
            t["breakout"] = "above the range high"
    candles = ((s.get("health") or {}).get("readiness") or {}).get("candles")
    if isinstance(candles, dict):
        candles["missing_today"] = candles.get("missing_today") or {"5m": 2}
        candles["missing_in_decision_window"] = candles.get("missing_in_decision_window") or {"15m": 1}
    if isinstance(s.get("connection"), dict):
        s["connection"]["ticks_rejected"] = s["connection"].get("ticks_rejected") or {"stale": 3}
    return s


def string_keys(x, out=None, key=None):
    out = set() if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            string_keys(v, out, k)
    elif isinstance(x, list):
        for v in x:
            string_keys(v, out, key)
    elif isinstance(x, str) and x and key:
        out.add(key)
    return out


def render(tmp_path, snap, journal):
    """The page's own script draws the snapshot on every tab and the journal tab from `journal`, then the lost-link page: every captured state."""
    steps = [{"op": "message", "snap": snap}]
    for tab in TABS:
        steps += [{"op": "tab", "name": tab}, {"op": "capture", "label": tab}]
    steps += [{"op": "eval", "code": "J.data = " + json.dumps(journal)}, {"op": "tab", "name": "journal"}, {"op": "capture", "label": "journal"}]
    steps += [{"op": "close"}, {"op": "tab", "name": "overview"}, {"op": "capture", "label": "lost link"}, {"op": "tab", "name": "options"}, {"op": "capture", "label": "lost link options"}]
    return run_page(tmp_path, steps)


def unescaped(states, paths):
    """{field of the snapshot: elements} for every payload that reached an element as markup."""
    found = {}
    for s in states:
        for element, e in s["els"].items():
            for m in RAW.finditer(e["html"] or ""):
                found.setdefault(re.sub(r"\[\d+\]", "[]", paths[int(m.group(1))]), set()).add(element)
    return {k: sorted(v) for k, v in sorted(found.items())}


@pytest.fixture(scope="module")
def journal(tmp_path_factory):
    return journal_answer(tmp_path_factory.mktemp("journal"))


@pytest.mark.parametrize("state", STATE_NAMES)
def test_every_string_of_every_state_is_escaped_where_the_page_draws_it(tmp_path, journal, state):
    build, _ = STATES[state]
    snap = enrich(jsonable(build(str(tmp_path / "engine")).snapshot()))
    paths = []
    states = render(tmp_path, poison(snap, paths), poison(copy.deepcopy(journal), paths, "$journal"))
    assert len(paths) > 500  # (a real state: there is plenty to poison)
    assert unescaped(states, paths) == {}
    assert [e for s in states for e in s["errors"]] == []  # drawing unknown values did not fail either


def test_the_enrichment_adds_what_the_states_leave_empty():
    snap = enrich(base_snapshot())
    sel = snap["options"]["selection"]
    assert sel["contract"]["flags"] and all(a["flags"] for a in sel["alternatives"]) and sel["alternatives"]
    assert all(t["breakout"] for t in snap["analysis"]["timeframes"].values())
    assert snap["health"]["readiness"]["candles"]["missing_today"] and snap["health"]["readiness"]["candles"]["missing_in_decision_window"] and snap["connection"]["ticks_rejected"]
    assert snap["trading"]["market"]["regime_facts"]["facts"] and snap["analysis"]["history"]["coverage"]  # (the others are in the base state already)


def test_the_keys_the_page_draws_are_escaped_too(tmp_path, journal):
    """The dicts the page walks with Object.entries (the regime facts, the candle coverage, the candles missing, the ticks rejected): their KEYS reach the page, so they get a payload as well."""
    snap = enrich(base_snapshot())
    paths = []
    states = render(tmp_path, poison(snap, paths, only=set(), keys=True), journal)
    assert {p.split(".<key")[0].rsplit(".", 1)[-1] for p in paths if ".<key" in p} == set(KEY_DICTS)  # (every one of them was reached: they are all filled)
    assert unescaped(states, paths) == {}


def test_a_setup_card_drawn_with_its_own_strategy_and_side_is_as_safe(tmp_path):
    """No tab draws setupCard(x, false) today (every use is inside its strategy's own block), but it is there: if a tab ever uses it, it must be as safe as the rest."""
    snap, paths = base_snapshot(), []
    setup = poison(copy.deepcopy(snap["trading"]["setups"][0]), paths)
    states = run_page(tmp_path, [{"op": "message", "snap": snap}, {"op": "eval", "code": "setHtml('alerts', setupCard(" + json.dumps(setup) + ", false))"}, {"op": "capture", "label": "card"}])
    html = states[0]["els"]["alerts"]["html"]
    assert 'class="card ' in html and len(paths) > 10  # (it was drawn, from a payload-carrying setup)
    assert unescaped(states, paths) == {}


def closed_state(tmp_path):
    """The one state with an ended setup in it (an EXPIRED one): the Ended setups list and each strategy's "last ended today" line are drawn only there."""
    return enrich(jsonable(STATES["closed"][0](str(tmp_path / "closed-engine")).snapshot()))


def everything(states):
    return " ".join((e["html"] or "") for s in states for e in s["els"].values())


def test_the_probe_reaches_what_it_is_for(tmp_path, journal):
    """A probe that never draws a section proves nothing about it: the findings of a selected contract, the ended setups, a breakout, the candles missing, the ticks rejected."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    base = everything(render(tmp_path / "a", enrich(base_snapshot()), journal))
    closed = everything(render(tmp_path / "b", closed_state(tmp_path), journal))
    for text in ("Suitability findings", 'class="why sev-warn"', 'class="why sev-avoid"', "above the range high", "5M 2", "15M 1", "stale:3"):
        assert text in base, text
    for text in ("Ended setups", "last ended today", 'class="st st-EXPIRED"'):
        assert text in closed, text


def run_cases(tmp_path, cases):
    """[(label, snapshot, journal, key)]: each is drawn with only that key poisoned, in a folder of its own, four at a time. -> [(label, key, how many payloads, what reached the page raw)]"""

    def one(numbered):
        n, (label, snap, jrn, key) = numbered
        folder = tmp_path / f"case{n}"
        folder.mkdir()
        paths = []
        states = render(folder, poison(copy.deepcopy(snap), paths, only={key}), poison(copy.deepcopy(jrn), paths, "$journal", only={key}))
        return label, key, len(paths), unescaped(states, paths)

    with ThreadPoolExecutor(4) as pool:
        return list(pool.map(one, enumerate(cases)))


def test_every_key_is_escaped_on_its_own_with_everything_else_intact(tmp_path, journal):
    """One key at a time. The page compares exact values to decide what to draw (the action, the kind and side of a contract, a strategy's id), so poisoning every string at once hides whatever is
    drawn only when they match: that is where an unescaped finding or an ended setup was not seen."""
    base, closed = enrich(base_snapshot()), closed_state(tmp_path)
    cases = [("base", base, journal, k) for k in sorted(string_keys(base) | string_keys(journal))]
    cases += [("closed", closed, journal, k) for k in sorted(string_keys(closed["trading"]["recent"]))]
    assert len(cases) > 70
    results = run_cases(tmp_path, cases)
    assert all(n for _, _, n, _ in results)  # (every key was in the state it was run on)
    assert {f"{label}:{key}": found for label, key, _, found in results if found} == {}


def test_the_harness_can_hand_the_journal_tab_what_a_fetch_would_bring(tmp_path, journal):
    states = run_page(tmp_path, [{"op": "message", "snap": base_snapshot()}, {"op": "eval", "code": "J.data = " + json.dumps(journal)}, {"op": "tab", "name": "journal"}, {"op": "capture", "label": "journal"}])
    html = states[0]["els"]["journal-body"]["html"]
    assert "Decision history" in html and journal["decisions"][0]["headline"].split(":")[0] in html  # the rows the answer carried are drawn


def test_a_strategy_link_with_a_quote_or_a_backslash_in_its_id_still_makes_a_valid_selector(tmp_path):
    """The Overview's link to a strategy scrolls to `[data-key="sd-<id>"]` once the Analysis tab is drawn. An id with a quote or a backslash in it must not end the selector's string early
    (a real browser throws a SyntaxError for that, and the page stops drawing). The harness's `document` is given a querySelector that records what it was asked."""
    bs = chr(92)
    ident = 'a"]b' + bs + "c"
    record = "document.querySelector = q => { document.getElementById('sel').textContent = q; return null; }; openStrategy(" + json.dumps(ident) + ")"
    states = run_page(tmp_path, [{"op": "message", "snap": base_snapshot()}, {"op": "eval", "code": record}, {"op": "tab", "name": "analysis"}, {"op": "capture", "label": "analysis"}])
    assert states[0]["els"]["sel"]["text"] == '[data-key="sd-a' + bs + '"]b' + bs + bs + 'c"]'
    assert states[0]["errors"] == []
