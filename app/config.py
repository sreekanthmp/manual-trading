"""Settings come from environment variables (a local .env file is loaded as a convenience)."""

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader. Real environment variables win over the file."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Config:
    api_key: str
    client_id: str
    password: str  # account PIN
    totp_secret: str  # base32 secret, not a 6-digit code
    gap_seconds: float = 15.0  # no tick for this long during market hours = data gap
    stale_seconds: float = 45.0  # no tick for this long = connection is dead, reconnect
    cache_dir: str = ".cache"
    # Prism DB (read-only history). The password has no default: set PG_PASSWORD.
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = ""
    pg_dbname: str = "prism"


def load_config() -> Config:
    load_dotenv()
    names = {
        "api_key": "ANGEL_API_KEY",
        "client_id": "ANGEL_CLIENT_ID",
        "password": "ANGEL_PASSWORD",
        "totp_secret": "ANGEL_TOTP",
    }
    values = {k: os.getenv(v, "").strip() for k, v in names.items()}
    missing = [names[k] for k, v in values.items() if not v]
    if missing:
        raise ConfigError("missing environment variables: " + ", ".join(missing))
    return Config(
        **values,
        pg_host=os.getenv("PG_HOST", "localhost"),
        pg_port=int(os.getenv("PG_PORT", "5432")),
        pg_user=os.getenv("PG_USER", "postgres"),
        pg_password=os.getenv("PG_PASSWORD", ""),
        pg_dbname=os.getenv("PG_DBNAME", "prism"),
    )
