"""Stage 5.1: the dashboard's own script, run under node (see dashboard_harness.py), driven the way the browser drives it. What these tests hold:

  * the STALE link: when the socket closes or goes silent the page dims the last snapshot, says "STALE since HH:MM - NOT ACTIONABLE", turns the market / feed / data / system chips
    to UNKNOWN, drops every "eligible" and "actionable now" wording and every frozen tick age, and a fresh snapshot restores the live page drawn from that snapshot alone
  * the loading state before the first snapshot
  * the Overview: price movement and exchange-time age from the backend, strategy / strategy ID / setup ID / agreeing strategies, the six regime facts with unknown told apart from
    none, the strategy monitor with direction, state, blockers, conflicts and its link to the Analysis, one-line alerts that expand, and no duplicated labels
  * the Analysis tab: all ten strategies, each side (CE / PE) on its own, in front of the indicator tables
  * unknown data is shown as unknown, never as 0, undefined or NaN"""

import copy
import re

import pytest
from dashboard_harness import PAGE, base_snapshot, run_page, shown, variant, words

SIDES = ("CE", "PE")
def tight(text):
    """`words` puts a space where a tag was: close up the ones before punctuation, so a sentence reads as the page shows it."""
    return re.sub(r"\s+([:,;)!?])", r"\1", text)


ACTIONABLE = re.compile(r"actionable now|Entry eligible|eligible now|eligible \(actionable", re.I)
TABS = ("overview", "options", "analysis", "journal", "trades")  # (Stage 8 added the Trades tab: a notebook of your own trades and a size calculator)


@pytest.fixture(scope="module")
def base():
    return base_snapshot()


def live(tmp_path, snap, tab="overview"):
    """The page after one snapshot, on that tab."""
    return run_page(tmp_path, [{"op": "message", "snap": snap}, {"op": "tab", "name": tab}, {"op": "capture", "label": "live"}])[0]


def trading(base, **over):
    out = copy.deepcopy(base["trading"])
    for k, v in over.items():
        out[k] = v
    return out


# ====================================================================================================================
# 1. The link: loading, live, stale, recovered
# ====================================================================================================================
def test_before_the_first_snapshot_the_page_says_it_is_loading_and_shows_no_decision(tmp_path):
    s = run_page(tmp_path, [{"op": "capture", "label": "loading"}])[0]
    text = shown(s, "decision")
    assert s["conn"] == "loading" and "LOADING" in text and "No decision is available yet" in text and "waiting for the first snapshot" in text
    assert not any(w in text for w in ("BUY CE", "BUY PE", "WAIT", "NO TRADE", "STALE"))  # a loading page is not a decision of any kind
    assert shown(s, "c-market", "c-feed", "c-data", "c-ready") == "CONNECTING FEED … DATA … WAITING" and words(s["els"]["spot"]["text"]) == "–"
    assert s["els"]["c-link"]["hidden"] is True and s["errors"] == []


def test_if_prism_cannot_be_reached_the_loading_page_says_so_and_keeps_retrying(tmp_path):
    steps = [{"op": "close"}, {"op": "capture", "label": "first"}, {"op": "fire", "ms": 2000}, {"op": "close"}, {"op": "capture", "label": "second"}]
    first, second = run_page(tmp_path, steps)
    assert "cannot be reached" not in shown(first, "decision") and first["els"]["c-link"]["hidden"] is True
    assert "Prism cannot be reached (attempt 2)" in shown(second, "decision") and second["els"]["c-link"]["hidden"] is False and second["conn"] == "loading"
    assert second["sockets"] == 2 and "BUY" not in shown(second, "decision")  # it reconnected once, and still shows no decision


def test_a_live_page_shows_the_price_its_movement_and_both_freshness_clocks_from_the_backend(base, tmp_path):
    s = live(tmp_path, base)
    ch = base["trading"]["market"]["change"]
    assert ch["available"] and shown(s, "spot-chg") == f"▲ +{ch['points']:.2f} ({ch['percent']:+.2f}%)" and s["els"]["spot-chg"]["html"].count('class="up"') == 1
    assert f"against the previous session's close: {ch['previous_close']:.2f}" in s["els"]["spot-chg"]["html"]  # said where the number comes from
    age = shown(s, "spot-age")
    spot = next(r for r in base["health"]["instruments"] if r["role"] == "SPOT")
    assert age == f"NIFTY tick received {spot['receipt_age']:.0f} s ago · exchange time {spot['exchange_age']:.0f} s old"  # two clocks, two words
    assert shown(s, "c-market", "c-feed", "c-data", "c-ready") == "MARKET OPEN FEED CONNECTED DATA TRUSTED SYSTEM READY" and s["conn"] == "live" and s["errors"] == []


