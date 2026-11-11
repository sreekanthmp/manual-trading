"""Phase 3 glue: sessions, the Phase 2 -> strategy context, the setup lifecycle, and the decision."""

from dataclasses import replace

import pytest
from helpers import FRI, MON, ist
from scenarios import OPEN, P, bull_pullback, t5

from app import trading as tr
from app.strategy import CONFIRMED, DETECTED, EXPIRED, INVALIDATED, WAITING, Params, Scan, Setup

SAT = (2026, 11, 7)


# -- trading session ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "h,m,s,phase,entries",
    [
        (8, 59, 59, "pre_open", False), (9, 15, 0, "opening", False), (9, 29, 59, "opening", False), (9, 30, 0, "morning", True),
        (11, 59, 59, "morning", True), (12, 0, 0, "midday", True), (13, 29, 59, "midday", True), (13, 30, 0, "afternoon", True),
        (14, 44, 59, "afternoon", True), (14, 45, 0, "late", False), (15, 29, 59, "late", False), (15, 30, 0, "closed", False),
    ],
)  # fmt: skip
def test_session_phases_follow_the_nse_day(h, m, s, phase, entries):
    info = tr.session_info(ist(h, m, s, day=FRI), P)
    assert (info["phase"], info["entries_allowed"]) == (phase, entries)
    assert info["strict"] == (phase == "midday")  # midday = stricter confirmation (thin participation)


def test_weekends_are_closed_and_the_timing_rules_are_configurable():
    assert tr.session_info(ist(10, 30, day=SAT), P)["phase"] == "closed"
    p = Params(observe_until="09:45", last_entry="14:00", midday_start="11:00", midday_end="12:00")
    assert tr.session_info(ist(9, 40, day=FRI), p)["phase"] == "opening"
    assert tr.session_info(ist(11, 30, day=FRI), p)["phase"] == "midday"
    assert tr.session_info(ist(14, 0, day=FRI), p)["phase"] == "late"
    assert "09:45" in tr.session_info(ist(9, 40, day=FRI), p)["note"] and "14:00" in tr.session_info(ist(14, 10, day=FRI), p)["note"]


def test_the_end_of_the_session_wins_whatever_order_the_times_are_configured_in():
    early_cutoff = Params(last_entry="10:50")  # a cutoff before the midday window
    assert tr.session_info(ist(10, 55, day=FRI), early_cutoff)["phase"] == "late"
    assert tr.session_info(ist(10, 45, day=FRI), early_cutoff)["phase"] == "morning"
    long_opening = Params(observe_until="11:00", midday_start="10:00", midday_end="12:00")  # opening overlaps midday
    assert tr.session_info(ist(10, 30, day=FRI), long_opening)["phase"] == "opening"
    assert tr.session_info(ist(11, 30, day=FRI), long_opening)["phase"] == "midday"
    assert tr.session_info(ist(15, 0, day=FRI), Params(close_time="14:00", last_entry="13:00"))["phase"] == "closed"
    assert tr.session_info(ist(10, 0, day=FRI), Params(open_time="10:30"))["phase"] == "pre_open"


def test_the_opening_range_is_the_first_fifteen_minutes():
    from scenarios import c5

    bars = [c5(0, 24500, 24520, 24490, 24510), c5(1, 24510, 24530, 24505, 24525), c5(2, 24525, 24528, 24495, 24500)]
    assert tr.opening_range(bars, ist(9, 20, day=FRI), P) == {"high": 24530, "low": 24490, "complete": False}  # still forming
    assert tr.opening_range(bars, OPEN + 900 + 10, P)["complete"] is True
    assert tr.opening_range(bars[:2], OPEN + 900 + 10, P)["complete"] is False  # a candle is missing
    assert tr.opening_range([], ist(9, 20, day=FRI), P) is None


# -- the Phase 2 analysis as the strategies see it ---------------------------------------------------------------------------
def tf(trend="bullish", adx=30.0, atr=10.0, ema=(24570.0, 24520.0), rsi=55.0, ratio=1.0, runs=(), warm=True, pattern="HH_HL", swings=(), cons=None, brk=None,
       atr_with_gap=None, ratio_with_gap=None):  # fmt: skip
    """`atr` / `ratio` are the intraday figures the strategies use; `atr14` / `atr_ratio` (with the overnight gap) default to the same."""
    return {
        "trend": trend, "adx": adx, "atr14": atr_with_gap or atr, "atr14_intraday": atr, "ema20": ema[0], "ema50": ema[1], "rsi14": rsi,
        "atr_ratio": ratio_with_gap or ratio, "atr_ratio_intraday": ratio, "warm": warm,
        "structure": {"pattern": pattern, "recent_swings": [{"kind": k, "start": s, "price": p} for k, s, p in swings]},
        "consolidation": cons, "breakout": brk, "missing": {"runs": list(runs)},
    }  # fmt: skip


def analysis(price=24596.0, trend="bullish", label="aligned_bullish", vol="normal", ratio=1.0, stale=False, runs5=(), runs15=(), clusters=(24565.0,),
             prev=None, cons15=None, warm=True):  # fmt: skip
    return {
        "available": True, "price": price, "history": {"stale": stale},
        "condition": {"trend": trend, "volatility": vol, "alignment": {"label": label, "text": "1H and 15M are both bullish: aligned."}},
        "timeframes": {
            "1h": tf(adx=30.0, atr=60.0, warm=warm), "15m": tf(atr=25.0, ratio=ratio, runs=runs15, swings=[("L", OPEN, 24500.0), ("H", OPEN + 2700, 24650.0)], cons=cons15),
            "5m": tf(adx=20.0, atr=10.0, runs=runs5), "1m": tf(adx=15.0, atr=4.0),
        },
        "reference": {"previous_day": prev}, "level_clusters": [{"price": x, "touches": 2, "from_1h": False} for x in clusters],
    }  # fmt: skip


