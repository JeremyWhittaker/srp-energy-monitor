"""Client tests for the logic that does not need a browser.

The scrape itself can only be verified against the live portal, but the URL
reasoning that decides *whether to log in at all* is pure and worth pinning
down -- getting it wrong silently skips authentication.
"""

from __future__ import annotations

import pytest

from srp_energy.client import (
    PASSWORD_SELECTORS,
    SRPClient,
    USERNAME_SELECTORS,
    USER_AGENT,
    SRPError,
)
from srp_energy.models import Series


@pytest.mark.parametrize(
    "url,expected",
    [
        # The portal's hostname is literally "myaccount.srpnet.com", so matching
        # the whole URL made every page look authenticated -- including the
        # login page -- and login was skipped entirely.
        ("https://myaccount.srpnet.com/power", False),
        ("https://myaccount.srpnet.com/", False),
        ("https://myaccount.srpnet.com/power/login", False),
        ("https://myaccount.srpnet.com/power/signin", False),
        ("https://myaccount.srpnet.com/power/myaccount/usage", True),
        ("https://myaccount.srpnet.com/power/MyAccount/Dashboard", True),
    ],
)
def test_looks_authenticated_examines_the_path_not_the_host(url, expected):
    assert SRPClient._looks_authenticated(url) is expected


def test_credentials_are_required():
    with pytest.raises(ValueError, match="username and password"):
        SRPClient("", "")
    with pytest.raises(ValueError, match="username and password"):
        SRPClient("user", "")


def test_page_property_fails_clearly_before_start():
    with pytest.raises(SRPError, match="not started"):
        _ = SRPClient("user", "pass").page


def test_session_freshness_starts_false():
    assert SRPClient("user", "pass").session_is_fresh is False


def test_visible_login_selectors_are_tried_before_hidden_ones():
    """The page carries both variants; the visible pair must come first."""
    assert USERNAME_SELECTORS[0] == 'input[name="username"]'
    assert PASSWORD_SELECTORS[0] == 'input[name="password"]'
    # The capitalised pair is hidden in the current markup but kept as fallback.
    assert 'input[name="UserName"]' in USERNAME_SELECTORS
    assert 'input[name="Password"]' in PASSWORD_SELECTORS


def test_user_agent_does_not_advertise_automation():
    """Cloudflare rejects the session outright if this looks like a bot."""
    assert "HeadlessChrome" not in USER_AGENT
    assert "Chrome/" in USER_AGENT


@pytest.mark.parametrize(
    "series,label",
    [
        (Series.USAGE, "Usage"),
        (Series.GENERATION, "Generation"),
        (Series.NET, "Net energy"),
        (Series.DEMAND, "Demand"),
    ],
)
def test_series_labels_match_the_portal_buttons(series, label):
    """These strings are clicked verbatim on the usage page."""
    assert series.label == label


async def _never_called(*args, **kwargs):  # pragma: no cover - guard
    raise AssertionError("should not be reached")


@pytest.mark.parametrize("series", list(Series))
def test_every_series_has_a_distinct_filename_stem(series):
    assert series.value in {"usage", "generation", "net", "demand"}
