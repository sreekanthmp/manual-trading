"""Market regime facts (Stage 3): a transparent, additive layer that says what kind of market this is, with the evidence.

Six facts, each a list of NAMED checks with the numbers they were made from, and a state derived from those checks by a stated rule. There is no score and no weighting: a
fact is "yes" because specific, listed conditions hold, and the conditions are on screen.

    strong_trend         bullish | bearish | none       1H and 15M agree, ADX high and not falling, EMAs stacked and sloping with it, structure with it, closes persist on its side
    weakening_trend      bullish | bearish | none       the established trend's direction, when at least rg_weak_min_checks of six checks say it is losing control (ADX falling, directional candles
                                                        fading, no new extreme, stretching away from the EMA20 without momentum, a rejected push, structure no longer confirming it)
    compression          yes | no                       at least rg_compress_min_checks of four: low ATR against its average, narrowing ranges, a consolidation box, low or falling ADX
    volatility_expansion up | down | none               ATR expanded, a strong directional candle, out of a compression box that ended a few candles ago (follow-through is reported)
    extended             bullish | bearish | none       far from the EMA20 and EMA50 in ATR units, and either a run of closes one way or no meaningful pullback
    pause                breakout | range_bound | trend_pause | pullback | none     told apart in that order: a breakout; a genuine range (1H and 15M both sideways, low ADX); a temporary pause
                                                        inside a continuing 1H trend (1H trending, 15M sideways); a pullback inside the trend

Rules of the layer:
  * It never changes an existing classification: the analysis's trend, alignment, condition and the decision's market regime are what they were. These facts sit beside them.
  * A check whose input is missing, whose candles have a hole, or whose indicator is not warm is UNKNOWN (ok = None), never false and never true; a fact that an unknown check
    could still decide is "unknown". When the data cannot be trusted at all (`data_ok` False), every fact is "unknown" and says why.
  * Nothing here is a signal. Only Trend Exhaustion Reversal reads these facts, and only as one of the requirements of its permission (see strategies/exhaustion.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import analysis as an
from .series import contiguity

if TYPE_CHECKING:  # only for the annotations: importing the strategy module here would make this one depend on the strategies that import it
    from .strategy import Params

DIRS = ("bullish", "bearish")
SGN = {"bullish": 1, "bearish": -1}
LABEL = {
    "strong_trend": "strong trend", "weakening_trend": "weakening trend", "compression": "compression", "volatility_expansion": "volatility expansion",
    "extended": "extended market", "pause": "range / pause / pullback / breakout",
}  # fmt: skip
INACTIVE = ("none", "no", "unknown")


def chk(name: str, ok: bool | None, text: str, info: bool = False) -> dict:
    """One check: ok True / False, or None when it cannot be judged (its input is missing). `info` checks are reported but do not decide."""
    return {"name": name, "ok": ok, "text": text, **({"info": True} if info else {})}


def _deciding(checks: list) -> list:
    return [c for c in checks if not c.get("info")]


def all_of(checks: list) -> str:
    """yes when every deciding check holds; unknown when none fails but one cannot be judged; otherwise no."""
    cs = _deciding(checks)
    if any(c["ok"] is False for c in cs):
        return "no"
    return "unknown" if any(c["ok"] is None for c in cs) else "yes"


def at_least(checks: list, need: int) -> str:
    """yes when `need` of the deciding checks hold; unknown when fewer hold but unjudgeable ones could still reach `need`; otherwise no."""
    cs = _deciding(checks)
    true, unknown = sum(1 for c in cs if c["ok"] is True), sum(1 for c in cs if c["ok"] is None)
    return "yes" if true >= need else "unknown" if true + unknown >= need else "no"


def fact(state: str, checks: list, why: str = "") -> dict:
    """A fact: its state, its checks, and a one-line summary that says why the state is what it is (what holds when it is active, what is not met or cannot be judged when it is not)."""
    deciding = _deciding(checks)
    names = {k: [c["name"] for c in deciding if c["ok"] is v] for k, v in (("held", True), ("failed", False), ("unknown", None))}
    if why:
        summary = why
    elif state == "unknown":
        summary = "cannot be judged: " + ", ".join(names["unknown"]) if names["unknown"] else "cannot be judged"
    elif state in ("none", "no"):
        summary = "not met: " + ", ".join(names["failed"]) if names["failed"] else "no check holds"
    else:
        summary = ", ".join(names["held"]) if names["held"] else "no check holds"
    return {"state": state, "checks": checks, "summary": summary}


def unknown_fact(why: str) -> dict:
    return {"state": "unknown", "checks": [chk("inputs", None, why)], "summary": why}


def _f(x, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


class Inputs:
    """What the facts are made from: the analysis of the 1H and 15M, and the completed 15M candles (with their holes known)."""

    def __init__(self, a: dict, series: dict):
        tfs = a["timeframes"]
        self.t1, self.t15 = tfs["1h"], tfs["15m"]
        self.c15 = list(series.get("15m", []))[-80:]
        self.atr15 = self.t15.get("atr14_intraday")
        self.ext = self.t15.get("extension") or {}
        self.contig = contiguity(self.c15, 900) if self.c15 else []
        closes = [c.close for c in self.c15]
        self.ema20 = an.ema(closes, 20) if closes else []
        self.alignment = ((a.get("condition") or {}).get("alignment") or {}).get("label")

    def window_ok(self, n: int, end: int = 0) -> bool:
        """The `n` candles ending `end` candles before the last exist and none follows a hole."""
        stop = len(self.c15) - end
        return stop >= n and all(self.contig[stop - n + 1 : stop]) if n > 1 else stop >= n

    def seg(self, n: int, end: int = 0) -> list:
        stop = len(self.c15) - end
        return self.c15[stop - n : stop]

    def dist20(self, end: int = 0) -> float | None:
        """The close `end` candles before the last, in ATR15 from that candle's EMA20 (+ above, - below)."""
        k = len(self.c15) - 1 - end
        if k < 0 or not self.atr15 or k >= len(self.ema20) or self.ema20[k] is None:
            return None
        return (self.c15[k].close - self.ema20[k]) / self.atr15


