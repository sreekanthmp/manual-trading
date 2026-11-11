"""The terminal-style dashboard: four tabs on one page and one WebSocket, nothing recomputed in the browser, and the read-only journal endpoint behind the Journal tab.
The browser behaviour itself (fit at 1920x1080 and 1366x768, the decision states, mobile) was checked by hand in a browser with mocked scenarios: no unit test sees it."""

import re
from pathlib import Path

import pytest
from test_phase3_integration import World

from app.journal import Journal
from app.main import create_app

HTML = (Path(__file__).resolve().parent.parent / "app" / "static" / "index.html").read_text(encoding="utf-8")


def test_the_page_has_the_five_tabs_and_each_has_its_panel():  # (four until Stage 8 added Trades)
    for name in ("overview", "options", "analysis", "journal", "trades"):
        assert f'data-tab="{name}"' in HTML and f'id="tab-{name}"' in HTML and f'id="tb-{name}"' in HTML, name
    assert HTML.count('role="tab"') == 5 and HTML.count('role="tabpanel"') == 5
    assert re.search(r'<section class="tab" id="tab-overview"[^>]*>', HTML) and 'id="tab-overview" role="tabpanel" aria-labelledby="tb-overview" hidden' not in HTML  # the default page is shown


def test_the_overview_shows_what_the_first_screen_must_answer():
    for ident in ('id="c-market"', 'id="c-feed"', 'id="spot"', 'id="decision"', 'id="tfrow"', 'id="ctx"', 'id="setup"', 'id="strat"', 'id="dhs"', 'id="ev3"'):
        assert ident in HTML, ident
    assert "slice(0, 3)" in HTML and "Full journal" in HTML  # only the latest three events; the rest is in the Journal
    assert "Decides the allowed side" in HTML and "Information only" in HTML  # only the 1H and 15M decide the allowed side
    for state in ("buy-ce", "buy-pe", "wait", "no-trade"):
        assert f".decision.{state}" in HTML, state
    assert "ACTION_CLS" in HTML and "esc(d.action)" in HTML  # the decision is always written out, never colour alone


def test_there_is_one_websocket_and_no_polling_so_switching_tabs_never_reconnects_or_resets_anything():
    assert HTML.count("new WebSocket(") == 1 and "setInterval" not in HTML
    assert "if (WS && WS.readyState <= 1) return;" in HTML  # a second connection is never opened
    show = HTML[HTML.index("function showTab"):HTML.index("document.addEventListener(\"click\"")]
    assert "connect(" not in show and "WS" not in show and "render(LAST)" in show  # a tab switch only redraws the last snapshot
    # the requests the page makes: the Journal tab's one read-only GET, and the one helper the Trades tab sends everything through (the trades it reads, a trade you log or void, a size you ask for): only when you ask, never on a timer
    assert HTML.count("fetch(") == 2 and 'fetch("/api/journal")' in HTML and HTML.count("sendJson(") >= 4 and "setTimeout(loadTrades" not in HTML and "setInterval" not in HTML


def test_the_page_keeps_the_open_sections_across_the_once_a_second_redraw_and_only_touches_the_page_when_something_changed():
    assert "const OPEN = new Set(" in HTML and "function setHtml" in HTML and "el._h !== html" in HTML


def test_the_regime_facts_and_the_permission_are_one_compact_overview_row_and_the_evidence_is_in_the_analysis_tab():
    for ident in ("function regimeRow", "function regimeBlock", "function permissionBlock", 'sec("a-regime"', 'sec("a-ct"', "const checkLines"):
        assert ident in HTML, ident
    overview = HTML[HTML.index("function renderOverview"):HTML.index("// ================= options")]
    assert overview.count("regimeRow(") == 1 and "regimeBlock" not in overview and "permissionBlock" not in overview  # one row: the checks and the structure are not on the first screen
    assert 'class="wide"' in HTML[HTML.index("function regimeRow"):HTML.index("function renderOverview")]
    analysis = HTML[HTML.index("function renderAnalysisTab"):HTML.index("// ================= journal")]
    assert analysis.index('sec("a-regime"') < analysis.index('sec("a-ct"') < analysis.index('sec("a-strat"')
    page = HTML[HTML.index("// ================= regime facts"):HTML.index("function setupCard")]
    assert "✓" in page and "✗" in page and "?" in page  # every check is shown as holding, not holding, or could not be judged
    assert not re.search(r"\b(adx|rsi|ema20|ema50|atr)\w*\s*[<>]=?\s*\d", page, re.I)  # the page shows the server's checks and decides nothing itself
    assert "Counter-trend permission" in HTML and "GRANTED" in page and "DENIED" in page  # granted or denied, in words and not colour alone


def test_nothing_in_the_page_places_or_changes_an_order():
    assert not re.search(r"placeOrder|modifyOrder|cancelOrder|method\s*:\s*[\"'](POST|PUT|DELETE|PATCH)", HTML, re.I)


# -- /api/journal: today's journal file, condensed, read-only ----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world():
    return World()


def journal_endpoint(md):
    app = create_app(md=md, feed=None)
    app.state.md = md
    return next(r for r in app.routes if getattr(r, "path", "") == "/api/journal").endpoint


def test_the_journal_endpoint_returns_the_decisions_and_every_lifecycle_event_newest_first(world, tmp_path):
    md, clock = world.engine()
    md.journal = Journal(tmp_path)
    for slot in (16, 17, 18, 19):
        world.step(md, clock, slot)
        md.safe_trading()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    out = journal_endpoint(md)()
    assert out["available"] and out["file"].startswith("decisions-") and out["records"] >= 4
    actions = [d["action"] for d in out["decisions"]]
    assert actions[0] == "BUY CE" and "WAIT" in actions  # newest first: the BUY, then the waiting before it
    top = out["decisions"][0]
    assert top["headline"].startswith("BUY CE") and top["reasons"] and top["strategy_name"] == "Trend Pullback" and top["time"] > out["decisions"][-1]["time"]
    assert [e["status"] for e in out["events"]] == ["CONFIRMED", "WAITING_FOR_CONFIRMATION", "DETECTED"]  # every lifecycle event, newest first, none twice
    assert len({e["seq"] for e in out["events"]}) == len(out["events"])
    assert all(r["reason"] != "candle" for r in [{"reason": d["reason"]} for d in out["decisions"]])  # the once-per-candle heartbeat lines are left out
    assert journal_endpoint(md)(limit=1)["decisions"] == out["decisions"][:1]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before  # reading never writes: the journal file is exactly as it was


def test_the_journal_endpoint_says_so_when_there_is_no_journal_or_it_cannot_be_read(world, tmp_path):
    md, _ = world.engine()
    md.journal = None
    out = journal_endpoint(md)()
    assert out["available"] is False and "no decision journal" in out["reason"] and out["decisions"] == [] and out["events"] == []

    class Broken:
        def load_day(self, now):
            raise OSError("disk unplugged")

    md.journal = Broken()
    out = journal_endpoint(md)()
    assert out["available"] is False and "could not be read" in out["reason"] and "disk unplugged" in out["reason"]
    md.journal = Journal(tmp_path)  # nothing written yet today: an empty journal, not an error
    empty = journal_endpoint(md)()
    assert empty["available"] and empty["decisions"] == [] and empty["events"] == []
