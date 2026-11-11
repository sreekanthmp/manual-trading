"""Alerts: the dashboard tells you when something needs you, instead of you having to watch it.

The page's own script runs under node (dashboard_harness.py) with a stand-in for the sound, the popup and the storage a browser offers. What is held:

  * the FIRST snapshot is a baseline and raises nothing, and an unchanged snapshot raises nothing
  * a setup becoming READY_TO_ACT raises the urgent alert (one sound, however many things changed in that snapshot; one popup each when popups are on)
  * a setup that WAS ready and is no longer (left the entry area, blocked, invalidated, ended) raises a warning that says why
  * Prism no longer ready, the live feed lost while the market is open, and the dashboard's own link lost raise a warning; becoming ready again is only noted
  * the three modes (sound + popup / sound / off) and what each does, persisted in the browser's storage, and the page working at all without sound, popups or storage
  * nothing a snapshot says reaches the page unescaped; no script error in any of it

Nothing here places or suggests an order: an alert only says that Prism's own stage changed."""

import copy
import json

import pytest
from dashboard_harness import base_snapshot, run_page, words

SOUND = {"act": [880, 1175, 880, 1175], "warn": [520, 390], "info": [660]}
MODE_KEY = "prism-alert-mode"


@pytest.fixture(scope="module")
def base():
    return base_snapshot()


def staged(base, stage, **over):
    """The snapshot with its one setup in that stage (the real BUY CE setup of the Phase 3 world is READY_TO_ACT)."""
    s = copy.deepcopy(base)
    s["trading"]["setups"][0]["stage"] = stage
    s["trading"]["setups"][0].update(over)
    return s


def msg(snap):
    return {"op": "message", "snap": snap}


def at(snap, seconds):
    """The same snapshot, `seconds` later (the server's clock: what the page's flicker rule reads)."""
    out = copy.deepcopy(snap)
    out["server_time"] = snap["server_time"] + seconds
    return out


def play(tmp_path, snaps, env=None, then=()):
    """The page after those snapshots (and these extra steps); the final capture."""
    steps = ([{"op": "env", **env}] if env is not None else []) + [msg(s) for s in snaps] + list(then)
    steps += [{"op": "eval", "code": "document.getElementById('probe').textContent = JSON.stringify(ALERT_LOG)"}, {"op": "capture", "label": "end"}]  # (the log the page keeps: every event, newest first)
    return run_page(tmp_path, steps)[-1]


def log_entries(state):
    return json.loads(state["els"]["probe"]["text"])


def log_text(state):
    """The events the page raised, newest first, as text (the page's ALERT_LOG)."""
    return " ".join(f"{x['title']} {x['text']}" for x in log_entries(state))


def line(state):
    """What the page's one alert line says, as text, and its html."""
    html = state["els"].get("alerts", {"html": ""})["html"]
    return words(html), html


def quiet(state):
    return not state["fx"]["tones"] and not state["fx"]["notes"] and not log_text(state) and "(" not in (state.get("title") or "")


# -- a baseline, and nothing for no change ---------------------------------------------------------------------------------------------------
def test_the_first_snapshot_is_a_baseline_even_when_a_setup_is_already_ready(tmp_path, base):
    st = play(tmp_path, [base])  # the page was opened while a BUY CE was ready: it shows it, it does not announce it
    assert quiet(st) and st["errors"] == []


def test_an_unchanged_snapshot_raises_nothing(tmp_path, base):
    st = play(tmp_path, [base, base, base])
    assert quiet(st) and st["errors"] == []


def test_stages_that_were_never_ready_raise_nothing_when_they_change_or_end(tmp_path, base):
    s1, s2, s3 = staged(base, "DETECTED"), staged(base, "WAITING_FOR_CONFIRMATION"), staged(base, "CONFIRMED")
    gone = copy.deepcopy(base)
    gone["trading"]["setups"] = []
    st = play(tmp_path, [s1, s2, s3, staged(base, "BLOCKED"), staged(base, "INVALIDATED"), gone])
    assert quiet(st)


# -- a setup becomes ready -------------------------------------------------------------------------------------------------------------------
def test_a_setup_becoming_ready_sounds_the_urgent_alert_and_says_what_and_where(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base])
    setup = base["trading"]["setups"][0]
    assert st["fx"]["tones"] == SOUND["act"] and st["fx"]["notes"] == []  # the default mode is a sound only
    text = log_text(st)
    assert "BUY CE ready: Trend Pullback" in text and setup["id"] in text
    assert "entry area 24281.00 – 24287.06" in text and "trigger 24281.00" in text and "invalidation 24270.48" in text
    assert "place any order yourself" in text
    assert st["title"] == "(1) Prism Market Data" and st["errors"] == []