# -- strong trend --------------------------------------------------------------------------------------------------------------------------------
def strong_trend(i: Inputs, p: Params) -> dict:
    t1, t15 = i.t1, i.t15
    if t1["trend"] not in DIRS:
        return fact("none", [chk("1H trend directional", False, f"1H is {t1['trend']}: there is no trend to be strong")])
    d = t1["trend"]
    s = SGN[d]
    adx, slope = t15.get("adx"), i.ext.get("adx_slope")
    e20, e50, stack, sl = t15.get("ema20"), t15.get("ema50"), t15.get("ema_stack_bars"), t15.get("ema20_slope")
    pattern = (t15.get("structure") or {}).get("pattern")
    want = "HH_HL" if s > 0 else "LH_LL"
    persist = None
    if i.window_ok(p.rg_persist_bars) and all(x is not None for x in i.ema20[-p.rg_persist_bars :]):
        n_on = sum(1 for c, e in zip(i.seg(p.rg_persist_bars), i.ema20[-p.rg_persist_bars :]) if s * (c.close - e) > 0)
        persist = n_on / p.rg_persist_bars
    checks = [
        chk("1H and 15M both " + d, None if t15["trend"] == "unknown" else t15["trend"] == d, f"1H {t1['trend']}, 15M {t15['trend']}"),
        chk(f"15M ADX at least {p.rg_strong_adx15:g}", None if adx is None else adx >= p.rg_strong_adx15, f"15M ADX {_f(adx)}"),
        chk("ADX not falling", None if slope is None else slope > -p.rg_adx_fall, f"15M ADX changed by {_f(slope)} over {an.ADX_SLOPE_BARS} candles (falling = {p.rg_adx_fall:g} or more lost)"),
        chk(f"15M EMA20 on the {d} side of the EMA50 for {p.rg_stack_bars}+ candles", None if None in (e20, e50, stack) else (s * (e20 - e50) > 0 and s * stack >= p.rg_stack_bars),
            f"EMA20 {_f(e20, 2)}, EMA50 {_f(e50, 2)}, stacked {stack if stack is not None else 'n/a'} candles"),
        chk("15M EMA20 sloping with the trend", None if sl is None else s * sl > 0, f"EMA20 moved {_f(sl, 2)} pts over {an.EMA_SLOPE_BARS} candles"),
        chk("15M structure with the trend", None if pattern in (None, "insufficient") else pattern == want, f"15M structure {pattern}, needs {want}"),
        chk(f"{p.rg_persist_share:.0%} of the last {p.rg_persist_bars} 15M closes on the trend side of the EMA20", None if persist is None else persist >= p.rg_persist_share,
            "unknown (not enough contiguous candles)" if persist is None else f"{persist:.0%} of them"),
    ]  # fmt: skip
    res = all_of(checks)
    return fact(d if res == "yes" else "none" if res == "no" else "unknown", checks)


