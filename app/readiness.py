"""Is Prism ready to be trusted, and if not, exactly why not and what is happening about it?

A single "Historical data is still loading." used to cover very different situations: the broker login had not finished, the database was being read, Angel
One refused the candle requests for their rate, candles were missing after a restart, or the feed was down. This names the situation, shows the start-up as
stages, and says whether decisions are permitted. It only READS what the engine already knows: it never changes a candle, a threshold or a decision rule, and
the decision keeps its own data checks (trading.data_issues, MarketData.health_issues) exactly as before. READY here means those checks have nothing to
object to."""

from datetime import datetime

from .candles import IST, OPEN_S, day_start, in_session
from .series import completed_series, missing_starts

INITIALIZING, HISTORICAL_LOADING, RECOVERING_GAPS = "INITIALIZING", "HISTORICAL_LOADING", "RECOVERING_GAPS"
READY, DEGRADED, FAILED = "READY", "DEGRADED", "FAILED"
STATES = (INITIALIZING, HISTORICAL_LOADING, RECOVERING_GAPS, READY, DEGRADED, FAILED)
SECS = {"1m": 60, "5m": 300, "15m": 900}
DECISION_WINDOW = (("5m", "completeness_bars_5m"), ("15m", "completeness_bars_15m"))  # the same windows Phase 3 refuses to decide inside
MAX_TRIES = 3  # tries a missing candle gets from the candle API (gaps.MAX_TRIES)


def _have(md, hist, tf) -> set:
    return {c.start for c in completed_series(hist, md.builder, md.spot_token, tf)}


def decision_window_gaps(md, hist, now: float) -> dict:
    """tf -> starts of the completed NIFTY candles that are absent from the history AND from the live candles, inside the window Phase 3 needs complete.
    Empty outside the session."""
    if hist is None or md.spot_token is None or not in_session(now):
        return {}
    opening, out = day_start(now) + OPEN_S, {}
    for tf, attr in DECISION_WINDOW:
        secs, bars = SECS[tf], getattr(md.params, attr, None)
        have = _have(md, hist, tf)
        if bars is None or not any(s >= opening for s in have):
            continue  # a day with no candle at all (a market holiday) is not examined, as in Phase 3
        window = max(opening, now - bars * secs)
        miss = [s for s in missing_starts(have, secs, now, opening) if s + secs > window]
        if miss:
            out[tf] = miss
    return out


def today_gaps(md, hist, now: float) -> dict:
    """tf -> every completed NIFTY candle of today's session that is absent (1M, 5M, 15M). Empty outside the session."""
    if hist is None or md.spot_token is None or not in_session(now):
        return {}
    opening, out = day_start(now) + OPEN_S, {}
    for tf, secs in SECS.items():
        have = _have(md, hist, tf)
        if any(s >= opening for s in have) and (m := missing_starts(have, secs, now, opening)):
            out[tf] = m
    return out


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def describe_gaps(gaps: dict) -> str:
    return "; ".join(f"{tf.upper()} {len(starts)} missing ({_hm(starts[0])}{'' if len(starts) == 1 else '-' + _hm(starts[-1])})" for tf, starts in gaps.items())


def loading_detail(md) -> str:
    """One short phrase for the 'still loading' message: what the start-up is waiting for right now."""
    if not md.contracts and md.connected_at is None:
        return "waiting for the Angel One login and today's contracts"
    if md.history is None:
        return "reading the candles from Prism DB"
    return "fetching today's candles from Angel One"