def series_of(ctx):
    return {"5m": ctx.c5, "15m": ctx.c15}


def test_the_context_is_built_from_the_phase2_analysis():
    now = t5(20) + 3
    a = analysis(prev={"pdh": 24700.0, "pdl": 24400.0, "pdc": 24550.0}, cons15={"low": 24560.0, "high": 24650.0, "bars": 12})
    ctx, env = tr.build_context(a, series_of(bull_pullback(19)), now, P)
    assert ctx.trend == {"1h": "bullish", "15m": "bullish", "5m": "bullish", "1m": "bullish"} and ctx.alignment == "aligned_bullish"
    assert ctx.atr["5m"] == 10.0 and ctx.atr["15m"] == 25.0 and ctx.adx["1h"] == 30.0 and ctx.ema["15m"] == (24570.0, 24520.0)
    assert ctx.swings["15m"] == [("L", OPEN, 24500.0), ("H", OPEN + 2700, 24650.0)] and ctx.pattern["15m"] == "HH_HL"
    labels = {lv.label for lv in ctx.levels}
    assert {"swing level x2", "previous day high", "previous day low", "15M range high", "15M range low"} <= labels
    assert not any("opening range" in label for label in labels) or env["opening_range"]["complete"]
    assert ctx.consolidation["15m"] == (24560.0, 24650.0) and ctx.c5 == bull_pullback(19).c5
    assert ctx.can_confirm and ctx.blockers == [] and not ctx.strict
    assert env["session"]["phase"] == "morning" and env["data"] == {"ok": True, "issues": []} and not env["vol_extreme"]


def test_candles_completed_inside_the_opening_range_cannot_confirm():
    ctx, _ = tr.build_context(analysis(), series_of(bull_pullback(19)), t5(20), P)
    assert ctx.confirm_after == ist(9, 30, day=FRI)  # 09:30, from the configurable observe_until
    ctx, _ = tr.build_context(analysis(), series_of(bull_pullback(19)), t5(20), replace(P, observe_until="09:45"))
    assert ctx.confirm_after == ist(9, 45, day=FRI)


def test_the_strategies_measure_candles_without_the_overnight_gap():
    a = analysis()
    a["timeframes"]["5m"].update(atr14=47.0, atr_ratio=2.4)  # what a gap-up open does to the plain ATR for hours
    a["timeframes"]["15m"].update(atr14=73.0, atr_ratio=2.4)
    ctx, env = tr.build_context(a, series_of(bull_pullback(19)), t5(20), P)
    assert (ctx.atr["5m"], ctx.atr["15m"], ctx.vol_ratio) == (10.0, 25.0, 1.0)  # the sizes the rules are expressed in
    assert not env["vol_extreme"] and ctx.can_confirm  # 2.4x with the gap would have meant 'unusually high': no trade
    assert tr.market_summary(a, env)["atr"] == {"5m": {"used": 10.0, "with_gap": 47.0}, "15m": {"used": 25.0, "with_gap": 73.0}}
    a["timeframes"]["15m"]["atr_ratio_intraday"] = 2.4  # the candles themselves are unusually large: that is volatility
    ctx, env = tr.build_context(a, series_of(bull_pullback(19)), t5(20), P)
    assert env["vol_extreme"] and not ctx.can_confirm


def test_the_opening_gap_is_reported_as_a_fact_and_only_when_it_is_known():
    a = analysis(prev={"pdh": 24700.0, "pdl": 24400.0, "pdc": 24500.0})
    a["reference"].update(session_date="2026-11-06", session_open=24950.0)
    gap = tr.opening_gap(a, t5(20))
    assert gap == {"points": 450.0, "percent": pytest.approx(1.8367, abs=1e-3), "open": 24950.0, "previous_close": 24500.0}
    assert tr.opening_gap(a, ist(9, 40, day=MON)) is None  # the open belongs to another day
    a["reference"]["previous_day"] = dict(a["reference"]["previous_day"], stale=True)
    assert tr.opening_gap(a, t5(20)) is None  # the previous close is not the last session's: no gap is claimed
    a["reference"].update(previous_day=None)
    assert tr.opening_gap(a, t5(20)) is None and tr.opening_gap(analysis(), t5(20)) is None


def test_opening_range_levels_appear_once_the_range_is_complete():
    ctx_c5 = bull_pullback(19).c5  # starts at 09:15: the first three candles are the opening range
    ctx, env = tr.build_context(analysis(), {"5m": ctx_c5, "15m": []}, t5(20) + 3, P)
    assert {"opening range high", "opening range low"} <= {lv.label for lv in ctx.levels} and env["opening_range"]["complete"]
    ctx, _ = tr.build_context(analysis(), {"5m": ctx_c5, "15m": []}, t5(20) + 3, replace(P, opening_range_levels=False))
    assert not any("opening range" in lv.label for lv in ctx.levels)