# -- weakening trend -----------------------------------------------------------------------------------------------------------------------------
def weakening_trend(i: Inputs, p: Params) -> dict:
    t1, t15 = i.t1, i.t15
    d = t1["trend"] if t1["trend"] in DIRS else t15["trend"] if t15["trend"] in DIRS else None
    if d is None:
        return fact("none", [chk("an established trend", False, f"1H is {t1['trend']} and 15M is {t15['trend']}: there is no trend to weaken")])
    s, w = SGN[d], p.rg_window
    slope = i.ext.get("adx_slope")
    pattern = (t15.get("structure") or {}).get("pattern")
    want = "HH_HL" if s > 0 else "LH_LL"
    body = fade = extreme = stretch = push = None
    if i.window_ok(2 * w):
        recent, earlier = i.seg(w), i.seg(w, w)
        r_body, e_body = sum(s * (c.close - c.open) for c in recent) / w, sum(s * (c.close - c.open) for c in earlier) / w
        fade = e_body > 0 and r_body <= p.rg_body_fade * e_body
        body = f"directional body averaged {_f(r_body, 2)} pts over the last {w} candles against {_f(e_body, 2)} over the {w} before"
        if s > 0:
            hi_r, hi_e = max(c.high for c in recent), max(c.high for c in earlier)
            extreme = (hi_r <= hi_e, f"the last {w} candles' high {_f(hi_r, 2)} against {_f(hi_e, 2)} in the {w} before")
            top = max(recent, key=lambda c: c.high)
        else:
            lo_r, lo_e = min(c.low for c in recent), min(c.low for c in earlier)
            extreme = (lo_r >= lo_e, f"the last {w} candles' low {_f(lo_r, 2)} against {_f(lo_e, 2)} in the {w} before")
            top = min(recent, key=lambda c: c.low)
        rng = top.high - top.low
        if rng > 0:
            wick = (top.high - max(top.open, top.close)) if s > 0 else (min(top.open, top.close) - top.low)
            loc = (top.close - top.low) / rng
            push = (wick / rng >= p.rg_wick_share and (loc <= 0.5 if s > 0 else loc >= 0.5), f"the candle that made the latest extreme gave back {wick / rng:.0%} of its range as a wick, closing at {loc:.0%} of its range")
    now_d, then_d = i.dist20(), i.dist20(w)
    if now_d is not None and then_d is not None and slope is not None:
        grew = s * now_d - s * then_d
        stretch = (grew >= p.rg_stretch_growth_atr15 and slope <= 0, f"distance from the EMA20 changed by {grew:+.2f} ATR15 over {w} candles while ADX changed by {_f(slope)}")
    checks = [
        chk("ADX falling", None if slope is None else slope <= -p.rg_adx_fall, f"15M ADX changed by {_f(slope)} over {an.ADX_SLOPE_BARS} candles (falling = {p.rg_adx_fall:g} or more lost)"),
        chk("directional candles fading", fade, body or "unknown (not enough contiguous candles)"),
        chk("no new extreme", None if extreme is None else extreme[0], extreme[1] if extreme else "unknown (not enough contiguous candles)"),
        chk("stretching from the EMA20 without momentum", None if stretch is None else stretch[0], stretch[1] if stretch else "unknown (EMA or ADX not available)"),
        chk("latest push rejected", None if push is None else push[0], push[1] if push else "unknown (not enough contiguous candles)"),
        chk(f"15M structure no longer {want}", None if pattern in (None, "insufficient") else pattern != want, f"15M structure {pattern}"),
    ]  # fmt: skip
    res = at_least(checks, p.rg_weak_min_checks)
    held = [c["name"] for c in checks if c["ok"] is True]
    why = ", ".join(held) if res == "yes" else f"{len(held)} of {len(checks)} weakening checks hold (needs {p.rg_weak_min_checks})" + (", some cannot be judged" if res == "unknown" else "")
    return fact(d if res == "yes" else "none" if res == "no" else "unknown", checks, why)


