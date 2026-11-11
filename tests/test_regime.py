"""Stage 3: the market regime facts (app/regime.py), on hand-built analysis dicts and 15M candle series. ATR(15M) = 25 throughout. Every fact is a list of named checks with the
numbers behind them; a check whose input is missing is UNKNOWN (ok None), never false, and a fact an unknown check could still decide is "unknown". Each directional fact is also run
mirrored (the bearish twin) with a transformation written here, not borrowed from the code under test."""

import copy
from dataclasses import replace

import pytest
from scenarios import OPEN, P

from app import regime as rg
from app.candles import Candle

ATR = 25.0
SIDES = ["bullish", "bearish"]
FLIP = {"bullish": "bearish", "bearish": "bullish", "up": "down", "down": "up", "HH_HL": "LH_LL", "LH_LL": "HH_HL"}
M = 50000.0


def candles(spec):
    return [Candle(OPEN + 900 * j, OPEN + 900 * (j + 1), o, h, low, c, 0, source="db") for j, (o, h, low, c) in enumerate(spec)]


def rising(n=40, start=24000.0, step=8.0, body=6.0):
    """A steady climb: each close `step` above the last, bodies of `body`, a little wick each side."""
    out = []
    for j in range(n):
        c = start + step * j
        out.append((c - body, c + 2.0, c - body - 1.0, c))
    return out


def tfd(trend="bullish", adx=32.0, ratio=1.0, ema20=24300.0, ema50=24200.0, slope=5.0, stack=20, pattern="HH_HL", box=None, breakout=None, adx_slope=1.0, e20atr=1.0, e50atr=1.5, run=2,
        warm=True, atr=ATR):  # fmt: skip
    return {"trend": trend, "adx": adx, "atr14_intraday": atr, "atr_ratio_intraday": ratio, "ema20": ema20, "ema50": ema50, "ema20_slope": slope, "ema_stack_bars": stack,
            "structure": {"pattern": pattern}, "consolidation": box, "breakout": breakout, "warm": warm,
            "extension": {"ema20_atr": e20atr, "ema50_atr": e50atr, "run": run, "adx_slope": adx_slope, "break_against": None}}  # fmt: skip


def analysis(t1, t15, alignment="aligned_bullish"):
    return {"timeframes": {"1h": t1, "15m": t15, "5m": {"trend": "sideways"}, "1m": {"trend": "sideways"}}, "condition": {"alignment": {"label": alignment}}}


def mirror_series(spec):
    return [(M - o, M - low, M - h, M - c) for o, h, low, c in spec]


def mirror_tf(t):
    m = copy.deepcopy(t)
    m["trend"] = FLIP.get(t["trend"], t["trend"])
    m["ema20_slope"] = None if t["ema20_slope"] is None else -t["ema20_slope"]
    m["ema_stack_bars"] = None if t["ema_stack_bars"] is None else -t["ema_stack_bars"]
    m["structure"] = {"pattern": FLIP.get(t["structure"]["pattern"], t["structure"]["pattern"])}
    e = m["extension"]
    e["ema20_atr"], e["ema50_atr"], e["run"] = (None if e[k] is None else -e[k] for k in ("ema20_atr", "ema50_atr", "run"))
    m["breakout"] = {"breakout": "breakdown", "breakdown": "breakout", None: None}[t["breakout"]]
    m["ema20"], m["ema50"] = M - t["ema20"], M - t["ema50"]
    if t["consolidation"]:
        m["consolidation"] = {"low": M - t["consolidation"]["high"], "high": M - t["consolidation"]["low"]}
    return m


def facts_of(t1, t15, spec, alignment="aligned_bullish", **kw):
    return rg.regime_facts(analysis(t1, t15, alignment), {"15m": candles(spec)}, kw.pop("p", P), **kw)


def mirrored(t1, t15, spec, alignment="aligned_bullish", **kw):
    return rg.regime_facts(analysis(mirror_tf(t1), mirror_tf(t15), alignment), {"15m": candles(mirror_series(spec))}, kw.pop("p", P), **kw)


def check(f, name_part):
    return next(c for c in f["checks"] if name_part in c["name"])


def twin(state):
    return FLIP.get(state, state)