def test_stricter_confirmation_applies_in_high_volatility_and_at_midday():
    ctx, _ = tr.build_context(analysis(vol="high", ratio=1.5), series_of(bull_pullback(19)), t5(20), P)
    assert ctx.strict and "volatility is high" in ctx.strict_why
    ctx, _ = tr.build_context(analysis(), series_of(bull_pullback(19)), ist(12, 30, day=FRI), P)
    assert ctx.strict and "midday" in ctx.strict_why
    ctx, _ = tr.build_context(analysis(vol="high", ratio=1.5), series_of(bull_pullback(19)), ist(12, 30, day=FRI), P)
    assert "volatility is high and midday" in ctx.strict_why


def test_a_confirmation_is_held_back_by_the_session_by_extreme_volatility_and_by_missing_candles():
    ctx, env = tr.build_context(analysis(), series_of(bull_pullback(19)), ist(9, 20, day=FRI), P)  # opening range still forming
    assert not ctx.can_confirm and ctx.blockers == [env["session"]["note"]] and "no entries before 09:30" in ctx.blockers[0]
    ctx, env = tr.build_context(analysis(vol="high", ratio=2.4), series_of(bull_pullback(19)), t5(20), P)
    assert not ctx.can_confirm and env["vol_extreme"] and "unusually high" in ctx.blockers[0]
    ctx, env = tr.build_context(analysis(stale=True), series_of(bull_pullback(19)), t5(20), P)
    assert not env["data"]["ok"] and "stale" in env["data"]["issues"][0] and not ctx.can_confirm
    ctx, env = tr.build_context(analysis(warm=False), series_of(bull_pullback(19)), t5(20), P)
    assert "1H: not enough completed candles" in env["data"]["issues"][0]


def test_only_recent_gaps_in_todays_session_are_data_problems():
    now = ist(11, 0, day=FRI)
    run = lambda a, b: {"start": ist(*a, day=FRI), "end": ist(*b, day=FRI), "count": 1}  # noqa: E731
    recent = analysis(runs5=[run((10, 20), (10, 25))])  # inside the last hour (12 five-minute candles)
    _, env = tr.build_context(recent, series_of(bull_pullback(19)), now, P)
    assert not env["data"]["ok"] and "5M candles missing 10:20-10:25" in env["data"]["issues"][0]
    old = analysis(runs5=[run((9, 45), (9, 50))])  # earlier today, more than an hour back
    assert tr.build_context(old, series_of(bull_pullback(19)), now, P)[1]["data"]["ok"]
    yesterday = analysis(runs5=[{"start": ist(15, 15, day=(2026, 11, 5)), "end": ist(15, 25, day=(2026, 11, 5)), "count": 2}])  # the source's end-of-day hole
    assert tr.build_context(yesterday, series_of(bull_pullback(19)), ist(9, 40, day=FRI), P)[1]["data"]["ok"]
    gap15 = analysis(runs15=[run((9, 45), (10, 0))])  # within the last 8 fifteen-minute candles (two hours)
    assert "15M candles missing 09:45-10:00" in tr.build_context(gap15, series_of(bull_pullback(19)), now, P)[1]["data"]["issues"][0]


# -- the lifecycle, driven by scripted scan results ----------------------------------------------------------------------------
def setup(key="trend_pullback:bull:1", status=DETECTED, strategy="trend_pullback", side="bull", **kw):
    return Setup(strategy, side, key, status, 1.0, level=24500.0, level_label="leg origin", trigger=kw.pop("trigger", 24590.0),
                 zone=(24590.0, 24600.0), invalidation=24559.5, **kw)  # fmt: skip


def script(monkeypatch, *results):
    """Make the book's strategies return these Scan results (one dict per update, keyed by slot)."""
    queue = list(results)
    monkeypatch.setattr(tr, "scan_all", lambda ctx, p, priors: queue.pop(0) if len(queue) > 1 else queue[0])


def book_at(now):
    env = {"session": tr.session_info(now, P), "data": {"ok": True, "issues": []}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None}
    return env


def tick(book, now):
    return book.update(None, book_at(now), now)


SLOT = ("trend_pullback", "bull")
T0 = ist(10, 40, day=FRI)


def test_a_setup_is_announced_once_and_only_moves_forward(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=DETECTED))}, {SLOT: Scan(True, "", setup(status=WAITING))},
           {SLOT: Scan(True, "", setup(status=WAITING, trigger=24592.0))}, {SLOT: Scan(True, "", setup(status=CONFIRMED))},
           {SLOT: Scan(True, "", setup(status=CONFIRMED))})  # fmt: skip
    for i in range(5):
        tick(book, T0 + 300 * i)
    states = [e["status"] for e in reversed(book.events)]
    assert states == [DETECTED, WAITING, CONFIRMED]  # one event per transition; unchanged updates are silent
    live = book.active[SLOT]
    assert live.status == CONFIRMED and live.detected_at == T0 and live.confirmed_at == T0 + 900
    assert len(book.events) == 3  # the repeated CONFIRMED update announced nothing


def test_a_confirmed_setup_is_frozen_and_never_goes_back(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=CONFIRMED, trigger=24590.0))},
           {SLOT: Scan(True, "", setup(status=WAITING, trigger=24999.0, blockers=["5M candles missing 10:20-10:25"], against=["new caution"]))})  # fmt: skip
    tick(book, T0)
    tick(book, T0 + 300)
    s = book.active[SLOT]
    assert s.status == CONFIRMED and s.trigger == 24590.0  # not downgraded, not moved
    assert s.blockers == ["5M candles missing 10:20-10:25"] and s.against == ["new caution"]  # only the surrounding conditions update