def test_the_exchange_time_is_told_apart_from_the_receipt_time_when_it_is_old_or_unknown(base, tmp_path):
    def with_spot(**kw):
        health = copy.deepcopy(base["health"])
        row = next(r for r in health["instruments"] if r["role"] == "SPOT")
        row.update(kw)
        return live(tmp_path, variant(base, health=health))

    old = with_spot(exchange_age=27.0, exchange_status="stale", exchange_reason="the exchange's last update was 27 s ago (limit 20 s)")
    assert "received 1 s ago · exchange time 27 s old: STALE" in shown(old, "spot-age") and 'class="stale"' in old["els"]["spot-age"]["html"] and "exchange 27 s STALE" in shown(old, "dhs")
    unknown = with_spot(exchange_age=None, exchange_status="unknown", exchange_reason="the exchange timestamp is not available")
    assert shown(unknown, "spot-age").endswith("exchange time unknown") and "exchange unknown" in shown(unknown, "dhs")
    assert "0 s" not in shown(unknown, "spot-age").split("exchange time")[1]  # an unknown age is never drawn as 0 s
    silent = with_spot(age=95.0, receipt_age=95.0, status="stale")
    snap = variant(base)
    snap["instruments"][0]["status"] = "stale"
    snap["instruments"][0]["age"] = 95.0
    assert "STALE: last NIFTY tick" in shown(live(tmp_path, snap), "spot-age") and silent["conn"] == "live"


def test_the_price_movement_is_unknown_not_zero_when_the_backend_cannot_work_it_out(base, tmp_path):
    for why in ("the previous close is stale (the history has not been refreshed for today's session)", "the previous close is not available", "no NIFTY price yet"):
        t = trading(base)
        t["market"]["change"] = {"available": False, "why": why}
        s = live(tmp_path, variant(base, trading=t))
        assert shown(s, "spot-chg") == "change unknown" and why in s["els"]["spot-chg"]["html"] and "0.00" not in shown(s, "spot-chg") and "+0" not in shown(s, "spot-chg")
    t = trading(base)
    del t["market"]["change"]  # an older server that does not send it
    assert "– vs prev close" in shown(live(tmp_path, variant(base, trading=t)), "spot-chg")
    s = live(tmp_path, variant(base, trading=None))
    assert "Trading decision not available yet" in shown(s, "decision") and shown(s, "spot-chg") == "– vs prev close"


# ---- the stale link -------------------------------------------------------------------------------------------------------------------------------------------------------
def stale_states(tmp_path, snap):
    steps = [{"op": "message", "snap": snap}]
    for tab in TABS:
        steps += [{"op": "tab", "name": tab}, {"op": "capture", "label": "live-" + tab}]
    steps += [{"op": "close"}]
    for tab in TABS:
        steps += [{"op": "tab", "name": tab}, {"op": "capture", "label": "stale-" + tab}]
    return {s["label"]: s for s in run_page(tmp_path, steps)}


def test_a_lost_link_makes_the_page_stale_not_actionable_and_unknown_on_every_tab(base, tmp_path):
    states = stale_states(tmp_path, base)
    live_overview = shown(states["live-overview"])
    assert ACTIONABLE.search(live_overview) and "BUY CE" in live_overview  # (the live page does say it: the test can see the words it forbids)
    for tab in TABS:
        s = states["stale-" + tab]
        everything = shown(s)
        assert s["conn"] == "stale" and not ACTIONABLE.search(everything), (tab, ACTIONABLE.search(everything))  # no "eligible" / "actionable now" anywhere, on any tab
        assert "STALE since 10:55 — NOT ACTIONABLE" in shown(s, "stale") and "last snapshot (made 10:55:06 IST)" in shown(s, "stale")  # when, and what is on screen
        assert "staled" in s["els"]["main"]["cls"].split() and shown(s, "c-market", "c-feed", "c-data", "c-ready") == "MARKET UNKNOWN FEED UNKNOWN DATA UNKNOWN SYSTEM UNKNOWN"
        assert s["els"]["c-link"]["hidden"] is False and "tick received" not in shown(s, "spot-age") and "STALE since 10:55: not current" in shown(s, "spot-age")  # no frozen tick age
        assert s["errors"] == []
    o = states["stale-overview"]
    head = shown(o, "decision")
    assert head.startswith("STALE last decision: BUY CE STALE since 10:55 — NOT ACTIONABLE") and "Last known, not current: BUY CE: Trend Pullback setup confirmed" in head
    assert "Entry not eligible: STALE" in head and o["els"]["decision"]["cls"] == "decision stale"
    assert "unknown: the page lost its link to Prism" in shown(o, "dhs") and "fresh" not in shown(o, "dhs") and "connected" not in shown(o, "dhs")  # no health verdict from a frozen snapshot
    assert "last known (STALE): not actionable" in shown(states["stale-analysis"]) and "was in the entry area (not current)" in shown(o, "setup")
    assert "Last selected contract STALE since 10:55: not current, not actionable" in shown(states["stale-options"], "opt-sel")
    assert shown(states["live-options"], "opt-sel").startswith("Selected contract")