# ====================================================================================================================
# Strong trend
# ====================================================================================================================
@pytest.mark.parametrize("direction", SIDES)
def test_a_strong_trend_needs_every_listed_check_and_names_the_direction(direction):
    t1, t15, spec = tfd(), tfd(), rising()
    f = (facts_of if direction == "bullish" else mirrored)(t1, t15, spec)["facts"]["strong_trend"]
    assert f["state"] == direction and all(c["ok"] is True for c in f["checks"])
    assert [c["name"] for c in f["checks"]][:2] == [f"1H and 15M both {direction}", "15M ADX at least 25"]
    assert check(f, "closes on the trend side")["text"] == "100% of them" and check(f, "ADX not falling")["text"].startswith("15M ADX changed by 1.0")


@pytest.mark.parametrize("direction", SIDES)
@pytest.mark.parametrize("change,failing", [
    ({"adx": 22.0}, "15M ADX at least"), ({"adx_slope": -3.0}, "ADX not falling"), ({"stack": 3}, "EMA20 on the"), ({"slope": -1.0}, "EMA20 sloping"),
    ({"pattern": "mixed"}, "15M structure with the trend"), ({"trend": "sideways"}, "1H and 15M both"),
])  # fmt: skip
def test_one_failed_check_means_not_strong_and_it_is_the_one_that_is_named(direction, change, failing):
    t15 = tfd(**change)
    f = (facts_of if direction == "bullish" else mirrored)(tfd(), t15, rising())["facts"]["strong_trend"]
    assert f["state"] == "none" and check(f, failing)["ok"] is False and [c["name"] for c in f["checks"] if c["ok"] is False] == [check(f, failing)["name"]]


def test_a_trend_that_did_not_persist_is_not_strong_even_when_the_numbers_say_so():
    choppy = [(24000 + 10 * (j % 2), 24012, 23990, 24000 + 10 * ((j + 1) % 2)) for j in range(40)]  # closes alternate either side of the EMA20
    f = facts_of(tfd(), tfd(), choppy)["facts"]["strong_trend"]
    assert f["state"] == "none" and check(f, "closes on the trend side")["ok"] is False


def test_with_a_neutral_1h_there_is_no_trend_to_be_strong():
    f = facts_of(tfd("sideways"), tfd(), rising())["facts"]["strong_trend"]
    assert f["state"] == "none" and "1H is sideways" in f["checks"][0]["text"]


# ====================================================================================================================
# Weakening trend
# ====================================================================================================================
def fading(n_before=28):
    """A strong climb, then six candles of the same climb at about the same pace, then six that stall: small bodies, no new high, and the top of the stall rejected."""
    spec = rising(n_before, step=8.0, body=7.0)
    last = spec[-1][3]
    spec += [(last + 8 * k, last + 8 * k + 9, last + 8 * k - 1, last + 8 * k + 7) for k in range(6)]  # the six before: bodies of 7, highs rising
    top = spec[-1][1]
    spec += [(top - 6, top - 3, top - 8, top - 5), (top - 5, top - 2, top - 7, top - 4), (top - 4, top - 1, top - 6, top - 3),  # the last six: bodies of 1, highs under the earlier six's
             (top - 16, top, top - 18, top - 15),  # the candle that made the latest extreme (it only matches the earlier high): an upper wick of most of its range, closing in its lower half
             (top - 2, top - 1, top - 5, top - 3), (top - 3, top - 2, top - 6, top - 4)]  # fmt: skip
    return spec


@pytest.mark.parametrize("direction", SIDES)
def test_a_stalling_trend_is_weakening_with_the_checks_that_say_so(direction):
    spec = fading()
    t15 = tfd(adx=34.0, adx_slope=-4.0, pattern="mixed")
    f = (facts_of if direction == "bullish" else mirrored)(tfd(), t15, spec)["facts"]["weakening_trend"]
    assert f["state"] == direction
    held = {c["name"] for c in f["checks"] if c["ok"] is True}
    assert {"ADX falling", "directional candles fading", "no new extreme", "latest push rejected"} <= held and "15M structure no longer" in " ".join(held)
    assert f["summary"] == "ADX falling, directional candles fading, no new extreme, latest push rejected, 15M structure no longer " + ("HH_HL" if direction == "bullish" else "LH_LL")


