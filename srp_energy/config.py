"""Configuration, read from the environment (optionally via a .env file).

Credentials are never written to disk by this project and never returned by the
API. See .env.example for the full list of settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without shadowing real env vars.

    Uses python-dotenv when available and falls back to a minimal parser so the
    package has no hard dependency on it.
    """
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class Settings:
    """Runtime configuration for the scraper, store and API."""

    srp_username: str = ""
    srp_password: str = ""

    data_dir: Path = field(default_factory=lambda: Path("./data"))
    downloads_dir: Path = field(default_factory=lambda: Path("./data/downloads"))
    database_path: Path = field(default_factory=lambda: Path("./data/srp.db"))

    host: str = "127.0.0.1"
    port: int = 8000

    headless: bool = True
    timeout_ms: int = 120_000
    download_timeout_ms: int = 60_000

    # Keep raw CSVs after importing them. Useful for auditing a parse, but they
    # accumulate; set SRP_KEEP_DOWNLOADS=false to delete after a successful import.
    keep_downloads: bool = True

    log_level: str = "INFO"

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = ".env") -> "Settings":
        if dotenv_path is not None:
            _load_dotenv(Path(dotenv_path))

        data_dir = Path(os.getenv("SRP_DATA_DIR", "./data")).expanduser()
        return cls(
            srp_username=os.getenv("SRP_USERNAME", ""),
            srp_password=os.getenv("SRP_PASSWORD", ""),
            data_dir=data_dir,
            downloads_dir=Path(
                os.getenv("SRP_DOWNLOADS_DIR", str(data_dir / "downloads"))
            ).expanduser(),
            database_path=Path(
                os.getenv("SRP_DATABASE_PATH", str(data_dir / "srp.db"))
            ).expanduser(),
            host=os.getenv("SRP_HOST", "127.0.0.1"),
            port=_env_int("SRP_PORT", 8000),
            headless=_env_bool("SRP_HEADLESS", True),
            timeout_ms=_env_int("SRP_TIMEOUT_MS", 120_000),
            download_timeout_ms=_env_int("SRP_DOWNLOAD_TIMEOUT_MS", 60_000),
            keep_downloads=_env_bool("SRP_KEEP_DOWNLOADS", True),
            log_level=os.getenv("SRP_LOG_LEVEL", "INFO").upper(),
        )

    def require_credentials(self) -> tuple[str, str]:
        """Return (username, password), failing with a usable message if unset."""
        if not self.srp_username or not self.srp_password:
            raise SystemExit(
                "SRP credentials are not configured.\n"
                "Set SRP_USERNAME and SRP_PASSWORD in your environment or in a "
                ".env file (copy .env.example to get started)."
            )
        return self.srp_username, self.srp_password

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