def test_the_last_snapshot_stays_available_for_inspection_beside_the_stale_notice(base, tmp_path):
    o = stale_states(tmp_path, base)["stale-overview"]
    t = base["trading"]
    d, setup = t["decision"], t["setups"][0]
    text = shown(o)
    for kept in (d["strategy_name"], d["strategy"], d["setup_id"], f"{setup['trigger']:.2f}", f"{setup['invalidation']:.2f}", f"{setup['zone'][0]:.2f} – {setup['zone'][1]:.2f}"):
        assert kept in text, kept  # the last snapshot's facts are all still there
    assert "STALE" in text and "BUY CE: Trend Pullback setup confirmed" in text  # ... told apart from a current decision by the words around them


def test_a_silent_link_is_stale_too_and_every_snapshot_restarts_the_one_watchdog_timer(base, tmp_path):
    steps = [{"op": "message", "snap": base}, {"op": "message", "snap": base}, {"op": "capture", "label": "two"}, {"op": "fire", "ms": 5000}, {"op": "capture", "label": "silent"}]
    two, silent = run_page(tmp_path, steps)
    assert two["conn"] == "live" and two["timers"].count(5000) == 1  # one timer, not one per snapshot: this is not polling
    assert silent["conn"] == "stale" and "STALE since 10:55 — NOT ACTIONABLE" in shown(silent, "stale") and "MARKET UNKNOWN" in shown(silent, "c-market")
    assert silent["sockets"] == 1  # the socket was still open: the page cannot know the link is good just because it is open


def test_a_fresh_snapshot_restores_the_live_page_drawn_from_that_snapshot_alone(base, tmp_path):
    wait = copy.deepcopy(base["trading"])
    wait["decision"].update({"action": "WAIT", "headline": "WAIT: nothing to act on yet", "setup_id": None, "strategy": None, "strategy_name": None, "also": []})
    wait["setups"], wait["explain"] = [], {**wait["explain"], "actionable": False, "setup": None}
    fresh = variant(base, trading=wait)
    steps = [{"op": "message", "snap": base}, {"op": "close"}, {"op": "capture", "label": "stale"}, {"op": "open"}, {"op": "capture", "label": "open-not-live"},
             {"op": "message", "snap": fresh}, {"op": "capture", "label": "recovered"}]
    stale, opened, back = run_page(tmp_path, steps)
    assert stale["conn"] == "stale" and opened["conn"] == "stale" and "STALE since" in shown(opened, "stale")  # an open socket is not a live page: only a snapshot is
    text = shown(back, "decision")
    assert back["conn"] == "live" and shown(back, "stale") == "" and "staled" not in back["els"]["main"]["cls"].split() and back["els"]["c-link"]["hidden"] is True
    assert text.startswith("WAIT") and "WAIT: nothing to act on yet" in text and "BUY CE" not in text and "STALE" not in text  # the NEW decision, nothing of the old one
    assert shown(back, "c-market", "c-feed", "c-data", "c-ready") == "MARKET OPEN FEED CONNECTED DATA TRUSTED SYSTEM READY" and "tick received" in shown(back, "spot-age")
    assert back["els"]["decision"]["cls"] == "decision wait" and "Strategy none" in text and "Setup ID –" in text


def test_a_page_that_goes_stale_and_recovers_twice_ends_exactly_as_a_page_that_never_lost_the_link(base, tmp_path):
    cycle = [{"op": "message", "snap": base}, {"op": "close"}, {"op": "fire", "ms": 2000}]
    steps = cycle + cycle + [{"op": "message", "snap": base}, {"op": "capture", "label": "again"}]
    never = run_page(tmp_path, [{"op": "message", "snap": base}, {"op": "capture", "label": "never"}])[0]
    again = run_page(tmp_path, steps)[0]
    for k in ("decision", "tfrow", "ctx", "setup", "strat", "rg", "dhs", "alerts", "stale", "spot", "spot-chg", "spot-age", "c-market", "c-feed", "c-data", "c-ready", "c-link"):
        assert shown(again, k) == shown(never, k), k  # (the clock-driven text is the same: the same snapshot)
    assert again["els"]["decision"]["cls"] == never["els"]["decision"]["cls"] and again["els"]["main"]["cls"] == never["els"]["main"]["cls"]


