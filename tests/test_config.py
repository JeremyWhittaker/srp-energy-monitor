"""Configuration tests.

The fallback .env parser matters more than it looks: the base package declares
no dependencies, so an install without the `scrape` or `dev` extra has no
python-dotenv and runs this code path for real.
"""

from __future__ import annotations

import os

import pytest

from srp_energy.config import Settings, _load_dotenv, _env_bool, _env_int


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SRP_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def no_dotenv_package(monkeypatch):
    """Force the built-in fallback parser instead of python-dotenv."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dotenv":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_fallback_parser_handles_export_prefix(tmp_path, no_dotenv_package):
    """`export FOO=bar` is a common .env convention and must not become a key."""
    env = tmp_path / ".env"
    env.write_text("export SRP_USERNAME=alice\nSRP_PASSWORD=secret\n")

    _load_dotenv(env)

    assert os.environ["SRP_USERNAME"] == "alice"
    assert os.environ["SRP_PASSWORD"] == "secret"
    assert "export SRP_USERNAME" not in os.environ


def test_fallback_parser_strips_inline_comments(tmp_path, no_dotenv_package):
    """An unstripped comment made `_env_int` raise on an otherwise valid file."""
    env = tmp_path / ".env"
    env.write_text("SRP_PORT=8000  # the port\n")

    _load_dotenv(env)

    assert os.environ["SRP_PORT"] == "8000"
    assert _env_int("SRP_PORT", 1) == 8000


def test_fallback_parser_keeps_hash_inside_quoted_values(tmp_path, no_dotenv_package):
    """A '#' can legitimately appear in a password."""
    env = tmp_path / ".env"
    env.write_text('SRP_PASSWORD="pa#ssw0rd"\n')

    _load_dotenv(env)

    assert os.environ["SRP_PASSWORD"] == "pa#ssw0rd"


def test_fallback_parser_ignores_comments_and_blanks(tmp_path, no_dotenv_package):
    env = tmp_path / ".env"
    env.write_text("# a comment\n\n   \nSRP_HOST=0.0.0.0\nnot_a_pair\n")

    _load_dotenv(env)

    assert os.environ["SRP_HOST"] == "0.0.0.0"


def test_real_environment_wins_over_dotenv(tmp_path, monkeypatch, no_dotenv_package):
    monkeypatch.setenv("SRP_USERNAME", "from-env")
    env = tmp_path / ".env"
    env.write_text("SRP_USERNAME=from-file\n")

    _load_dotenv(env)

    assert os.environ["SRP_USERNAME"] == "from-env"


def test_missing_dotenv_file_is_not_an_error(tmp_path):
    _load_dotenv(tmp_path / "does-not-exist")  # must not raise


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("", True)],
)
def test_env_bool(monkeypatch, value, expected):
    monkeypatch.setenv("SRP_HEADLESS", value)
    assert _env_bool("SRP_HEADLESS", True) is expected


def test_env_int_rejects_garbage(monkeypatch):
    monkeypatch.setenv("SRP_PORT", "not-a-number")

    with pytest.raises(ValueError, match="must be an integer"):
        _env_int("SRP_PORT", 8000)


def test_settings_from_env_derives_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SRP_DATA_DIR", str(tmp_path / "d"))

    settings = Settings.from_env(dotenv_path=None)

    assert settings.database_path == tmp_path / "d" / "srp.db"
    assert settings.downloads_dir == tmp_path / "d" / "downloads"


def test_require_credentials_fails_clearly_when_unset():
    settings = Settings()

    with pytest.raises(SystemExit, match="SRP_USERNAME"):
        settings.require_credentials()
