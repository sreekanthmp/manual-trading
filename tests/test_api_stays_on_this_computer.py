"""Stage 7.2: Prism's API has no login, so it is served to this computer only unless the owner says otherwise.

The Stage 7 audit found nothing between a `--host 0.0.0.0` and the decisions, the feed's state, the candles and the journal. Now a launch from the uvicorn command line with a host that is not
this computer is refused before anything is started (not even the one-copy lock is taken), with a message that says why and what to do; PRISM_ALLOW_REMOTE=1 is the owner's yes, and then it
is still said in the log. A launch that is not uvicorn's has no host to read, and is not checked."""

import asyncio
import logging
import sys

import pytest

from app import instance
from app import main as app_main

BASE = ["python", "-m", "uvicorn", "app.main:app", "--port", "8001"]
NOT_FREE = lambda host, port: False  # noqa: E731  (no port is taken: only the host is under test)


def argv(*host):
    return [*BASE, *host]


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.255.255.254", "::1", "[::1]", "localhost", "LOCALHOST", " localhost "])
def test_the_addresses_that_reach_only_this_computer_are_loopback(host):
    assert instance.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "8.8.8.8", "example.com", "my-pc", "", "not an address", "localhost.example.com"])
def test_everything_else_is_not(host):
    assert not instance.is_loopback(host)


@pytest.mark.parametrize("flags", [["--host", "0.0.0.0"], ["--host=0.0.0.0"], ["--host", "::"], ["--host", "192.168.1.20"], ["--host", "example.com"]])
def test_a_launch_beyond_this_computer_is_refused_before_anything_is_started(tmp_path, flags):
    with pytest.raises(instance.InstanceError) as e:
        instance.guard(tmp_path, argv(*flags), in_use=NOT_FREE, env={})
    msg = str(e.value)
    assert "no login" in msg and flags[-1].split("=")[-1] in msg and "--host 127.0.0.1" in msg and "PRISM_ALLOW_REMOTE=1" in msg and "Nothing has been started" in msg
    assert not (tmp_path / instance.LOCK_NAME).exists() and not (tmp_path / instance.INFO_NAME).exists()  # not even the one-copy lock was taken: nothing was started
    assert instance.guard(tmp_path, argv("--host", "127.0.0.1"), in_use=NOT_FREE, env={})["port"] == 8001  # and a right launch right after is fine
    instance.release()


@pytest.mark.parametrize("flags", [[], ["--host", "127.0.0.1"], ["--host=127.0.0.1"], ["--host", "localhost"], ["--host", "::1"]])
def test_a_launch_on_this_computer_is_not_asked_anything(tmp_path, flags):
    assert instance.guard(tmp_path, argv(*flags), in_use=NOT_FREE, env={})["port"] == 8001  # (uvicorn's own default host is 127.0.0.1)
    instance.release()


@pytest.mark.parametrize("value", ["1", "true", "YES", "on", " 1 "])
def test_the_owners_yes_lets_it_through_and_it_is_still_said_in_the_log(tmp_path, caplog, value):
    with caplog.at_level(logging.WARNING):
        info = instance.guard(tmp_path, argv("--host", "0.0.0.0"), in_use=NOT_FREE, env={"PRISM_ALLOW_REMOTE": value})
    instance.release()
    assert info["port"] == 8001
    assert any("beyond this computer" in r.getMessage() and "--host 0.0.0.0" in r.getMessage() and "no login" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["", "0", "no", "false", "off", "maybe"])
def test_anything_but_a_yes_is_a_no(tmp_path, value):
    assert not instance.remote_allowed({"PRISM_ALLOW_REMOTE": value})
    with pytest.raises(instance.InstanceError, match="no login"):
        instance.guard(tmp_path, argv("--host", "0.0.0.0"), in_use=NOT_FREE, env={"PRISM_ALLOW_REMOTE": value})


def test_the_setting_is_read_from_the_environment_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PRISM_ALLOW_REMOTE", raising=False)
    with pytest.raises(instance.InstanceError):
        instance.guard(tmp_path, argv("--host", "0.0.0.0"), in_use=NOT_FREE)
    monkeypatch.setenv("PRISM_ALLOW_REMOTE", "1")
    assert instance.guard(tmp_path, argv("--host", "0.0.0.0"), in_use=NOT_FREE)["port"] == 8001
    instance.release()