@pytest.mark.parametrize("direction", SIDES)
def test_a_healthy_trend_is_not_weakening(direction):
    f = (facts_of if direction == "bullish" else mirrored)(tfd(), tfd(adx_slope=2.0), rising())["facts"]["weakening_trend"]
    assert f["state"] == "none" and sum(1 for c in f["checks"] if c["ok"] is True) < 2


def test_two_checks_are_enough_and_one_is_not():
    spec = rising()
    one = facts_of(tfd(), tfd(adx_slope=-4.0), spec)["facts"]["weakening_trend"]
    assert one["state"] == "none" and sum(1 for c in one["checks"] if c["ok"] is True) == 1  # a falling ADX alone is not enough
    two = facts_of(tfd(), tfd(adx_slope=-4.0, pattern="mixed"), spec)["facts"]["weakening_trend"]
    assert two["state"] == "bullish"  # ... with the structure no longer confirming it, it is
    stricter = facts_of(tfd(), tfd(adx_slope=-4.0, pattern="mixed"), spec, p=replace(P, rg_weak_min_checks=3))["facts"]["weakening_trend"]
    assert stricter["state"] == "none"  # and the number of checks is the one in Params


def test_no_trend_means_nothing_to_weaken():
    f = facts_of(tfd("sideways"), tfd("sideways"), rising())["facts"]["weakening_trend"]
    assert f["state"] == "none" and "no trend to weaken" in f["checks"][0]["text"]


# ====================================================================================================================
# Compression, expansion, extension
# ====================================================================================================================
def squeeze():
    """Wide candles (50 pt ranges), then six of 24 pts, then six of 12 pts, all inside a 24 point box: the ranges narrow."""
    wide = [(24000 + 4 * j, 24030 + 4 * j, 23980 + 4 * j, 24010 + 4 * j) for j in range(22)]
    medium = [(24100 + (j % 3), 24114, 24090, 24104 + (j % 2)) for j in range(6)]
    tight = [(24100 + (j % 3), 24108, 24096, 24104 + (j % 2)) for j in range(6)]
    return wide + medium + tight


def test_compression_needs_three_of_its_four_checks():
    spec = squeeze()
    box = {"low": 24090.0, "high": 24114.0}
    yes = facts_of(tfd(), tfd(adx=14.0, adx_slope=-3.0, ratio=0.6, box=box), spec)["facts"]["compression"]
    assert yes["state"] == "yes" and sum(1 for c in yes["checks"] if c["ok"] is True) == 4
    assert check(yes, "ranges narrowing")["text"] == "the last 6 candles average a 12.0 pt range against 24.0 in the 6 before"
    three = facts_of(tfd(), tfd(adx=30.0, adx_slope=1.0, ratio=0.6, box=box), spec)["facts"]["compression"]
    assert three["state"] == "yes" and check(three, "directional movement")["ok"] is False  # three of four is enough
    two = facts_of(tfd(), tfd(adx=30.0, adx_slope=1.0, ratio=1.2, box=box), spec)["facts"]["compression"]
    assert two["state"] == "no"
    trending = facts_of(tfd(), tfd(), rising())["facts"]["compression"]
    assert trending["state"] == "no"


def box_series():
    """A long stay inside a 20 point box (24,090-24,110), 28 candles in all: nothing has left it."""
    return [(24100, 24110, 24090, 24100 + (3 if j % 2 else -3)) for j in range(26)] + [(24100, 24108, 24092, 24102)] * 2


STRONG = (24102, 24140, 24100, 24136)  # a 34 pt body, closing at 90% of its range, 26 pts above the box edge
FOLLOW = (24136, 24138, 24128, 24132)  # the next candle stays inside the strong one's range but well above the old edge


def breakout_series(follow=False):
    """The box and then the strong candle that left it (the last candle), and with `follow` one more candle that held above the old edge."""
    return box_series() + [STRONG] + ([FOLLOW] if follow else [])


