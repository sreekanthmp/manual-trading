"""Stage 6.4: the Journal tab, in a real browser, says when the journal file holds lines that could not be read (it used to show the records that were readable and nothing else).

Needs Chrome, Chromium or Edge and the `websockets` package, like the other browser tests (and is skipped, with the reason, without them)."""

import json

from helpers import FRI, ist
from test_dashboard_layout_browser import browser, goto, page, served  # noqa: F401

from app.journal import Journal

T0 = ist(10, 0, 5, day=FRI)


def record(t):
    return {"v": 2, "time": "2026-11-06T10:00:05+05:30", "t": t, "reason": "changed", "price": 24500.0,
            "decision": {"action": "WAIT", "headline": "WAIT: an earlier run", "reasons": [], "against": [], "setup_id": None, "strategy": None, "strategy_name": None, "also": []},
            "market": None, "session": {"phase": "morning"}, "events": []}  # fmt: skip


def write(path, *, garbage):
    lines = [json.dumps(record(T0)), "{this line was cut short by a crash"] if garbage else [json.dumps(record(T0))]
    path.write_text("\n".join(lines + [json.dumps(record(T0 + 10))]) + "\n", encoding="utf-8")


def test_the_journal_tab_says_when_the_file_holds_lines_it_could_not_read(page, served, tmp_path):
    md = served["md"]
    j = Journal(tmp_path)
    path = j.path(md.clock())
    write(path, garbage=True)
    md.journal = j
    try:
        page.size(1366, 768)
        goto(page, served["url"] + "#journal")
        page.until("typeof CONN !== 'undefined' && CONN === 'live' && document.getElementById('journal-body').innerText.includes(\"Today's decision journal\")", timeout=25)
        page.until("document.getElementById('journal-body').innerText.includes('could not be read')", timeout=15)
        text = page.js("document.getElementById('journal-body').innerText")
        assert "1 line(s) of the file could not be read and are not shown" in text and "WAIT: an earlier run" in text  # the records that are readable are all there, and the page says it is not all
        path.write_text("", encoding="utf-8")
        write(path, garbage=False)  # the file is whole again
        page.js("document.getElementById('j-refresh').click()")
        page.until("!document.getElementById('journal-body').innerText.includes('could not be read')", timeout=15)
        assert page.js("document.getElementById('jserr').textContent") == "" and page.errors == []
    finally:
        md.journal = None
