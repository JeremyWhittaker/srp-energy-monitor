"""CLI argument handling.

The parser is where user typos land, and a typo should produce a usage error
rather than a traceback from deep inside `datetime`.
"""

from __future__ import annotations

from datetime import date

import pytest

from srp_energy.cli import build_parser


def test_hourly_accepts_iso_dates():
    args = build_parser().parse_args(["hourly", "--start", "2026-08-01", "--end", "2026-08-25"])

    assert args.start == date(2026, 8, 1)
    assert args.end == date(2026, 8, 25)


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-01", "08/25/2026", ""])
def test_hourly_rejects_a_malformed_date(bad, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hourly", "--start", bad])

    assert "YYYY-MM-DD" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["show", "export"])
@pytest.mark.parametrize("bad", ["999999999", "-5", "0", "seven"])
def test_days_is_bounded(command, bad, capsys):
    """`date.today() - timedelta(days=999999999)` is an OverflowError traceback."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "--days", bad])

    assert "days" in capsys.readouterr().err


def test_days_accepts_a_sane_value():
    assert build_parser().parse_args(["show", "--days", "7"]).days == 7


def test_series_choices_are_enforced(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["show", "--series", "gas"])

    assert "invalid choice" in capsys.readouterr().err


def test_import_requires_a_directory(capsys):
    """`directory` is positional and required; the docs used to imply a default."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import"])

    assert "directory" in capsys.readouterr().err


def test_a_subcommand_is_required(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([])

    assert "required" in capsys.readouterr().err