# -- compression ---------------------------------------------------------------------------------------------------------------------------------
def compression(i: Inputs, p: Params) -> dict:
    t15, w = i.t15, p.rg_window
    ratio, adx, slope = t15.get("atr_ratio_intraday"), t15.get("adx"), i.ext.get("adx_slope")
    narrow = None
    if i.window_ok(2 * w):
        rec, ear = sum(c.high - c.low for c in i.seg(w)) / w, sum(c.high - c.low for c in i.seg(w, w)) / w
        narrow = (ear > 0 and rec <= p.rg_compress_narrow * ear, f"the last {w} candles average a {rec:.1f} pt range against {ear:.1f} in the {w} before")
    box = t15.get("consolidation")
    checks = [
        chk("ATR low against its average", None if ratio is None else ratio <= p.rg_compress_atr_ratio, f"15M ATR is {_f(ratio, 2)}x its recent average (compression: {p.rg_compress_atr_ratio:g} or less)"),
        chk("ranges narrowing", None if narrow is None else narrow[0], narrow[1] if narrow else "unknown (not enough contiguous candles)"),
        chk("consolidation box", None if not i.window_ok(an.CONSOLIDATION_BARS) else box is not None,
            f"the last {an.CONSOLIDATION_BARS} 15M candles span {_f(box['high'] - box['low'], 1)} pts" if box else f"the last {an.CONSOLIDATION_BARS} candles are not inside {an.CONSOLIDATION_ATR:g}x ATR"),
        chk("directional movement low or falling", None if adx is None or slope is None else (adx < p.rg_compress_adx or slope <= -p.rg_adx_fall), f"15M ADX {_f(adx)}, changed by {_f(slope)}"),
    ]  # fmt: skip
    res = at_least(checks, p.rg_compress_min_checks)
    return fact(res, checks, f"{sum(1 for c in checks if c['ok'] is True)} of {len(checks)} compression checks hold (needs {p.rg_compress_min_checks})")


# -- volatility expansion ------------------------------------------------------------------------------------------------------------------------
def prior_box(i: Inputs, p: Params) -> dict | None:
    """The compression box (the analysis's own rule: the last 12 candles inside 3 ATR) that ended at most rg_expand_box_back candles ago and that the last close is now beyond."""
    margin = p.break_margin_atr15 * (i.atr15 or 0)
    last = i.c15[-1]
    for k in range(1, p.rg_expand_box_back + 1):
        if not i.window_ok(an.CONSOLIDATION_BARS, k):
            continue
        seg = i.seg(an.CONSOLIDATION_BARS, k)
        hi, lo = max(c.high for c in seg), min(c.low for c in seg)
        if hi - lo > an.CONSOLIDATION_ATR * i.atr15:
            continue
        if last.close > hi + margin:
            return {"side": "up", "edge": hi, "box": (lo, hi), "ended": k, "closes_beyond": sum(1 for c in i.c15[-2:] if c.close > hi + margin)}
        if last.close < lo - margin:
            return {"side": "down", "edge": lo, "box": (lo, hi), "ended": k, "closes_beyond": sum(1 for c in i.c15[-2:] if c.close < lo - margin)}
    return None