def test_a_setup_that_ended_is_never_announced_again(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))}, {SLOT: Scan(True, "closed below", setup(status=INVALIDATED, note="closed below"))},
           {SLOT: Scan(True, "", setup(status=CONFIRMED))})  # fmt: skip
    for i in range(3):
        tick(book, T0 + 300 * i)
    assert SLOT not in book.active and [s.status for s in book.recent] == [INVALIDATED] and book.recent[0].note == "closed below"
    assert [e["status"] for e in reversed(book.events)] == [WAITING, INVALIDATED]  # the third update (same key) is ignored
    assert "trend_pullback:bull:1" in book.terminal


def test_a_new_structure_after_an_ended_setup_is_a_new_setup(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(key="k1", status=CONFIRMED))}, {SLOT: Scan(True, "", setup(key="k1", status=EXPIRED, note="entry window"))},
           {SLOT: Scan(True, "", setup(key="k2", status=WAITING))})  # fmt: skip
    for i in range(3):
        tick(book, T0 + 300 * i)
    assert book.active[SLOT].key == "k2" and book.active[SLOT].status == WAITING
    assert [e["id"] for e in reversed(book.events)] == ["k1", "k1", "k2"]


def test_a_newer_structure_supersedes_an_unfinished_setup(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(key="k1", status=WAITING))}, {SLOT: Scan(True, "", setup(key="k2", status=DETECTED))})
    tick(book, T0)
    tick(book, T0 + 300)
    assert book.active[SLOT].key == "k2" and [(s.key, s.status) for s in book.recent] == [("k1", EXPIRED)]
    assert "superseded" in book.recent[0].note


def test_conditions_that_disappear_end_the_setup_with_the_reason(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))}, {SLOT: Scan(True, "price left the zone", None)})
    tick(book, T0)
    tick(book, T0 + 300)
    assert [(s.status, s.note) for s in book.recent] == [(EXPIRED, "no longer present: price left the zone")]  # its opportunity passed
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=CONFIRMED))}, {SLOT: Scan(False, "1H is sideways now", None)})
    tick(book, T0)
    tick(book, T0 + 300)
    assert [(s.status, s.note) for s in book.recent] == [(INVALIDATED, "1H is sideways now")]  # its premise failed


def test_a_setup_that_was_never_live_is_not_announced_when_it_is_already_over(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "closed below the origin", setup(key="old", status=INVALIDATED, note="x"))})
    tick(book, T0)
    assert not book.active and not book.recent and not book.events and "old" in book.terminal


def test_an_unconfirmed_setup_expires_after_the_detection_limit(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=DETECTED))})
    tick(book, T0)
    tick(book, T0 + 120 * 60)
    assert SLOT in book.active  # exactly two hours: still allowed
    tick(book, T0 + 120 * 60 + 60)
    assert SLOT not in book.active and "no confirmation within 120 minutes" in book.recent[0].note


def test_the_session_boundaries_end_setups(monkeypatch):
    waiting, confirmed = setup(key="w", status=WAITING), setup(key="c", status=CONFIRMED, strategy="breakout_retest")
    both = {SLOT: Scan(True, "", waiting), ("breakout_retest", "bull"): Scan(True, "", confirmed)}
    book = tr.SetupBook(P)
    script(monkeypatch, both)
    tick(book, ist(14, 40, day=FRI))
    assert len(book.active) == 2
    tick(book, ist(14, 46, day=FRI))  # late session: no new entries
    assert list(book.active) == [("breakout_retest", "bull")] and "late session" in book.recent[0].note  # the confirmed one stays
    tick(book, ist(15, 31, day=FRI))
    assert not book.active and "the market is closed" in book.recent[0].note
    assert {s.status for s in book.recent} == {EXPIRED}


def test_nothing_new_starts_late_in_the_session_or_when_the_market_is_closed(monkeypatch):
    for when in (ist(14, 50, day=FRI), ist(16, 0, day=FRI), ist(8, 0, day=FRI), ist(11, 0, day=SAT)):
        book = tr.SetupBook(P)
        script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))})
        tick(book, when)
        assert not book.active and not book.events


def test_a_new_session_starts_clean(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(key="k1", status=CONFIRMED))})
    tick(book, ist(11, 0, day=FRI))
    assert SLOT in book.active
    script(monkeypatch, {SLOT: Scan(True, "", setup(key="k1", status=WAITING))})  # the very same key on Monday is a different day
    tick(book, ist(10, 0, day=MON))
    assert book.active[SLOT].status == WAITING and book.recent[0].note == "the session ended" and book.recent[0].status == EXPIRED


def test_each_strategy_and_side_tracks_its_own_setup(monkeypatch):
    book = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(key="a", status=WAITING)),
                         ("breakout_retest", "bull"): Scan(True, "", setup(key="b", status=DETECTED, strategy="breakout_retest")),
                         ("range_reversal", "bear"): Scan(False, "not range-bound", None)})  # fmt: skip
    tick(book, T0)
    assert sorted(k for k in book.active) == [("breakout_retest", "bull"), ("trend_pullback", "bull")]


