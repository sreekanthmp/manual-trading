"""Stage 5.4: the dashboard's quality, measured in a REAL browser (headless Chrome or Edge on this machine, driven over its DevTools websocket; skipped, with the reason, when there is
none), on the real application and the real WebSocket push, over a mocked market (no broker, no database). The states are the real engines of `dashboard_states.py`: BUY CE, BUY PE,
WAIT, NO TRADE, market closed, feed down, a stale spot, an option window with no quotes or no suitable contract, and the counter-trend reversal's decisions.

At 1366x768 and 1920x1080, in every state, on every tab:
  * no horizontal scrollbar; the Overview needs no vertical one; no console error and no error line of the page's own
  * no text smaller than 10 px (as the eye gets it, zoom included); whatever a one-line cell cuts off with an ellipsis can be read in full on hover
  * each strategy is shown once: the Overview's live row and its folded list make the ten, and the Analysis tab has ten sections
also: the four tabs by click, by keyboard and by address; WAIT, NO TRADE and BUY look different (a word and a colour); a lost link marks every tab as stale; and a cold start says what it
is waiting for and shows no number that was not measured."""

import re
import time
from pathlib import Path

import pytest
from dashboard_states import STATES
from test_dashboard_layout_browser import SIZES, browser, goto, keep, metrics, open_overview, page, served  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
TABS = ["overview", "options", "analysis", "journal", "trades"]
MIN_FONT_PX = 10

MEASURE = r"""(() => {
  const d = document.documentElement, zoom = parseFloat(getComputedStyle(document.body).zoom) || 1;
  const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  let smallest = 99, smallestText = "";
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const n = w.currentNode, t = n.textContent.trim(), p = n.parentElement;
    if (!t || !p || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(p.tagName) || !vis(p)) continue;
    const fs = parseFloat(getComputedStyle(p).fontSize) * zoom;
    if (fs < smallest) { smallest = fs; smallestText = t.slice(0, 40); }
  }
  const noTip = [];
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue;
    const cs = getComputedStyle(e);
    if ((cs.textOverflow === 'ellipsis' || cs.overflow === 'hidden') && e.scrollWidth > e.clientWidth + 1 && (e.innerText || '').trim() && !(e.title || (e.closest('[title]') || {}).title))
      noTip.push((e.id || (typeof e.className === 'string' ? e.className : '') || e.tagName) + ': ' + e.innerText.trim().slice(0, 40));
  }
  const names = sel => [...document.querySelectorAll(sel)].map(e => e.textContent.trim());
  return {vw: innerWidth, vh: innerHeight, scrollH: d.scrollHeight, scrollW: d.scrollWidth, bottom: Math.round(document.getElementById('tab-overview').getBoundingClientRect().bottom), tab: TAB, conn: CONN, jserr: document.getElementById('jserr').textContent,
          smallest: +smallest.toFixed(1), smallestText, noTip, overviewStrategies: names('#strat td:first-child'), analysisStrategies: names('#analysis-body details.sd > summary .nm'),
          action: ((document.querySelector('#decision .dtxt') || {}).textContent || '').trim()};
})()"""


def server_state(served):
    """What the server's event loop is doing (a diagnostic for a timeout): the WebSocket clients it holds, and where each of its tasks is waiting."""
    import asyncio
    import traceback

    try:
        route = next(r for r in served["app"].routes if getattr(r, "path", None) == "/ws")
        cells = dict(zip(route.endpoint.__code__.co_freevars, (c.cell_contents for c in route.endpoint.__closure__)))
        wake_cells = dict(zip(served["md"].on_change.__code__.co_freevars, (c.cell_contents for c in served["md"].on_change.__closure__)))
        loop = wake_cells["loop"]

        async def dump():
            return [" <- ".join(f.name for f in reversed(traceback.extract_stack(frame)[-3:])) for task in asyncio.all_tasks() for frame in task.get_stack(limit=1)]

        stacks = asyncio.run_coroutine_threadsafe(dump(), loop).result(5)
        return {"clients": len(cells["clients"]), "tasks": stacks}
    except Exception as e:  # a diagnostic must not hide the failure it explains
        return f"(could not be read: {e!r})"


