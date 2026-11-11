"""Stage 7.1: what a fresh install and a fresh configuration are told.

The Stage 7 audit found that `requirements.txt` did not name the WebSocket server library uvicorn needs (uvicorn alone has none: /ws is not served and the dashboard never goes live), that
nothing was pinned, that `.env.example` listed fourteen settings Prism never reads (and not the ones it does), and that a comment pointed at a script that does not exist. Held here:

  * every package is pinned to one exact version, listed once, and the WebSocket server library is among them
  * every third-party module the app, and the tests, import belongs to a declared package (so a new import cannot be forgotten)
  * `.env.example` documents the settings Prism reads and nothing else; the example thresholds in it are valid; no secret has a value in it
  * the pre-commit file points at nothing that is missing
  * with no WebSocket library installed, a real start-up says so in the log (instead of leaving a dashboard that sits on "loading")"""

import ast
import asyncio
import importlib.metadata as md
import logging
import re
import sys
from pathlib import Path

import pytest
from helpers import CFG
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app import main as app_main
from app.options import OptionParams
from app.strategy import Params

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
SECRETS = ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP", "PG_PASSWORD")


def declared() -> dict:
    out = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            out.setdefault(canonicalize_name(req.name), []).append(req)
    return out


# ====================================================================================================================
# 1. The requirements
# ====================================================================================================================
def test_every_package_is_pinned_to_one_exact_version_and_listed_once():
    for name, reqs in declared().items():
        assert len(reqs) == 1, f"{name} is listed more than once"
        specs = list(reqs[0].specifier)
        assert len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version, f"{name} is not pinned to one exact version: {reqs[0]}"


def test_the_websocket_server_library_the_dashboard_needs_is_declared():
    names = set(declared())
    assert names & {"websockets", "wsproto"}, "uvicorn has no WebSocket server of its own: /ws would not be served"
    assert {"uvicorn", "fastapi"} <= names


def local_modules(folder: Path) -> set:
    return {p.stem for p in folder.glob("*.py")} | {p.name for p in folder.iterdir() if p.is_dir()}


def third_party_imports(folder: Path, local: set) -> dict:
    stdlib, found = set(sys.stdlib_module_names), {}
    for path in sorted(folder.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if n not in stdlib and n not in local and n != "app":
                    found.setdefault(n, set()).add(path.name)
    return found


@pytest.mark.parametrize("folder", ["app", "tests"])
def test_every_third_party_module_that_is_imported_belongs_to_a_declared_package(folder):
    owners, names = md.packages_distributions(), set(declared())
    imports = third_party_imports(ROOT / folder, local_modules(ROOT / folder))
    assert imports, "the scan found nothing to check"
    missing = {m: sorted(files)[:2] for m, files in imports.items() if not ({canonicalize_name(d) for d in owners.get(m, [])} & names)}
    assert not missing, f"imported, but no declared package provides it: {missing}"


def test_the_tools_the_documentation_tells_you_to_run_are_declared():
    assert {"uvicorn", "pytest"} <= set(declared())  # `python -m uvicorn app.main:app` and `python -m pytest`


def test_the_python_the_pins_need_is_the_one_the_readme_and_the_requirements_say():
    assert "Python 3.11 or later" in (ROOT / "README.md").read_text(encoding="utf-8") and "Python 3.11 or later" in REQUIREMENTS.read_text(encoding="utf-8")


# ====================================================================================================================
# 2. The example configuration
# ====================================================================================================================
def example_names() -> dict:
    """name -> (value, commented out) for every `NAME=value` line of .env.example, a commented-out example included."""
    out = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(#\s*)?([A-Z][A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(2)] = (m.group(3).strip(), bool(m.group(1)))
    return out


def names_the_app_reads() -> set:
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "app").rglob("*.py"))
    return set(re.findall(r"[\"']((?:ANGEL|PG|JOURNAL|PRISM)_[A-Z_]+)[\"']", src))


def test_the_example_documents_every_setting_prism_reads_and_nothing_else():
    doc, read = example_names(), names_the_app_reads()
    assert {"ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP", "PG_PASSWORD", "JOURNAL_DIR", "JOURNAL_FSYNC", "PRISM_ALLOW_REMOTE"} <= read  # (the scan finds what it should)
    assert read <= set(doc), f"read by the app, not in .env.example: {sorted(read - set(doc))}"
    unknown = {n for n in doc if n not in read and not n.startswith(("STRATEGY_", "OPTIONS_"))}
    assert not unknown, f"in .env.example, never read by the app: {sorted(unknown)}"