def assess(md, now: float) -> dict:
    today, live = datetime.fromtimestamp(now, IST).date(), in_session(now)
    err, kind = md.history_status.get("error"), md.history_kind
    has_history = md.history is not None
    current = has_history and md.history_date == today
    next_try = md.history_next_try(now)
    retry = f"; retrying in {next_try:.0f} s" if next_try is not None else ""
    window = decision_window_gaps(md, md.history, now) if current else {}
    missing_today = today_gaps(md, md.history, now) if current else {}
    pending = md.recovery_status(now)["pending_candles"] if md.builder is not None else 0
    exhausted = sum(1 for tf, ss in missing_today.items() for s in ss if md.recovery_tries.get((tf, s), 0) >= MAX_TRIES)
    still_trying = sum(len(ss) for ss in missing_today.values()) - exhausted + pending
    tm = md.timeguard.status() if md.timeguard is not None else None
    clock_bad = bool(tm) and not tm["trusted"]
    connected = md.state == "connected"
    spot_age = md.spot_age(now)
    spot_ok = spot_age is not None and spot_age <= md.params.max_price_age
    unwarm = []
    if current:
        a = md.safe_analysis()  # a failing analysis must not take the readiness check, the health endpoint or the dashboard push down with it
        if a.get("available"):
            unwarm = [tf.upper() for tf in ("1h", "15m", "5m") if not a["timeframes"][tf]["warm"]]

    # ---- the stages, in the order the start-up goes through them ----
    def stage(key, label, status, detail):
        return {"key": key, "label": label, "status": status, "detail": detail}

    broker = md.contracts or md.connected_at is not None
    stages = [
        stage("config", "Configuration", "failed" if md.state == "config_error" else "done", md.feed_reason if md.state == "config_error" else "credentials present"),
        stage("clock", "Clock and session", "failed" if clock_bad else "done", tm["reason"] if clock_bad else "PC clock checked against a real timer" if tm else "not checked"),
        stage("broker", "Angel One login", "failed" if md.state == "auth_failed" else "done" if broker else "active", md.feed_reason if md.state == "auth_failed" else "logged in" if broker else "logging in"),
        stage("instruments", "Instruments and contracts", "done" if md.contracts and md.selected_on == today else "active" if broker else "pending",
              f"{len(md.contracts)} contracts" if md.contracts else "choosing today's contracts"),
        stage("history_db", "Historical candles (Prism DB)", "done" if has_history else "failed" if err else "active", err if (err and not has_history) else f"{sum(len(v) for v in md.history.values())} candles" if has_history else "reading"),
        stage("history_today", "Today's candles (Angel One)",
              "done" if current else "active" if md.history_busy else ("retrying" if next_try is not None and kind != "auth" and md.history_attempts < md.history_max_attempts else "failed") if err and has_history else "pending",
              "complete and validated" if current else (err or "fetching") if has_history else "waiting for the database"),
        stage("candles", "Candle continuity", "done" if current and not window and not pending else "active" if current else "pending",
              describe_gaps(window) if window else f"{pending} partial candle(s) being recovered" if pending else "no missing candles in the decision window" if current else "not checked yet"),
        stage("feed", "Live feed", "done" if connected and (spot_ok or not live) else "active" if md.state in ("starting", "connecting") else "failed" if not connected else "warn",
              f"connected, NIFTY tick {spot_age:.0f} s ago" if connected and spot_age is not None else "connected" if connected else md.feed_reason or md.state),
    ]

    # ---- the overall state ----
    cap = md.history_max_attempts
    if md.state in ("config_error", "auth_failed"):
        state, why = FAILED, f"the live feed has stopped ({md.feed_reason or md.state}): fix it and restart Prism"
    elif kind == "auth" and not current:
        state, why = FAILED, f"Angel One rejected the login for the candle history ({err}): check the credentials and restart Prism"
    elif not current and md.history_attempts >= cap:
        state, why = FAILED, f"today's history could not be completed after {md.history_attempts} attempts ({err or 'no error recorded'}): restart Prism"
    elif not has_history and md.connected_at is None and not md.contracts:
        state, why = INITIALIZING, "starting: " + loading_detail(md)
    elif not has_history:
        if md.history_busy or not err:
            state, why = HISTORICAL_LOADING, loading_detail(md)
        else:
            state, why = DEGRADED, f"the history could not be read ({err}){retry}"
    elif not current:
        if md.history_busy or not err:
            state, why = HISTORICAL_LOADING, loading_detail(md)
        elif kind in ("rate_limit", "network", "validation") or "backfill incomplete" in err:
            plain = {"rate_limit": "Angel One is limiting the candle requests (too many in a short time), so today's candles are incomplete: waiting out its cool-down",
                     "network": "Angel One could not be reached for today's candles, so they are incomplete",
                     "validation": err}.get(kind, f"today's candles are incomplete ({err})")  # the raw error stays in history.error
            state, why = RECOVERING_GAPS, f"{plain}{retry}"
        else:
            state, why = DEGRADED, f"today's history is not refreshed ({err}){retry}"
    elif window or pending:
        what = describe_gaps(window) if window else f"{pending} partial candle(s)"
        if still_trying > 0:
            state, why = RECOVERING_GAPS, f"recovering missing candles: {what}"
        else:
            state, why = DEGRADED, f"candles are missing and Angel One did not supply them: {what}"
    elif md.connected_at is None and md.state in ("starting", "connecting"):
        state, why = INITIALIZING, "waiting for the first live connection"
    elif not connected:
        state, why = DEGRADED, f"no live connection ({md.feed_reason or md.state}); reconnecting"
    elif clock_bad:
        state, why = DEGRADED, f"the clock cannot be trusted: {tm['reason']}"
    elif live and not spot_ok:
        state, why = DEGRADED, "no recent NIFTY tick" if spot_age is None else f"the NIFTY price is {spot_age:.0f} s old"
    elif unwarm:
        state, why = DEGRADED, f"not enough completed candles to classify {', '.join(unwarm)} yet"
    else:
        state, why = READY, ("ready for the open: history complete, feed connected, clock trusted" if not live else "data complete and trusted")

    cool = md.limiter.status()
    return {
        "state": state, "ready": state == READY, "decisions_permitted": state == READY, "summary": why, "pre_market": not live and state == READY,
        "stages": stages,
        "history": {"state": md.history_status.get("state"), "current_for_today": current, "last_success": md.history_loaded_at, "error": err, "error_kind": kind,
                    "attempts": md.history_attempts, "max_attempts": cap, "next_try_in": next_try, "busy": md.history_busy},
        "candles": {"missing_in_decision_window": {tf: len(v) for tf, v in window.items()}, "missing_today": {tf: len(v) for tf, v in missing_today.items()},
                    "pending_partial": pending, "unavailable_after_retries": exhausted, "recovering": still_trying > 0},
        "rate_limit": {"cooling_down": cool["cooling_down"], "cooldown_seconds": cool["cooldown_seconds"]},
    }