# ====================================================================================================================
# 2. The Overview
# ====================================================================================================================
def test_the_decision_names_the_strategy_its_id_the_setup_id_and_the_strategies_that_agree(base, tmp_path):
    d, setup = base["trading"]["decision"], base["trading"]["setups"][0]
    s = live(tmp_path, base)
    text = shown(s, "decision")
    assert f"Strategy {d['strategy_name']}" in text and f"Strategy ID {d['strategy']}" in text and f"Setup ID {d['setup_id']}" in text and "Agrees none" in text
    for field in ("trigger", "invalidation"):
        assert f"{setup[field]:.2f}" in shown(s, "setup")
    assert f"{setup['zone'][0]:.2f} – {setup['zone'][1]:.2f}" in shown(s, "setup") and "Entry eligible (actionable now)" in text
    t = trading(base)
    t["decision"]["also"] = ["Range Bounce (same CE opportunity)", "Momentum Continuation (same CE opportunity)"]
    agree = shown(live(tmp_path, variant(base, trading=t)), "decision")
    assert "Agrees Range Bounce (same CE opportunity), Momentum Continuation (same CE opportunity)" in agree  # the others are shown, the one picked is the one named


def test_the_setup_panel_shows_prisms_own_expiry_wording_and_calculates_nothing(base, tmp_path):
    setup = base["trading"]["setups"][0]
    s = live(tmp_path, base)
    panel = shown(s, "setup")
    assert "Expiry · what ends it (Prism's own wording)" in panel and setup["stage_text"] in panel  # the backend's sentence
    assert not re.search(r"\b(valid until|expires at|expires in|minutes left)\b", panel, re.I)  # no time of expiry is made up
    html = PAGE.read_text(encoding="utf-8")
    assert "confirm_valid_bars" not in html and "detected_max_minutes" not in html and "Date.now" not in html.replace("fmtT", "")  # no lifecycle rule and no clock in the page


def test_the_wait_and_no_trade_decisions_are_told_apart_in_words_and_class(base, tmp_path):
    def decision(action, headline, **kw):
        t = copy.deepcopy(base["trading"])
        t["decision"].update({"action": action, "headline": headline, "setup_id": None, "strategy": None, "strategy_name": None, "also": []})
        t["setups"], t["explain"] = [], {**t["explain"], "actionable": False, "setup": None}
        t.update(kw)
        return live(tmp_path, variant(base, trading=t))

    wait, no_trade = decision("WAIT", "WAIT: the market is tradable but there is no setup yet"), decision("NO TRADE", "NO TRADE: the market data is not reliable enough")
    assert shown(wait, "decision").startswith("WAIT") and wait["els"]["decision"]["cls"] == "decision wait" and "Entry not eligible" in shown(wait, "decision")
    assert shown(no_trade, "decision").startswith("NO TRADE") and no_trade["els"]["decision"]["cls"] == "decision no-trade"
    assert "No live setup" in shown(wait, "setup") and "No strategy has a live setup" in shown(wait, "strat")


def test_the_overview_says_each_thing_once_no_duplicated_labels(base, tmp_path):
    s = live(tmp_path, base)
    html = lambda *ids: " ".join(s["els"][i]["html"] for i in ids)  # noqa: E731
    page = html("decision", "tfrow", "ctx", "setup", "strat", "rg", "dhs")
    for label, count in (("<span>Condition</span>", 1), ("<span>Volatility</span>", 1), ("<span>Market direction</span>", 1), ("<span>Setup side</span>", 1), ('<span class="k">Strategy</span>', 1),
                         ("<span>Confirmation</span>", 0), ("<span>Direction</span>", 0), ("<span>Strategy</span>", 0), ("<span>Entry</span>", 0), ("<span>Start-up</span>", 0)):
        assert page.count(label) == count, (label, page.count(label))
    assert "ADX" not in shown(s, "ctx") and shown(s, "tfrow").count("ADX") == 4  # the ADX of each timeframe is on its own card, and nowhere else
    assert page.count("Entry ") <= 2 and shown(s, "decision").count("Entry eligible") == 1  # the Entry verdict is stated once, in the decision


def test_the_one_screen_overview_keeps_the_timeframes_and_adds_the_structure_of_each(base, tmp_path):
    s = live(tmp_path, base)
    cards = shown(s, "tfrow")
    for tf in ("1H", "15M", "5M", "1M"):
        assert tf in cards
    assert re.findall(r"RSI [\d.]+ · (HH·HL|LH·LL|mixed|–)", cards) and "Decides the allowed side" in cards and "Information only" in cards and "1H + 15M alignment" in cards


# ---- the six regime facts -------------------------------------------------------------------------------------------------------------------------------------------------
def regime(base, tmp_path, facts=None, available=True, why=None):
    t = copy.deepcopy(base["trading"])
    rf = t["market"]["regime_facts"]
    rf["available"] = available
    if why:
        rf["why"] = why
    for k, f in (facts or {}).items():
        rf["facts"][k] = {**rf["facts"][k], **f}
    return live(tmp_path, variant(base, trading=t))


def cells(state):
    return re.findall(r'<div class="rgc ([a-z]+)[^"]*"[^>]*><span>([^<]+)</span><b>([^<]+)</b>(?:<small>([^<]*)</small>)?', state["els"]["rg"]["html"])


