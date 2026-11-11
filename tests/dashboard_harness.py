"""A way to run the dashboard's own script (app/static/index.html) without a browser: under node, in a vm, against a stand-in for the DOM, a WebSocket and the timers. The page is
driven exactly as the browser drives it (snapshots arriving on the socket, the socket closing, a timer firing, a click, a toggled <details>), and after each step the test reads what
the page drew: the innerHTML, the text and the class of every element it touched. No layout is computed here (see test_dashboard_layout_browser.py for that); this is the content.

    run_page(tmp_path, steps) -> [ {label, conn, tab, errors, els: {id: {html, text, cls, hidden, title}}}, ... ]   one entry per "capture" step

Steps: {"op": "message", "snap": {...}}  a snapshot arrives         {"op": "close"}  the socket closes          {"op": "open"}  the socket opens
       {"op": "fire", "ms": 5000}        every pending timer set with that delay runs (5000: the silence watchdog; 2000: the reconnect)
       {"op": "tab", "name": "analysis"} showTab             {"op": "click", "dataset": {...}}  a click on an element with that dataset         {"op": "toggle", "key": "alerts", "open": true}   {"op": "eval", "code": "J.data = {...}"}  runs that code in the page (to set what a fetch would have brought)
       {"op": "capture", "label": "..."} record what the page shows now"""

import copy
import html as htmllib
import json
import re
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest
from helpers import SPOT_TOKEN, master_rows
from test_options import snap as option_snap
from test_phase3_integration import World

from app.candles import IST
from app.instruments import Chain

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "app" / "static" / "index.html"

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [, , htmlPath, stepsPath] = process.argv;
const html = fs.readFileSync(htmlPath, "utf8"), steps = JSON.parse(fs.readFileSync(stepsPath, "utf8"));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {}, handlers = {}, sockets = [], timers = [], errors = [], out = [];
let nextId = 1;
// browser services the page may use for alerts; a first {"op": "env"} step shapes them: store (what localStorage holds), permission (Notification), no_audio / no_notification / no_storage (the browser lacks it)
const env = steps.length && steps[0].op === "env" ? steps.shift() : {};
const fx = {tones: [], notes: [], store: Object.assign({}, env.store || {}), resumed: 0, asked: 0, contexts: 0, requests: []};
const routes = env.fetch || {};
function fakeFetch(url, opts) {
  const method = (opts && opts.method) || "GET";
  fx.requests.push({method, url, body: opts && opts.body ? JSON.parse(opts.body) : null});
  const key = Object.keys(routes).find(k => k === method + " " + url || (k.endsWith("*") && (method + " " + url).startsWith(k.slice(0, -1))));
  if (!key) return Promise.reject(new Error("no network"));
  const r = Array.isArray(routes[key]) ? (routes[key].length > 1 ? routes[key].shift() : routes[key][0]) : routes[key];
  return Promise.resolve({ok: r.status >= 200 && r.status < 300, status: r.status, json: () => (r.body === undefined ? Promise.reject(new Error("no json")) : Promise.resolve(r.body))});
}
class FakeAudioContext {
  constructor() { this.state = "suspended"; this.currentTime = 0; this.destination = {}; fx.contexts++; }
  resume() { this.state = "running"; fx.resumed++; }
  createOscillator() { const o = {frequency: {value: 0}, type: "", connect() {}, start() { fx.tones.push(o.frequency.value); }, stop() {}}; return o; }
  createGain() { return {gain: {value: 0}, connect() {}}; }
}
class FakeNotification { constructor(title, opts) { fx.notes.push({title, body: opts && opts.body, tag: opts && opts.tag, sticky: !!(opts && opts.requireInteraction)}); } static requestPermission() { fx.asked++; return Promise.resolve("granted"); } }
FakeNotification.permission = env.permission || "default";
function mk(id) {
  const e = {id, innerHTML: "", textContent: "", className: "", hidden: false, title: "", style: {setProperty() {}}, dataset: {}, offsetHeight: 0, value: ""};
  const set = () => new Set(e.className.split(/\s+/).filter(Boolean));
  e.classList = {add(c) { const s = set(); s.add(c); e.className = [...s].join(" "); }, remove(c) { const s = set(); s.delete(c); e.className = [...s].join(" "); },
    toggle(c, f) { const s = set(), want = f === undefined ? !s.has(c) : !!f; want ? s.add(c) : s.delete(c); e.className = [...s].join(" "); }, contains(c) { return set().has(c); }};
  Object.assign(e, {setAttribute() {}, getAttribute() { return null; }, addEventListener() {}, focus() {}, querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; }});
  return e;
}
const el = id => els[id] || (els[id] = mk(id));
const document = {getElementById: el, addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); }, querySelectorAll() { return []; }, querySelector() { return null; }, documentElement: el("html")};
class WebSocket { constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); } close() { this.readyState = 3; } }
const ctx = {document, WebSocket, location: {protocol: "http:", host: "x", hash: ""}, history: {replaceState() {}}, console: {log() {}, error(...a) { errors.push(a.map(String).join(" ")); }},
  setTimeout(fn, ms) { const t = {id: nextId++, fn, ms, live: true}; timers.push(t); return t.id; }, clearTimeout(id) { for (const t of timers) if (t.id === id) t.live = false; },
  fetch: fakeFetch, addEventListener() {}};