def swap(served, page, name, build, tmp_path):
    """Serve another engine to the open page, and wait until the page has drawn a snapshot of it."""
    md = build(str(tmp_path))
    md.instance = {"pid": 0, "port": 0, "marker": name}  # (it travels in health.diagnostics: how the page tells which engine it has been given)
    md.on_change = served["md"].on_change
    md.snapshot()  # the first snapshot of an engine computes its whole analysis: done here, so the server's event loop is not held up for seconds (the page would go stale after 5 s of silence)
    served["app"].state.md = md
    served["md"].on_change()  # wake the ticker: the push does not wait for its second
    ready = f"LAST && LAST.health && LAST.health.diagnostics && LAST.health.diagnostics.instance && LAST.health.diagnostics.instance.marker === {name!r} && CONN === 'live'"
    try:
        page.until(ready, timeout=12)
    except AssertionError:  # the push did not come (the machine is busy, or a socket is held up): a fresh page connection is always sent the current snapshot at once
        goto(page, served["url"] + "#" + page.js("TAB"))
        try:
            page.until(f"typeof CONN !== 'undefined' && {ready}", timeout=30)
        except AssertionError:
            began = time.time()
            md.snapshot()
            server = server_state(served)
            seen = page.js("({conn: CONN, tries: TRIES, ws: WS && WS.readyState, marker: LAST && LAST.health && LAST.health.diagnostics && LAST.health.diagnostics.instance, hash: location.hash})")
            raise AssertionError(f"{name}: no snapshot of it reached the page, even on a fresh connection; the page: {seen}; one snapshot of it takes {time.time() - began:.2f} s; "
                                 f"the server is serving it: {served['app'].state.md is md}; the server: {server}")
    page.pause(0.5)
    return md


def show(page, tab):
    page.js(f"document.getElementById('tb-{tab}').click()")
    page.until(f"TAB === '{tab}'")
    page.pause(0.35)


@pytest.fixture
def restore(served):
    yield
    served["app"].state.md = served["md"]
    served["md"].on_change()


@pytest.mark.parametrize("width,height", SIZES)
def test_every_state_on_every_tab_is_within_the_screen_readable_and_free_of_errors(page, served, restore, tmp_path, width, height):
    open_overview(page, served, width, height)
    for state, (build, action) in STATES.items():
        swap(served, page, state, build, tmp_path)
        for tab in TABS:
            show(page, tab)
            m = page.js(MEASURE)
            where = f"{state} / {tab} at {width}x{height}"
            assert m["tab"] == tab and m["conn"] == "live" and m["jserr"] == "", f"{where}: {m['jserr']}"
            assert m["scrollW"] <= m["vw"], f"{where}: {m['scrollW']} px wide on a {m['vw']} px screen"
            if tab == "overview":
                assert m["scrollH"] <= m["vh"] and m["bottom"] <= m["vh"], f"{where}: needs {max(m['scrollH'], m['bottom'])} px of {m['vh']}"
                assert m["action"] == action, f"{where}: shows {m['action']!r}, not {action!r}"
                every = m["overviewStrategies"]  # the live rows and the folded list together
                assert len(every) == len(set(every)) and len(every) in (0, 10), f"{where}: the monitor lists {len(every)} strategies, with duplicates: {every}"
            if tab == "analysis" and state not in ("no_trade", "closed", "disconnected"):
                assert len(m["analysisStrategies"]) == len(set(m["analysisStrategies"])) == 10, f"{where}: {m['analysisStrategies']}"
            assert m["smallest"] >= MIN_FONT_PX, f"{where}: text of {m['smallest']} px: {m['smallestText']!r}"
            assert m["noTip"] == [], f"{where}: cut off with no way to read it: {m['noTip']}"
        if width == 1366:
            show(page, "overview")
            keep(page, tmp_path, f"state-{state}-{width}x{height}")
    assert page.errors == [], page.errors