@pytest.mark.parametrize("direction", SIDES)
def test_a_volatility_expansion_is_a_strong_candle_out_of_a_compression_box_with_atr_expanded(direction):
    run = facts_of if direction == "bullish" else mirrored
    word = "up" if direction == "bullish" else "down"
    t15 = tfd(ratio=1.6, box=None)
    first = run(tfd(), t15, breakout_series())["facts"]["volatility_expansion"]
    assert first["state"] == word and all(c["ok"] is True for c in first["checks"] if not c.get("info"))
    assert check(first, "follow-through")["ok"] is False and check(first, "follow-through").get("info")  # a report, not a requirement: the candle that left the box has no follow-through yet
    assert f"the {word} edge" in check(first, "out of a compression box")["text"] and "ended 1 candle(s) ago" in check(first, "out of a compression box")["text"]
    held = run(tfd(), t15, breakout_series(follow=True))["facts"]["volatility_expansion"]
    assert held["state"] == word and check(held, "follow-through")["ok"] is True and "2 of the last 2 closes" in check(held, "follow-through")["text"]  # the old edge is the reference
    if direction == "bullish":
        assert "close 24132.00 is beyond the up edge 24110.00 of the box that ended 2 candle(s) ago" in check(held, "out of a compression box")["text"]
    flat = run(tfd(), tfd(ratio=1.0), breakout_series())["facts"]["volatility_expansion"]
    assert flat["state"] == "none" and check(flat, "ATR expanded")["ok"] is False  # no ATR expansion: a big candle alone is not it
    inside = run(tfd(), t15, box_series())["facts"]["volatility_expansion"]
    assert inside["state"] == "none" and check(inside, "out of a compression box")["ok"] is False  # still inside the box
    weak_candle = box_series() + [(24102, 24125, 24100, 24118)]  # beyond the edge, but a small body (16 pts, under 1 ATR15) with no force
    weak = run(tfd(), t15, weak_candle)["facts"]["volatility_expansion"]
    assert weak["state"] == "none" and check(weak, "strong directional candle")["ok"] is False


@pytest.mark.parametrize("direction", SIDES)
def test_an_extended_market_is_far_from_both_emas_with_a_run_or_no_pullback(direction):
    spec = rising()
    t15 = tfd(e20atr=2.6, e50atr=3.6, run=7)
    f = (facts_of if direction == "bullish" else mirrored)(tfd(), t15, spec)["facts"]["extended"]
    assert f["state"] == direction and check(f, "ADX behaviour").get("info") and "consecutive" in check(f, "consecutive")["name"]
    near = (facts_of if direction == "bullish" else mirrored)(tfd(), tfd(e20atr=1.2, e50atr=3.6, run=7), spec)["facts"]["extended"]
    assert near["state"] == "none" and check(near, "from the EMA20")["ok"] is False  # not far enough from the EMA20
    no_run = (facts_of if direction == "bullish" else mirrored)(tfd(), tfd(e20atr=2.6, e50atr=3.6, run=2), spec)["facts"]["extended"]
    assert no_run["state"] == "bullish" if direction == "bullish" else no_run["state"] == "bearish"  # no run, but also no meaningful pullback in the last 12 closes: still extended
    last = rising(34)[-1][3]
    retraced = rising(34) + [(last - 8 * k + 3, last - 8 * k + 4, last - 8 * k - 1, last - 8 * k) for k in range(1, 7)]  # six closes down, 48 pts (1.9 ATR15) off the high
    broken = (facts_of if direction == "bullish" else mirrored)(tfd(), tfd(e20atr=2.6, e50atr=3.6, run=2), retraced)["facts"]["extended"]
    assert broken["state"] == "none" and check(broken, "no meaningful pullback")["ok"] is False and check(broken, "consecutive")["ok"] is False  # neither a run nor an unbroken climb