def volatility_expansion(i: Inputs, p: Params) -> dict:
    t15 = i.t15
    ratio = t15.get("atr_ratio_intraday")
    if not i.c15 or not i.atr15:
        return unknown_fact("no completed 15M candles or no 15M ATR")
    box = prior_box(i, p)
    side = box["side"] if box else None
    strong = None
    if side:
        s = 1 if side == "up" else -1
        strong = any(
            s * (c.close - c.open) >= p.rg_expand_body_atr15 * i.atr15 and c.high > c.low and ((c.close - c.low) if s > 0 else (c.high - c.close)) / (c.high - c.low) >= p.rg_expand_close_loc
            for c in i.c15[-2:]
        )
    checks = [
        chk("ATR expanded", None if ratio is None else ratio >= p.rg_expand_atr_ratio, f"15M ATR is {_f(ratio, 2)}x its recent average (expansion: {p.rg_expand_atr_ratio:g} or more)"),
        chk("out of a compression box", box is not None,
            f"close {i.c15[-1].close:.2f} is beyond the {box['side']} edge {box['edge']:.2f} of the box that ended {box['ended']} candle(s) ago" if box
            else f"the last close is not beyond a compression box that ended in the last {p.rg_expand_box_back} candles"),
        chk("strong directional candle", None if strong is None else strong, f"a {side} candle of at least {p.rg_expand_body_atr15:g} ATR15 closing in its top {1 - p.rg_expand_close_loc:.0%}" if side else "no breakout side to look for one"),
        chk("follow-through", None if box is None else box["closes_beyond"] >= 2, f"{box['closes_beyond']} of the last 2 closes beyond the edge" if box else "not applicable", info=True),
    ]  # fmt: skip
    res = all_of(checks)
    return fact(side if res == "yes" else "none" if res == "no" else "unknown", checks)


# -- extended ------------------------------------------------------------------------------------------------------------------------------------
def extended(i: Inputs, p: Params) -> dict:
    t15, ext = i.t15, i.ext
    d20, d50, run, adx, slope = ext.get("ema20_atr"), ext.get("ema50_atr"), ext.get("run"), t15.get("adx"), ext.get("adx_slope")
    if d20 is None or d50 is None:
        return unknown_fact("the 15M EMA distance is not available")
    d = "bullish" if d20 > 0 else "bearish"
    s = SGN[d]
    free = None
    n = p.rg_ext_pullback_bars
    if i.window_ok(n) and i.atr15:
        peak, worst = None, 0.0
        for c in (x.close for x in i.seg(n)):
            peak = c if peak is None else (max(peak, c) if s > 0 else min(peak, c))
            worst = max(worst, (peak - c) if s > 0 else (c - peak))
        free = (worst <= p.rg_ext_pullback_atr15 * i.atr15, f"the biggest retracement in the last {n} closes was {worst / i.atr15:.2f} ATR15 (limit {p.rg_ext_pullback_atr15:g})")
    checks = [
        chk(f"{p.rg_ext_atr20:g}+ ATR15 from the EMA20", s * d20 >= p.rg_ext_atr20, f"the 15M close is {abs(d20):.1f} ATR {'above' if s > 0 else 'below'} its EMA20"),
        chk(f"{p.rg_ext_atr50:g}+ ATR15 from the EMA50", s * d50 >= p.rg_ext_atr50, f"the 15M close is {abs(d50):.1f} ATR {'above' if s > 0 else 'below'} its EMA50"),
        chk(f"{p.rg_ext_run}+ consecutive {'higher' if s > 0 else 'lower'} closes", None if run is None else s * run >= p.rg_ext_run, f"{abs(run or 0)} consecutive closes {'up' if (run or 0) > 0 else 'down'}"),
        chk("no meaningful pullback", None if free is None else free[0], free[1] if free else "unknown (not enough contiguous candles)"),
        chk("ADX behaviour", None if adx is None else adx >= p.rg_strong_adx15, f"15M ADX {_f(adx)}, changed by {_f(slope)} over {an.ADX_SLOPE_BARS} candles", info=True),
    ]  # fmt: skip
    if any(c["ok"] is False for c in checks[:2]):
        return fact("none", checks)
    persistent = at_least(checks[2:4], 1)
    return fact(d if persistent == "yes" else "none" if persistent == "no" else "unknown", checks)