def test_the_five_tabs_open_by_click_by_keyboard_and_by_address(page, served):  # (four until Stage 8 added Trades)
    open_overview(page, served, 1366, 768)
    visible = "[...document.querySelectorAll('section.tab')].filter(s => !s.hidden).map(s => s.id)"
    selected = "[...document.querySelectorAll('#tabs button')].filter(b => b.getAttribute('aria-selected') === 'true').map(b => b.dataset.tab)"
    assert page.js("[...document.querySelectorAll('#tabs button')].map(b => b.dataset.tab)") == TABS and page.js("document.getElementById('tabs').getAttribute('role')") == "tablist"
    for tab in TABS:  # by click: exactly that panel is shown, exactly that tab is selected, and the address follows
        page.js(f"document.getElementById('tb-{tab}').click()")
        page.until(f"TAB === '{tab}'")
        assert page.js(visible) == [f"tab-{tab}"] and page.js(selected) == [tab] and page.js("location.hash") == f"#{tab}"
    page.js("document.getElementById('tb-overview').click()")
    page.js("document.getElementById('tb-overview').focus()")
    for key, want in (("ArrowRight", "options"), ("ArrowRight", "analysis"), ("ArrowRight", "journal"), ("ArrowRight", "trades"), ("ArrowRight", "overview"), ("ArrowLeft", "trades"), ("ArrowLeft", "journal")):  # by keyboard, wrapping round
        page.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=key, windowsVirtualKeyCode=39 if key == "ArrowRight" else 37)
        page.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=key, windowsVirtualKeyCode=39 if key == "ArrowRight" else 37)
        page.until(f"TAB === '{want}'", timeout=5)
        assert page.js(visible) == [f"tab-{want}"] and page.js("document.activeElement.id") == f"tb-{want}"
    for tab in TABS:  # by address: the page opens on that tab
        goto(page, served["url"] + f"#{tab}")
        page.until(f"typeof CONN !== 'undefined' && TAB === '{tab}' && CONN === 'live'")
        assert page.js(visible) == [f"tab-{tab}"] and page.js(selected) == [tab]
    goto(page, served["url"] + "#nonsense")  # an address that names no tab opens the Overview
    page.until("typeof CONN !== 'undefined' && CONN === 'live'")
    assert page.js("TAB") == "overview" and page.js(visible) == ["tab-overview"]
    assert page.js("document.getElementById('tabs').getBoundingClientRect().top") <= 60 and page.errors == []  # (the tabs stay below the header, in reach)


def test_wait_no_trade_and_buy_are_told_apart_by_the_word_and_by_the_colour(page, served, restore, tmp_path):
    open_overview(page, served, 1366, 768)
    seen = {}
    for state in ("buy_ce", "buy_pe", "wait", "no_trade", "ter_denied"):
        swap(served, page, state, STATES[state][0], tmp_path)
        seen[state] = page.js("""(() => { const d = document.getElementById('decision'), t = d.querySelector('.dtxt'), cs = getComputedStyle(d);
            return {word: t.textContent.trim(), ink: getComputedStyle(t).color, border: cs.borderTopColor, fill: cs.backgroundColor, cls: d.className}; })()""")
    assert [seen[k]["word"] for k in ("buy_ce", "buy_pe", "wait", "no_trade")] == ["BUY CE", "BUY PE", "WAIT", "NO TRADE"]
    for a in ("wait", "no_trade"):  # WAIT is amber and NO TRADE is neutral grey: in the word, the lettering, the frame and the fill
        for b in ("buy_ce", "buy_pe"):
            assert (seen[a]["ink"], seen[a]["border"]) != (seen[b]["ink"], seen[b]["border"]), (a, b)
    assert seen["wait"]["ink"] != seen["no_trade"]["ink"] and seen["wait"]["border"] != seen["no_trade"]["border"] and seen["wait"]["fill"] != seen["no_trade"]["fill"]
    assert seen["buy_ce"]["ink"] != seen["buy_pe"]["ink"] and seen["buy_ce"]["fill"] != seen["buy_pe"]["fill"]
    assert "wait" in seen["wait"]["cls"].split() and "no-trade" in seen["no_trade"]["cls"].split() and seen["ter_denied"]["word"] == STATES["ter_denied"][1]
    assert page.errors == []


