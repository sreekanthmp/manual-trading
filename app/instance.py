"""One Prism at a time.

Two copies of Prism fight over the same Angel One login, the same feed connection, the same decision journal and the same port. Starting a second copy
used to run the whole start-up (it wrote a "restart" record and a "Historical data is still loading" decision into the shared journal and began a login)
and only then failed to bind the port. This takes an operating-system lock before any of that happens, so a second copy stops at once with a message that
says what is running, and it checks that the port is free. The lock is released by the system when the process ends, however it ends: there is no stale
lock file to clean up after a crash."""

import ipaddress
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)
LOCK_NAME, INFO_NAME = "prism.lock", "prism.instance.json"


class InstanceError(RuntimeError):
    """Prism cannot start here: another copy is running, or the port is taken. The message says what to do."""


_held = None  # the open lock file: it must stay open for as long as this process runs


def _try_lock(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def read_info(directory) -> dict | None:
    try:
        return json.loads((Path(directory) / INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def acquire(directory="logs", port: int | None = None, clock=time.time) -> dict:
    """Take the lock or raise InstanceError. Returns what is written about this instance (pid, start time, port)."""
    global _held
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / LOCK_NAME, "a+b")
    if not _try_lock(f):
        f.close()
        other = read_info(d) or {}
        pid, since = other.get("pid"), other.get("started_at")
        when = time.strftime("%H:%M:%S", time.localtime(since)) if since else "an unknown time"
        where = f" on port {other['port']}" if other.get("port") else ""
        raise InstanceError(
            f"another Prism is already running (process {pid or 'unknown'}, started {when}{where}). Two copies would fight over the Angel One login, the feed and the "
            f"journal, so this one has not started anything. Use the dashboard of the running copy, or stop it first (Ctrl+C in its window) and start this one again.")
    _held = f
    info = {"pid": os.getpid(), "started_at": clock(), "port": port}
    try:
        (d / INFO_NAME).write_text(json.dumps(info), encoding="utf-8")
    except OSError as e:  # the lock is what protects; the note is only for the message of a second copy
        log.warning("could not write the instance note: %s", e)
    return info


def release() -> None:
    global _held
    if _held is not None:
        try:
            _held.close()  # closing the file drops the lock
        except OSError:
            pass
        _held = None


def target_address(argv=None, env=None) -> tuple[str, int] | None:
    """The host and port uvicorn was told to listen on, read from the command line (`python -m uvicorn app.main:app --port 8000`) or, as uvicorn does when the command line is silent, from
    UVICORN_HOST / UVICORN_PORT. None when Prism was not started by uvicorn from a command line, so nothing is guessed."""
    argv = list(sys.argv if argv is None else argv)
    if not any("uvicorn" in str(a) for a in argv):
        return None
    env = os.environ if env is None else env
    host, port = env.get("UVICORN_HOST") or "127.0.0.1", env.get("UVICORN_PORT") or 8000  # uvicorn's own defaults, and its environment variables (the command line wins below)
    for i, a in enumerate(argv):
        a = str(a)
        if a == "--port" and i + 1 < len(argv):
            port = argv[i + 1]
        elif a.startswith("--port="):
            port = a.split("=", 1)[1]
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
    try:
        return host, int(port)
    except ValueError:
        return None


def port_in_use(host: str, port: int) -> bool:
    """True when something is already listening there (the same test uvicorn's own bind would fail)."""
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def is_loopback(host: str) -> bool:
    """Does this host name or address reach only this computer? `localhost` and 127.x.x.x / ::1 do; 0.0.0.0, `::`, a LAN address or any other name does not (a name is not trusted to mean
    this computer)."""
    h = str(host).strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def remote_allowed(env=None) -> bool:
    """Has the owner said, in PRISM_ALLOW_REMOTE, that Prism may be served beyond this computer?"""
    return str((os.environ if env is None else env).get("PRISM_ALLOW_REMOTE", "")).strip().lower() in ("1", "true", "yes", "on")


def guard(directory="logs", argv=None, in_use=port_in_use, clock=time.time, env=None) -> dict:
    """Everything that must be true before Prism starts anything: it is served to this computer only (its API has no login), nobody else is running it, and its port is free. Raises
    InstanceError, otherwise returns the instance note. A port taken by a program that is not Prism (a Django server, say) is explained too. The host is read from the uvicorn command line:
    a launch that is not uvicorn's is not checked (there is nothing to read)."""
    addr = target_address(argv, env)
    if addr and not is_loopback(addr[0]):
        if not remote_allowed(env):
            raise InstanceError(
                f"Prism's API has no login: anyone who can reach it sees your decisions, the state of your feed and your journal. It was started with --host {addr[0]}, which is not this "
                f"computer only. Start it with --host 127.0.0.1, or set PRISM_ALLOW_REMOTE=1 if you really mean to share it on your network. Nothing has been started.")
        log.warning("Prism is served beyond this computer (--host %s) because PRISM_ALLOW_REMOTE is set: its API has no login", addr[0])
    info = acquire(directory, addr[1] if addr else None, clock)
    if addr and in_use(*addr):
        release()
        raise InstanceError(
            f"port {addr[1]} is already in use by another program (it is not another Prism: that was checked first). Start Prism on a free port, for example "
            f"`python -m uvicorn app.main:app --host {addr[0]} --port {addr[1] + 1}`, or stop the program that holds port {addr[1]}. Nothing has been started.")
    return info