def test_all_six_regime_facts_are_shown_active_none_and_unknown_each_as_what_it_is(base, tmp_path):
    s = regime(base, tmp_path, facts={"weakening_trend": {"state": "unknown", "summary": "cannot be judged: ADX falling, directional candles fading"},
                                      "strong_trend": {"state": "bullish"}, "compression": {"state": "no"}, "volatility_expansion": {"state": "none"}, "extended": {"state": "bearish"}, "pause": {"state": "range_bound"}})  # fmt: skip
    got = {name: (cls, word, small) for cls, name, word, small in cells(s)}
    assert list(got) == ["Strong trend", "Weakening trend", "Compression", "Expansion", "Extension", "Range / pause / pullback"]  # all six, in a fixed order
    assert got["Strong trend"][:2] == ("up", "bullish") and got["Extension"][:2] == ("down", "bearish") and got["Range / pause / pullback"][:2] == ("flat", "range bound")  # active: named
    assert got["Compression"][:2] == ("mut", "none") and got["Expansion"][:2] == ("mut", "none")  # inactive: muted "none" ("no" is not shown as a different thing)
    cls, word, small = got["Weakening trend"]
    assert (cls, word) == ("unk", "unknown") and small == "cannot be judged: ADX falling, directional candles fading"  # unknown: its own style, and the reason on the spot
    assert "none" not in word and "no" != word and 'class="rgc unk"' in s["els"]["rg"]["html"] and s["els"]["rg"]["html"].count("rgc unk") == 1


def test_when_the_regime_cannot_be_trusted_every_fact_is_unknown_and_the_reason_is_said_once(base, tmp_path):
    why = "the market data is not reliable right now (5M candles missing 10:05-10:15)"
    s = regime(base, tmp_path, available=False, why=why)
    got = cells(s)
    assert len(got) == 6 and all(c == "unk" and w == "unknown" for c, _, w, _ in got) and not any("none" == w for _, _, w, _ in got)  # not one of them reads as "none"
    assert shown(s, "rg").count(why) == 1 and "Regime facts unavailable" in shown(s, "rg") and "nothing is assumed" in shown(s, "rg")  # the reason once, beneath, not six times
    gone = live(tmp_path, variant(base, trading={**base["trading"], "market": {**base["trading"]["market"], "regime_facts": None}}))
    assert len(cells(gone)) == 6 and all(c == "unk" for c, _, _, _ in cells(gone)) and "not received yet" in shown(gone, "rg")


def test_the_counter_trend_permission_is_on_the_overview_only_when_it_is_needed(base, tmp_path):
    t = copy.deepcopy(base["trading"])
    t["counter_trend"] = [{"strategy": "trend_exhaustion_reversal", "strategy_name": "Trend Exhaustion Reversal", "side": "PE", "needed": True, "granted": False,
                           "reason": "denied: weakening continuation (regime fact: 0 of 6 weakening checks hold (needs 2))", "checks": [], "structure": None, "setup_id": None}]  # fmt: skip
    s = live(tmp_path, variant(base, trading=t))
    assert "Counter-trend permission: PE denied denied: weakening continuation" in tight(shown(s, "rg")) and "class='flat'" in s["els"]["rg"]["html"]
    t["counter_trend"][0].update({"needed": False})
    assert "Counter-trend" not in shown(live(tmp_path, variant(base, trading=t)), "rg")
    t["counter_trend"][0].update({"needed": True, "granted": True, "reason": "granted: the bearish trend is weakening and the 15M structure has turned"})
    granted = live(tmp_path, variant(base, trading=t))
    assert "PE granted granted: the bearish trend is weakening" in tight(shown(granted, "rg")) and "<b class='up'>granted</b>" in granted["els"]["rg"]["html"]


# ---- the strategy monitor ------------------------------------------------------------------------------------------------------------------------------------------
def with_conflict(base):
    t = copy.deepcopy(base["trading"])
    first = copy.deepcopy(t["setups"][0])
    other = {**copy.deepcopy(first), "strategy": "range_bounce", "strategy_name": "Range Bounce", "direction": "PE", "side": "bear", "id": "range_bounce:bear:1793946300", "status": "WAITING_FOR_CONFIRMATION",
             "stage": "WAITING_FOR_CONFIRMATION", "blockers": ["a completed 5M candle must still close below the trigger"], "actionable": False}  # fmt: skip
    t["setups"].append(other)
    t["decision"].update({"action": "WAIT", "headline": "WAIT: bullish and bearish setups disagree", "setup_id": None, "strategy": None, "strategy_name": None, "also": [],
                          "against": ["ignored Trend Pullback (CE): it runs against the 1H trend and holds no counter-trend permission"]})  # fmt: skip
    for m in t["strategies"]:
        if m["id"] == "range_bounce":
            m.update({"setups": [other["id"]], "state": "WAITING_FOR_CONFIRMATION"})
    return t