# ====================================================================================================================
# Range, trend pause, pullback, breakout
# ====================================================================================================================
def test_range_trend_pause_pullback_and_breakout_are_told_apart_in_a_stated_order():
    spec = rising()
    rng = facts_of(tfd("sideways"), tfd("sideways", adx=14.0), spec, "range_bound")["facts"]["pause"]
    assert rng["state"] == "range_bound" and "alignment range_bound" in rng["summary"]
    hot = facts_of(tfd("sideways"), tfd("sideways", adx=27.0), spec, "range_bound")["facts"]["pause"]
    assert hot["state"] != "range_bound"  # too much trend strength for a genuine range
    pause = facts_of(tfd(), tfd("sideways", adx=15.0), spec, "pausing")["facts"]["pause"]
    assert pause["state"] == "trend_pause"  # the 1H trends, the 15M is sideways: a temporary pause inside the trend
    shallow = rising(39) + [(24304, 24305, 24296, 24298)]  # 8 pts (0.3 ATR15) under the 12-candle high: under the minimum pullback, so not one
    sh = facts_of(tfd(), tfd(), shallow, "aligned_bullish")["facts"]["pause"]
    assert sh["state"] == "none" and "none of a breakout" in sh["summary"]
    deep = rising(34) + [(24270 - 6 * j, 24274 - 6 * j, 24262 - 6 * j, 24266 - 6 * j) for j in range(6)]  # 30 pts off the high: 0.5+ ATR15, still above the EMA50
    assert facts_of(tfd(ema50=24100.0), tfd(ema50=24100.0), deep, "aligned_bullish")["facts"]["pause"]["state"] == "pullback"
    brk = facts_of(tfd(), tfd(breakout="breakout"), spec, "aligned_bullish")["facts"]["pause"]
    assert brk["state"] == "breakout" and "Phase 2 flags 15M breakout" in brk["summary"]
    both = facts_of(tfd(), tfd("sideways", breakout="breakout"), spec, "pausing")["facts"]["pause"]
    assert both["state"] == "breakout"  # the order is the rule: a breakout outranks a pause


# ====================================================================================================================
# Missing, stale and untrustworthy inputs
# ====================================================================================================================
def test_when_the_data_cannot_be_trusted_every_fact_is_unknown_and_says_why():
    f = facts_of(tfd(), tfd(), rising(), data_ok=False, data_why="5M candles missing 10:05-10:15")
    assert f["available"] is False and "5M candles missing 10:05-10:15" in f["why"] and f["active"] == []
    assert {k: v["state"] for k, v in f["facts"].items()} == {k: "unknown" for k in rg.LABEL}
    assert all(c["ok"] is None for v in f["facts"].values() for c in v["checks"])  # and nothing is guessed as a check either
    cold = facts_of(tfd(warm=False), tfd(), rising())
    assert cold["available"] is False and "not enough completed 1H / 15M candles" in cold["why"]
    unk = rg.regime_facts(analysis(tfd("unknown"), tfd()), {"15m": candles(rising())}, P)
    assert unk["available"] is False
    no_atr = rg.regime_facts(analysis(tfd(), tfd(atr=None)), {"15m": candles(rising())}, P)
    assert no_atr["available"] is False and "ATR" in no_atr["why"]
    no_candles = rg.regime_facts(analysis(tfd(), tfd()), {"15m": []}, P)
    assert no_candles["available"] is False


def test_a_missing_input_makes_a_check_unknown_never_false_and_the_fact_unknown():
    t15 = tfd()
    t15["extension"]["adx_slope"] = None  # the ADX history is not available
    f = facts_of(tfd(), t15, rising())["facts"]
    assert check(f["strong_trend"], "ADX not falling")["ok"] is None and f["strong_trend"]["state"] == "unknown"  # one checks cannot be judged and none fails: unknown
    assert check(f["weakening_trend"], "ADX falling")["ok"] is None
    t15 = tfd(adx_slope=-4.0, pattern="insufficient")  # the structure cannot be classified yet: one more unknown check
    w = facts_of(tfd(), t15, rising())["facts"]["weakening_trend"]
    assert check(w, "structure no longer")["ok"] is None and w["state"] in ("none", "unknown")
    short = rg.regime_facts(analysis(tfd(), tfd()), {"15m": candles(rising(8))}, P)["facts"]  # too few candles for any window
    assert check(short["strong_trend"], "closes on the trend side")["ok"] is None and check(short["weakening_trend"], "candles fading")["ok"] is None
    assert short["strong_trend"]["state"] == "unknown"


def test_a_hole_in_the_candles_makes_the_checks_that_span_it_unknown_not_true_or_false():
    spec = candles(rising())
    gapped = spec[:30] + spec[31:]  # one 15M candle is missing inside the last twelve
    f = rg.regime_facts(analysis(tfd(), tfd()), {"15m": gapped}, P)["facts"]
    assert check(f["strong_trend"], "closes on the trend side")["ok"] is None and f["strong_trend"]["state"] == "unknown"
    assert check(f["compression"], "consolidation box")["ok"] is None
    assert check(f["extended"], "no meaningful pullback")["ok"] is None and check(f["weakening_trend"], "candles fading")["ok"] is None
    old_gap = spec[:5] + spec[6:]  # a hole far back, outside every window that is used
    ok = rg.regime_facts(analysis(tfd(), tfd()), {"15m": old_gap}, P)["facts"]
    assert ok["strong_trend"]["state"] == "bullish"  # does not matter