def test_a_put_setup_says_buy_pe(tmp_path, base):
    put = staged(base, "CONFIRMED", direction="PE")
    ready = staged(base, "READY_TO_ACT", direction="PE")
    assert "BUY PE ready: Trend Pullback" in log_text(play(tmp_path, [put, ready]))


def test_a_setup_that_appears_already_ready_is_announced(tmp_path, base):
    none = copy.deepcopy(base)
    none["trading"]["setups"] = []
    st = play(tmp_path, [none, base])
    assert st["fx"]["tones"] == SOUND["act"] and "BUY CE ready" in log_text(st)


def test_two_setups_ready_at_once_sound_once_and_pop_up_twice(tmp_path, base):
    second = copy.deepcopy(base)
    other = copy.deepcopy(second["trading"]["setups"][0])
    other.update(id="breakout_retest:bull:1793940300", strategy="breakout_retest", strategy_name="Breakout and Retest")
    second["trading"]["setups"].append(other)
    first = staged(base, "CONFIRMED")
    first["trading"]["setups"].append({**copy.deepcopy(other), "stage": "CONFIRMED"})
    st = play(tmp_path, [first, second], env={"store": {MODE_KEY: "sound + popup"}, "permission": "granted"})
    assert st["fx"]["tones"] == SOUND["act"]  # one sound for the snapshot, not two
    assert sorted(n["title"] for n in st["fx"]["notes"]) == ["BUY CE ready: Breakout and Retest", "BUY CE ready: Trend Pullback"]
    assert all(n["sticky"] for n in st["fx"]["notes"]) and len({n["tag"] for n in st["fx"]["notes"]}) == 2
    assert log_text(st).count("BUY CE ready") == 2 and st["title"] == "(2) Prism Market Data"


# -- a ready setup stops being ready ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("after, over, said", [
    ("BLOCKED", {"blockers": ["5M candles missing 10:25-11:20 (inside the last 12 candles)"]}, "something blocks entry now: 5M candles missing 10:25-11:20"),
    ("BLOCKED", {"blockers": []}, "something blocks entry now."),
    ("INVALIDATED", {}, "it is now invalidated"),
    ("MISSED", {}, "it is now missed"),
    ("EXPIRED", {}, "it is now expired"),
])
def test_a_ready_setup_that_stops_being_ready_raises_a_warning_that_says_why(tmp_path, base, after, over, said):
    st = play(tmp_path, [base, staged(base, after, **over)])
    assert st["fx"]["tones"] == SOUND["warn"]
    text = log_text(st)
    assert "BUY CE no longer ready: Trend Pullback" in text and said in text
    assert st["errors"] == []


def test_a_ready_setup_that_has_gone_altogether_is_said_to_have_ended(tmp_path, base):
    gone = copy.deepcopy(base)
    gone["trading"]["setups"] = []
    st = play(tmp_path, [base, gone])
    assert st["fx"]["tones"] == SOUND["warn"] and "it has ended" in log_text(st)


def test_a_setup_that_goes_ready_and_then_ends_is_two_alerts_newest_first(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base, staged(base, "INVALIDATED")])
    text = log_text(st)
    assert text.index("no longer ready") < text.index("BUY CE ready")
    assert st["fx"]["tones"] == SOUND["act"] + SOUND["warn"] and st["title"] == "(2) Prism Market Data"


def test_leaving_the_entry_area_is_not_an_alert_by_itself(tmp_path, base):
    st = play(tmp_path, [base, at(staged(base, "CONFIRMED"), 5), at(staged(base, "CONFIRMED"), 600)])  # (it is on the page; it becomes news only if the setup ends or is blocked)
    assert quiet(st)


# -- a flickering entry area is one piece of news, not twenty (the real journal of 2026-11-11: one setup went READY_TO_ACT <-> CONFIRMED ~20 times in 7 minutes) ----------------------
def flicker(base, times, first="CONFIRMED", step=5, other="CONFIRMED"):
    """Snapshots every `step` seconds that alternate between ready and `other`, `times` of them."""
    return [at(base if i % 2 else staged(base, other), i * step) for i in range(times)] if first == other else []