def test_the_strategy_monitor_shows_each_live_setup_with_direction_state_blocker_and_a_link_to_its_analysis(base, tmp_path):
    s = live(tmp_path, variant(base, trading=with_conflict(base)))
    rows = re.findall(r"<tr><td[^>]*>([^<]+)</td><td class=\"dir ([a-z]+)\"[^>]*>([A-Z]{2})</td><td><span class=\"pillst\"><span class=\"st st-([A-Z_]+)\">([^<]+)</span></span></td><td class=\"why\"[^>]*>([^<]*)</td><td class=\"go\">(.*?)</td></tr>", s["els"]["strat"]["html"])
    by = {r[0]: r for r in rows}
    assert set(by) == {"Trend Pullback", "Range Bounce"}  # a row for each live setup, and only for those
    assert by["Trend Pullback"][1:5] == ("up", "CE", "READY_TO_ACT", "ready") and by["Range Bounce"][1:5] == ("down", "PE", "WAITING_FOR_CONFIRMATION", "waiting")
    assert by["Range Bounce"][5] == "a completed 5M candle must still close below the trigger"  # the first thing that blocks entry
    assert 'data-goto="analysis" data-strategy="range_bounce"' in by["Range Bounce"][6] and 'data-strategy="trend_pullback"' in by["Trend Pullback"][6]
    assert "⚠ a call and a put setup are both live (CE Trend Pullback, PE Range Bounce): neither is taken" in shown(s, "strat")  # the conflict, said once
    assert "⚠ ignored Trend Pullback (CE): it runs against the 1H trend and holds no counter-trend permission" in shown(s, "strat")
    assert "9 without a setup" in shown(s, "strat") or "8 without a setup" in shown(s, "strat")
    assert 'data-goto="analysis" data-strategy=""' in PAGE.read_text(encoding="utf-8")  # and a link to all ten


def test_the_link_from_the_monitor_opens_that_strategys_analysis_detail(base, tmp_path):
    steps = [{"op": "message", "snap": variant(base, trading=with_conflict(base))}, {"op": "click", "dataset": {"goto": "analysis", "strategy": "range_bounce"}}, {"op": "capture", "label": "gone"}]
    s = run_page(tmp_path, steps)[0]
    assert s["tab"] == "analysis" and "sd-range_bounce" in s["open"] and "a-strat" in s["open"]
    body = s["els"]["analysis-body"]["html"]
    assert re.search(r'<details class="sd" data-key="sd-range_bounce" open>', body) and not re.search(r'<details class="sd" data-key="sd-failed_breakout" open>', body)
    all_ten = run_page(tmp_path, [{"op": "message", "snap": base}, {"op": "click", "dataset": {"goto": "analysis", "strategy": ""}}, {"op": "capture", "label": "all"}])[0]
    assert all_ten["tab"] == "analysis" and "a-strat" in all_ten["open"]


# ---- alerts ---------------------------------------------------------------------------------------------------------------------------------------------------------------------
def degraded(base):
    h = copy.deepcopy(base["health"])
    h["readiness"] = {**h["readiness"], "ready": False, "state": "RECOVERING_GAPS", "summary": "recovering missing candles: 4 partial candle(s)", "decisions_permitted": False}
    h["issues"] = ["no live connection for 6001 s (disconnected)"]
    h["warnings"] = ["NIFTY spot is stale: last tick 95 s ago", "28 of 28 option quotes are stale or missing (see Options Analysis)"]
    return variant(base, health=h)


def test_the_alerts_are_one_line_with_the_count_and_the_full_list_opens_on_a_click(base, tmp_path):
    steps = [{"op": "message", "snap": degraded(base)}, {"op": "capture", "label": "closed"}, {"op": "toggle", "key": "alerts", "open": True}, {"op": "message", "snap": degraded(base)},
             {"op": "capture", "label": "open"}, {"op": "toggle", "key": "alerts", "open": False}, {"op": "message", "snap": degraded(base)}, {"op": "capture", "label": "closed-again"}]
    closed, opened, again = run_page(tmp_path, steps)
    html = closed["els"]["alerts"]["html"]
    assert html.count("<details") == 1 and html.count("<summary") == 1 and 'class="alerts bad"' in html and " open" not in html.split("<summary")[0]  # one block, collapsed
    summary = tight(words(re.search(r"<summary[^>]*>(.*?)</summary>", html, re.S).group(1)))
    assert summary == "⛔ 4 alerts: Data not trusted (+3 more) ▾ details" and "\n" not in summary  # the count, the most serious one, and how many more
    listed = words(re.search(r'<div class="alertlist">(.*)</div></details>', html, re.S).group(1))
    for text in ("NOT READY · RECOVERING GAPS", "recovering missing candles: 4 partial candle(s)", "no live connection for 6001 s (disconnected)", "NIFTY spot is stale: last tick 95 s ago",
                 "28 of 28 option quotes are stale or missing"):
        assert text in listed, text  # every alert is still there, in the list
    assert re.search(r'data-key="alerts" open>', opened["els"]["alerts"]["html"]) and "alerts" in opened["open"]  # opened by the reader and kept open by the next snapshots
    assert 'data-key="alerts">' in again["els"]["alerts"]["html"] and "alerts" not in again["open"]