def test_the_lifecycle_end_to_end_on_the_real_strategy_scenarios():
    """Slot by slot the pullback goes DETECTED -> WAITING -> CONFIRMED, is announced three times, and ends INVALIDATED."""
    from test_strategies import extend

    book = tr.SetupBook(P)
    ctxs = [bull_pullback(n) for n in (16, 17, 18, 19)]
    for c in ctxs:
        book.update(c, book_at(c.now), c.now)
    assert [e["status"] for e in reversed(book.events)] == [DETECTED, WAITING, CONFIRMED]
    pullback = [s for s in book.active.values() if s.strategy == "trend_pullback" and s.side == "bull"]
    assert len(pullback) == 1 and pullback[0].status == CONFIRMED and pullback[0].trigger == 24590.0
    book.update(ctxs[-1], book_at(ctxs[-1].now), ctxs[-1].now)  # the same data again: nothing new
    assert len(book.events) == 3
    broken = extend(bull_pullback(19), (24596, 24597, 24550, 24555))
    book.update(broken, book_at(broken.now), broken.now)
    assert book.recent[0].status == INVALIDATED and "invalidation level" in book.recent[0].note
    book.update(ctxs[-1], book_at(ctxs[-1].now), ctxs[-1].now)  # earlier data replayed: the ended setup is not revived
    assert not [s for s in book.active.values() if s.strategy == "trend_pullback" and s.side == "bull"]


# -- presenting a setup against the live price -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "side,price,state",
    [
        ("bull", 24595.0, "in_entry_area"), ("bull", 24590.0, "in_entry_area"), ("bull", 24600.0, "in_entry_area"), ("bull", 24600.5, "extended"),
        ("bull", 24589.5, "below_trigger"), ("bull", 24560.0, "below_trigger"), ("bull", 24559.5, "through_invalidation"), ("bull", 24500.0, "through_invalidation"),
        ("bear", 25405.0, "in_entry_area"), ("bear", 25399.5, "extended"), ("bear", 25410.5, "above_trigger"), ("bear", 25440.5, "through_invalidation"),
    ],
)  # fmt: skip
def test_price_state_of_a_confirmed_setup(side, price, state):
    if side == "bull":
        s = replace(setup(status=CONFIRMED), zone=(24590.0, 24600.0), invalidation=24559.5)
    else:  # the mirror image: an entry area just below the trigger 25,410 and the invalidation above it
        s = replace(setup(status=CONFIRMED, side="bear"), zone=(25400.0, 25410.0), invalidation=25440.5)
    assert tr.price_state(s, price) == state


def test_only_confirmed_setups_have_a_price_state():
    assert tr.price_state(setup(status=WAITING), 24595.0) is None and tr.price_state(setup(status=CONFIRMED), None) is None


def view(status=CONFIRMED, direction="CE", strategy="trend_pullback", **kw):
    d = {"id": f"{strategy}:{direction}", "strategy": strategy, "strategy_name": {"trend_pullback": "Trend Pullback", "breakout_retest": "Breakout and Retest", "range_reversal": "Range Reversal"}[strategy],
         "direction": direction, "side": "bull" if direction == "CE" else "bear", "status": status, "zone": [24590.0, 24600.0], "trigger": 24590.0,
         "invalidation": 24559.5, "confirm_text": "a completed 5M candle closes above 24,590", "reasons": ["why 1", "why 2"], "against": [], "blockers": [],
         "price_state": "in_entry_area" if status == CONFIRMED else None, "actionable": status == CONFIRMED}  # fmt: skip
    d.update(kw)
    return d


def monitors(active=True, note=""):
    return [{"id": s, "name": s, "active": active, "why": f"{s} suits the market", "state": "NO SETUP", "note": note, "setups": []}
            for s in ("trend_pullback", "breakout_retest", "range_reversal")]  # fmt: skip


MARKET = {"direction": "bullish", "regime": "trend_up", "text": "Bullish trend", "volatility": "normal", "alignment": "aligned_bullish", "vol_ratio": 1.0}


def environment(now=None, **over):
    now = now or ist(11, 0, day=FRI)
    e = {"session": tr.session_info(now, P), "data": {"ok": True, "issues": []}, "vol_extreme": False, "vol_ratio": 1.0, "opening_range": None}
    e.update(over)
    return e


def decide(setups=(), market=MARKET, env=None, mons=None, price=24595.0, tick_ok=True):
    return tr.decide(market, env or environment(), list(setups), mons or monitors(), price, tick_ok, 10.0)


def test_a_confirmed_actionable_setup_is_a_buy_in_its_direction():
    d = decide([view(direction="CE")])
    assert (d["action"], d["strategy"], d["setup_id"]) == ("BUY CE", "trend_pullback", "trend_pullback:CE") and d["reasons"] == ["why 1", "why 2"]
    d = decide([view(direction="PE")], market={**MARKET, "regime": "trend_down", "direction": "bearish"})
    assert d["action"] == "BUY PE" and d["headline"].startswith("BUY PE")


@pytest.mark.parametrize(
    "state,text",
    [("extended", "do not chase"), ("below_trigger", "moved back through the trigger"), ("through_invalidation", "already through the invalidation")],
)
def test_a_confirmed_setup_whose_price_is_not_right_is_not_a_buy(state, text):
    d = decide([view(price_state=state, actionable=False)], price=24610.0)
    assert d["action"] == "WAIT" and "not actionable" in d["headline"] and any(text in r for r in d["reasons"])


def test_blockers_keep_a_confirmed_setup_from_being_a_buy():
    d = decide([view(blockers=["5M candles missing 10:20-10:25"], actionable=False)])
    assert d["action"] == "WAIT" and "5M candles missing 10:20-10:25" in d["reasons"]


def test_a_developing_setup_means_wait_and_names_what_to_wait_for():
    d = decide([view(status=WAITING)])
    assert d["action"] == "WAIT" and "developing" in d["headline"] and "a completed 5M candle closes above 24,590" in d["reasons"]
    quiet = decide([])
    assert quiet["action"] == "WAIT" and "no setup yet" in quiet["headline"]
    assert decide([view(status=DETECTED)])["action"] == "WAIT"