# -- range, pause, pullback, breakout ----------------------------------------------------------------------------------------------------------------
def pause(i: Inputs, p: Params, expansion: dict) -> dict:
    t1, t15 = i.t1, i.t15
    flags = [f"{lab} {t['breakout']}" for lab, t in (("1H", t1), ("15M", t15)) if t.get("breakout")]
    adx = t15.get("adx")
    d = t1["trend"] if t1["trend"] in DIRS else None
    pull = None
    if d and i.window_ok(p.rg_ext_pullback_bars) and i.atr15 and t15.get("ema50") is not None:
        s, seg = SGN[d], i.seg(p.rg_ext_pullback_bars)
        extreme = max(c.high for c in seg) if s > 0 else min(c.low for c in seg)
        back = s * (extreme - i.c15[-1].close)
        on_side = s * (i.c15[-1].close - t15["ema50"]) > 0
        pull = (back >= p.min_pullback_atr15 * i.atr15 and on_side,
                f"price is {back / i.atr15:.2f} ATR15 off its {p.rg_ext_pullback_bars}-candle {'high' if s > 0 else 'low'}, {'still on the trend side of' if on_side else 'through'} the 15M EMA50")  # fmt: skip
    checks = [
        chk("breakout", bool(flags) or expansion["state"] in ("up", "down"), ("Phase 2 flags " + ", ".join(flags)) if flags else f"volatility expansion is {expansion['state']}"),
        chk("genuine range (1H and 15M both sideways, ADX low)", None if adx is None or i.alignment is None else (i.alignment == "range_bound" and adx < p.range_max_adx),
            f"alignment {i.alignment}, 15M ADX {_f(adx)} (a range needs under {p.range_max_adx:g})"),
        chk("trend pause (1H trending, 15M sideways)", None if i.alignment is None else i.alignment == "pausing", f"alignment {i.alignment}"),
        chk("pullback inside the trend", None if pull is None else pull[0], pull[1] if pull else "no 1H trend, or not enough contiguous candles"),
    ]  # fmt: skip
    for name, c in zip(("breakout", "range_bound", "trend_pause", "pullback"), checks):  # the first that holds names the state: the order is the rule
        if c["ok"] is True:
            return fact(name, checks, c["text"])
        if c["ok"] is None:
            return fact("unknown", checks, f"cannot tell whether this is a {name.replace('_', ' ')}: {c['text']}")
    return fact("none", checks, "none of a breakout, a range, a trend pause or a pullback")


# -- all of them ---------------------------------------------------------------------------------------------------------------------------------
def regime_facts(a: dict, series: dict, p: Params, data_ok: bool = True, data_why: str = "") -> dict:
    """The regime facts, from the analysis `a` and the completed candles. Always returns the same shape; `available` False (every fact "unknown", `why` says why) when the data
    cannot be trusted or a timeframe has not enough completed candles."""
    t1, t15 = a["timeframes"]["1h"], a["timeframes"]["15m"]
    why = None
    if not data_ok:
        why = "the market data is not reliable right now" + (f" ({data_why})" if data_why else "")
    elif "unknown" in (t1["trend"], t15["trend"]) or not t1.get("warm") or not t15.get("warm"):
        why = "not enough completed 1H / 15M candles to classify the market yet"
    elif not t15.get("atr14_intraday") or not series.get("15m"):
        why = "the 15M ATR or the 15M candles are not available"
    if why:
        return {"available": False, "why": why, "as_of": None, "facts": {k: unknown_fact(why) for k in LABEL}, "active": []}
    i = Inputs(a, series)
    facts = {"strong_trend": strong_trend(i, p), "weakening_trend": weakening_trend(i, p), "compression": compression(i, p), "volatility_expansion": volatility_expansion(i, p),
             "extended": extended(i, p)}  # fmt: skip
    facts["pause"] = pause(i, p, facts["volatility_expansion"])
    active = [f"{LABEL[k]}: {f['state']}" for k, f in facts.items() if f["state"] not in INACTIVE]
    return {"available": True, "why": None, "as_of": i.c15[-1].end if i.c15 else None, "facts": facts, "active": active}