def test_a_launch_that_is_not_uvicorns_has_no_host_to_read_and_is_not_checked(tmp_path):
    assert instance.guard(tmp_path, ["python", "some_script.py", "--host", "0.0.0.0"], in_use=NOT_FREE, env={})["port"] is None
    instance.release()


def test_the_real_start_up_refuses_a_launch_beyond_this_computer(monkeypatch, tmp_path):
    """Through the application's own start-up, not only the guard: it is what a person who types the wrong --host meets."""
    monkeypatch.setattr(sys, "argv", argv("--host", "0.0.0.0"))
    monkeypatch.delenv("PRISM_ALLOW_REMOTE", raising=False)
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path))
    ran = []

    def must_not_run(*a, **k):
        ran.append("went on")
        raise AssertionError("the application went on with its start-up after the host was refused")

    monkeypatch.setattr(app_main, "load_config", must_not_run)
    monkeypatch.setattr(app_main, "Journal", must_not_run)  # nothing is written to the journal
    app = app_main.create_app()

    async def enter():
        async with app.router.lifespan_context(app):
            ran.append("served")

    with pytest.raises(instance.InstanceError, match="no login"):
        asyncio.run(enter())
    assert ran == [] and not (tmp_path / instance.LOCK_NAME).exists()  # nothing was started and nothing was locked


def test_the_readme_and_the_example_say_how_to_start_it_and_how_to_share_it():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    readme, example = (root / "README.md").read_text(encoding="utf-8"), (root / ".env.example").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in readme and "PRISM_ALLOW_REMOTE=1" in readme and "no login" in readme
    assert "PRISM_ALLOW_REMOTE" in example and "no login" in example


# -- uvicorn also takes its host and port from the environment when the command line is silent ----------------------------------------------------------
def test_the_host_and_port_come_from_the_environment_when_the_command_line_says_nothing():
    assert instance.target_address(["python", "-m", "uvicorn", "app.main:app"], {"UVICORN_HOST": "0.0.0.0", "UVICORN_PORT": "9100"}) == ("0.0.0.0", 9100)
    assert instance.target_address(["python", "-m", "uvicorn", "app.main:app"], {}) == ("127.0.0.1", 8000)
    assert instance.target_address(["python", "-m", "uvicorn", "app.main:app"], {"UVICORN_HOST": "", "UVICORN_PORT": ""}) == ("127.0.0.1", 8000)  # (empty is not set)
    assert instance.target_address(["python", "-m", "uvicorn", "app.main:app"], {"UVICORN_PORT": "abc"}) is None  # (as with a bad --port: nothing is guessed)


def test_the_command_line_wins_over_the_environment_as_it_does_in_uvicorn():
    env = {"UVICORN_HOST": "0.0.0.0", "UVICORN_PORT": "9100"}
    assert instance.target_address(argv("--host", "127.0.0.1"), env) == ("127.0.0.1", 8001)  # (BASE has --port 8001)


def test_a_host_set_in_the_environment_is_refused_just_the_same(tmp_path):
    with pytest.raises(instance.InstanceError, match="no login") as e:
        instance.guard(tmp_path, ["python", "-m", "uvicorn", "app.main:app"], in_use=NOT_FREE, env={"UVICORN_HOST": "0.0.0.0"})
    assert "--host 0.0.0.0" in str(e.value) and not (tmp_path / instance.LOCK_NAME).exists()
    assert instance.guard(tmp_path, argv("--host", "127.0.0.1"), in_use=NOT_FREE, env={"UVICORN_HOST": "0.0.0.0"})["port"] == 8001  # the command line says this computer
    instance.release()
    assert instance.guard(tmp_path, ["python", "-m", "uvicorn", "app.main:app"], in_use=NOT_FREE, env={"UVICORN_HOST": "0.0.0.0", "PRISM_ALLOW_REMOTE": "1"})["port"] == 8000
    instance.release()


def test_the_port_that_is_checked_is_the_one_uvicorn_will_bind(tmp_path):
    seen = []
    instance.guard(tmp_path, ["python", "-m", "uvicorn", "app.main:app"], in_use=lambda h, p: seen.append((h, p)) or False, env={"UVICORN_PORT": "9100"})
    instance.release()
    assert seen == [("127.0.0.1", 9100)]