def test_opposite_setups_are_a_conflict_not_a_coin_flip():
    d = decide([view(status=WAITING, direction="CE"), view(status=CONFIRMED, direction="PE", strategy="range_reversal")])
    assert d["action"] == "WAIT" and "disagree" in d["headline"]
    assert "CE: Trend Pullback is WAITING_FOR_CONFIRMATION" in d["reasons"][0] and "PE: Range Reversal is CONFIRMED" in d["reasons"][0]
    assert decide([view(status=DETECTED, direction="CE"), view(status=CONFIRMED, direction="PE")])["action"] == "BUY PE"  # a merely detected setup is no conflict


def test_two_strategies_on_the_same_opportunity_give_one_decision():
    a, b = view(strategy="trend_pullback"), view(strategy="breakout_retest", trigger=24592.0, zone=[24592.0, 24602.0])
    d = decide([a, b])
    assert d["action"] == "BUY CE" and d["strategy"] == "breakout_retest" and d["also"] == ["Trend Pullback (same CE opportunity)"]
    apart = view(strategy="trend_pullback", trigger=24650.0, zone=[24650.0, 24660.0])
    assert decide([a, apart])["also"] == []  # different entry areas are not the same opportunity


@pytest.mark.parametrize(
    "env_over,market_over,headline",
    [
        (dict(data={"ok": False, "issues": ["5M candles missing 10:20-10:25"]}), {}, "data is not reliable"),
        (dict(vol_extreme=True, vol_ratio=2.4), {}, "volatility is unusually high"),
        ({}, dict(regime="conflicting", text="1H is bullish but 15M is bearish: conflicting timeframes."), "conflicting timeframes"),
        ({}, dict(regime="unclear", text="Not enough completed candles to classify the market"), "Not enough completed candles"),
    ],
)
def test_conditions_that_make_the_market_untradable_mean_no_trade(env_over, market_over, headline):
    d = decide([view()], market={**MARKET, **market_over}, env=environment(**env_over))  # even a confirmed setup is not taken
    assert d["action"] == "NO TRADE" and headline in d["headline"] and d["setup_id"] is None


def test_the_session_decides_when_trading_is_allowed():
    assert decide([view()], env=environment(ist(8, 0, day=FRI)))["action"] == "NO TRADE"
    assert decide([view()], env=environment(ist(15, 40, day=FRI)))["action"] == "NO TRADE"
    late = decide([view()], env=environment(ist(14, 50, day=FRI)))
    assert late["action"] == "NO TRADE" and "no new entries after 14:45" in late["headline"]
    opening = decide([view(status=WAITING)], env=environment(ist(9, 20, day=FRI)))
    assert opening["action"] == "WAIT" and "no entries before 09:30" in opening["headline"]


def test_no_strategy_that_suits_the_market_means_no_trade():
    d = decide([], mons=monitors(active=False))
    assert d["action"] == "NO TRADE" and "no strategy suits" in d["headline"]


def test_a_stale_price_blocks_a_decision_to_act():
    d = decide([view(status=WAITING)], tick_ok=False)
    assert d["action"] == "WAIT" and "no recent price tick" in d["headline"]


def test_market_summary_names_the_regime():
    a = analysis(trend="bullish", label="aligned_bullish")
    assert tr.market_summary(a, {"vol_ratio": 1.0})["regime"] == "trend_up"
    assert tr.market_summary(analysis(trend="bearish", label="aligned_bearish"), {"vol_ratio": None})["regime"] == "trend_down"
    assert tr.market_summary(analysis(trend="sideways", label="range_bound"), {"vol_ratio": None})["regime"] == "range"
    assert tr.market_summary(analysis(trend="sideways", label="developing"), {"vol_ratio": None})["regime"] == "developing"
    assert tr.market_summary(analysis(trend="sideways", label="conflicting"), {"vol_ratio": None})["regime"] == "conflicting"
    unclear = analysis(label="unknown")
    unclear["timeframes"]["1h"]["trend"] = "unknown"
    assert tr.market_summary(unclear, {"vol_ratio": None})["regime"] == "unclear"


def test_the_strategy_monitor_explains_active_and_inactive_strategies():
    scans = {("trend_pullback", "bull"): Scan(True, "pullback so far 10.0 pts: too shallow", None), ("trend_pullback", "bear"): Scan(False, "1H is bullish", None),
             ("range_reversal", "bull"): Scan(False, "the market is not range-bound", None), ("range_reversal", "bear"): Scan(False, "the market is not range-bound", None),
             ("breakout_retest", "bull"): Scan(True, "", None), ("breakout_retest", "bear"): Scan(False, "1H bullish: against", None)}  # fmt: skip
    m = {x["id"]: x for x in tr.monitor(scans, [view(strategy="breakout_retest", status=WAITING)], MARKET)}
    assert m["trend_pullback"]["active"] and m["trend_pullback"]["state"] == "NO SETUP" and "too shallow" in m["trend_pullback"]["note"]
    assert not m["range_reversal"]["active"] and m["range_reversal"]["state"] == "INACTIVE" and "not range-bound" in m["range_reversal"]["why"]
    assert m["breakout_retest"]["state"] == WAITING and m["breakout_retest"]["setups"] == ["breakout_retest:CE"]


