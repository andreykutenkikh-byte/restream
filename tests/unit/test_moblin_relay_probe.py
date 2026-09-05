"""Real subprocess regressions for the isolated self-test's probe deadline."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from bootstrap_worker.errors import safe_failure
from bootstrap_worker.relay_installer import _SELF_TEST_STAGE_CODES
from scripts.ci_node_onboarding_smoke import (
    SAFE_BOOTSTRAP_DIAGNOSTIC_CODES,
    safe_self_test_progress,
)

SELF_TEST = Path(__file__).resolve().parents[2] / "deploy" / "moblin-relay" / "self-test"


def load_probe():
    with (
        patch.dict(sys.modules, {"fcntl": ModuleType("fcntl"), "resource": ModuleType("resource")}),
        patch.dict(os.environ, {"MOBLIN_RELAY_SELF_TEST_STAGE_FILE": ""}),
    ):
        return runpy.run_path(str(SELF_TEST), run_name="_isolated_probe_test")


def test_probe_drains_both_pipes_before_either_can_block() -> None:
    namespace = load_probe()
    probe = namespace["run_probe"]
    # Far larger than ordinary pipe capacity: the old stdout-first loop could
    # wait forever after this child filled stderr before producing stdout.
    result = probe(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('e' * 1048576); sys.stderr.flush(); "
            "sys.stdout.write('v' * 1048576); sys.stdout.flush()",
        ],
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == "v" * 1048576
    assert result.stderr == "e" * 1048576


def test_probe_deadline_covers_reading_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = load_probe()
    created = []
    original = subprocess.Popen

    def record_process(*args, **kwargs):
        child = original(*args, **kwargs)
        created.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", record_process)
    with pytest.raises(namespace["TestFailure"], match="^local media probe timed out$") as failure:
        namespace["run_probe"](
            [
                sys.executable,
                "-c",
                "import sys,time; print('PRIVATE_PROBE_FIXTURE', file=sys.stderr, flush=True); "
                "time.sleep(10)",
            ],
            timeout=0.2,
        )
    assert created and all(child.poll() is not None for child in created)
    assert "PRIVATE_PROBE_FIXTURE" not in str(failure.value)


def test_probe_nonzero_exit_and_stderr_are_preserved_for_strict_assertions() -> None:
    namespace = load_probe()
    result = namespace["run_probe"](
        [sys.executable, "-c", "import sys; sys.stderr.write('invalid-frame'); sys.exit(9)"],
        timeout=10,
    )
    assert result.returncode == 9
    assert result.stderr == "invalid-frame"
    assert result.stdout == ""


@pytest.mark.parametrize(
    "stage",
    [
        "cont-kick",
        "cont-final",
        "cont-capture",
        "cont-ledger",
        "cont-reader",
        "sink-format",
        "sink-gop",
        "sink-decode",
        "sink-video",
        "sink-audio",
        "sink-timestamps",
    ],
)
def test_continuity_substages_survive_safe_failure_mapping(stage: str) -> None:
    assert len(stage) <= 16
    code = _SELF_TEST_STAGE_CODES[stage]
    assert code in SAFE_BOOTSTRAP_DIAGNOSTIC_CODES
    assert code + "_timeout" in SAFE_BOOTSTRAP_DIAGNOSTIC_CODES
    assert safe_failure(code).safe_message != safe_failure("unknown").safe_message
    assert safe_failure(code + "_timeout").safe_message != safe_failure("unknown").safe_message


def test_progress_diagnostic_retains_only_fixed_stage_and_bounded_numbers() -> None:
    assert safe_self_test_progress(
        {
            "job_id": "test-job",
            "stage": "sink-video",
            "elapsed_seconds": 287.12345,
            "strict_segment_index": 12,
            "stdout": "PRIVATE_FIXTURE_MARKER",
            "url": "rtmps://example.test/live#private-fixture",
        },
        job_id="test-job",
    ) == {"stage": "sink-video", "elapsed_seconds": 287.123, "strict_segment_index": 12}


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": "previous-job"},
        {"stage": "PRIVATE_FIXTURE_MARKER"},
        {"stage": []},
        {"elapsed_seconds": True},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": -1},
        {"elapsed_seconds": 901},
        {"elapsed_seconds": 10**1000},
        {"elapsed_seconds": "287"},
        {"strict_segment_index": True},
        {"strict_segment_index": 0},
        {"strict_segment_index": 33},
        {"strict_segment_index": "12"},
    ],
)
def test_progress_diagnostic_rejects_untrusted_or_stale_fields(change: dict) -> None:
    payload = {"job_id": "test-job", "stage": "sink-video", "elapsed_seconds": 287}
    payload.update(change)
    assert safe_self_test_progress(payload, job_id="test-job") == {"progress": "unavailable"}