if (!env.no_audio) ctx.AudioContext = FakeAudioContext;
if (!env.no_notification) ctx.Notification = FakeNotification;
if (!env.no_storage) ctx.localStorage = {getItem: k => (k in fx.store ? fx.store[k] : null), setItem: (k, v) => { fx.store[k] = String(v); }};
ctx.URLSearchParams = URLSearchParams;  // (a browser has it; a bare vm context does not)
ctx.window = ctx;
vm.createContext(ctx);
const ev = code => vm.runInContext(code, ctx);
ev(script);
const last = () => sockets[sockets.length - 1];
const flush = () => new Promise(r => setImmediate(r));  // the page's fetches settle between steps
(async () => {
await flush();  // (the page asked for the trades as it started)
for (const s of steps) {
  if (s.op === "message") last().onmessage({data: JSON.stringify(s.snap)});
  else if (s.op === "close") { last().readyState = 3; last().onclose(); }
  else if (s.op === "open") last().onopen();
  else if (s.op === "fire") for (const t of timers.filter(t => t.live && t.ms === s.ms)) { t.live = false; t.fn(); }
  else if (s.op === "tab") ev(`showTab(${JSON.stringify(s.name)})`);
  else if (s.op === "click") for (const h of handlers.click || []) h({target: {id: s.id || "", dataset: s.target_dataset, closest: () => ({dataset: s.dataset || {}})}});
  else if (s.op === "toggle") for (const h of handlers.toggle || []) h({target: {dataset: {key: s.key}, open: s.open}});
  else if (s.op === "eval") ev(s.code);
  else if (s.op === "capture") {
    const snap = {};
    for (const [id, e] of Object.entries(els)) snap[id] = {html: e.innerHTML, text: e.textContent, cls: e.className, hidden: e.hidden, title: e.title};
    out.push({label: s.label, title: document.title, fx: JSON.parse(JSON.stringify(fx)), conn: ev("CONN"), tab: ev("TAB"), open: ev("[...OPEN]"), sockets: sockets.length, timers: timers.filter(t => t.live).map(t => t.ms), errors: errors.slice(), els: snap});
  }
  await flush();
}
console.log(JSON.stringify(out));
})();
"""


def node():
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not installed: the dashboard's script was not run")
    return path


def run_page(tmp_path, steps):
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "steps.json").write_text(json.dumps(steps, allow_nan=False), encoding="utf-8")
    done = subprocess.run([node(), str(tmp_path / "harness.js"), str(PAGE), str(tmp_path / "steps.json")], capture_output=True, text=True, encoding="utf-8", timeout=60)  # (node writes UTF-8)
    assert done.returncode == 0, done.stderr[:800]
    return json.loads(done.stdout)


def words(markup: str) -> str:
    """What a reader sees of some HTML: the tags gone, the entities decoded, the white space collapsed."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", markup or ""))).strip()


def shown(state: dict, *ids: str) -> str:
    """The visible text of those elements (all of them when none is named) in a captured state."""
    chosen = ids or tuple(state["els"])
    return " ".join(words(state["els"][i]["html"]) or state["els"][i]["text"] for i in chosen if i in state["els"])


# ---- a real snapshot: the engine's own, with a NIFTY spot tick, an option window with quotes, a real analysis and a real BUY CE (the Phase 3 test world) -----------------------------
def base_engine():
    """(md, clock): the Phase 3 test world after its pullback has confirmed (a real BUY CE), with a NIFTY spot tick and 28 option contracts with quotes. The engine itself, so it can be served."""
    w = World()
    md, clock = w.engine()
    for slot in (16, 17, 18, 19):
        w.step(md, clock, slot)
        md.safe_trading()
    now, spot = clock.t, w.at(37)
    chain = Chain(master_rows(lo=24000, hi=26000), datetime.fromtimestamp(now, IST))
    window = chain.window(spot)
    md.set_contracts(window.contracts, date(2026, 11, 6), spot)
    md.set_chain(chain, spot, window.contracts)
    md.set_state("connected")
    md.on_message({"subscription_mode": 2, "exchange_type": 1, "token": SPOT_TOKEN, "last_traded_price": int(spot * 100), "exchange_timestamp": int((now - 1) * 1000)})
    for c in md.contracts:
        if not c.kind:
            continue
        intrinsic = max(spot - c.strike, 0) if c.kind == "CE" else max(c.strike - spot, 0)
        price = round(intrinsic + 45.0 - abs(spot - c.strike) / 20, 2)
        md.on_message(option_snap(c.token, price, now - 2, bid=(price - 0.15, 600), ask=(price + 0.15, 600), volume=2_000_000, oi=3_000_000, ltt=now - 20))
    clock.t = now + 1
    return md, clock


def base_snapshot() -> dict:
    """What /api/state returns on that world: real analysis, real decision, real health."""
    md, _ = base_engine()
    return json.loads(json.dumps(md.snapshot(), allow_nan=False))


def variant(base: dict, **sections) -> dict:
    """A deep copy of the snapshot with whole sections (trading, health, options, ...) replaced by what a scenario says."""
    out = copy.deepcopy(base)
    for k, v in sections.items():
        out[k] = json.loads(json.dumps(v, allow_nan=False))
    return out
