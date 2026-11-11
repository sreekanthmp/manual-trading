"""Indicators, market structure and market condition. Pure functions over completed candles; nothing
here produces trading signals, and nothing here invents a candle.

Conventions (standard textbook definitions):
- EMA: seeded with the simple average of the first n values, then alpha = 2 / (n + 1). pandas'
  `ewm(span=n, adjust=False)` seeds with the first value instead; the two converge quickly (the seed
  weight after m more bars is (1 - alpha)^m), so they agree once the series is warm.
- RSI, ATR, ADX / +DI / -DI: Wilder smoothing, seeded with the plain average / sum of the first n values.
- ATR comes in two forms. `atr14` is the textbook one (true range against the previous close, so the overnight
  gap is counted as volatility for hours). `atr14_intraday` measures the first candle of each session by its own
  range: it is the scale of one candle, and it is what the thresholds below and Phase 3 use. See the README
  ("ATR and the overnight gap") for the measurements behind that choice.
- Indicators run over the candles that exist. A missing interval is reported (see `timeframe`) but the
  smoothing still treats the next candle as following the previous one, as charting software does.
  Structure (swings, breakouts, consolidation) is never inferred across a hole.

All thresholds below are initial assumptions, not values optimised against any data."""

from datetime import datetime

from .candles import IST, OPEN_S, day_start
from .series import contiguity, missing_intervals

ADX_TREND = 20.0  # ADX at or above this: a trend is present
VOL_HIGH, VOL_LOW = 1.3, 0.7  # 15M ATR against its own recent average
CONSOLIDATION_BARS, CONSOLIDATION_ATR = 12, 3.0  # last 12 bars inside a range of 3 x ATR
WARMUP_BARS = 150  # 3 x the slowest period (EMA 50): the seed's weight is then about 2 %
WINDOW = 500  # most recent completed candles used per timeframe
SWING_K = 2  # a pivot needs 2 lower highs (higher lows) on each side
ATR_AVG_BARS = 100  # "recent average" of ATR
ADX_SLOPE_BARS = 5  # how far back the ADX is compared with, for "rising" or "falling"
EMA_SLOPE_BARS = 5  # how far back an EMA is compared with, for its slope
LEVEL_CLUSTER_ATR = 0.5  # swings closer than this many ATRs form one level
MAX_LEVELS_PER_SIDE = 3
RSI_STRETCHED = (70.0, 30.0)  # context only; RSI does not vote in the trend classification
SECS = {"1h": 3600, "15m": 900, "5m": 300, "1m": 60}
LABEL = {"1h": "1H", "15m": "15M", "5m": "5M", "1m": "1M"}
OPPOSITE = {"bullish": "bearish", "bearish": "bullish"}


# -- indicators -----------------------------------------------------------------------------------------
def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    e = sum(values[:n]) / n
    out[n - 1] = e
    k = 2 / (n + 1)
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def ema_slope(series: list, bars: int = EMA_SLOPE_BARS) -> float | None:
    """How far an EMA has moved over the last `bars` candles, in points (+ rising). None while the series is too short or has no value that far back."""
    if len(series) <= bars or series[-1] is None or series[-1 - bars] is None:
        return None
    return series[-1] - series[-1 - bars]


def ema_stack_bars(fast: list, slow: list) -> int | None:
    """How many candles in a row, up to the last one, the fast EMA has been above the slow one (positive) or below it (negative). 0 when they are equal on the
    last candle, None when either has no value yet. The EMAs themselves are not a signal: this says how long the same ordering has held."""
    if not fast or not slow or fast[-1] is None or slow[-1] is None:
        return None
    run, sign = 0, 0
    for a, b in zip(reversed(fast), reversed(slow)):
        if a is None or b is None or a == b:
            break
        s = 1 if a > b else -1
        if run and s != sign:
            break
        sign, run = s, run + 1
    return sign * run