def test_the_example_thresholds_are_real_settings_with_valid_values():
    doc = example_names()
    strategy = {n: v for n, (v, _) in doc.items() if n.startswith("STRATEGY_")}
    options = {n: v for n, (v, _) in doc.items() if n.startswith("OPTIONS_")}
    assert strategy and options
    Params.from_env(env=strategy)  # a name that is not a field is ignored by from_env, so check them one by one as well
    OptionParams.from_env(env=options)
    from dataclasses import fields

    assert all(n[len("STRATEGY_"):].lower() in {f.name for f in fields(Params)} for n in strategy)
    assert all(n[len("OPTIONS_"):].lower() in {f.name for f in fields(OptionParams)} for n in options)
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"({len(fields(Params))} of them)" in text and f"({len(fields(OptionParams))})" in text  # the counts it states are the real ones


def test_no_secret_has_a_value_in_the_example():
    doc = example_names()
    assert all(doc[n] == ("", False) for n in SECRETS)


def test_the_pre_commit_file_points_at_nothing_that_is_missing():
    text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    for path in re.findall(r"[\w./\\-]+\.(?:ps1|sh|bat)\b", text):  # (the scripts it names: the `files:` patterns are regular expressions, not paths)
        assert (ROOT / path.replace("\\", "/")).exists(), f".pre-commit-config.yaml mentions {path}, which does not exist"


# ====================================================================================================================
# 3. A start-up with no WebSocket library says so
# ====================================================================================================================
def only(monkeypatch, installed):
    """Pretend only these libraries are installed."""
    real = app_main.importlib.util.find_spec
    monkeypatch.setattr(app_main.importlib.util, "find_spec", lambda name, *a, **k: object() if name in installed else (None if name in ("websockets", "wsproto") else real(name, *a, **k)))


@pytest.mark.parametrize("installed,expected", [(("websockets",), "websockets"), (("wsproto",), "wsproto"), (("websockets", "wsproto"), "websockets"), ((), None)])
def test_the_library_uvicorn_would_serve_the_dashboard_with_is_found_or_missed(monkeypatch, installed, expected):
    only(monkeypatch, installed)
    assert app_main.websocket_library() == expected


def real_startup(monkeypatch, tmp_path):
    class FakeFeed:  # no network: what is under test is the start-up itself
        def __init__(self, cfg, session, md):
            self.cfg, self.session, self.md = cfg, session, md

        def start(self):
            pass

        def stop(self):
            pass

        def watchdog(self):
            self.md.watchdog()

    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(app_main.instance, "guard", lambda directory, *a, **k: {"pid": 1, "port": None, "started_at": 0})
    monkeypatch.setattr(app_main.instance, "release", lambda: None)
    monkeypatch.setattr(app_main, "load_config", lambda: CFG)
    monkeypatch.setattr(app_main, "Feed", FakeFeed)
    monkeypatch.setattr(app_main, "AngelSession", lambda cfg, limiter=None: None)
    monkeypatch.setattr(app_main, "run_history", lambda *a, **k: None)
    monkeypatch.setattr(app_main, "run_validation", lambda *a, **k: None)

    async def scenario():
        app = app_main.create_app()
        async with app.router.lifespan_context(app):
            return True

    return asyncio.run(scenario())


def test_a_real_start_up_with_no_websocket_library_says_so_in_the_log(monkeypatch, tmp_path, caplog):
    only(monkeypatch, ())
    with caplog.at_level(logging.INFO):
        assert real_startup(monkeypatch, tmp_path)
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("no WebSocket library is installed" in m and "requirements.txt" in m and "websockets" in m for m in messages)


def test_a_real_start_up_with_the_library_installed_says_nothing_about_it(monkeypatch, tmp_path, caplog):
    only(monkeypatch, ("websockets",))
    with caplog.at_level(logging.INFO):
        assert real_startup(monkeypatch, tmp_path)
    assert not any("WebSocket library" in r.getMessage() for r in caplog.records)


def test_an_engine_a_test_passes_in_is_not_checked(monkeypatch, caplog):
    """(The check is for the real thing: a test that hands the app its own engine has no use for the dashboard's socket.)"""
    from helpers import Clock, ist

    from app.engine import MarketData

    only(monkeypatch, ())

    async def scenario():
        app = app_main.create_app(MarketData(CFG, Clock(ist(10, 0))), feed=None)
        async with app.router.lifespan_context(app):
            return True

    with caplog.at_level(logging.INFO):
        assert asyncio.run(scenario())
    assert not any("WebSocket library" in r.getMessage() for r in caplog.records)