@pytest.mark.parametrize("tab", TABS)
def test_a_lost_link_marks_every_tab_as_stale_and_not_actionable(page, served, restore, tab):
    served["md"].set_state("connected")
    page.size(1366, 768)
    goto(page, served["url"] + f"#{tab}")
    page.until(f"typeof CONN !== 'undefined' && CONN === 'live' && TAB === '{tab}'")
    page.pause(1.2)
    page.js("WS.close()")
    page.until("CONN === 'stale'", timeout=3)
    page.pause(0.3)
    s = page.js("""({banner: document.getElementById('stale').innerText, bannerShown: document.getElementById('stale').getBoundingClientRect().height > 0,
        dim: getComputedStyle(document.querySelector('section.tab:not([hidden])')).opacity, chips: [...document.querySelectorAll('.chip:not([hidden])')].map(c => c.innerText).slice(0, 4),
        text: document.getElementById('main').innerText.toLowerCase(), jserr: document.getElementById('jserr').textContent})""")
    assert "STALE since" in s["banner"] and s["bannerShown"] and float(s["dim"]) < 1, tab  # the banner is on every tab, and the page is dimmed
    assert s["chips"] == ["MARKET UNKNOWN", "FEED UNKNOWN", "DATA UNKNOWN", "SYSTEM UNKNOWN"] and s["jserr"] == ""
    assert "actionable now" not in s["text"] and "entry eligible" not in s["text"]  # nothing says it can be acted on
    if tab == "options":
        assert "not current, not actionable" in s["text"]  # (the contract card says it is the last one)
    page.until("CONN === 'live'", timeout=15)  # and the next snapshot restores it
    page.pause(0.8)
    assert page.js("document.getElementById('stale').innerText") == "" and float(page.js("getComputedStyle(document.querySelector('section.tab:not([hidden])')).opacity")) == 1.0
    assert page.errors == []


def test_a_cold_start_says_what_it_is_waiting_for_and_shows_no_number_that_was_not_measured(page, served, restore, tmp_path):
    open_overview(page, served, 1366, 768)
    swap(served, page, "no_trade", STATES["no_trade"][0], tmp_path)  # history still loading, no NIFTY tick, no option quote
    ov = page.js("document.body.innerText")  # (the header, with the price, is outside <main>)
    assert "NO TRADE" in ov and "Historical data is still loading" in ov  # the decision says why there is none
    assert page.js("document.getElementById('spot').textContent") == "–" and "no NIFTY tick yet" in ov and "unknown" in ov.lower()  # no price, no zero price
    assert "Regime facts: not received yet. Every fact is unknown" in ov and page.js("document.querySelectorAll('#rg .rgc').length") == 6
    assert re.search(r"28 no data", ov) and "0 / 28 fresh" in ov  # a real zero (none is fresh) next to the 28 that have no data
    show(page, "options")
    cells = page.js("[...document.querySelectorAll('#opt-table tr')].slice(1).map(r => [...r.children].slice(4, 10).map(c => c.textContent.trim()))")
    assert len(cells) == 28 and all(set(row) <= {"–", "– × –"} for row in cells), cells[:2]  # LTP, bid, ask, spread, volume, open interest: unknown, never 0
    assert "NO DATA" in page.js("document.getElementById('opt-table').innerText").upper()
    show(page, "analysis")
    assert "Analysis not available yet" in page.js("document.getElementById('analysis-body').innerText")  # a tab with nothing to show says why
    show(page, "journal")
    page.pause(0.8)
    assert page.js("document.getElementById('journal-body').innerText.trim().length") > 20
    assert page.errors == []


def hex_to_linear(c):
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(h):
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * hex_to_linear(r) + 0.7152 * hex_to_linear(g) + 0.0722 * hex_to_linear(b)


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_text_colours_are_readable_on_every_background_they_are_used_on():
    css = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    v = dict(re.findall(r"--([a-z0-9]+):\s*(#[0-9a-fA-F]{6})", re.search(r":root\s*\{[^}]*\}", css).group(0)))
    grounds = ("bg", "panel", "panel2", "neubg")
    for ink in ("fg", "mut", "ok", "bad", "warn", "acc"):  # every text colour on the plain grounds: WCAG AA (4.5 : 1)
        for ground in grounds:
            assert contrast(v[ink], v[ground]) >= 4.5, (ink, ground, round(contrast(v[ink], v[ground]), 2))
    for ink, ground in (("ok", "okbg"), ("bad", "badbg"), ("warn", "warnbg"), ("mut", "neubg"), ("fg", "okbg"), ("fg", "badbg"), ("fg", "warnbg")):  # and on the tinted ones
        assert contrast(v[ink], v[ground]) >= 4.5, (ink, ground, round(contrast(v[ink], v[ground]), 2))
    assert contrast("#c3cad4", v["neubg"]) >= 4.5  # the NO TRADE lettering
