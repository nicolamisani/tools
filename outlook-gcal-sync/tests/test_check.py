"""The preflight checker, which runs without config, database or credentials."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.check import main

FIXTURES = Path(__file__).parent / "fixtures"
GOOGLE = str(FIXTURES / "google-calendar-export.ics")
OUTLOOK = str(FIXTURES / "apple-calendar-export.ics")


def test_a_good_export_passes(capsys):
    assert main([OUTLOOK]) == 0
    out = capsys.readouterr().out
    assert "ready to upload" in out
    assert "1 series" in out


def test_calendar_identity_is_shown(capsys):
    main([OUTLOOK])
    out = capsys.readouterr().out
    assert "Work" in out
    assert "Europe/Rome" in out


def test_the_wrong_calendar_fails_with_a_nonzero_exit(capsys):
    assert main([GOOGLE, "--account", "nicola.misani@gmail.com"]) == 1
    assert "export of your Google calendar" in capsys.readouterr().out


def test_titles_are_withheld_unless_asked(capsys):
    main([OUTLOOK])
    assert "Kickoff with the vendor" not in capsys.readouterr().out


def test_sample_reveals_titles_on_request(capsys):
    main([OUTLOOK, "--sample", "3"])
    assert "Kickoff with the vendor" in capsys.readouterr().out


def test_a_non_calendar_file_is_explained(tmp_path, capsys):
    junk = tmp_path / "export.olm"
    junk.write_bytes(b"\x00\x01 not a calendar")
    assert main([str(junk)]) == 1
    assert "does not look like an iCalendar file" in capsys.readouterr().err


def test_a_missing_file_exits_two(tmp_path, capsys):
    assert main([str(tmp_path / "nope.ics")]) == 2


def test_window_size_is_configurable(capsys):
    main([OUTLOOK, "--past", "0", "--future", "1"])
    out = capsys.readouterr().out
    assert "In window    0 would sync" in out


def test_the_checker_imports_nothing_that_needs_credentials():
    """It must run on a laptop with no .env and no database."""
    import subprocess, sys, textwrap

    code = textwrap.dedent(
        """
        import sys
        import app.check          # noqa: F401
        banned = [m for m in sys.modules if m.startswith(("googleapiclient", "google.oauth2"))]
        print(",".join(banned))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"pulled in {result.stdout}"
