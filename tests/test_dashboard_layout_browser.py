"""Stage 5.1: the dashboard laid out by a REAL browser, automatically: headless Chrome (or Edge) is started on the machine, driven over its DevTools websocket, and pointed at the real
application served on a free local port (the real FastAPI app, the real WebSocket push, on a mocked market: no broker, no database). For each size it measures the page the way a trader
sees it, and collects the browser's own console errors.

  * the Overview fits ONE screen (no vertical scrollbar, no horizontal one) at 1366x768 and 1920x1080, normally and in a degraded state (the feed down, three alerts)
  * a lost link in a real browser makes the page stale at once and a fresh snapshot restores it
  * every tab opens without a console error or an uncaught exception
  * narrower desktop widths do not overflow horizontally

It needs a browser and the `websockets` package. If either is missing the whole module is SKIPPED, and the skip says which (see the reason in the pytest report): the content tests in
test_dashboard_stage5.py do not need either. Set PRISM_SHOTS=<directory> to also keep a screenshot of each measured state."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("websockets", reason="the `websockets` package is not installed: the layout in a real browser was not measured")
from dashboard_harness import base_engine  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from app.main import create_app  # noqa: E402

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]  # fmt: skip


def find_browser():
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        if shutil.which(name):
            return shutil.which(name)
    return next((p for p in CANDIDATES if os.path.exists(p)), None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chrome, Chromium or Edge was found on this machine: the layout in a real browser was not measured")


class Page:
    """One tab of the browser, over the DevTools protocol."""

    def __init__(self, ws_url):
        self._ctx = connect(ws_url, max_size=None)  # (entered by hand: the library wants a context manager, and a fixture owns the page's life)
        self.ws, self.n, self.errors = self._ctx.__enter__(), 0, []
        for method in ("Page.enable", "Runtime.enable", "Log.enable"):
            self.call(method)

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            self.note(msg)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def note(self, msg):
        m, p = msg.get("method"), msg.get("params", {})
        if m == "Runtime.exceptionThrown":
            self.errors.append("exception: " + (p["exceptionDetails"].get("exception", {}).get("description") or p["exceptionDetails"]["text"]))
        elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif m == "Log.entryAdded" and p["entry"]["level"] == "error" and "favicon" not in p["entry"].get("url", "") and "favicon" not in p["entry"]["text"]:
            self.errors.append("log: " + p["entry"]["text"])

    def js(self, expression):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def size(self, width, height):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    def until(self, expression, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expression):
                return True
            time.sleep(0.15)
        raise AssertionError(f"timed out waiting for: {expression}")

    def pause(self, seconds):  # (events keep arriving while the test waits)
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.note(json.loads(self.ws.recv(timeout=0.2)))
            except TimeoutError:
                pass

    def shot(self, path):
        import base64

        Path(path).write_bytes(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))


@pytest.fixture(scope="module")
def served():
    import uvicorn

    md, clock = base_engine()
    app = create_app(md, feed=None)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    end = time.time() + 15
    while not server.started and time.time() < end:
        time.sleep(0.05)
    assert server.started, "the application did not start"
    yield {"url": f"http://127.0.0.1:{port}/", "md": md, "clock": clock, "app": app}
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    profile, port = tmp_path_factory.mktemp("profile"), free_port()
    proc = subprocess.Popen([BROWSER, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
                             "--disable-extensions", "--hide-scrollbars=false", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        end, targets = time.time() + 20, None
        while time.time() < end:
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                if any(t["type"] == "page" for t in targets):
                    break
            except OSError:
                time.sleep(0.2)
        assert targets and any(t["type"] == "page" for t in targets), "the browser did not start"
        yield next(t for t in targets if t["type"] == "page")["webSocketDebuggerUrl"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def page(browser):
    p = Page(browser)
    yield p
    p._ctx.__exit__(None, None, None)


def goto(page, url):
    """A real load of the page, whatever the tab was showing before: going through a blank page means a change of #hash alone is never mistaken for a navigation."""
    page.call("Page.navigate", url="about:blank")
    page.pause(0.3)
    page.call("Page.navigate", url=url)


def open_overview(page, served, width, height):
    page.size(width, height)
    goto(page, served["url"] + "#overview")
    page.until("typeof CONN !== 'undefined' && CONN === 'live' && document.getElementById('decision').innerText.length > 10 && !document.getElementById('decision').innerText.includes('LOADING')")
    page.pause(1.2)  # a few pushes later, with every panel drawn