def test_the_facts_never_change_the_analysis_they_were_made_from_and_always_have_the_same_shape():
    a = analysis(tfd(), tfd(adx_slope=-4.0, pattern="mixed"))
    before = copy.deepcopy(a)
    f = rg.regime_facts(a, {"15m": candles(rising())}, P)
    assert a == before  # no classification, trend or alignment was touched
    assert set(f) == {"available", "why", "as_of", "facts", "active"} and set(f["facts"]) == set(rg.LABEL)
    assert all(set(v) == {"state", "checks", "summary"} and v["checks"] and all(set(c) >= {"name", "ok", "text"} for c in v["checks"]) for v in f["facts"].values())
    assert all(v["state"] in {"bullish", "bearish", "none", "yes", "no", "up", "down", "breakout", "range_bound", "trend_pause", "pullback", "unknown"} for v in f["facts"].values())
    assert f["as_of"] == candles(rising())[-1].end and all(label.split(": ")[0] in rg.LABEL.values() for label in f["active"])
    assert not any(isinstance(c["ok"], (int, float)) and not isinstance(c["ok"], bool) for v in f["facts"].values() for c in v["checks"])  # a check is true, false or unknown: never a score
    unmirrored = [k for k, v in f["facts"].items() if "score" in v or "confidence" in v]
    assert unmirrored == []  # no composite confidence number anywhere


def test_the_summary_says_why_a_fact_is_what_it_is_not_what_happened_to_hold():
    f = facts_of(tfd(), tfd(adx_slope=-3.0), rising())["facts"]
    assert f["strong_trend"]["summary"] == "not met: ADX not falling"  # a "none" fact names what is missing, not the checks that did hold
    assert f["weakening_trend"]["summary"] == "1 of 6 weakening checks hold (needs 2)"
    assert f["strong_trend"]["state"] == "none" and f["weakening_trend"]["state"] == "none"
    t15 = tfd()
    t15["extension"]["adx_slope"] = None
    g = facts_of(tfd(), t15, rising())["facts"]
    assert g["strong_trend"]["summary"] == "cannot be judged: ADX not falling"
    active = facts_of(tfd(), tfd(), rising())["facts"]["strong_trend"]
    assert active["state"] == "bullish" and active["summary"].startswith("1H and 15M both bullish, 15M ADX at least 25")  # an active fact says what holds


def test_a_fact_that_unjudgeable_checks_could_still_decide_is_unknown_and_one_they_cannot_is_not():
    t15 = tfd(pattern="mixed")
    t15["extension"]["adx_slope"] = None  # the ADX history is missing: two of the six weakening checks cannot be judged
    w = facts_of(tfd(), t15, rising())["facts"]["weakening_trend"]
    assert w["state"] == "unknown" and "some cannot be judged" in w["summary"]  # one holds and two are unknown: two would be enough, so it is not "none"
    stricter = facts_of(tfd(), t15, rising(), p=replace(P, rg_weak_min_checks=4))["facts"]["weakening_trend"]
    assert stricter["state"] == "none"  # one holds and two are unknown: three can never reach four


def test_a_stretch_from_the_ema20_counts_as_weakening_only_while_the_adx_is_not_rising():
    base = rising(34, step=4.0, body=3.0)
    top = base[-1][3]
    accelerating = base + [(top + 14 * k - 11, top + 14 * k + 2, top + 14 * k - 12, top + 14 * k) for k in range(1, 7)]  # the last six candles pull away from the EMA20
    falling = facts_of(tfd(), tfd(adx_slope=-3.0), accelerating)["facts"]["weakening_trend"]
    assert check(falling, "stretching")["ok"] is True and "changed by +1.72 ATR15 over 6 candles while ADX changed by -3.0" in check(falling, "stretching")["text"]
    rising_adx = facts_of(tfd(), tfd(adx_slope=3.0), accelerating)["facts"]["weakening_trend"]
    assert check(rising_adx, "stretching")["ok"] is False  # the same stretch with the ADX still rising is momentum, not weakness


