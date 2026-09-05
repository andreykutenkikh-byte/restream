from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

RELAYCTL = Path(__file__).resolve().parents[2] / "deploy/moblin-relay/relayctl"


def load():
    with patch.dict(sys.modules, {"fcntl": ModuleType("fcntl"), "termios": ModuleType("termios")}):
        return runpy.run_path(str(RELAYCTL), run_name="_history_cli_test")


def test_history_timestamps_require_an_explicit_timezone():
    parse = load()["history_timestamp"]
    assert parse("2026-09-05T23:03:00+10:00") == parse("2026-09-05T13:03:00Z")
    for invalid in ("yesterday", "2026-09-05T13:03:00", "1960-01-01T00:00:00Z"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse(invalid)


def test_history_json_is_read_only_and_preserves_missing_measurements(capsys):
    ns = load()
    command = ns["cmd_history"]
    calls = []
    result = {"rows": [{"input_unique_bitrate_bps": None}], "error_code": "none"}

    def read(**kwargs):
        calls.append(kwargs)
        return result

    with patch.dict(command.__globals__, {"load_history_reader": lambda: read}):
        assert (
            command(
                ["--json", "--since", "2026-09-05T13:00:00Z", "--until", "2026-09-05T14:00:00Z"]
            )
            == 0
        )
    assert len(calls) == 1 and calls[0]["limit"] == 10_000
    assert json.loads(capsys.readouterr().out) == result


def test_history_rejects_bad_ranges_before_loading_code(capsys):
    ns = load()
    command = ns["cmd_history"]

    def unexpected():
        raise AssertionError("must not load")

    with patch.dict(command.__globals__, {"load_history_reader": unexpected}):
        assert command(["--limit", "10001"]) == 2
        assert command(["--since", "2026-09-05T14:00:00Z", "--until", "2026-09-05T13:00:00Z"]) == 2
    assert "Invalid history" in capsys.readouterr().err


def test_history_summary_warns_about_truncation_and_unknown_data(capsys):
    command = load()["cmd_history"]
    result = {
        "rows": [{"timestamp": 1_780_000_000, "source": "UNKNOWN"}],
        "error_code": "none",
        "truncated": True,
    }
    with patch.dict(command.__globals__, {"load_history_reader": lambda: lambda **_: result}):
        assert command([]) == 0
    output = capsys.readouterr().out
    assert "WARNING: export is truncated" in output
    assert "Input unique bitrate: no valid samples" in output
    assert "not YouTube viewer confirmation" in output


def test_incident_classifier_explains_local_credential_owner_error_without_echoing_text():
    classify = load()["classify_incident"]
    event = classify("moblin-relay-normalize:credential-check:parent-owner-unsafe")
    assert event[1] == "RECOVERY_CREDENTIAL_PARENT_OWNER_UNSAFE"
    assert "root" in event[2]
    assert classify("moblin-relay-normalize:credential-check:private-secret-marker") is None