def metrics(page):
    return page.js("""(() => { const d = document.documentElement; return {vw: innerWidth, vh: innerHeight, scrollH: d.scrollHeight, scrollW: d.scrollWidth,
        zoom: getComputedStyle(document.body).zoom, tab: TAB, alerts: document.querySelectorAll('#alerts details').length}; })()""")


def keep(page, tmp_path, name):
    out = os.environ.get("PRISM_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.shot(Path(out) / f"{name}.png")


SIZES = [(1366, 768), (1920, 1080)]


@pytest.mark.parametrize("width,height", SIZES)
def test_the_overview_fits_one_screen_at_both_required_sizes(page, served, tmp_path, width, height):
    served["md"].set_state("connected")
    open_overview(page, served, width, height)
    m = metrics(page)
    assert (m["vw"], m["vh"]) == (width, height) and m["tab"] == "overview"
    assert m["scrollH"] <= m["vh"], f"the Overview needs {m['scrollH']} px of a {m['vh']} px screen"  # no vertical scrollbar
    assert m["scrollW"] <= m["vw"], f"the Overview is {m['scrollW']} px wide on a {m['vw']} px screen"  # no horizontal one
    assert page.js("document.getElementById('decision').innerText.includes('BUY CE') && document.getElementById('rg').querySelectorAll('.rgc').length === 6")
    keep(page, tmp_path, f"overview-{width}x{height}")
    assert page.errors == []


@pytest.mark.parametrize("width,height", SIZES)
def test_a_degraded_overview_with_alerts_still_fits_one_screen(page, served, tmp_path, width, height):
    md, clock = served["md"], served["clock"]
    md.set_state("disconnected", "the broker closed the connection (test)")
    md.down_since = clock.t - 6001  # the feed has been down for a long time: no live connection, no decision, and alerts
    try:
        open_overview(page, served, width, height)
        page.until("document.getElementById('alerts').innerText.includes('alerts')")
        m = metrics(page)
        assert m["alerts"] == 1 and "alerts" in page.js("document.querySelector('#alerts summary').innerText")  # one summary line, however many alerts
        assert page.js("document.querySelector('#alerts summary').getBoundingClientRect().height") < 40  # one line
        assert m["scrollH"] <= m["vh"] and m["scrollW"] <= m["vw"], f"degraded: {m['scrollH']} px of {m['vh']}, {m['scrollW']} wide of {m['vw']}"
        keep(page, tmp_path, f"overview-degraded-{width}x{height}")
        assert page.js("document.getElementById('decision').innerText.includes('NO TRADE')")
        page.js("document.querySelector('#alerts summary').click()")  # the full list opens, over the page, and the layout does not move
        before = page.js("document.getElementById('decision').getBoundingClientRect().top")
        page.until("document.querySelector('#alerts .alertlist').getBoundingClientRect().height > 40")
        assert page.js("document.getElementById('decision').getBoundingClientRect().top") == before
        assert page.js("document.querySelectorAll('#alerts .alertlist .alert').length") >= 2
        assert page.errors == []
    finally:
        md.set_state("connected")
        md.down_since = None


def test_losing_the_link_in_a_real_browser_makes_the_page_stale_and_a_fresh_snapshot_restores_it(page, served, tmp_path):
    served["md"].set_state("connected")
    open_overview(page, served, 1366, 768)
    assert "eligible (actionable now)" in page.js("document.getElementById('decision').innerText")  # live: the page says it can be acted on
    page.js("WS.close()")  # the link drops
    page.until("CONN === 'stale'", timeout=3)
    stale = page.js("""({head: document.getElementById('decision').innerText, banner: document.getElementById('stale').innerText, chips: [...document.querySelectorAll('.chip:not([hidden])')].map(c => c.innerText),
        everything: document.getElementById('main').innerText, dim: getComputedStyle(document.querySelector('#tab-overview')).opacity})""")
    assert "STALE since" in stale["head"] and "NOT ACTIONABLE" in stale["head"] and "STALE since" in stale["banner"]
    assert stale["chips"][:4] == ["MARKET UNKNOWN", "FEED UNKNOWN", "DATA UNKNOWN", "SYSTEM UNKNOWN"] and float(stale["dim"]) < 1  # unknown, and dimmed
    assert "actionable now" not in stale["everything"].lower() and "entry eligible" not in stale["everything"].lower()
    keep(page, tmp_path, "overview-stale-1366x768")
    page.until("CONN === 'live'", timeout=15)  # the page reconnects by itself (every 2 s) and the first snapshot makes it live
    page.pause(1.0)
    back = page.js("({head: document.getElementById('decision').innerText, banner: document.getElementById('stale').innerText, chips: [...document.querySelectorAll('.chip:not([hidden])')].map(c => c.innerText), dim: getComputedStyle(document.querySelector('#tab-overview')).opacity})")
    assert "BUY CE" in back["head"] and "STALE" not in back["head"] and back["banner"] == "" and back["chips"][0] == "MARKET OPEN" and float(back["dim"]) == 1.0
    assert page.errors == []


def test_every_tab_opens_without_a_console_error(page, served):
    open_overview(page, served, 1366, 768)
    for tab in ("options", "analysis", "journal", "overview"):
        page.js(f"document.getElementById('tb-{tab}').click()")
        page.until(f"TAB === '{tab}'")
        page.pause(1.3)
        assert page.js("document.getElementById('jserr').textContent") == "", tab
    page.js("document.getElementById('tb-analysis').click()")
    page.pause(0.8)
    assert page.js("[...document.querySelectorAll('#analysis-body details.sd')].length") == 10  # all ten strategies, in a real browser
    assert page.errors == [], page.errors


@pytest.mark.parametrize("width,height", [(1280, 720), (1100, 700), (900, 700)])
def test_narrower_desktop_widths_do_not_overflow_sideways_on_any_tab(page, served, width, height):
    open_overview(page, served, width, height)
    for tab in ("overview", "options", "analysis", "journal"):
        page.js(f"document.getElementById('tb-{tab}').click()")
        page.pause(0.8)
        m = metrics(page)
        assert m["scrollW"] <= m["vw"], f"{tab} at {width}: {m['scrollW']} px wide"
    assert page.errors == []


def open_options(page, served, width, height):
    page.size(width, height)
    goto(page, served["url"] + "#options")
    page.until("typeof CONN !== 'undefined' && CONN === 'live' && TAB === 'options' && document.getElementById('opt-sel').innerText.length > 20")
    page.pause(1.2)


@pytest.mark.parametrize("width,height", SIZES)
def test_the_options_tab_shows_the_contract_within_the_width_at_both_required_sizes(page, served, tmp_path, width, height):
    served["md"].set_state("connected")
    open_options(page, served, width, height)
    m = metrics(page)
    assert m["tab"] == "options" and m["scrollW"] <= m["vw"], f"the Options tab is {m['scrollW']} px wide on a {m['vw']} px screen"  # tables scroll inside their box, the page does not
    card = page.js("(() => { const r = document.getElementById('opt-sel').getBoundingClientRect(); return {left: r.left, right: r.right, text: document.getElementById('opt-sel').innerText}; })()")
    up = card["text"].upper()  # (the page upper-cases its small labels with CSS: what is read is what innerText gives)
    assert 0 <= card["left"] and card["right"] <= width and up.startswith("SELECTED CONTRACT")
    for needed in ("CHOSEN FOR BUY CE FROM TREND PULLBACK", "ALSO MATCHES: NONE", "QUOTE AGE: RECEIVED", "QUOTE AGE: EXCHANGE TIME", "ALTERNATIVES", "BID × UNITS", "OPEN INTEREST (UNITS)"):
        assert needed in up, needed
    assert page.js("document.querySelectorAll('#opt-table tr.opt-sel').length") == 1
    keep(page, tmp_path, f"options-{width}x{height}")
    assert page.errors == []


def test_the_options_tab_says_no_contract_selected_in_a_real_browser_when_the_decision_is_not_a_buy(page, served, tmp_path):
    md, clock = served["md"], served["clock"]
    md.set_state("disconnected", "the broker closed the connection (test)")
    md.down_since = clock.t - 6001  # no live connection: NO TRADE
    try:
        open_options(page, served, 1366, 768)
        page.until("document.getElementById('opt-sel').innerText.toUpperCase().includes('NO CONTRACT SELECTED')")
        text = page.js("document.getElementById('opt-sel').innerText").upper()
        assert text.startswith("NO CONTRACT SELECTED") and "FINAL DECISION: NO TRADE" in text and "SELECTED CONTRACT" not in text.replace("NO CONTRACT SELECTED", "") and "CHOSEN FOR" not in text
        assert page.js("document.querySelectorAll('#opt-table tr.opt-sel').length") == 0 and page.js("document.querySelectorAll('#opt-table tr').length") > 20  # both sides listed, none picked
        assert metrics(page)["scrollW"] <= 1366
        keep(page, tmp_path, "options-no-trade-1366x768")
        assert page.errors == []
    finally:
        md.set_state("connected")
        md.down_since = None