def test_the_monitor_does_not_explain_a_live_setup_with_a_note_about_its_other_side():
    """A confirmed bearish range reversal, while the bullish side's structure was broken (nothing was announced for it)."""
    scans = {("range_reversal", "bull"): Scan(True, "price broke through the range edge 25,869.99: a genuine break, not a reversal", None),
             ("range_reversal", "bear"): Scan(True, "", None)}  # fmt: skip
    live = view(strategy="range_reversal", direction="PE", status=CONFIRMED)
    row = next(x for x in tr.monitor(scans, [live], MARKET) if x["id"] == "range_reversal")
    assert row["state"] == CONFIRMED and row["note"] == "CE: price broke through the range edge 25,869.99: a genuine break, not a reversal"
    alone = next(x for x in tr.monitor(scans, [], MARKET) if x["id"] == "range_reversal")
    assert alone["state"] == "NO SETUP" and alone["note"].startswith("CE: ")  # with no live setup the note says which side it is about
    both = {("range_reversal", "bull"): Scan(True, "no known level", None), ("range_reversal", "bear"): Scan(True, "price is 40% of the way across the range", None)}
    assert next(x for x in tr.monitor(both, [], MARKET) if x["id"] == "range_reversal")["note"] == "CE: no known level · PE: price is 40% of the way across the range"


def test_present_judges_the_setups_against_the_live_price():
    book = tr.SetupBook(P)
    ctx = bull_pullback(19)
    env = book_at(ctx.now)
    core = book.update(ctx, env, ctx.now)
    out = {}
    for price in (24595.0, 24612.0, 24585.0, 24550.0):
        a = analysis(price=price)
        out[price] = tr.present(core, a, env, ctx.now, P, tick_age=1.0)
    assert out[24595.0]["decision"]["action"] == "BUY CE" and out[24595.0]["decision"]["strategy_name"] == "Trend Pullback"
    s = out[24595.0]["setups"][0]
    assert (s["status"], s["trigger"], s["invalidation"], s["zone"], s["price_state"], s["actionable"]) == (CONFIRMED, 24590, 24559.5, [24590, 24600.0], "in_entry_area", True)
    assert out[24612.0]["decision"]["action"] == "WAIT" and out[24612.0]["setups"][0]["price_state"] == "extended"
    assert out[24585.0]["setups"][0]["price_state"] == "below_trigger" and out[24550.0]["setups"][0]["price_state"] == "through_invalidation"
    assert out[24595.0]["decision"]["reasons"] and out[24595.0]["strategies"][0]["id"] == "trend_pullback"
    stale = tr.present(core, analysis(price=24595.0), env, ctx.now, P, tick_age=60.0)  # the feed stopped a minute ago
    assert stale["decision"]["action"] == "WAIT" and stale["setups"][0]["actionable"] is False
    assert tr.present(core, analysis(price=24595.0), env, ctx.now, P, tick_age=None)["data"]["price_ok"] is False


def test_unavailable_is_a_no_trade_with_the_reason():
    u = tr.unavailable("history is loading", 123.0)
    assert u["available"] is False and u["decision"]["action"] == "NO TRADE" and "history is loading" in u["decision"]["headline"]


# -- a restart: today is picked up from the journal --------------------------------------------------------------------------------
def journal_of(book):
    """What the journal holds for this book: an evaluated record with its live setups, and every event so far (oldest first)."""
    return [{"market": {"regime": "trend_up"}, "setups": [tr.setup_dict(s, 24595.0, True, True) for s in book.active.values()],
             "events": list(reversed(book.events))}]  # fmt: skip


def test_a_restarted_book_carries_a_confirmed_setup_on_without_announcing_it_again(monkeypatch):
    first = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))}, {SLOT: Scan(True, "", setup(status=CONFIRMED))})
    tick(first, T0)
    tick(first, T0 + 300)
    again = tr.SetupBook(P)
    assert again.restore(journal_of(first), T0 + 320) == 1
    s = again.active[SLOT]
    assert s.resumed and s.status == CONFIRMED and (s.detected_at, s.confirmed_at) == (T0, T0 + 300)  # the original times, not the new run's
    assert s.trigger == 24590.0 and s.zone == (24590.0, 24600.0) and s.invalidation == 24559.5  # and the geometry it was confirmed with
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=CONFIRMED))})  # the candles still say the same thing
    tick(again, T0 + 330)
    assert [e["status"] for e in reversed(again.events)] == [WAITING, CONFIRMED]  # only what the first run announced
    assert again.seq == first.seq == 2 and again.active[SLOT].resumed
    assert tr.setup_dict(again.active[SLOT], 24595.0, True, True)["resumed"] is True


def test_a_restored_setup_that_is_still_waiting_advances_with_one_event_and_keeps_its_detection_time(monkeypatch):
    first = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=DETECTED))}, {SLOT: Scan(True, "", setup(status=WAITING))})
    tick(first, T0)
    tick(first, T0 + 300)
    again = tr.SetupBook(P)
    again.restore(journal_of(first), T0 + 400)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=CONFIRMED))})  # confirmed while the app was down
    tick(again, T0 + 600)
    assert [e["status"] for e in reversed(again.events)] == [DETECTED, WAITING, CONFIRMED]
    s = again.active[SLOT]
    assert (s.detected_at, s.confirmed_at) == (T0, T0 + 600) and again.events[0]["seq"] == 2 and s.resumed