def test_a_single_alert_and_no_alert_read_correctly(base, tmp_path):
    h = copy.deepcopy(base["health"])
    h["warnings"] = ["ATM_CE NIFTY10NOV202624300CE has no quote yet"]
    one = live(tmp_path, variant(base, health=h))
    assert "⚠ 1 alert: ATM_CE NIFTY10NOV202624300CE has no quote yet ▾ details" in tight(words(one["els"]["alerts"]["html"])) and 'class="alerts warn"' in one["els"]["alerts"]["html"]
    assert one["els"]["alerts"]["html"].count("more)") == 0
    assert live(tmp_path, base)["els"]["alerts"]["html"] == ""  # nothing wrong: nothing drawn


# ====================================================================================================================
# 3. The Analysis tab
# ====================================================================================================================
NAMES = {"trend_pullback": "Trend Pullback", "breakout_retest": "Breakout and Retest", "range_reversal": "Range Reversal", "momentum_continuation": "Momentum Continuation",
         "ema_trend_continuation": "EMA Trend Continuation", "volatility_expansion": "Volatility Expansion", "opening_range_breakout": "Opening Range Breakout",
         "failed_breakout": "Failed Breakout (Trap Reversal)", "range_bounce": "Range Bounce", "trend_exhaustion_reversal": "Trend Exhaustion Reversal"}  # fmt: skip


def strategy_block(html, sid):
    m = re.search(rf'<details class="sd" data-key="sd-{sid}"[^>]*>(.*?)</details>(?=<details class="sd"|<div style="margin-top:6px">)', html, re.S)
    assert m, sid
    return m.group(1)


def test_the_analysis_tab_leads_with_the_regime_the_permission_and_all_ten_strategies_before_the_indicators(base, tmp_path):
    body = live(tmp_path, base, "analysis")["els"]["analysis-body"]["html"]
    order = [m for m in re.findall(r'<details class="sec" data-key="([a-z-]+)"', body)]
    assert order.index("a-regime") < order.index("a-ct") < order.index("a-strat") < order.index("a-tech") < order.index("a-struct") < order.index("a-levels")  # decision first, indicators after
    assert order[:3] == ["a-regime", "a-ct", "a-strat"] and "a-strat" in live(tmp_path, base, "analysis")["open"]
    ids = re.findall(r'<details class="sd" data-key="sd-([a-z_]+)"', body)
    assert ids == list(NAMES) and len(set(ids)) == 10  # all ten, once each: no duplicate strategy card
    assert all(NAMES[i] in strategy_block(body, i) for i in NAMES)


def test_each_strategy_shows_the_ce_and_the_pe_side_on_its_own_with_its_own_note(base, tmp_path):
    body = live(tmp_path, base, "analysis")["els"]["analysis-body"]["html"]
    for z in base["trading"]["strategies"]:
        block = strategy_block(body, z["id"])
        parts = re.split(r'<div class="side (?:ce|pe)">', block)[1:]
        assert len(parts) >= 2, z["id"]
        ce, pe = [p for p in parts if "CE (call)" in p[:40]][0], [p for p in parts if "PE (put)" in p[:40]][0]
        for side, mine, other in (("CE", ce, pe), ("PE", pe, ce)):
            sd = next(x for x in z["sides"] if x["side"] == side)
            if sd["note"]:
                assert words(sd["note"]) in words(mine), (z["id"], side)  # each side carries its own scanner's note
                if sd["note"] not in [y["note"] for y in z["sides"] if y["side"] != side]:
                    assert words(sd["note"]) not in words(other), (z["id"], side)  # and never the other side's
        assert f"id {z['id']}" in words(block) and "Market conditions:" in block
    pull = strategy_block(body, "trend_pullback")
    assert "no live" not in pull and "Expiry · status (Prism's wording)" in pull and "To confirm:" in pull  # the live setup's own facts sit under its strategy
    setup = base["trading"]["setups"][0]
    for value in (f"{setup['trigger']:.2f}", f"{setup['invalidation']:.2f}", f"{setup['zone'][0]:.2f} – {setup['zone'][1]:.2f}"):
        assert value in words(pull)


def test_the_live_setup_appears_once_under_its_strategy_and_not_again_as_a_separate_card(base, tmp_path):
    body = live(tmp_path, base, "analysis")["els"]["analysis-body"]["html"]
    setup = base["trading"]["setups"][0]
    assert words(body).count(setup["confirm_text"]) == 1  # (the confirmation text of the setup is shown in exactly one place)
    assert 'data-key="a-setups"' in body and "live setups are under their strategy above" in words(body)