def test_a_rejected_push_needs_the_wick_and_the_close_in_the_far_half_when_the_wick_share_is_lowered():
    spec = fading()
    top = spec[-3][1]
    spec[-3] = (top - 14, top, top - 20, top - 8)  # a 40% upper wick that still closes in the upper part of its range (60%)
    default = facts_of(tfd(), tfd(adx_slope=-4.0), spec)["facts"]["weakening_trend"]
    assert check(default, "latest push rejected")["ok"] is False  # 40% is under the 50% share
    lowered = facts_of(tfd(), tfd(adx_slope=-4.0), spec, p=replace(P, rg_wick_share=0.3))["facts"]["weakening_trend"]
    assert check(lowered, "latest push rejected")["ok"] is False and "closing at 60% of its range" in check(lowered, "latest push rejected")["text"]  # the wick is enough, the close is not
    spec[-3] = (top - 14, top, top - 20, top - 12)  # the same wick, closing in the lower part (40%): rejected
    assert check(facts_of(tfd(), tfd(adx_slope=-4.0), spec, p=replace(P, rg_wick_share=0.3))["facts"]["weakening_trend"], "latest push rejected")["ok"] is True


@pytest.mark.parametrize("direction", SIDES)
def test_an_expansion_needs_the_strong_candle_to_close_near_its_extreme(direction):
    run = facts_of if direction == "bullish" else mirrored
    long_wick = box_series() + [(24100, 24175, 24099, 24130)]  # a 30 pt body (over 1 ATR15), out of the box, but closing at 41% of its range with a long upper wick
    f = run(tfd(), tfd(ratio=1.6), long_wick)["facts"]["volatility_expansion"]
    assert f["state"] == "none" and check(f, "out of a compression box")["ok"] is True and check(f, "strong directional candle")["ok"] is False


@pytest.mark.parametrize("direction", SIDES)
def test_an_extended_market_must_be_far_from_the_ema50_as_well_as_the_ema20(direction):
    f = (facts_of if direction == "bullish" else mirrored)(tfd(), tfd(e20atr=2.6, e50atr=2.0, run=7), rising())["facts"]["extended"]
    assert f["state"] == "none" and check(f, "from the EMA20")["ok"] is True and check(f, "from the EMA50")["ok"] is False


def test_a_pause_that_cannot_be_judged_is_unknown_unless_something_earlier_in_the_order_already_holds():
    unknown = facts_of(tfd(), tfd(), rising(), alignment=None)["facts"]["pause"]
    assert unknown["state"] == "unknown" and unknown["summary"].startswith("cannot tell whether this is a range bound")  # the alignment is missing: nothing is assumed
    assert check(unknown, "genuine range")["ok"] is None
    breakout = facts_of(tfd(), tfd(breakout="breakout"), rising(), alignment=None)["facts"]["pause"]
    assert breakout["state"] == "breakout"  # a breakout is judged first, from its own flag


def test_every_threshold_the_facts_use_is_a_named_parameter():
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "app" / "regime.py").read_text(encoding="utf-8")
    used = set(re.findall(r"\bp\.([a-z_0-9]+)", src))
    assert used and all(name in P.__dataclass_fields__ for name in used), used - set(P.__dataclass_fields__)
    assert {n for n in used if n.startswith("rg_")} == {n for n in P.__dataclass_fields__ if n.startswith("rg_")}  # and every rg_ parameter is used: none is decoration


def test_the_regime_facts_are_mirrored_for_the_bearish_view_and_nothing_else_changes():
    from app import strategy as st

    r = rg.regime_facts(analysis(tfd(), tfd(e20atr=2.6, e50atr=3.6, run=7)), {"15m": candles(rising())}, P)
    m = st.mirror_regime(r)
    assert m["facts"]["strong_trend"]["state"] == "bearish" and m["facts"]["extended"]["state"] == "bearish"
    assert m["facts"]["compression"]["state"] == r["facts"]["compression"]["state"] and m["facts"]["pause"]["state"] == r["facts"]["pause"]["state"]
    assert {k: v["checks"] for k, v in m["facts"].items()} == {k: v["checks"] for k, v in r["facts"].items()} and m["available"] == r["available"]
    assert st.mirror_regime({}) == {} and st.mirror_regime(None) == {}