def test_a_setup_flickering_in_and_out_of_the_entry_area_raises_one_alert(tmp_path, base):
    st = play(tmp_path, flicker(base, 41))  # 40 changes in 200 s
    assert st["fx"]["tones"] == SOUND["act"] and log_text(st).count("BUY CE") == 1 and "no longer" not in log_text(st)
    assert st["title"] == "(1) Prism Market Data"


def test_a_setup_that_was_ready_when_the_page_opened_and_then_flickers_raises_nothing(tmp_path, base):
    snaps = [base, at(staged(base, "CONFIRMED"), 5), at(base, 10), at(staged(base, "CONFIRMED"), 15), at(base, 20)]
    assert quiet(play(tmp_path, snaps))


def test_a_setup_that_comes_back_to_the_entry_area_after_five_minutes_is_news_again(tmp_path, base):
    out = [staged(base, "CONFIRMED"), at(base, 10), at(staged(base, "CONFIRMED"), 20)]
    assert log_text(play(tmp_path, out + [at(base, 10 + 299)])).count("BUY CE ready") == 1  # 299 s after it was last ready: the same entry area
    st = play(tmp_path, out + [at(base, 10 + 300)])
    assert log_text(st).count("BUY CE ready") == 2 and st["fx"]["tones"] == SOUND["act"] * 2  # 300 s: it was away long enough to be worth saying again


