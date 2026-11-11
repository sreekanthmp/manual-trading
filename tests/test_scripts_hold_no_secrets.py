"""The scripts beside the app hold no secret (Stage 8).

`angelone/daily_update.py` used to carry the database password in the file itself. It now reads the database login from PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DBNAME (the names Prism uses),
from the environment or from the git-ignored `.env` beside the app, and stops with a message that says what to set when there is no password. These tests read the files and run the script in a
temporary folder with no `.env`; no database and no broker is contacted."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "angelone" / "daily_update.py"
SECRETISH = re.compile(r"""(?ix)\b(pass(?:word|wd)?|pwd|secret|totp|api_?key|client_?secret|private_?key)\w*\s*[:=]\s*(?P<q>['"])(?P<v>[^'"\n]+)(?P=q)""")


def scripts():
    return sorted(p for folder in ("angelone", "app", "scripts") for p in (ROOT / folder).glob("*.py") if p.is_file())


def test_no_script_assigns_a_secret_as_a_literal():
    found = []
    for p in scripts():
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = SECRETISH.search(line)
            if m and m.group("v").strip() and not line.lstrip().startswith("#"):
                found.append(f"{p.relative_to(ROOT)}:{n}")  # the line is not printed: it may be a secret
    assert not found, f"a secret-looking value is written in: {found}"


def test_the_daily_update_reads_its_database_login_from_the_environment():
    src = SCRIPT.read_text(encoding="utf-8")
    for name in ("PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DBNAME"):
        assert f'os.getenv("{name}"' in src, name
    assert "PG_PASSWORD is not set" in src and ".env" in src
    assert not re.search(r"""password\s*=\s*['"][^'"]+['"]""", src)


def run_script(tmp_path, env):
    """Import a copy of the script from a folder that has no `.env` beside it, with exactly this environment."""
    folder = tmp_path / "angelone"
    folder.mkdir(exist_ok=True)
    (folder / "daily_update.py").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    return subprocess.run([sys.executable, "-c", "import daily_update; print('PG', daily_update.PG['host'], daily_update.PG['port'], daily_update.PG['user'], daily_update.PG['dbname'])"],
                          cwd=folder, env={**env, "PYTHONPATH": str(folder)}, capture_output=True, text=True, timeout=60)  # fmt: skip


@pytest.fixture
def bare_env():
    import os

    keep = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "PYTHONIOENCODING", "VIRTUAL_ENV")}
    return keep


def test_without_a_password_the_script_stops_and_says_what_to_set(tmp_path, bare_env):
    done = run_script(tmp_path, bare_env)
    assert done.returncode != 0 and "PG_PASSWORD is not set" in (done.stderr + done.stdout)


def test_with_a_password_in_the_environment_it_loads_and_takes_the_other_settings_from_it_too(tmp_path, bare_env):
    done = run_script(tmp_path, {**bare_env, "PG_PASSWORD": "not-a-real-one", "PG_HOST": "db.internal", "PG_PORT": "6543", "PG_USER": "writer", "PG_DBNAME": "other"})
    assert done.returncode == 0, done.stderr[-400:]
    assert done.stdout.strip() == "PG db.internal 6543 writer other"
    assert "not-a-real-one" not in done.stdout + done.stderr


def test_the_defaults_are_the_prism_ones_when_only_the_password_is_set(tmp_path, bare_env):
    done = run_script(tmp_path, {**bare_env, "PG_PASSWORD": "x"})
    assert done.returncode == 0 and done.stdout.strip() == "PG localhost 5432 postgres prism"


def test_the_script_reads_the_dotenv_beside_the_app_and_a_real_variable_wins(tmp_path, bare_env):
    (tmp_path / ".env").write_text("PG_PASSWORD='from-the-file'\nPG_USER=file_user\n# PG_HOST=commented\n", encoding="utf-8")
    done = run_script(tmp_path, bare_env)
    assert done.returncode == 0 and "file_user" in done.stdout
    done = run_script(tmp_path, {**bare_env, "PG_USER": "env_user"})
    assert "env_user" in done.stdout and "file_user" not in done.stdout  # the real environment wins over the file
    assert "from-the-file" not in done.stdout + done.stderr


def test_the_env_file_is_git_ignored_and_example_files_hold_no_value():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\.env\s*$", ignore)
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not [line for line in example.splitlines() if re.match(r"\s*(ANGEL_\w+|PG_PASSWORD)\s*=\s*\S", line)]