def test_setups_that_had_ended_stay_ended_after_a_restart(monkeypatch):
    first = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))}, {SLOT: Scan(True, "closed below", setup(status=INVALIDATED, note="closed below"))})
    tick(first, T0)
    tick(first, T0 + 300)
    assert not first.active
    again = tr.SetupBook(P)
    assert again.restore(journal_of(first), T0 + 400) == 0 and "trend_pullback:bull:1" in again.terminal
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))})  # the candles would make the same structure look alive again
    tick(again, T0 + 500)
    assert not again.active and len(again.events) == 2  # it is not revived, and nothing is announced
    script(monkeypatch, {SLOT: Scan(True, "", setup(key="trend_pullback:bull:2", status=DETECTED))})  # a new structure is a new setup
    tick(again, T0 + 800)
    assert again.active[SLOT].key == "trend_pullback:bull:2" and again.events[0]["seq"] == 2 and not again.active[SLOT].resumed


def test_a_setup_that_expired_by_the_clock_is_not_given_a_fresh_clock_by_a_restart(monkeypatch):
    first = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=DETECTED))})
    tick(first, T0)
    tick(first, T0 + 120 * 60 + 60)  # no confirmation within 120 minutes: it ended
    assert not first.active
    again = tr.SetupBook(P)
    again.restore(journal_of(first), T0 + 130 * 60)
    tick(again, T0 + 130 * 60)  # the same unconfirmed structure is still on the chart
    assert not again.active  # without the journal it would have lived another two hours


def test_a_restored_setup_keeps_counting_its_detection_limit_from_the_original_time(monkeypatch):
    first = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=DETECTED))})
    tick(first, T0)
    again = tr.SetupBook(P)
    again.restore(journal_of(first), T0 + 119 * 60)
    tick(again, T0 + 121 * 60)
    assert not again.active and "no confirmation within 120 minutes" in again.recent[0].note


def test_restore_only_fills_a_book_that_has_not_started_and_needs_records(monkeypatch):
    first = tr.SetupBook(P)
    script(monkeypatch, {SLOT: Scan(True, "", setup(status=WAITING))})
    tick(first, T0)
    records = journal_of(first)
    running = tr.SetupBook(P)
    tick(running, T0)
    before = (dict(running.active), list(running.events))
    assert running.restore(records, T0 + 60) == 0 and (dict(running.active), list(running.events)) == before
    assert tr.SetupBook(P).restore([], T0) == 0


def test_restore_reads_the_last_evaluated_record_and_skips_what_it_cannot_read():
    good = tr.setup_dict(setup(status=CONFIRMED), 24595.0, True, True)
    garbage = [{"id": "nonsense"}, {"id": "range_reversal:bull:5", "strategy": "range_reversal", "side": "bull", "status": "WAITING_FOR_CONFIRMATION"},
               dict(good, strategy="mystery"), dict(good, status="EXPIRED"), dict(good, id="trend_pullback:bear:1")]  # fmt: skip
    records = [{"market": {"regime": "range"}, "setups": [good] + garbage, "events": []},
               {"market": None, "setups": [], "events": [], "decision": {}}]  # the run that crashed while it was still loading history
    book = tr.SetupBook(P)
    assert book.restore(records, T0) == 1  # the loading record carries no setups of its own: the one before it counts
    assert list(book.active) == [SLOT] and book.active[SLOT].resumed


def test_restore_keeps_the_newest_events_and_continues_the_numbering():
    events = [{"seq": i, "time": T0 + i, "id": f"trend_pullback:bull:{i}", "strategy": "trend_pullback", "strategy_name": "Trend Pullback",
               "direction": "CE", "status": "DETECTED", "text": f"DETECTED: {i}"} for i in range(70)]  # fmt: skip
    book = tr.SetupBook(P)
    book.restore([{"market": {}, "setups": [], "events": events}], T0 + 100)
    assert len(book.events) == 60 and book.events[0]["seq"] == 69 and book.events[-1]["seq"] == 10  # newest first, the oldest dropped
    assert book.seq == 70


def test_a_setup_that_an_older_journal_revived_counts_as_live_when_that_is_where_its_story_ended():
    dead_then_alive = [{"seq": 0, "time": T0, "id": "trend_pullback:bull:1", "status": "EXPIRED", "text": "x"},
                       {"seq": 0, "time": T0 + 60, "id": "trend_pullback:bull:1", "status": "WAITING_FOR_CONFIRMATION", "text": "y"}]  # fmt: skip
    live = tr.setup_dict(setup(status=WAITING), 24595.0, True, True)
    book = tr.SetupBook(P)
    assert book.restore([{"market": {}, "setups": [live], "events": dead_then_alive}], T0 + 120) == 1 and not book.terminal


def test_the_setup_dict_carries_what_a_restart_and_the_review_need():
    candle = {"start": T0 - 305.0, "end": T0 - 5.0, "open": 24585.0, "high": 24598.0, "low": 24584.0, "close": 24596.0}
    d = tr.setup_dict(Setup("trend_pullback", "bull", "trend_pullback:bull:1", CONFIRMED, 1.0, extreme=24562.0, confirmed_bar_end=T0 - 5.0, confirm_candle=candle), 24595.0, True, True)
    assert (d["extreme"], d["confirmed_bar_end"], d["resumed"], d["confirm_candle"], d["break_candle"]) == (24562.0, T0 - 5.0, False, candle, None)
    again = tr.setup_from_dict(dict(d, detected_at=T0, status_since=T0))  # and a restart gets it all back
    assert (again.extreme, again.confirmed_bar_end, again.confirm_candle, again.break_candle, again.resumed) == (24562.0, T0 - 5.0, candle, None, True)