def test_a_setup_that_stays_ready_for_hours_is_announced_once(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED")] + [at(base, 60 * i) for i in range(1, 200)])
    assert st["fx"]["tones"] == SOUND["act"]


def test_a_setup_blocked_again_and_again_raises_one_warning_per_five_minutes(tmp_path, base):
    blocked = staged(base, "BLOCKED", blockers=["the feed is not trusted"])
    snaps = [base, at(blocked, 5), at(base, 10), at(blocked, 15), at(base, 20), at(blocked, 25)]
    st = play(tmp_path, snaps)
    assert log_text(st).count("no longer ready") == 1 and st["fx"]["tones"] == SOUND["warn"]
    st = play(tmp_path, snaps + [at(base, 400), at(blocked, 405)])
    assert log_text(st).count("no longer ready") == 2  # 5 minutes on, it is news again


def test_an_ended_setup_is_always_news_it_cannot_flicker_back(tmp_path, base):
    gone = copy.deepcopy(base)
    gone["trading"]["setups"] = []
    st = play(tmp_path, [base, at(gone, 5)])
    assert log_text(st).count("no longer ready") == 1 and "it has ended" in log_text(st)


# -- the data stops being trustworthy --------------------------------------------------------------------------------------------------------
def readiness(base, ready, state="READY", summary="data complete and trusted"):
    s = copy.deepcopy(base)
    s["health"]["readiness"].update(ready=ready, state=state, summary=summary)
    return s


def test_prism_becoming_not_ready_raises_a_warning_and_becoming_ready_again_is_only_noted(tmp_path, base):
    bad = readiness(base, False, "RECOVERING_GAPS", "recovering missing candles: 5M 12 missing (10:25-11:20)")
    st = play(tmp_path, [base, bad])
    assert st["fx"]["tones"] == SOUND["warn"]
    assert "Prism is NOT READY" in log_text(st) and "RECOVERING GAPS: recovering missing candles" in log_text(st) and "Decisions are blocked" in log_text(st)
    st = play(tmp_path, [base, bad, base])
    assert st["fx"]["tones"] == SOUND["warn"]  # the second change (ready again) made no sound
    assert "Prism is ready again" in log_text(st) and st["title"] == "(2) Prism Market Data"


def test_prism_flapping_between_ready_and_not_ready_raises_one_warning_per_five_minutes(tmp_path, base):
    bad = readiness(base, False, "RECOVERING_GAPS", "recovering missing candles")
    snaps = [base, at(bad, 5), at(base, 10), at(bad, 15)]
    st = play(tmp_path, snaps)
    assert log_text(st).count("Prism is NOT READY") == 1 and st["fx"]["tones"] == SOUND["warn"]
    assert log_text(play(tmp_path, snaps + [at(base, 400), at(bad, 405)])).count("Prism is NOT READY") == 2


def test_a_page_opened_while_prism_is_not_ready_does_not_announce_it(tmp_path, base):
    assert quiet(play(tmp_path, [readiness(base, False, "HISTORICAL_LOADING", "x"), readiness(base, False, "HISTORICAL_LOADING", "x")]))


def feed(base, state, live=True):
    s = copy.deepcopy(base)
    s["connection"]["state"] = state
    s["health"]["live"] = live
    return s


def test_losing_the_feed_while_the_market_is_open_raises_a_warning(tmp_path, base):
    st = play(tmp_path, [base, feed(base, "disconnected")])
    assert st["fx"]["tones"] == SOUND["warn"]
    assert "Live feed lost" in log_text(st) and "the feed is disconnected" in log_text(st)


def test_a_feed_that_drops_and_comes_back_and_drops_raises_one_warning_per_five_minutes(tmp_path, base):
    snaps = [base, at(feed(base, "disconnected"), 5), at(base, 10), at(feed(base, "disconnected"), 15)]
    assert log_text(play(tmp_path, snaps)).count("Live feed lost") == 1


def test_a_feed_that_is_down_while_the_market_is_closed_raises_nothing(tmp_path, base):
    assert quiet(play(tmp_path, [feed(base, "connected", live=False), feed(base, "disconnected", live=False)]))


def test_a_feed_that_was_never_connected_is_not_lost(tmp_path, base):
    assert quiet(play(tmp_path, [feed(base, "connecting"), feed(base, "disconnected")]))


def test_losing_the_dashboard_link_raises_a_warning_once_and_the_page_stays_quiet_while_stale(tmp_path, base):
    st = play(tmp_path, [base], then=[{"op": "close"}, {"op": "fire", "ms": 5000}, {"op": "close"}])
    assert st["fx"]["tones"] == SOUND["warn"] and st["conn"] == "stale"
    assert log_text(st).count("Lost the link to Prism") == 1 and "nothing on it is current" in log_text(st)


def test_what_changed_while_the_link_was_lost_is_announced_when_it_comes_back(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED")], then=[{"op": "close"}, msg(base)])
    assert st["conn"] == "live" and "BUY CE ready" in log_text(st)
    assert st["fx"]["tones"] == SOUND["warn"] + SOUND["act"]


# -- the three modes -------------------------------------------------------------------------------------------------------------------------
def test_off_still_logs_and_counts_but_makes_no_sound_and_no_popup(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base], env={"store": {MODE_KEY: "off"}, "permission": "granted"})
    assert st["fx"]["tones"] == [] and st["fx"]["notes"] == []
    assert "BUY CE ready" in log_text(st) and st["title"] == "(1) Prism Market Data"


def test_popups_need_the_browsers_permission(tmp_path, base):
    for permission in ("default", "denied"):
        st = play(tmp_path, [staged(base, "CONFIRMED"), base], env={"store": {MODE_KEY: "sound + popup"}, "permission": permission})
        assert st["fx"]["tones"] == SOUND["act"] and st["fx"]["notes"] == []  # still a sound and a log entry


def test_sound_mode_never_pops_up_even_when_the_browser_would_allow_it(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base], env={"store": {MODE_KEY: "sound"}, "permission": "granted"})
    assert st["fx"]["tones"] == SOUND["act"] and st["fx"]["notes"] == []


def test_a_popup_carries_the_title_the_text_and_a_tag_and_stays_for_an_urgent_one(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base], env={"store": {MODE_KEY: "sound + popup"}, "permission": "granted"})
    (n,) = st["fx"]["notes"]
    setup = base["trading"]["setups"][0]
    assert n["title"] == "BUY CE ready: Trend Pullback" and setup["id"] in n["body"] and n["tag"] == "ready:" + setup["id"] and n["sticky"] is True


def test_a_warning_popup_does_not_stay_on_screen_and_an_info_one_is_not_shown_at_all(tmp_path, base):
    bad = readiness(base, False, "DEGRADED", "no recent NIFTY tick")
    st = play(tmp_path, [base, bad, base], env={"store": {MODE_KEY: "sound + popup"}, "permission": "granted"})
    assert [(n["title"], n["sticky"]) for n in st["fx"]["notes"]] == [("Prism is NOT READY", False)]  # "ready again" is only in the log