def test_the_reversal_strategy_shows_its_weakening_evidence_its_structure_5m_confirmation_and_why_it_was_denied(base, tmp_path):
    t = copy.deepcopy(base["trading"])
    t["counter_trend"] = [{"strategy": "trend_exhaustion_reversal", "strategy_name": "Trend Exhaustion Reversal", "side": "PE", "needed": True, "granted": False, "setup_id": None, "structure": None,
                           "reason": "denied: weakening continuation (regime fact: 0 of 6 weakening checks hold (needs 2))", "exhausted_trend": "bullish",
                           "checks": [{"name": "an established trend", "ok": True, "text": "the 1H trend is bullish", "group": "established"},
                                      {"name": "weakening continuation", "ok": False, "text": "regime fact: 0 of 6 weakening checks hold (needs 2)", "group": "weakening"}]}]  # fmt: skip
    body = live(tmp_path, variant(base, trading=t), "analysis")["els"]["analysis-body"]["html"]
    ter = tight(words(strategy_block(body, "trend_exhaustion_reversal")))
    assert "Reversal evidence" in ter and "Weakening trend" in ter and "(regime fact:" in ter  # the weakening evidence: the regime fact and its checks
    assert "15M structural reversal and the counter-trend permission" in ter and "PE: permission DENIED — denied: weakening continuation" in ter  # the permission and why it was refused
    assert "✗ weakening continuation: regime fact: 0 of 6 weakening checks hold (needs 2)" in ter and "an established trend" not in ter.split("permission DENIED")[1]  # only the checks that failed
    assert "5M confirmation" in ter and "no setup exists until the 15M structural change has been confirmed" in ter
    assert "PE (put)" in ter and "CE (call)" in ter


# ====================================================================================================================
# 4. Navigation, unknown data, console
# ====================================================================================================================
def test_the_five_tabs_are_there_and_each_switches_without_errors(base, tmp_path):  # (four until Stage 8 added Trades)
    html = PAGE.read_text(encoding="utf-8")
    assert html.count('role="tab"') == 5 and html.count('role="tabpanel"') == 5
    steps = [{"op": "message", "snap": base}]
    for name in TABS:
        steps += [{"op": "click", "dataset": {"tab": name}}, {"op": "capture", "label": name}]
    states = run_page(tmp_path, steps)
    assert [s["tab"] for s in states] == list(TABS) and all(s["errors"] == [] for s in states)
    assert all(s["els"]["jserr"]["text"] == "" for s in states)  # the page's own error line stays empty on every tab


def test_nothing_unknown_is_drawn_as_undefined_nan_null_or_a_zero_on_any_tab(base, tmp_path):
    thin = copy.deepcopy(base)
    thin["analysis"] = {"available": False, "history": {"state": "loading"}, "warnings": ["Historical data is still loading"], "reliable": False}
    thin["options"] = {"available": False, "reason": "no option contracts are selected yet"}
    for inst in thin["instruments"]:
        inst["ltp"], inst["age"], inst["exchange_time"], inst["received_time"] = None, None, None, None
    h = thin["health"]
    for row in h["instruments"]:
        row.update({"age": None, "receipt_age": None, "exchange_age": None, "exchange_time": None, "received_time": None, "exchange_status": "unknown", "exchange_reason": "no quote has been received"})
    t = thin["trading"]
    t["market"].update({"change": {"available": False, "why": "no NIFTY price yet"}, "gap": None, "atr": {"5m": {"used": None, "with_gap": None}, "15m": {"used": None, "with_gap": None}}})
    for variant_snap in (thin, variant(base, trading=None), variant(base, trading={**t, "market": None, "session": None})):
        for tab in TABS:
            s = live(tmp_path, variant_snap, tab)
            text = shown(s)
            for bad in ("undefined", "NaN", "[object", "Infinity"):
                assert bad not in text, (tab, bad)
            assert " null" not in text and s["errors"] == [] and s["els"]["jserr"]["text"] == "", tab
    nothing = live(tmp_path, thin)
    assert shown(nothing, "spot") == "–" and shown(nothing, "spot-chg") == "" and "no NIFTY tick yet" in shown(nothing, "spot-age")  # no price: no movement, no age


def test_the_page_script_does_no_market_arithmetic_of_its_own():
    html = PAGE.read_text(encoding="utf-8")
    script = html[html.index("<script>") :]
    for forbidden in (r"\bpercent =", r"\bpoints =", r"previous_close -", r"\bprice - ", r"\.ltp -", r"\bpdc -", r"\bnow - "):  # the move from the previous close is the backend's, and so is every age
        assert not re.search(forbidden, script), forbidden
    assert "market.change" in script and "hs.exchange_age" in script and "hs.exchange_status" in script
