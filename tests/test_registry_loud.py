"""Stage 7.2: an empty registry is an error, not a silent NO TRADE.

The strategies register themselves when `app.strategy` is imported (it imports the strategy modules last). The Stage 7 audit noted that `import app.registry` alone leaves the list empty, and that
an engine run that way would scan nothing and answer "no strategy suits the market" for ever. Now asking an empty registry for its scanners, and creating an engine with one, raise at once and say
what to import."""

import subprocess
import sys
from pathlib import Path

import pytest
from helpers import CFG, Clock, ist

from app import registry
from app.engine import MarketData

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def emptied():
    saved = list(registry.REGISTRY)
    registry.REGISTRY.clear()
    registry._refresh()
    try:
        yield
    finally:
        registry.REGISTRY[:] = saved
        registry._refresh()


def test_asking_an_empty_registry_for_its_scanners_fails_and_says_what_to_import(emptied):
    with pytest.raises(RuntimeError, match=r"no strategy is registered: import app\.strategy"):
        registry.scanners()
    with pytest.raises(RuntimeError, match="no strategy is registered"):
        registry.require()


def test_an_engine_cannot_be_created_with_no_strategy_registered(emptied):
    with pytest.raises(RuntimeError, match="no strategy is registered"):
        MarketData(CFG, Clock(ist(10, 0)))


def test_the_derived_tables_follow_the_list_in_both_directions():
    with_none = None
    try:
        saved = list(registry.REGISTRY)
        registry.REGISTRY.clear()
        registry._refresh()
        with_none = len(registry.NAMES)
    finally:
        registry.REGISTRY[:] = saved
        registry._refresh()
    assert with_none == 0 and len(registry.NAMES) == 10 and len(registry.PRIORITY) == 10 and len(registry.scanners()) == 10


def test_the_real_registry_is_complete_and_usable():
    registry.require()  # does not raise
    assert len(registry.NAMES) == 10 and len(registry.scanners()) == 10
    assert MarketData(CFG, Clock(ist(10, 0))) is not None


def test_importing_the_registry_alone_leaves_it_empty_and_importing_the_strategies_fills_it():
    """In a fresh process, as a new tool would meet it."""
    code = "\n".join([
        "from app import registry",
        "print(len(registry.NAMES))",
        "try:",
        "    registry.require()",
        "except RuntimeError as e:",
        "    print('raises', 'import app.strategy' in str(e))",
        "import app.strategy",
        "registry.require()",
        "print(len(registry.NAMES), len(registry.scanners()))",
    ])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.split() == ["0", "raises", "True", "10", "10"]