def test_the_button_cycles_the_mode_saves_it_asks_for_popup_permission_once_and_plays_a_test_tone(tmp_path, base):
    steps = [{"op": "message", "snap": base}, {"op": "eval", "code": "cycleAlertMode()"}, {"op": "capture", "label": "a"},
             {"op": "eval", "code": "cycleAlertMode()"}, {"op": "capture", "label": "b"}, {"op": "eval", "code": "cycleAlertMode()"}, {"op": "capture", "label": "c"}]
    a, b, c = run_page(tmp_path, [{"op": "env", "permission": "default"}, *steps])
    assert (a["els"]["alert-mode"]["text"], a["fx"]["store"][MODE_KEY], a["fx"]["tones"], a["fx"]["asked"]) == ("🔔 off", "off", [], 0)  # the default is "sound": the first click turns it off
    assert (b["els"]["alert-mode"]["text"], b["fx"]["store"][MODE_KEY], b["fx"]["asked"]) == ("🔔 sound + popup", "sound + popup", 1)  # permission asked when popups are chosen
    assert b["fx"]["tones"] == SOUND["info"]  # a tone so you hear that it works
    assert (c["els"]["alert-mode"]["text"], c["fx"]["store"][MODE_KEY], c["fx"]["asked"]) == ("🔔 sound", "sound", 1)
    assert c["fx"]["tones"] == SOUND["info"] * 2 and "off" in a["els"]["alert-mode"]["cls"] and "off" not in b["els"]["alert-mode"]["cls"]


def test_permission_is_not_asked_again_when_it_is_already_decided(tmp_path, base):
    for permission in ("granted", "denied"):
        steps = [{"op": "env", "permission": permission, "store": {MODE_KEY: "off"}}, {"op": "eval", "code": "cycleAlertMode()"}, {"op": "capture", "label": "x"}]
        (st,) = run_page(tmp_path, steps)
        assert st["els"]["alert-mode"]["text"] == "🔔 sound + popup" and st["fx"]["asked"] == 0


def test_the_saved_mode_is_the_one_the_page_starts_in_and_a_junk_value_is_ignored(tmp_path, base):
    for saved, shown in (("off", "🔔 off"), ("sound + popup", "🔔 sound + popup"), ("sound", "🔔 sound"), ("loud", "🔔 sound"), ("", "🔔 sound")):
        (st,) = run_page(tmp_path, [{"op": "env", "store": {MODE_KEY: saved}}, {"op": "capture", "label": "x"}])
        assert st["els"]["alert-mode"]["text"] == shown, saved


# -- the page without the browser's help -----------------------------------------------------------------------------------------------------
def test_without_sound_popups_or_storage_the_alert_still_reaches_the_log_and_nothing_breaks(tmp_path, base):
    env = {"no_audio": True, "no_notification": True, "no_storage": True}
    st = play(tmp_path, [staged(base, "CONFIRMED"), base, staged(base, "BLOCKED")], env=env, then=[{"op": "eval", "code": "cycleAlertMode(); cycleAlertMode(); cycleAlertMode()"}])
    assert st["errors"] == [] and "BUY CE ready" in log_text(st) and "no longer ready" in log_text(st)
    assert st["els"]["alert-mode"]["text"] == "🔔 sound"  # three clicks round the three modes, with nothing stored and nothing to play


def test_a_snapshot_with_no_trading_health_or_connection_does_not_break_the_alerts(tmp_path, base):
    odd = {k: v for k, v in base.items() if k not in ("trading", "health", "connection")}
    odd["connection"] = {"state": "connected", "ticks_rejected": {}}
    st = play(tmp_path, [base, odd, base])
    assert not [e for e in st["errors"] if e.startswith("alerts")]  # (the rest of the page may complain about a snapshot with no trading section: the alerts may not)


# -- seen, cleared, escaped ------------------------------------------------------------------------------------------------------------------
def test_a_click_on_the_page_clears_the_unseen_count_in_the_title_and_the_log_stays(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base], then=[{"op": "click", "dataset": {"tab": "overview"}}])
    assert st["title"] == "Prism Market Data" and "BUY CE ready" in log_text(st)


def test_clear_empties_the_log_and_the_count(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base], then=[{"op": "click", "id": "alert-clear"}])
    assert log_text(st) == "" and st["title"] == "Prism Market Data"


# -- the events are in the page's ONE alert line (the Overview has to stay on one screen), not in a second banner -------------------------------------------
def test_a_setup_that_became_ready_is_on_the_one_alert_line_in_green_with_what_to_look_at(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base])
    text, html = line(st)
    assert html.count("<details") == 1 and 'class="alerts act"' in html and "1 alert" in text and "BUY CE ready: Trend Pullback" in text
    assert "entry area 24281.00" in text and 'id="alert-clear"' in html and "alertlog" not in st["els"]  # one line: no banner of its own