def _rsi_value(gain: float, loss: float) -> float:
    if loss == 0:
        return 50.0 if gain == 0 else 100.0  # no movement at all is neutral, not "overbought"
    return 100 - 100 / (1 + gain / loss)


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(x, 0) for x in ch[:n]) / n
    loss = sum(max(-x, 0) for x in ch[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            gain = (gain * (n - 1) + max(ch[i - 1], 0)) / n
            loss = (loss * (n - 1) + max(-ch[i - 1], 0)) / n
        out[i] = _rsi_value(gain, loss)
    return out


def _true_range(c, prev_close: float) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr(candles, n: int = 14, intraday: bool = False) -> list[float | None]:
    """Wilder ATR. With `intraday=True` the first candle of a session is measured by its own range instead of
    against the previous session's close, so the overnight gap does not count as the size of a candle. A hole
    inside a session is not an overnight gap: the move across it still counts."""
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= n:
        return out

    def true_range(i: int) -> float:
        if intraday and day_start(candles[i].start) != day_start(candles[i - 1].start):
            return candles[i].high - candles[i].low
        return _true_range(candles[i], candles[i - 1].close)

    tr = [true_range(i) for i in range(1, len(candles))]
    a = sum(tr[:n]) / n
    out[n] = a
    for i in range(n + 1, len(candles)):
        a = (a * (n - 1) + tr[i - 1]) / n
        out[i] = a
    return out


def adx(candles, n: int = 14) -> tuple[list, list, list]:
    """(ADX, +DI, -DI) series aligned with candles."""
    size = len(candles)
    adx_s: list[float | None] = [None] * size
    pdi_s: list[float | None] = [None] * size
    mdi_s: list[float | None] = [None] * size
    if size <= 2 * n - 1:
        return adx_s, pdi_s, mdi_s
    tr, pdm, mdm = [], [], []
    for i in range(1, size):
        up, down = candles[i].high - candles[i - 1].high, candles[i - 1].low - candles[i].low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(_true_range(candles[i], candles[i - 1].close))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dx: list[float] = []
    for i in range(n, size):
        if i > n:
            s_tr += tr[i - 1] - s_tr / n
            s_p += pdm[i - 1] - s_p / n
            s_m += mdm[i - 1] - s_m / n
        pdi = 100 * s_p / s_tr if s_tr else 0.0
        mdi = 100 * s_m / s_tr if s_tr else 0.0
        pdi_s[i], mdi_s[i] = pdi, mdi
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
        if len(dx) == n:
            adx_s[i] = sum(dx) / n
        elif len(dx) > n:
            adx_s[i] = (adx_s[i - 1] * (n - 1) + dx[-1]) / n
    return adx_s, pdi_s, mdi_s


# -- session reference values ----------------------------------------------------------------------------
def session_reference(series_by_tf: dict, forming: dict | None) -> dict:
    """Open, VWAP and average price of the latest session. VWAP is only calculated when every candle
    carries a traded volume and the total is positive. NIFTY spot is an index with no volume, so there
    the plain average of typical prices is reported separately, under its own name."""
    forming = forming or {}
    days = [day_start(cs[-1].start) for cs in (series_by_tf.get(t) for t in ("1m", "5m", "15m")) if cs]
    f1 = forming.get("1m")
    if f1 is not None:
        days.append(day_start(f1.start))
    out = {"session_date": None, "session_open": None, "vwap": None, "vwap_note": None, "session_avg_price": None}
    if not days:
        return out
    day = max(days)
    out["session_date"] = datetime.fromtimestamp(day, IST).date().isoformat()
    for t in ("1m", "5m", "15m"):  # the 09:15 candle of any timeframe carries the session open
        first = next((c for c in series_by_tf.get(t) or () if c.start == day + OPEN_S), None)
        if first is not None:
            out["session_open"] = first.open
            break
    else:
        if f1 is not None and f1.start == day + OPEN_S:
            out["session_open"] = f1.open  # first minute still forming
    cs = [c for c in (series_by_tf.get("1m") or series_by_tf.get("5m") or ()) if day_start(c.start) == day]
    if not cs:
        return out
    typical = [(c.high + c.low + c.close) / 3 for c in cs]
    out["session_avg_price"] = sum(typical) / len(typical)
    if all(c.volume is not None for c in cs) and sum(c.volume for c in cs) > 0:
        total = sum(c.volume for c in cs)
        out["vwap"] = sum(t * c.volume for t, c in zip(typical, cs)) / total
    else:
        out["vwap_note"] = "No traded volume for this instrument (NIFTY is an index): VWAP is not available"
    return out


# -- structure ----------------------------------------------------------------------------------------------
def swings(candles, k: int = SWING_K, contiguous: list[bool] | None = None) -> list[tuple[int, str, float]]:
    """Confirmed pivots (index, "H"/"L", price): a high above the k bars before it and not below the k
    bars after it (lows mirrored). A pivot needs k later candles, so it is only reported once those have
    completed, and never when a hole lies inside its 2k+1 candles."""
    out = []
    for i in range(k, len(candles) - k):
        if contiguous is not None and not all(contiguous[i - k + 1 : i + k + 1]):
            continue
        h, low = candles[i].high, candles[i].low
        left, right = candles[i - k : i], candles[i + 1 : i + 1 + k]
        if all(h > c.high for c in left) and all(h >= c.high for c in right):
            out.append((i, "H", h))
        if all(low < c.low for c in left) and all(low <= c.low for c in right):
            out.append((i, "L", low))
    return out


def structure(candles, k: int = SWING_K, contiguous: list[bool] | None = None) -> dict:
    sw = swings(candles, k, contiguous)
    highs, lows = [s for s in sw if s[1] == "H"], [s for s in sw if s[1] == "L"]
    recent = sorted(highs[-3:] + lows[-3:])
    out = {
        "pattern": "insufficient",
        "swing_high": highs[-1][2] if highs else None,
        "swing_low": lows[-1][2] if lows else None,
        "recent_swings": [{"start": candles[i].start, "kind": kind, "price": p} for i, kind, p in recent],
    }
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1][2] > highs[-2][2], lows[-1][2] > lows[-2][2]
        lh, ll = highs[-1][2] < highs[-2][2], lows[-1][2] < lows[-2][2]
        out["pattern"] = "HH_HL" if hh and hl else "LH_LL" if lh and ll else "mixed"
    return out


def breakout(candles, st: dict, atr_value: float | None, contiguous: list[bool] | None = None) -> str | None:
    """A fresh close beyond the latest confirmed swing (the 3 closes before it were on the other side)."""
    if len(candles) < 5 or (contiguous is not None and not all(contiguous[-3:])):
        return None  # too short, or a hole in the last 4 candles: "fresh" cannot be judged
    buf = 0.1 * (atr_value or 0)
    last, prev = candles[-1].close, [c.close for c in candles[-4:-1]]
    if st["swing_high"] is not None and last > st["swing_high"] + buf and min(prev) <= st["swing_high"] + buf:
        return "breakout"
    if st["swing_low"] is not None and last < st["swing_low"] - buf and max(prev) >= st["swing_low"] - buf:
        return "breakdown"
    return None


def consolidation(candles, atr_value: float | None, contiguous: list[bool] | None = None) -> dict | None:
    if not atr_value or len(candles) < CONSOLIDATION_BARS:
        return None
    if contiguous is not None and not all(contiguous[-(CONSOLIDATION_BARS - 1) :]):
        return None  # a hole could hide part of the range
    recent = candles[-CONSOLIDATION_BARS:]
    hi, lo = max(c.high for c in recent), min(c.low for c in recent)
    return {"high": hi, "low": lo, "bars": CONSOLIDATION_BARS} if hi - lo <= CONSOLIDATION_ATR * atr_value else None


def _cluster(points: list[tuple[float, bool]], tol: float) -> list[dict]:
    groups: list[list[tuple[float, bool]]] = []
    for p in sorted(points):
        if groups and p[0] - sum(x[0] for x in groups[-1]) / len(groups[-1]) <= tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [{"price": sum(x[0] for x in g) / len(g), "touches": len(g), "from_1h": any(x[1] for x in g)} for g in groups]


def key_levels(series_1h, series_15m, atr_15m: float | None) -> list[dict]:
    """Swing prices from the 1H and 15M charts merged into levels. A level needs 2+ touches or to come
    from the 1H chart; this keeps the list short. Which side of the price they fall on is decided later."""
    h1, m15 = series_1h[-120:], series_15m[-200:]
    pts = [(s[2], True) for s in swings(h1, SWING_K, contiguity(h1, 3600))]
    pts += [(s[2], False) for s in swings(m15, SWING_K, contiguity(m15, 900))]
    if not pts:
        return []
    tol = LEVEL_CLUSTER_ATR * atr_15m if atr_15m else 0.001 * pts[0][0]
    return [lv for lv in _cluster(pts, tol) if lv["touches"] >= 2 or lv["from_1h"]]


def split_levels(clusters: list[dict], price: float | None) -> dict:
    """The nearest few levels above (resistance) and below (support) the current price."""
    if price is None:
        return {"support": [], "resistance": []}

    def fmt(lv):
        return {"price": round(lv["price"], 2), "touches": lv["touches"]}

    above = sorted((lv for lv in clusters if lv["price"] > price), key=lambda lv: lv["price"])
    below = sorted((lv for lv in clusters if lv["price"] < price), key=lambda lv: -lv["price"])
    return {"support": [fmt(lv) for lv in below[:MAX_LEVELS_PER_SIDE]],
            "resistance": [fmt(lv) for lv in above[:MAX_LEVELS_PER_SIDE]]}  # fmt: skip


# -- how far and how fast price has run ---------------------------------------------------------------------------
def extension(candles, ema20: float | None, ema50: float | None, atr_value: float | None, adx_s: list, trend: str, brk: str | None) -> dict:
    """Facts about how stretched the move is. Nothing here is a signal, a veto or a verdict: no number is compared with a threshold in this function.
    (Since Stage 3 the regime layer, app/regime.py, reads these facts and compares them with its own named, initial-assumption thresholds; none has been calibrated.)
    ema20_atr / ema50_atr: the last close's distance from the EMA, in intraday ATRs (+ above, - below).
    run: consecutive closes in the same direction at the end (+ up, - down). adx_slope: the ADX now minus ADX_SLOPE_BARS candles ago.
    break_against: a break of the latest swing in the opposite direction to the trend, if there is one."""
    closes = [c.close for c in candles]
    run = 0
    for i in range(len(closes) - 1, 0, -1):
        step = closes[i] - closes[i - 1]
        if step == 0 or (run != 0 and (step > 0) != (run > 0)):
            break
        run += 1 if step > 0 else -1
    last = closes[-1] if closes else None
    now_adx = adx_s[-1] if adx_s else None
    then_adx = adx_s[-1 - ADX_SLOPE_BARS] if len(adx_s) > ADX_SLOPE_BARS else None
    against = {"bullish": "breakdown", "bearish": "breakout"}.get(trend)
    return {
        "ema20_atr": None if None in (last, ema20, atr_value) or not atr_value else (last - ema20) / atr_value,
        "ema50_atr": None if None in (last, ema50, atr_value) or not atr_value else (last - ema50) / atr_value,
        "run": run, "adx_slope": None if None in (now_adx, then_adx) else now_adx - then_adx,
        "break_against": brk if against is not None and brk == against else None,
    }  # fmt: skip


def extension_text(label: str, e: dict | None) -> str:
    """The extension facts of one timeframe as one sentence (empty if there is nothing to say). No verdict is attached to any number."""
    if not e:
        return ""
    parts = []
    if e.get("ema20_atr") is not None and e.get("ema50_atr") is not None:
        side = lambda x: "above" if x >= 0 else "below"  # noqa: E731
        parts.append(f"the last {label} close is {abs(e['ema20_atr']):.1f} ATR {side(e['ema20_atr'])} its EMA20 and {abs(e['ema50_atr']):.1f} ATR {side(e['ema50_atr'])} its EMA50")
    if abs(e.get("run") or 0) >= 2:
        parts.append(f"{abs(e['run'])} consecutive {'higher' if e['run'] > 0 else 'lower'} closes")
    if e.get("adx_slope") is not None:
        parts.append(f"{label} ADX {'rose' if e['adx_slope'] > 0 else 'fell' if e['adx_slope'] < 0 else 'did not change'} by {abs(e['adx_slope']):.1f} over {ADX_SLOPE_BARS} candles")
    if e.get("break_against"):
        parts.append(f"a {label} {e['break_against']} of its latest swing level has just happened, against the trend")
    return "; ".join(parts) + "." if parts else ""


# -- one timeframe --------------------------------------------------------------------------------------------
def _last(s):
    return s[-1] if s else None


def timeframe(candles, secs: int, now: float) -> dict:
    """Indicators, structure and data completeness from completed candles (the most recent WINDOW)."""
    cs = candles[-WINDOW:]
    info = {"bars": len(cs), "warm": len(cs) >= WARMUP_BARS, "trend": "unknown", "last_completed": None,
            "missing": missing_intervals(cs, secs, now)}  # fmt: skip
    if not cs:
        return info
    contig = contiguity(cs, secs)
    closes = [c.close for c in cs]
    last = cs[-1]
    adx_s, pdi_s, mdi_s = adx(cs)
    atr_s, atr_i = atr(cs), atr(cs, intraday=True)
    a, ai = _last(atr_s), _last(atr_i)
    recent_atr = [x for x in atr_s[-ATR_AVG_BARS:] if x is not None]
    recent_i = [x for x in atr_i[-ATR_AVG_BARS:] if x is not None]
    st = structure(cs, SWING_K, contig)
    e20, e50 = ema(closes, 20), ema(closes, 50)
    info.update(
        last_completed={"start": last.start, "end": last.end, "open": last.open, "high": last.high, "low": last.low,
                        "close": last.close, "volume": last.volume, "source": last.source},
        ema20=_last(e20), ema50=_last(e50), ema20_slope=ema_slope(e20), ema50_slope=ema_slope(e50), ema_stack_bars=ema_stack_bars(e20, e50), rsi14=_last(rsi(closes)),
        adx=_last(adx_s), plus_di=_last(pdi_s), minus_di=_last(mdi_s), atr14=a,
        atr_ratio=a / (sum(recent_atr) / len(recent_atr)) if a and recent_atr else None,
        atr14_intraday=ai, atr_ratio_intraday=ai / (sum(recent_i) / len(recent_i)) if ai and recent_i else None,
        structure=st, breakout=breakout(cs, st, ai, contig), consolidation=consolidation(cs, ai, contig),
    )  # fmt: skip
    if info["warm"] and None not in (info["ema50"], info["adx"]):
        score = 0
        if info["ema20"] > info["ema50"] and last.close > info["ema20"]:
            score += 1
        elif info["ema20"] < info["ema50"] and last.close < info["ema20"]:
            score -= 1
        score += (info["plus_di"] > info["minus_di"]) - (info["plus_di"] < info["minus_di"])
        score += {"HH_HL": 1, "LH_LL": -1}.get(st["pattern"], 0)
        info["score"] = score
        info["trend"] = "bullish" if score >= 2 and info["adx"] >= ADX_TREND else \
            "bearish" if score <= -2 and info["adx"] >= ADX_TREND else "sideways"  # fmt: skip
    info["extension"] = extension(cs, info["ema20"], info["ema50"], ai, adx_s, info["trend"], info["breakout"])
    return info


# -- previous day ----------------------------------------------------------------------------------------------
def previous_day(daily, today, session_dates) -> dict | None:
    """PDH / PDL / PDC from the last daily candle before today. `session_dates` are the days that have
    intraday data: if the latest of them has no daily candle, the levels are flagged stale."""
    prev = [c for c in daily if datetime.fromtimestamp(c.start, IST).date() < today]
    if not prev:
        return None
    c = prev[-1]
    d = datetime.fromtimestamp(c.start, IST).date()
    out = {"date": d.isoformat(), "pdh": c.high, "pdl": c.low, "pdc": c.close, "stale": False, "note": None}
    latest = max((s for s in session_dates if s < today), default=None)
    if latest is not None and d < latest:
        out["stale"], out["note"] = True, f"no daily candle for the latest session ({latest.isoformat()})"
    return out


# -- timeframes together --------------------------------------------------------------------------------------
def alignment(tfs: dict) -> dict:
    """How the 1H and 15M agree. Conflicts are explained, never forced into agreement."""
    h, m = tfs["1h"]["trend"], tfs["15m"]["trend"]
    if "unknown" in (h, m):
        return {"label": "unknown", "text": "Not enough completed 1H / 15M candles to compare the two timeframes."}
    if h == m and h != "sideways":
        return {"label": f"aligned_{h}", "text": f"1H and 15M are both {h}: higher-timeframe {h} alignment (trend continuation)."}
    if h == m:
        return {"label": "range_bound", "text": "1H and 15M are both sideways: possible range-bound conditions."}
    if h != "sideways" and m != "sideways":
        s15 = tfs["15m"]
        against = ("LH_LL", "breakdown") if h == "bullish" else ("HH_HL", "breakout")
        broke = s15["structure"]["pattern"] == against[0] or s15.get("breakout") == against[1]
        tail = ("15M structure has broken against the 1H trend, so reversal risk is higher." if broke else
                "15M structure has not broken against the 1H trend, which fits a pullback better than a reversal.")  # fmt: skip
        return {"label": "conflicting", "text": f"1H is {h} but 15M is {m}: conflicting timeframes. {tail}"}
    if h != "sideways":
        return {"label": "pausing", "text": f"1H is {h} while 15M is sideways: the higher-timeframe trend is pausing."}
    return {"label": "developing", "text": f"15M is {m} but 1H is sideways: a move is developing that the 1H does not confirm yet."}


def _evidence(t: dict) -> str:
    parts = []
    if t.get("ema50") is not None:
        parts.append(f"EMA20 {t['ema20']:.2f} {'>' if t['ema20'] > t['ema50'] else '<'} EMA50 {t['ema50']:.2f}")
    if t.get("plus_di") is not None:
        parts.append(f"+DI {t['plus_di']:.1f} {'>' if t['plus_di'] > t['minus_di'] else '<'} -DI {t['minus_di']:.1f}")
    if t.get("adx") is not None:
        parts.append(f"ADX {t['adx']:.1f}")
    parts.append(f"structure {t['structure']['pattern']}")
    return ", ".join(parts)


def condition(tfs: dict) -> dict:
    """Trend (from 1H, with 15M not against it) and volatility (from 15M ATR) are separate dimensions:
    a market can be bullish and highly volatile at once. Each comes with the evidence behind it."""
    al = alignment(tfs)
    h, m, f, o = (tfs[k]["trend"] for k in ("1h", "15m", "5m", "1m"))
    reasons = [al["text"]]
    if "unknown" in (h, m):
        trend = "unknown"
    else:
        trend = h if h in OPPOSITE and m != OPPOSITE[h] else "sideways"
        reasons.append(f"Trend {trend}. 1H: {_evidence(tfs['1h'])}. 15M: {_evidence(tfs['15m'])}.")
        if f != "unknown" and o != "unknown":
            rel = "agrees with" if f == trend else "is neutral to" if "sideways" in (f, trend) else "is against"
            reasons.append(f"Short term: 5M {f} {rel} the {trend} trend; 1M {o}.")
    hi, lo = RSI_STRETCHED
    for k in ("1h", "15m"):
        r = tfs[k].get("rsi14")
        if r is not None:
            zone = f"above {hi:.0f} (stretched)" if r >= hi else f"below {lo:.0f} (stretched)" if r <= lo else "in the normal range"
            reasons.append(f"Momentum: {LABEL[k]} RSI {r:.1f} is {zone}.")
    ext = {k: tfs[k].get("extension") for k in ("1h", "15m")}
    for k in ("15m", "1h"):  # facts about how far the move has run: the regime layer (app/regime.py) reads them, with thresholds of its own; no number is a signal by itself
        text = extension_text(LABEL[k], ext[k])
        if text:
            reasons.append("Extension (a fact: the regime layer reads it, and no number is a signal by itself): " + text)
    t15 = tfs["15m"]
    # Volatility asks whether candles are unusually large now, so it uses the ATR without the overnight gap
    ratio = t15.get("atr_ratio_intraday", t15.get("atr_ratio"))
    scale = t15.get("atr14_intraday", t15.get("atr14"))
    vol = "unknown" if ratio is None else "high" if ratio >= VOL_HIGH else "low" if ratio <= VOL_LOW else "normal"
    if ratio is not None:
        pct = 100 * scale / t15["last_completed"]["close"]
        gap = f", overnight gap excluded; {t15['atr14']:.2f} if counted" if t15.get("atr14") and abs(t15["atr14"] - scale) > 0.05 * scale else ""
        reasons.append(f"Volatility {vol}: 15M ATR {scale:.2f} ({pct:.2f}% of price{gap}) is {ratio:.2f}x its recent average.")
    for k in ("1h", "15m"):
        if tfs[k].get("breakout"):
            reasons.append(f"{LABEL[k]} {tfs[k]['breakout']} of its latest swing level.")
    if t15.get("consolidation"):
        c = t15["consolidation"]
        reasons.append(f"15M is consolidating between {c['low']:.2f} and {c['high']:.2f}.")
    if trend == "unknown":
        reasons.append("Trend is unknown until both 1H and 15M have enough completed candles.")
    return {"trend": trend, "volatility": vol, "alignment": al, "reasons": reasons, "extension": ext}


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def _gap_text(m: dict, secs: int) -> str:
    """The latest session's gaps as '15:15-15:27 (13), 15:29 (1)' (at most the last three)."""
    runs = [r for r in m["runs"] if datetime.fromtimestamp(r["start"], IST).date().isoformat() == m["latest_session"]]
    parts = []
    for r in runs[-3:]:
        last = r["start"] + (r["count"] - 1) * secs
        parts.append(f"{_hhmm(r['start'])}" + (f"-{_hhmm(last)}" if r["count"] > 1 else "") + f" ({r['count']})")
    return ", ".join(parts)


def data_warnings(tfs: dict, prev_day: dict | None) -> list[str]:
    """Plain sentences about data that is incomplete. Missing intervals only warn for the latest session
    (earlier sessions are counted in the timeframe's `missing`, not repeated here)."""
    out = []
    for k in ("1h", "15m", "5m", "1m"):
        t = tfs[k]
        m = t["missing"]
        if m["latest_session_missing"]:
            out.append(
                f"{LABEL[k]}: {m['latest_session_missing']} of {m['latest_session_expected']} intervals missing in the latest "
                f"session ({m['latest_session']}): gaps at {_gap_text(m, SECS[k])} IST. "
                "Nothing is filled in; indicators use the candles that exist."
            )
        if t["bars"] and not t["warm"]:
            out.append(f"{LABEL[k]}: only {t['bars']} completed candles, {WARMUP_BARS} are needed before its trend is classified.")
    if prev_day and prev_day["stale"]:
        out.append(f"Previous-day levels are for {prev_day['date']}: {prev_day['note']}.")
    return out


def analyze(series_by_tf: dict, daily, forming: dict | None, now: float) -> dict:
    """Everything the dashboard shows except the live price, forming candles and the support/resistance
    split, which change on every tick and are added by the caller. series_by_tf: completed candles for
    '1h', '15m', '5m', '1m'; forming: the candle currently forming per timeframe (only its open is used)."""
    tfs = {tf: timeframe(series_by_tf.get(tf, []), secs, now) for tf, secs in SECS.items()}
    today = datetime.fromtimestamp(now, IST).date()
    sessions = {datetime.fromtimestamp(c.start, IST).date() for c in series_by_tf.get("15m", [])}
    ref = session_reference(series_by_tf, forming)
    ref["previous_day"] = previous_day(daily, today, sessions)
    cond = condition(tfs)
    warnings = data_warnings(tfs, ref["previous_day"])
    if any(tfs[k]["missing"]["latest_session_missing"] for k in ("1h", "15m")):
        cond["reasons"].append("Data note: the direction rests on incomplete 1H / 15M data (see warnings).")
    return {
        "available": True,
        "timeframes": tfs,
        "reference": ref,
        "level_clusters": key_levels(series_by_tf.get("1h", []), series_by_tf.get("15m", []), tfs["15m"].get("atr14_intraday")),
        "condition": cond,
        "warnings": warnings,
    }
