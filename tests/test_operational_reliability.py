"""Operational reliability: one Prism at a time, a start-up that says what it is doing, a history that is only 'complete' once it has been validated, recovery of
candles that are absent, and decisions that stay blocked until the data is trustworthy. Mocked Angel One responses only: no broker login, no database."""

import asyncio
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import market as MK
import pytest
from helpers import CFG, FRI, SPOT_TOKEN, Clock, hhmm, ist, msg, sample_contracts
from test_history import PG, Refusing
from test_phase3_integration import DAY, World

from app import history, instance, series
from app import main as app_main
from app import readiness as rd
from app.auth import AuthError
from app.candles import IST, Candle, CandleBuilder
from app.engine import MarketData
from app.gaps import API_INTERVAL, recover

ROOT = Path(__file__).resolve().parent.parent
INTERVAL_TF = {v: k for k, v in API_INTERVAL.items()}


# -- fixtures: a market whose candles are known, a candle API that replays them -------------------------------------------------------------------------
def world_db(w):
    """What Prism DB holds: every session before today."""
    cut = ist(0, 0, day=FRI)
    return MK.to_history([c for c in w.m1 if c.end <= cut])


class WorldApi:
    """getCandleData for the world: the real candles between fromdate and todate. `refuse`: refused for its rate; `silent`: answers without any candle; `skip`: HH:MM
    starts it does not have; `error`: raised on every call."""

    def __init__(self, w, refuse=False, silent=False, skip=(), error=None):
        self.hist, self.requests = MK.to_history(w.m1), []
        self.refuse, self.silent, self.skip, self.error = refuse, silent, set(skip), error

    def call(self, method, params):
        assert method == "getCandleData"
        self.requests.append(params)
        if self.error:
            raise self.error
        if self.refuse:
            raise Exception("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")
        if self.silent:
            return {"status": True, "data": []}
        tf = INTERVAL_TF[params["interval"]]
        lo, hi = (datetime.strptime(params[k], "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp() for k in ("fromdate", "todate"))
        rows = [[datetime.fromtimestamp(c.start, IST).isoformat(), c.open, c.high, c.low, c.close, 0] for c in self.hist[tf] if lo <= c.start <= hi and hhmm(c.start) not in self.skip]
        return {"status": True, "data": rows}


def set_history(w, md, slot, drop5=(), current=True, error=None):
    """Today's history up to the end of `slot`, as a refresh would leave it, with the 5M candles whose HH:MM is in `drop5` absent. Returns the time."""
    now = w.clock_after(slot)
    md.clock.t = now
    md.last_spot_recv = now - 1
    if not current:
        md.history_date = None  # a restart: no refresh has been validated for today yet
    hist = MK.to_history([c for c in w.m1 if c.end <= now])
    hist["5m"] = [c for c in hist["5m"] if hhmm(c.start) not in drop5]
    md.set_history(hist, {"state": "ready", "error": error}, current_date=DAY if current else None, loaded_at=now if current else None)
    return now


def world_md(slot=19, drop5=(), current=True, error=None):
    w = World()
    md, clock = w.engine()
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    md.set_state("connected")
    set_history(w, md, slot, drop5, current, error)
    return w, md, clock


def step_to_buy(w, md, clock, last=19, drop_at_last=()):
    """Slots 16.. as the live engine sees them, the candles of `drop_at_last` (5M HH:MM) absent from the last one."""
    out = None
    for slot in range(16, last + 1):
        set_history(w, md, slot, drop5=drop_at_last if slot == last else ())
        out = md.trading()
    return out


# =========================================================================================================================================================
# one Prism at a time
# =========================================================================================================================================================
def test_a_second_copy_is_refused_before_it_starts_anything(tmp_path):
    info = instance.acquire(tmp_path, 8000)
    try:
        with pytest.raises(instance.InstanceError, match="another Prism is already running") as e:
            instance.acquire(tmp_path, 8000)
        assert str(os.getpid()) in str(e.value) and "port 8000" in str(e.value) and "stop it first" in str(e.value)  # it says who is running and what to do
        assert info["pid"] == os.getpid() and instance.read_info(tmp_path)["pid"] == os.getpid()
    finally:
        instance.release()
    instance.acquire(tmp_path, 8000)  # the lock is gone with its holder
    instance.release()


def test_another_process_cannot_take_the_lock_and_the_lock_is_free_once_its_holder_exits(tmp_path):
    code = "import sys; sys.path.insert(0, sys.argv[1]); from app import instance; instance.acquire(sys.argv[2]); print('got it')"
    instance.acquire(tmp_path, 8000)
    try:
        other = subprocess.run([sys.executable, "-c", code, str(ROOT), str(tmp_path)], capture_output=True, text=True, timeout=60)
        assert other.returncode != 0 and "another Prism is already running" in other.stderr  # the cross-process case that matters: a second launch
    finally:
        instance.release()
    free = subprocess.run([sys.executable, "-c", code, str(ROOT), str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert free.returncode == 0 and "got it" in free.stdout  # and that process ended: no stale lock is left behind
    instance.acquire(tmp_path)
    instance.release()


def test_a_port_that_is_taken_is_explained_and_nothing_is_left_locked(tmp_path):
    argv = ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"]
    with pytest.raises(instance.InstanceError, match="port 8001 is already in use by another program") as e:
        instance.guard(tmp_path, argv, in_use=lambda h, p: True)
    assert "--port 8002" in str(e.value) and "Nothing has been started" in str(e.value)
    assert instance.guard(tmp_path, argv, in_use=lambda h, p: False)["port"] == 8001  # the lock was released by the failed attempt
    instance.release()


@pytest.mark.parametrize("argv,expected", [
    (["python", "-m", "uvicorn", "app.main:app", "--port", "8001"], ("127.0.0.1", 8001)),
    (["uvicorn", "app.main:app", "--port=9000", "--host=0.0.0.0"], ("0.0.0.0", 9000)),
    (["python", "-m", "uvicorn", "app.main:app"], ("127.0.0.1", 8000)),  # uvicorn's own default
    (["pytest", "tests"], None),  # not started by uvicorn: nothing is guessed
    (["python", "-m", "uvicorn", "app.main:app", "--port", "abc"], None),
])  # fmt: skip
def test_the_port_is_read_from_the_uvicorn_command_line(argv, expected):
    assert instance.target_address(argv, {}) == expected  # (no UVICORN_HOST / UVICORN_PORT of the machine this runs on)


def test_port_in_use_sees_a_listening_socket():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert instance.port_in_use("127.0.0.1", port) is True
    finally:
        s.close()
    assert instance.port_in_use("127.0.0.1", port) is False


def test_the_application_stops_at_once_when_another_copy_runs_and_reads_no_configuration(monkeypatch):
    def refused(*a, **k):
        raise instance.InstanceError("another Prism is already running (process 1)")

    def must_not_run(*a, **k):
        raise AssertionError("the application went on with its start-up after the lock was refused")

    monkeypatch.setattr(app_main.instance, "guard", refused)
    monkeypatch.setattr(app_main, "load_config", must_not_run)
    monkeypatch.setattr(app_main, "Journal", must_not_run)  # nothing is written to the shared journal
    app = app_main.create_app()

    async def enter():
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(instance.InstanceError):
        asyncio.run(enter())


def test_start_once_never_starts_a_second_task_with_the_same_name():
    gate = threading.Event()
    assert app_main.start_once("test-history", gate.wait) is True
    assert app_main.start_once("test-history", gate.wait) is False  # a repeated start-up cannot leave two of them
    gate.set()
    for t in threading.enumerate():
        if t.name == "test-history":
            t.join(5)
    assert app_main.start_once("test-history", lambda: None) is True  # once it has ended it can be started again


# =========================================================================================================================================================
# start-up states and readiness
# =========================================================================================================================================================
def test_start_up_goes_from_initializing_through_loading_to_ready_and_says_what_it_waits_for():
    w = World()
    md, clock = w.engine()
    now = clock.t = w.clock_after(19)
    r = md.readiness()
    assert r["state"] == "INITIALIZING" and "waiting for the Angel One login" in r["summary"] and r["decisions_permitted"] is False
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    md.set_state("connecting")
    r = md.readiness()
    assert r["state"] == "HISTORICAL_LOADING" and "reading the candles from Prism DB" in r["summary"]
    assert "(reading the candles from Prism DB)" in md.analysis()["warnings"][0] and "still loading" in md.analysis()["warnings"][0]  # not a bare "loading"
    md.set_state("connected")
    hist = MK.to_history([c for c in w.m1 if c.end <= now])
    md.set_history(hist, {"state": "db_loaded", "error": None})  # the DB part is in, today's candles are being fetched
    md.history_busy = True
    r = md.readiness()
    assert r["state"] == "HISTORICAL_LOADING" and "fetching today's candles from Angel One" in r["summary"]
    assert [s["status"] for s in r["stages"] if s["key"] == "history_today"] == ["active"]
    md.history_busy = False
    md.set_history(hist, {"state": "ready", "error": None}, current_date=DAY, loaded_at=now)
    md.last_spot_recv = now - 1
    r = md.readiness()
    assert r["state"] == "READY" and r["decisions_permitted"] is True and all(s["status"] == "done" for s in r["stages"])


def test_ready_needs_the_feed_a_fresh_price_and_a_trusted_clock():
    _, md, clock = world_md()
    assert md.readiness()["state"] == "READY"
    md.set_state("disconnected", "network down")
    r = md.readiness()
    assert r["state"] == "DEGRADED" and "no live connection" in r["summary"] and not r["decisions_permitted"]
    md.set_state("connected")
    md.last_spot_recv = clock.t - 120
    r = md.readiness()
    assert r["state"] == "DEGRADED" and "NIFTY price is 120 s old" in r["summary"]
    md.last_spot_recv = clock.t - 1
    md.timeguard = SimpleNamespace(trusted=False, reason="the PC clock jumped", status=lambda: {"trusted": False, "reason": "the PC clock jumped", "events": []})
    r = md.readiness()
    assert r["state"] == "DEGRADED" and "clock cannot be trusted" in r["summary"]
    md.timeguard = None
    md.set_state("auth_failed", "giving up after 5 rejected logins")
    r = md.readiness()
    assert r["state"] == "FAILED" and "fix it and restart" in r["summary"]


def test_before_the_open_prism_says_ready_only_when_the_history_is_complete():
    w = World()
    md, clock = w.engine()
    clock.t = ist(8, 30, day=FRI)
    md.set_contracts(sample_contracts(w.at(0)), DAY, w.at(0))
    md.set_state("connected")
    assert md.readiness()["state"] == "HISTORICAL_LOADING" and md.readiness()["ready"] is False  # nothing loaded: never READY
    md.set_history(MK.to_history([c for c in w.m1 if c.end <= ist(0, 0, day=FRI)]), {"state": "ready", "error": None}, current_date=DAY, loaded_at=clock.t)
    r = md.readiness()
    assert r["state"] == "READY" and r["pre_market"] is True and "ready for the open" in r["summary"]
    md.set_history(md.history, {"state": "ready", "error": "backfill incomplete (1 failed: 5m ...)"})  # a refresh that did not validate does not count
    md.history_date = None
    assert md.readiness()["ready"] is False


# =========================================================================================================================================================
# the history refresh: failures are told apart, retried soon, and never marked complete until validated
# =========================================================================================================================================================
def refresh(md, clock, session, loader, sleep=lambda s: None):
    return history.refresh(PG, md, session, now=clock, sleep=sleep, loader=loader)


def fresh_md(t=None):
    w = World()
    md, clock = w.engine()
    clock.t = t or w.clock_after(19)
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    md.set_state("connected")
    return w, md, clock


def test_a_database_failure_at_start_up_is_reported_retried_and_ends_failed_only_when_attempts_run_out():
    w, md, clock = fresh_md()

    def down(cfg, now):
        raise ConnectionError("connection refused")

    assert refresh(md, clock, WorldApi(w), down) is False
    r = md.readiness()
    assert md.history_kind == "db" and md.history_status["state"] == "error" and r["state"] == "DEGRADED" and "Prism DB" in r["summary"]
    md.history_attempts = history.MAX_ATTEMPTS
    assert md.readiness()["state"] == "FAILED" and "restart Prism" in md.readiness()["summary"]


def test_a_network_failure_is_retried_after_a_short_delay_and_is_not_an_authentication_failure():
    w, md, clock = fresh_md()
    api = WorldApi(w, error=ConnectionError("candle API unreachable"))
    assert refresh(md, clock, api, lambda cfg, now: world_db(w)) is False
    assert md.history_kind == "network" and md.history_date is None  # not marked complete
    assert md.history_retry_at == pytest.approx(clock.t + history.RETRY_FAST[0])  # 20 s, not 5 minutes
    r = md.readiness()
    assert r["state"] == "RECOVERING_GAPS" and "retrying in 20 s" in r["summary"] and r["decisions_permitted"] is False
    assert refresh(md, clock, WorldApi(w), lambda cfg, now: world_db(w)) is True  # the next try works
    assert md.history_failures == 0 and md.history_kind is None and md.history_retry_at is None and md.history_date == DAY
    md.last_spot_recv = clock.t - 1
    assert md.readiness()["state"] == "READY"


def test_a_rejected_login_is_not_retried_in_a_burst_and_ends_failed_with_the_reason():
    w, md, clock = fresh_md()
    api = WorldApi(w, error=AuthError("login rejected: Invalid TOTP"))
    assert refresh(md, clock, api, lambda cfg, now: world_db(w)) is False
    assert len(api.requests) == 1  # the whole fill stopped at the first rejection: asking again only risks the account
    assert history.classify(AuthError("x")) == "auth" and md.history_kind == "auth"
    assert md.history_retry_at == pytest.approx(clock.t + history.AUTH_RETRY)
    r = md.readiness()
    assert r["state"] == "FAILED" and "rejected the login" in r["summary"]


def test_failures_are_classified():
    assert history.classify(AuthError("login rejected")) == "auth"
    assert history.classify(Exception("Couldn't parse ... b'Access denied because of exceeding access rate'")) == "rate_limit"
    assert history.classify(ConnectionError("down")) == "network" and history.classify(TimeoutError("slow")) == "network"
    assert history.classify(ValueError("something else")) == "other"


def test_a_rate_limited_refresh_waits_out_the_limiters_cool_down_before_its_retry():
    w, md, clock = fresh_md()
    md.limiter.refused("exceeding access rate")  # the shared limiter is now cooling down
    assert refresh(md, clock, Refusing(refusals=10**6), lambda cfg, now: world_db(w)) is False
    assert md.history_kind == "rate_limit"
    assert md.history_retry_at - clock.t >= md.limiter.status()["cooldown_seconds"] + 5.0 - 0.01  # not before the cool-down plus a margin
    assert md.readiness()["state"] == "RECOVERING_GAPS" and md.readiness()["rate_limit"]["cooling_down"] is True


def test_the_refresh_loop_retries_a_refused_backfill_after_20_then_40_seconds_not_5_minutes():
    w = World()
    clock = Clock(ist(10, 0, 30))
    md = MarketData(CFG, clock)
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    md.set_state("connected")
    stop, tries = threading.Event(), []
    api = Refusing(refusals=18)  # the first two refreshes (3 windows x 3 tries each) are refused entirely, the third is answered

    def loader(cfg, now):
        tries.append(now - ist(10, 0, 30))
        return world_db(w)

    def wait(secs):
        clock.t += secs
        if md.history_date == DAY or clock.t > ist(10, 10):
            stop.set()

    history.run(PG, md, api, stop, now=clock, wait=wait, loader=loader, sleep=lambda s: None)
    assert tries == [0, 20, 60]  # at once, 20 s later, 40 s after that
    assert md.history_date == DAY and md.history_attempts == 3 and md.history_failures == 0


def test_only_one_history_refresh_runs_at_a_time_and_a_second_request_does_not_repeat_the_fetch():
    w, md, clock = fresh_md()
    entered, gate = threading.Event(), threading.Event()

    class Blocking(WorldApi):
        def call(self, method, params):
            entered.set()
            gate.wait(10)
            return super().call(method, params)

    api, results = Blocking(w), []
    t = threading.Thread(target=lambda: results.append(refresh(md, clock, api, lambda cfg, now: world_db(w))))
    t.start()
    assert entered.wait(10)
    before = len(api.requests)
    assert refresh(md, clock, api, lambda cfg, now: world_db(w)) is False  # refused at once: no second fetch of the same candles
    assert len(api.requests) == before
    gate.set()
    t.join(30)
    assert results == [True]


def test_a_refresh_is_validated_against_the_candles_the_decision_uses_before_it_is_called_complete():
    w, md, clock = fresh_md()
    api = WorldApi(w, skip=("10:35",))  # Angel One answers, but without one 5M candle inside the decision window
    assert refresh(md, clock, api, lambda cfg, now: world_db(w)) is False
    assert md.history_kind == "validation" and "candle validation failed" in md.history_status["error"] and "5M" in md.history_status["error"] and "10:35" in md.history_status["error"]
    assert md.history_date is None and md.analysis()["history"]["stale"] is True  # not marked current: decisions stay blocked
    assert md.readiness()["state"] == "RECOVERING_GAPS"
    assert refresh(md, clock, WorldApi(w), lambda cfg, now: world_db(w)) is True  # the candle arrives on the retry
    assert md.history_date == DAY


# =========================================================================================================================================================
# recovery of missing candles
# =========================================================================================================================================================
def test_fill_gap_asks_for_an_absent_interval_inside_todays_session_and_only_from_there():
    w = World()
    now = w.clock_after(19)
    hist = MK.to_history([c for c in w.m1 if c.end <= now])
    hist["5m"] = [c for c in hist["5m"] if hhmm(c.start) != "10:35"]
    api = WorldApi(w)
    added = history.fill_gap(api, SPOT_TOKEN, hist, now, sleep=lambda s: None)
    asked = {r["interval"]: r["fromdate"] for r in api.requests}
    assert asked["FIVE_MINUTE"] == "2026-11-06 10:35" and added >= 1  # from the hole, not from 09:15 and not from the end
    assert "10:35" in {hhmm(c.start) for c in hist["5m"]} and len({c.start for c in hist["5m"]}) == len(hist["5m"])  # filled once, no duplicate


def test_an_absent_candle_that_is_not_partial_and_in_no_recorded_gap_is_recovered_from_the_candle_api():
    w, md, clock = world_md(drop5=("10:35",))
    assert md.missing_spot_starts(clock.t) == {"5m": [ist(10, 35)]}  # absent from the history and from the live candles
    r = md.readiness()
    assert r["state"] == "RECOVERING_GAPS" and "5M 1 missing (10:35)" in r["summary"] and r["candles"]["missing_in_decision_window"] == {"5m": 1}
    api = WorldApi(w)
    assert recover(api, md, sleep=lambda s: None) == 1
    assert [r["interval"] for r in api.requests] == ["FIVE_MINUTE"] and md.missing_spot_starts(clock.t) == {}
    got = next(c for c in md.builder.closed[(SPOT_TOKEN, "5m")] if c.start == ist(10, 35))
    real = next(c for c in MK.to_history(w.m1)["5m"] if c.start == ist(10, 35))
    assert (got.open, got.high, got.low, got.close) == (real.open, real.high, real.low, real.close) and got.source == "history" and not got.partial  # the API's candle, not a made-up one
    assert md.readiness()["state"] == "READY"
    assert recover(api, md, sleep=lambda s: None) == 0 and len(api.requests) == 1  # nothing left: not asked again


def test_a_refused_recovery_request_gives_the_try_back_and_is_one_request_per_sweep():
    w, md, _ = world_md(drop5=("10:35",))
    refusing = WorldApi(w, refuse=True)
    for _ in range(6):
        assert recover(refusing, md, sleep=lambda s: None) == 0
    assert len(refusing.requests) == 6 and md.recovery_tries.get(("5m", ist(10, 35)), 0) == 0  # refused six times: the candle has used none of its tries
    assert recover(WorldApi(w), md, sleep=lambda s: None) == 1  # and is recovered as soon as the API answers


def test_a_candle_the_api_does_not_have_is_reported_unavailable_never_invented_and_blocks_decisions():
    w, md, _ = world_md(drop5=("10:35",))
    before = len(md.history["5m"]), len(md.builder.closed[(SPOT_TOKEN, "5m")])
    silent = WorldApi(w, silent=True)
    for _ in range(5):
        recover(silent, md, sleep=lambda s: None)
    assert len(silent.requests) == 3  # it had three tries, then it was left alone
    assert (len(md.history["5m"]), len(md.builder.closed[(SPOT_TOKEN, "5m")])) == before  # nothing was made up to fill the hole
    r = md.readiness()
    assert r["state"] == "DEGRADED" and "did not supply them" in r["summary"] and r["candles"]["unavailable_after_retries"] == 1 and not r["decisions_permitted"]


def test_partial_recovery_reports_what_is_still_missing_and_keeps_trying_for_it():
    w, md, _ = world_md(drop5=("10:35", "10:40"))
    api = WorldApi(w, skip=("10:40",))  # Angel One has one of the two
    assert recover(api, md, sleep=lambda s: None) == 1
    r = md.readiness()
    assert r["state"] == "RECOVERING_GAPS" and r["candles"]["missing_today"] == {"5m": 1} and "5M 1 missing (10:40)" in r["summary"]  # tries remain, so it is still recovering
    assert r["decisions_permitted"] is False  # and no decision is permitted meanwhile


def test_the_recovery_sweep_runs_every_15_seconds_while_candles_are_missing_and_every_minute_otherwise():
    from app import feed

    w, md, clock = world_md(drop5=("10:35",))
    assert md.needs_recovery(clock.t) is True and feed.FAST_RECOVERY_EVERY == 15.0 and feed.RECOVERY_EVERY == 60.0
    recover(WorldApi(w), md, sleep=lambda s: None)
    assert md.needs_recovery(clock.t) is False


def test_a_candle_missing_when_the_history_is_not_current_is_left_to_the_refresh():
    w, md, clock = world_md(drop5=("10:35",), current=False, error="backfill incomplete (1 failed: 5m)")
    assert md.missing_spot_starts(clock.t) == {}  # the refresh is the one fetching today's candles until it has validated them
    api = WorldApi(w)
    assert recover(api, md, sleep=lambda s: None) == 0 and api.requests == []


# =========================================================================================================================================================
# a restart in the middle of the session
# =========================================================================================================================================================
def test_after_a_restart_the_candles_are_recovered_merged_and_continuous_before_decisions_resume():
    w = World()
    clock = Clock(ist(10, 52, 20))
    md = MarketData(CFG, clock)  # the new process
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    md.set_state("connected")
    angel = WorldApi(w)
    assert refresh(md, clock, angel, lambda cfg, now: world_db(w)) is True  # today's candles from the open to the last completed ones
    assert md.history_date == DAY
    for ts in range(int(ist(10, 52, 21)), int(ist(11, 0, 13)), 10):  # live ticks resume mid-candle, every 10 s (the last, 11:00:01, closes the 10:55 candle normally)
        clock.t = ts
        md.on_message(msg(SPOT_TOKEN, w.at(36), ts, exch=1))
    clock.t = ist(11, 0, 12)  # past the 10 s grace after the 11:00 boundary: the 10:50 5M and 10:45 15M candles are due
    md.last_spot_recv = clock.t - 1
    gaps = rd.decision_window_gaps(md, md.history, clock.t)
    assert gaps == {"5m": [ist(10, 50)], "15m": [ist(10, 45)]}  # the candles the restart cut in half: partial live candles are never used
    r = md.readiness()
    assert r["state"] == "RECOVERING_GAPS" and r["decisions_permitted"] is False and r["candles"]["pending_partial"] >= 2
    assert md.trading()["decision"]["action"] == "NO TRADE" and "candles missing" in " ".join(md.trading()["decision"]["reasons"])  # blocked while they are missing
    assert recover(angel, md, sleep=lambda s: None) >= 2
    assert rd.decision_window_gaps(md, md.history, clock.t) == {}
    assert md.readiness()["state"] == "READY" and md.readiness()["decisions_permitted"] is True
    m5 = series.completed_series(md.history, md.builder, SPOT_TOKEN, "5m")
    assert len({c.start for c in m5}) == len(m5) and series.missing_intervals(m5, 300, clock.t)["latest_session_missing"] == 0  # one candle per start, none missing
    assert next(c for c in m5 if c.start == ist(10, 50)).source == "history"


# =========================================================================================================================================================
# candle integrity
# =========================================================================================================================================================
def spot_tick(ts, price, exch=None):
    from helpers import tick

    return tick(SPOT_TOKEN, price, ts if exch is None else exch, recv=ts)


def test_a_late_tick_never_reopens_a_closed_candle_and_a_duplicate_never_adds_one():
    b = CandleBuilder([SPOT_TOKEN])
    for ts, price in ((ist(10, 0, 30), 100.0), (ist(10, 3, 0), 101.0), (ist(10, 5, 10), 102.0)):
        b.on_tick(spot_tick(ts, price))
    closed = b.closed[(SPOT_TOKEN, "5m")]
    assert [hhmm(c.start) for c in closed] == ["10:00"] and closed[0].close == 101.0
    b.on_tick(spot_tick(ist(10, 4, 0), 999.0, exch=ist(10, 4, 0)))  # out of order: it belongs to a candle that is already closed
    assert [hhmm(c.start) for c in b.closed[(SPOT_TOKEN, "5m")]] == ["10:00"] and b.closed[(SPOT_TOKEN, "5m")][0].high == 101.0
    day = [Candle(ist(10, 0), ist(10, 5), 1, 2, 0.5, 1.5, source="history"), Candle(ist(10, 0), ist(10, 5), 9, 9, 9, 9, source="history")]
    b.fill(SPOT_TOKEN, "5m", day)
    b.fill(SPOT_TOKEN, "5m", day)
    assert [hhmm(c.start) for c in b.closed[(SPOT_TOKEN, "5m")]] == ["10:00"]  # a valid live candle is not overwritten and nothing is duplicated


def test_history_and_live_candles_merge_with_history_winning_and_partial_live_candles_never_used():
    hist = {"5m": [Candle(ist(10, 0), ist(10, 5), 1, 2, 0.5, 1.5, source="angel")]}
    b = CandleBuilder([SPOT_TOKEN])
    b.closed[(SPOT_TOKEN, "5m")].extend([
        Candle(ist(10, 0), ist(10, 5), 7, 8, 6, 7, source="live"),  # the same start as the history: the history wins
        Candle(ist(10, 5), ist(10, 10), 7, 8, 6, 7, source="live", partial=True),  # partial: not trustworthy
        Candle(ist(10, 10), ist(10, 15), 7, 8, 6, 7, source="live"),  # complete and not in the history: used
    ])  # fmt: skip
    merged = series.completed_series(hist, b, SPOT_TOKEN, "5m")
    assert [(hhmm(c.start), c.source) for c in merged] == [("10:00", "angel"), ("10:10", "live")]


def test_the_forming_candle_is_never_in_the_completed_series():
    b = CandleBuilder([SPOT_TOKEN])
    b.on_tick(spot_tick(ist(9, 59, 50), 99.0))  # watching since before the 10:00 candle began, so it is not a partial one
    b.on_tick(spot_tick(ist(10, 0, 30), 100.0))
    assert (SPOT_TOKEN, "5m") in b.current and series.completed_series({}, b, SPOT_TOKEN, "5m") == []  # still forming: it does not count
    b.on_tick(spot_tick(ist(10, 5, 5), 101.0))
    assert [hhmm(c.start) for c in series.completed_series({}, b, SPOT_TOKEN, "5m")] == ["10:00"]


def test_an_hour_is_built_only_from_a_complete_set_of_15m_candles_and_from_all_of_them():
    quarters = [Candle(ist(9, 15) + 900 * i, ist(9, 30) + 900 * i, 10 + i, 20 + i, 5 + i, 12 + i) for i in range(4)]  # the hours start at 09:15
    (hour,) = series.aggregate(quarters, 3600, 900)
    assert hhmm(hour.start) == "09:15" and (hour.open, hour.high, hour.low, hour.close) == (10, 23, 5, 15) and hour.source == "aggregated"  # first open, highest high, lowest low, last close
    assert series.aggregate(quarters[:3], 3600, 900) == [] and series.aggregate(quarters[:2] + quarters[3:], 3600, 900) == []  # an incomplete hour makes no candle at all


def test_missing_starts_reports_only_completed_intervals_that_are_absent():
    have = {ist(9, 15) + 300 * i for i in range(9)} - {ist(9, 30)}  # 09:15 .. 09:55 except 09:30
    miss = series.missing_starts(have, 300, ist(10, 5, 15), ist(9, 15))
    assert [hhmm(s) for s in miss] == ["09:30", "10:00"]  # 10:00 ended at 10:05:00 and its 10 s grace is over; 10:05 has not ended
    assert series.missing_starts(have, 300, ist(10, 5, 5), ist(9, 15)) == [ist(9, 30)]  # inside the grace period the candle is not yet due
    assert series.missing_starts(set(), 300, ist(9, 14), ist(9, 15)) == []  # before the open nothing is expected


def test_a_day_with_no_candle_at_all_is_not_a_gap():
    w = World()
    md, clock = w.engine()
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    clock.t = ist(11, 0, 30)  # a market holiday on a weekday: the history holds earlier sessions only
    assert rd.decision_window_gaps(md, world_db(w), clock.t) == {} and rd.today_gaps(md, world_db(w), clock.t) == {}


# =========================================================================================================================================================
# the decision stays blocked until the data is trustworthy
# =========================================================================================================================================================
def test_no_buy_while_todays_history_has_not_been_validated_and_the_normal_flow_resumes_after():
    w, md, clock = world_md(slot=16)
    out = step_to_buy(w, md, clock, last=18)
    assert out["decision"]["action"] == "WAIT"
    set_history(w, md, 19, current=False, error="backfill incomplete (1 failed: 5m 2026-11-06 10:35: candle API down)")  # the refresh did not validate
    out = md.trading()
    assert out["decision"]["action"] == "NO TRADE" and "historical data is stale: today's refresh has not completed" in out["decision"]["reasons"]
    r = md.readiness()
    assert r["state"] == "RECOVERING_GAPS" and r["decisions_permitted"] is False
    set_history(w, md, 19)  # validated now
    out = md.trading()
    assert out["decision"]["action"] == "BUY CE" and md.readiness()["decisions_permitted"] is True  # and only now


def test_no_buy_while_required_candles_are_missing_and_the_decision_returns_after_the_recovery():
    w, md, clock = world_md(slot=16)
    out = step_to_buy(w, md, clock, last=19, drop_at_last=("10:35",))
    assert out["decision"]["action"] == "NO TRADE" and "5M candles missing" in " ".join(out["decision"]["reasons"])
    assert md.readiness()["state"] == "RECOVERING_GAPS" and md.readiness()["decisions_permitted"] is False
    assert recover(WorldApi(w), md, sleep=lambda s: None) == 1
    out = md.trading()
    assert out["decision"]["action"] == "BUY CE" and md.readiness()["state"] == "READY"


def test_a_slow_load_never_turns_into_a_buy():
    w = World()
    md, clock = w.engine()
    md.set_contracts(sample_contracts(w.at(37)), DAY, w.at(37))
    md.set_state("connected")
    for minutes in (1, 30, 120, 300):  # however long it takes
        clock.t = w.clock_after(19) + 60 * minutes
        md.last_spot_recv = clock.t - 1
        out = md.trading()
        assert out["available"] is False and out["decision"]["action"] == "NO TRADE" and "still loading" in out["decision"]["headline"]
        assert md.readiness()["state"] == "HISTORICAL_LOADING" and md.readiness()["decisions_permitted"] is False


def test_the_existing_stale_price_protection_is_unchanged_by_the_readiness_check():
    w, md, clock = world_md(slot=16)
    step_to_buy(w, md, clock, last=19)
    md.last_spot_recv = clock.t - 120  # the live price stopped
    out = md.trading()
    assert out["decision"]["action"] != "BUY CE" and "BUY" not in out["decision"]["action"]
    assert md.readiness()["decisions_permitted"] is False


# =========================================================================================================================================================
# diagnostics
# =========================================================================================================================================================
def test_health_and_the_snapshot_carry_the_readiness_and_the_diagnostics_and_stay_json():
    import json

    _, md, clock = world_md()
    md.instance = {"pid": 4242, "started_at": clock.t - 90, "port": 8000}
    md.started_at = clock.t - 90
    h = md.health(clock.t)
    assert h["readiness"]["state"] == "READY" and h["diagnostics"]["analysis_permitted"] is True
    d = h["diagnostics"]
    assert d["uptime_seconds"] == pytest.approx(90) and d["instance"]["pid"] == 4242 and d["startup_state"] == "READY"
    assert {"history", "missing_candles", "last_recovery_attempt", "last_spot_tick_age", "feed_state", "reconnects", "background_tasks", "startup_stage"} <= set(d)
    assert d["history"]["current_for_today"] is True and d["history"]["last_success"] == clock.t and d["last_spot_tick_age"] == pytest.approx(1.0)
    snap = md.snapshot()
    assert snap["health"]["readiness"]["state"] == "READY"
    json.dumps(snap)


def test_the_readiness_endpoint_and_the_health_endpoint_answer():
    _, md, _ = world_md(drop5=("10:35",))
    app = app_main.create_app(md=md, feed=None)
    app.state.md = md
    route = lambda p: next(r for r in app.routes if getattr(r, "path", "") == p).endpoint  # noqa: E731
    r = route("/api/readiness")()
    assert r["state"] == "RECOVERING_GAPS" and r["stages"] and r["candles"]["missing_in_decision_window"] == {"5m": 1}
    h = route("/api/health")()
    assert h["readiness"]["state"] == "RECOVERING_GAPS" and h["diagnostics"]["missing_candles"] == {"5m": 1}


def test_a_state_change_is_logged_once_not_on_every_poll(caplog):
    _, md, _ = world_md()
    with caplog.at_level("INFO", logger="app.engine"):
        for _ in range(5):
            md.readiness()
        md.set_state("disconnected", "network down")
        for _ in range(5):
            md.readiness()
    lines = [r.getMessage() for r in caplog.records if "readiness:" in r.getMessage()]
    assert len(lines) == 2 and "start -> READY" in lines[0] and "READY -> DEGRADED" in lines[1]


def test_a_failing_analysis_cannot_break_the_readiness_check_or_the_health_endpoint(monkeypatch):
    _, md, clock = world_md()
    monkeypatch.setattr(md, "analysis", lambda: (_ for _ in ()).throw(ZeroDivisionError("bug in the analysis")))
    h = md.health(clock.t)  # must not raise
    assert h["readiness"]["state"] in ("READY", "DEGRADED") and isinstance(h["diagnostics"], dict)


def test_the_dashboard_shows_the_readiness_stages_and_the_not_ready_banner():
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for needle in ("c-ready", "NOT READY", "READY FOR OPEN", "Start-up stage", "Decisions permitted", "Decisions are blocked until the data is complete and trusted", "readinessBlock", "Next retry"):
        assert needle in html, needle