def test_an_event_leaves_the_line_after_ten_minutes_and_stays_in_the_log(tmp_path, base):
    snaps = [staged(base, "CONFIRMED"), at(base, 5)]
    assert "BUY CE ready" in line(play(tmp_path, snaps + [at(base, 5 + 600)]))[0]  # ten minutes on: still there
    st = play(tmp_path, snaps + [at(base, 5 + 601)])
    assert line(st)[0] == "" and "BUY CE ready" in log_text(st) and st["title"] == "(1) Prism Market Data"  # off the line, still in the log (and the count)


def test_the_state_alerts_are_not_repeated_on_the_line_as_events(tmp_path, base):
    bad = readiness(base, False, "RECOVERING_GAPS", "recovering missing candles")
    bad["health"]["readiness"]["ready"] = False
    st = play(tmp_path, [base, at(bad, 5)])
    text, html = line(st)
    assert html.count('<div class="alert ') == 1 and "1 alert" in text and "NOT READY" in text and "Prism is NOT READY" in log_text(st)
    assert 'id="alert-clear"' not in html  # nothing to clear on the line: the event is in the log, the state is the readiness line  # the readiness line is the state; the event is the sound and the log


def test_the_worst_thing_colours_the_line(tmp_path, base):
    bad = readiness(base, False, "FAILED", "login rejected")
    ready = staged(base, "READY_TO_ACT")
    snap = copy.deepcopy(ready)
    snap["health"]["readiness"].update(ready=False, state="FAILED", summary="login rejected")
    st = play(tmp_path, [staged(base, "CONFIRMED"), snap])
    text, html = line(st)
    assert 'class="alerts bad"' in html and "2 alerts" in text and html.count("<details") == 1  # the failure leads; the ready setup is listed with it
    assert 'class="alerts warn"' in line(play(tmp_path, [base, at(staged(base, "INVALIDATED"), 5)]))[1]


def test_clear_empties_the_line_and_the_log(tmp_path, base):
    st = play(tmp_path, [staged(base, "CONFIRMED"), base], then=[{"op": "click", "id": "alert-clear"}])
    assert line(st)[0] == "" and log_entries(st) == [] and st["title"] == "Prism Market Data"


def test_the_log_keeps_the_last_five_and_the_count_goes_on(tmp_path, base):
    def many(stage):
        s = copy.deepcopy(base)
        s["trading"]["setups"] = [{**copy.deepcopy(base["trading"]["setups"][0]), "id": f"trend_pullback:bull:{1793940000 + 300 * i}", "stage": stage} for i in range(8)]
        return s

    st = play(tmp_path, [many("CONFIRMED"), many("READY_TO_ACT")])  # eight setups become ready at once: eight alerts, one sound
    assert log_text(st).count("BUY CE ready") == 8 and st["title"] == "(8) Prism Market Data" and st["fx"]["tones"] == SOUND["act"]
    text, html = line(st)
    assert html.count('<div class="alert act">') == 5 and "5 alerts" in text  # the line lists the latest five of the eight


def test_what_a_snapshot_says_is_escaped_in_the_log(tmp_path, base):
    evil = '<img src=x onerror="alert(1)">'
    s1 = staged(base, "CONFIRMED")
    s2 = staged(base, "READY_TO_ACT", id=evil, strategy_name=evil)
    s1["trading"]["setups"][0]["id"] = evil
    st = play(tmp_path, [s1, s2])
    html = st["els"]["alerts"]["html"]  # (the one alert line: the setup that became ready is on it, escaped)
    assert "<img" not in html and "&lt;img" in html and st["errors"] == []


def test_alert_changes_is_a_pure_function_of_two_snapshots(tmp_path, base):
    code = ("document.getElementById('probe').textContent = JSON.stringify([alertChanges(null, S), alertChanges(S, null), alertChanges(undefined, undefined), alertChanges(S, S)]);")
    steps = [msg(base), {"op": "eval", "code": "var S = " + json.dumps(base)}, {"op": "eval", "code": code}, {"op": "capture", "label": "p"}]
    st = run_page(tmp_path, steps)[-1]
    assert st["els"]["probe"]["text"] == "[[],[],[],[]]"
