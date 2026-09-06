"""Real subprocess regressions for the isolated self-test's probe deadline."""

from __future__ import annotations

import io
import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from test_ci_native_result_contract import native_result as native_result

from bootstrap_worker.errors import safe_failure
from bootstrap_worker.relay_installer import _SELF_TEST_STAGE_CODES
from scripts import ci_node_onboarding_smoke as smoke
from scripts.ci_node_onboarding_smoke import (
    SAFE_BOOTSTRAP_DIAGNOSTIC_CODES,
    print_self_test_progress,
    safe_self_test_progress,
    safe_strict_sink_reader_timings,
)
from scripts.ci_output_smoke import SmokeFailure

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
        "reset-live",
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
            "failure_lines": [5678, 1234],
            "stdout": "PRIVATE_FIXTURE_MARKER",
            "url": "rtmps://example.test/live#private-fixture",
        },
        job_id="test-job",
    ) == {
        "stage": "sink-video",
        "elapsed_seconds": 287.123,
        "strict_segment_index": 12,
        "failure_lines": [5678, 1234],
    }


def test_wait_failure_diagnostic_retains_only_boolean_predicate_evidence() -> None:
    flags = {
        "live": True,
        "normalized": True,
        "path_ready": True,
        "ingest_live": True,
        "metrics_ok": True,
        "core_alive": True,
        "ingest_one": True,
        "sink_one": False,
        "sink_growth": False,
        "state_ok": True,
        "ingest_match": True,
    }
    result = safe_self_test_progress(
        {
            "job_id": "job",
            "stage": "crash-live",
            "elapsed_seconds": 152.9,
            "failure_flags": flags,
            "failure_wait_seconds": 6.12345,
            "sink_ids": ["PRIVATE_FIXTURE_ID"],
        },
        job_id="job",
    )
    assert result == {
        "stage": "crash-live",
        "elapsed_seconds": 152.9,
        "failure_flags": flags,
        "failure_wait_seconds": 6.123,
    }


def media_diagnostic(scope: str = "crash") -> dict:
    value = {
        "scope": scope,
        "elapsed_seconds": 11.42123,
        "log_ok": True,
        "markers": {"attached": 1, "start-timeout": 1},
        "first_seen": {"attached": 5.12345, "start-timeout": 11.12345},
        "supervisor_count": 1,
        "child_count": 0,
        "supervisor_seen_seconds": 5.12345,
        "child_seen_seconds": 5.92345,
    }
    if scope == "capture":
        value.update(reader_input=True, reader_output=False, reader_frames=0)
    return value


@pytest.mark.parametrize("scope", ["crash", "capture"])
def test_failure_media_diagnostic_is_fixed_bounded_projection(scope: str) -> None:
    value = media_diagnostic(scope)
    result = safe_self_test_progress(
        {
            "job_id": "job",
            "stage": "crash-live",
            "elapsed_seconds": 152,
            "failure_media": value,
            "raw_stderr": "PRIVATE_FIXTURE_MARKER",
        },
        job_id="job",
    )
    assert result["failure_media"] == {
        **value,
        "elapsed_seconds": 11.421,
        "first_seen": {"attached": 5.123, "start-timeout": 11.123},
        "supervisor_seen_seconds": 5.123,
        "child_seen_seconds": 5.923,
    }
    assert "PRIVATE_FIXTURE_MARKER" not in json.dumps(result)
    assert result["failure_media"]["markers"] is not value["markers"]


READER_WALL_TIMINGS = (
    "reader_input_seconds",
    "reader_output_seconds",
    "reader_first_frame_seconds",
    "reader_last_frame_seconds",
)
READER_TIMINGS = (*READER_WALL_TIMINGS, "reader_media_seconds")


@pytest.mark.parametrize("with_end", [False, True])
def test_capture_probe_phase_timings_are_optional_and_safely_projected(with_end):
    value = {**media_diagnostic("capture"), "reader_probe_start_seconds": 0.12345}
    if with_end:
        value["reader_probe_end_seconds"] = 1.23456
    result = safe_self_test_progress(
        {"job_id": "job", "stage": "outage-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    )
    assert result["failure_media"]["reader_probe_start_seconds"] == 0.123
    if with_end:
        assert result["failure_media"]["reader_probe_end_seconds"] == 1.235
    assert value["reader_probe_start_seconds"] == 0.12345


@pytest.mark.parametrize("field", ["reader_probe_start_seconds", "reader_probe_end_seconds"])
@pytest.mark.parametrize(
    "invalid", [True, None, -0.001, 11.422, float("nan"), float("inf"), 10**1000, "PRIVATE"]
)
def test_capture_probe_phase_timings_reject_unsafe_values(field, invalid):
    value = {**media_diagnostic("capture"), "reader_probe_start_seconds": 0, field: invalid}
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "outage-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


@pytest.mark.parametrize(
    "fields",
    [
        {"reader_probe_end_seconds": 1.0},
        {"reader_probe_start_seconds": 2.0, "reader_probe_end_seconds": 1.0},
        {"reader_probe_start_seconds": 2.0, "reader_input_seconds": 1.0},
        {
            "reader_probe_start_seconds": 0.0,
            "reader_probe_end_seconds": 2.0,
            "reader_input_seconds": 1.0,
        },
        {"scope": "crash", "reader_probe_start_seconds": 0.0},
        {"reader_probe_start_seconds": 0.0, "probe_context": "PRIVATE"},
    ],
)
def test_capture_probe_projection_rejects_missing_or_reordered_phase_evidence(fields):
    value = {**media_diagnostic("capture"), **fields}
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "outage-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


def test_capture_reader_timing_projection_distinguishes_acquisition_from_media_time() -> None:
    value = {
        **media_diagnostic("capture"),
        "reader_output": True,
        "reader_frames": 71,
        "reader_input_seconds": 7.12345,
        "reader_output_seconds": 8.12345,
        "reader_first_frame_seconds": 8.23456,
        "reader_last_frame_seconds": 11.42123,
        # Buffered media time can exceed this capture's elapsed wall time.
        "reader_media_seconds": 20.98765,
    }
    result = safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    )
    assert {name: result["failure_media"][name] for name in READER_TIMINGS} == {
        "reader_input_seconds": 7.123,
        "reader_output_seconds": 8.123,
        "reader_first_frame_seconds": 8.235,
        "reader_last_frame_seconds": 11.421,
        "reader_media_seconds": 20.988,
    }
    assert value["reader_first_frame_seconds"] == 8.23456


@pytest.mark.parametrize("field", READER_TIMINGS)
def test_capture_reader_timing_fields_are_individually_optional(field: str) -> None:
    value = {
        **media_diagnostic("capture"),
        "reader_output": True,
        "reader_frames": 1,
        field: 0.0,
    }
    result = safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    )
    assert result["failure_media"][field] == 0.0
    assert not (set(READER_TIMINGS) - {field}) & result["failure_media"].keys()


@pytest.mark.parametrize("field", READER_WALL_TIMINGS)
@pytest.mark.parametrize(
    "invalid", [True, None, -0.001, 11.422, float("nan"), float("inf"), 10**1000, "PRIVATE_TIMING"]
)
def test_capture_reader_wall_timings_reject_unsafe_values(field: str, invalid) -> None:
    value = {
        **media_diagnostic("capture"),
        "reader_output": True,
        "reader_frames": 1,
        field: invalid,
    }
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


@pytest.mark.parametrize(
    ("field", "evidence", "unobserved"),
    [
        ("reader_input_seconds", "reader_input", False),
        ("reader_output_seconds", "reader_output", False),
        ("reader_first_frame_seconds", "reader_frames", 0),
        ("reader_last_frame_seconds", "reader_frames", 0),
    ],
)
def test_capture_reader_timing_requires_its_observed_milestone(
    field: str, evidence: str, unobserved
) -> None:
    value = {**media_diagnostic("capture"), field: 1.0, evidence: unobserved}
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


def test_capture_reader_frame_timing_cannot_regress() -> None:
    value = {
        **media_diagnostic("capture"),
        "reader_frames": 71,
        "reader_first_frame_seconds": 2.0,
        "reader_last_frame_seconds": 1.0,
    }
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


@pytest.mark.parametrize("seconds", [0, 1.23456, 660])
def test_capture_reader_media_time_has_its_own_bound(seconds: float) -> None:
    value = {**media_diagnostic("capture"), "reader_media_seconds": seconds}
    result = safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    )
    assert result["failure_media"]["reader_media_seconds"] == round(seconds, 3)


@pytest.mark.parametrize(
    "invalid", [True, None, -0.001, 660.001, float("nan"), float("inf"), 10**1000, "PRIVATE_TIMING"]
)
def test_capture_reader_media_time_rejects_unsafe_values(invalid) -> None:
    value = {**media_diagnostic("capture"), "reader_media_seconds": invalid}
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


@pytest.mark.parametrize("field", READER_TIMINGS)
def test_reader_timing_cannot_appear_in_crash_scope(field: str) -> None:
    value = {**media_diagnostic("crash"), field: 1.0}
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


def test_capture_reader_unknown_timing_field_still_rejects_entire_diagnostic() -> None:
    value = {**media_diagnostic("capture"), "reader_private_seconds": 1.0}
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "crash-live", "elapsed_seconds": 152, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


def strict_reader_timings(count: int = 13) -> list[dict]:
    return [
        {
            "segment": index,
            "diagnostic": {
                **media_diagnostic("capture"),
                "reader_output": True,
                "reader_frames": 90,
                "reader_probe_start_seconds": 0.12345,
                "reader_probe_end_seconds": 1.02345,
                "reader_input_seconds": 1.12345,
                "reader_output_seconds": 2.12345,
                "reader_first_frame_seconds": 2.23456,
                "reader_last_frame_seconds": 5.23456,
                "reader_media_seconds": 20.98765,
            },
        }
        for index in range(1, count + 1)
    ]


@pytest.mark.parametrize("count", [1, 13, 32])
def test_success_reader_timings_project_bounded_capture_rows(count: int) -> None:
    value = strict_reader_timings(count)
    result = safe_strict_sink_reader_timings(value)
    assert result is not None and len(result) == count
    assert result[0]["diagnostic"]["reader_probe_start_seconds"] == 0.123
    assert result[0]["diagnostic"]["reader_probe_end_seconds"] == 1.023
    assert result[0]["diagnostic"]["reader_first_frame_seconds"] == 2.235
    assert result[0]["diagnostic"]["reader_media_seconds"] == 20.988
    assert result[0]["diagnostic"] is not value[0]["diagnostic"]
    assert value[0]["diagnostic"]["reader_first_frame_seconds"] == 2.23456


@pytest.mark.parametrize("invalid", [None, {}, "PRIVATE_FIXTURE", [], strict_reader_timings(33)])
def test_success_reader_timings_reject_unsafe_container(invalid) -> None:
    assert safe_strict_sink_reader_timings(invalid) is None


@pytest.mark.parametrize(
    "change",
    [
        {"segment": True},
        {"segment": 0},
        {"segment": 33},
        {"segment": "PRIVATE_FIXTURE"},
        {"segment": []},
        {"raw_stderr": "PRIVATE_FIXTURE"},
        {"diagnostic": media_diagnostic("crash")},
        {"diagnostic": {**media_diagnostic("capture"), "stderr": "PRIVATE_FIXTURE"}},
        {"diagnostic": {**media_diagnostic("capture"), "reader_input_seconds": float("nan")}},
    ],
)
def test_success_reader_timings_reject_entire_list_on_any_unsafe_row(change: dict) -> None:
    value = strict_reader_timings()
    value[-1].update(change)
    assert safe_strict_sink_reader_timings(value) is None


def test_success_reader_timings_require_complete_unique_rows() -> None:
    assert safe_strict_sink_reader_timings([{"segment": 1}]) is None
    assert safe_strict_sink_reader_timings([{"diagnostic": media_diagnostic("capture")}]) is None
    assert safe_strict_sink_reader_timings([None]) is None
    value = strict_reader_timings()
    value[-1]["segment"] = 1
    assert safe_strict_sink_reader_timings(value) is None


@pytest.mark.parametrize("count", [1, 12, 13, 14, 32])
def test_native_result_optional_reader_timings_must_match_capture_count(
    native_result: dict, count: int
) -> None:
    native_result["strict_sink_reader_timings"] = strict_reader_timings(count)
    assert smoke.native_self_test_result_failure(native_result) == (
        None if count == 13 else "native_result_media"
    )


@pytest.mark.parametrize("invalid", [None, [], "PRIVATE_FIXTURE", [{"segment": 1}]])
def test_native_result_rejects_present_but_invalid_reader_timings(
    native_result: dict, invalid
) -> None:
    native_result["strict_sink_reader_timings"] = invalid
    assert smoke.native_self_test_result_failure(native_result) == "native_result_media"


@pytest.mark.parametrize("failure", [None, "header", "timings"])
def test_remote_result_probe_projects_only_successful_safe_reader_timings(
    native_result: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str | None,
) -> None:
    native_result["strict_sink_reader_timings"] = strict_reader_timings()
    native_result["raw_report"] = "PRIVATE_FIXTURE_REPORT"
    if failure == "header":
        native_result["status"] = "PRIVATE_FIXTURE_STATUS"
    elif failure == "timings":
        native_result["strict_sink_reader_timings"][-1]["diagnostic"]["stderr"] = "PRIVATE_FIXTURE"
    raw = json.dumps(native_result).encode()

    class Reader(io.BytesIO):
        def fileno(self) -> int:
            return 17

    monkeypatch.setattr(os, "open", lambda *_args: 17)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0x20000, raising=False)
    monkeypatch.setattr(os, "O_NONBLOCK", 0x800, raising=False)
    monkeypatch.setattr(os, "fdopen", lambda *_args: Reader(raw))
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1, st_size=len(raw)
        ),
    )
    exec(  # noqa: S102 - repository-owned probe with mocked protected report
        compile(smoke.native_result_probe_source(), "<fixed-native-result-probe>", "exec"), {}
    )
    output = capsys.readouterr().out
    assert "PRIVATE_FIXTURE" not in output
    lines = output.splitlines()
    if failure:
        assert lines == ["native_result_header" if failure == "header" else "native_result_media"]
    else:
        assert lines[0] == "NATIVE_SELF_TEST_RESULT_OK"
        assert json.loads(lines[1]) == {
            "strict_sink_reader_timings": safe_strict_sink_reader_timings(strict_reader_timings())
        }
        assert len(lines) == 2


@pytest.mark.parametrize(
    "change",
    [None, "unknown-envelope", "unknown-row", "bad-count", "extra-line", "raw-line", "bad-json"],
)
def test_host_lifecycle_revalidates_timing_projection_before_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str | None,
) -> None:
    payload = {"strict_sink_reader_timings": strict_reader_timings()}
    if change == "unknown-envelope":
        payload["raw_stderr"] = "PRIVATE_FIXTURE"
    elif change == "unknown-row":
        payload["strict_sink_reader_timings"][-1]["raw_stderr"] = "PRIVATE_FIXTURE"
    elif change == "bad-count":
        payload["strict_sink_reader_timings"].pop()
    lines = ["NATIVE_SELF_TEST_RESULT_OK", json.dumps(payload)]
    if change == "extra-line":
        lines.append("PRIVATE_FIXTURE")
    elif change == "raw-line":
        lines[1] = "PRIVATE_FIXTURE"
    elif change == "bad-json":
        lines[1] = '{"strict_sink_reader_timings": '
    stages = []

    def fake_compose(*args, **kwargs):
        if "python3" in args:
            assert kwargs["max_capture_bytes"] == 64 * 1024
            output = ("\n".join(lines) + "\n").encode()
        elif "REMOTE_NATIVE_LIFECYCLE_OK" in args[-1]:
            output = b"REMOTE_NATIVE_LIFECYCLE_OK\n"
        else:
            output = b""
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(smoke, "compose", fake_compose)
    monkeypatch.setattr(smoke, "set_smoke_stage", stages.append)
    if change:
        with pytest.raises(SmokeFailure, match="^native self-test reader timings are invalid$"):
            smoke.verify_remote_lifecycle()
        assert stages[-1] == "native_result_media"
        assert capsys.readouterr().out == ""
    else:
        smoke.verify_remote_lifecycle()
        output = capsys.readouterr().out.splitlines()
        prefix = "Native strict sink reader diagnostic: "
        assert all(line.startswith(prefix) for line in output)
        assert [
            json.loads(line[len(prefix) :]) for line in output
        ] == safe_strict_sink_reader_timings(strict_reader_timings())


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "PRIVATE_FIXTURE"},
        {"scope": []},
        {"scope": None},
        {"log_ok": 1},
        {"log_ok": "true"},
        {"elapsed_seconds": True},
        {"elapsed_seconds": -1},
        {"elapsed_seconds": 661},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": 10**1000},
        {"elapsed_seconds": "PRIVATE_FIXTURE"},
        {"markers": {"PRIVATE_FIXTURE": 1}},
        {"markers": {"attached": True}},
        {"markers": {"attached": 0}},
        {"markers": {"attached": 256}},
        {"markers": {"attached": "PRIVATE_FIXTURE"}},
        {"markers": []},
        {"first_seen": {"PRIVATE_FIXTURE": 1}},
        {"first_seen": {"active": 1}},
        {"first_seen": {"attached": True}},
        {"first_seen": {"attached": float("nan")}},
        {"first_seen": {"attached": 12}},
        {"first_seen": {"attached": -1}},
        {"first_seen": []},
        {"supervisor_count": True},
        {"child_count": 33},
        {"child_count": -1},
        {"child_count": "PRIVATE_FIXTURE"},
        {"supervisor_seen_seconds": float("inf")},
        {"child_seen_seconds": 12},
        {"child_seen_seconds": True},
        {"child_seen_seconds": None},
        {"reader_input": True},
        {"stderr": "PRIVATE_FIXTURE"},
    ],
)
def test_failure_media_diagnostic_rejects_every_unknown_or_unsafe_field(change: dict) -> None:
    value = media_diagnostic()
    value.update(change)
    payload = {
        "job_id": "job",
        "stage": "crash-live",
        "elapsed_seconds": 152,
        "failure_media": value,
    }
    assert safe_self_test_progress(payload, job_id="job") == {"progress": "unavailable"}


@pytest.mark.parametrize(
    "change",
    [
        {"reader_input": 1},
        {"reader_output": "PRIVATE_FIXTURE"},
        {"reader_frames": True},
        {"reader_frames": -1},
        {"reader_frames": 10001},
        {"reader_frames": 1.5},
        {"reader_frames": "PRIVATE_FIXTURE"},
    ],
)
def test_capture_diagnostic_reader_values_are_not_free_text(change: dict) -> None:
    value = media_diagnostic("capture")
    value.update(change)
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "stall-live", "elapsed_seconds": 127, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


@pytest.mark.parametrize(
    "field",
    [
        "scope",
        "elapsed_seconds",
        "log_ok",
        "markers",
        "first_seen",
        "reader_input",
        "reader_output",
        "reader_frames",
    ],
)
def test_capture_diagnostic_requires_its_complete_schema(field: str) -> None:
    value = media_diagnostic("capture")
    del value[field]
    assert safe_self_test_progress(
        {"job_id": "job", "stage": "stall-live", "elapsed_seconds": 127, "failure_media": value},
        job_id="job",
    ) == {"progress": "unavailable"}


def test_protected_progress_reader_keeps_existing_file_guards_and_bounded_projection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    job_id = "11111111-1111-4111-8111-111111111111"

    def compose(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "job_id": job_id,
                    "stage": "crash-live",
                    "elapsed_seconds": 152,
                    "failure_media": {**media_diagnostic(), "stderr": "PRIVATE_FIXTURE_MARKER"},
                }
            ),
            "",
        )

    monkeypatch.setitem(print_self_test_progress.__globals__, "compose", compose)
    print_self_test_progress(job_id)
    output = capsys.readouterr().out
    assert output.strip() == 'Self-test progress diagnostic: {"progress": "unavailable"}'
    assert "PRIVATE_FIXTURE_MARKER" not in output
    args, kwargs = calls[0]
    script = args[-1]
    assert kwargs == {"max_capture_bytes": 4096}
    for guard in (
        "stat.S_ISREG(before.st_mode)",
        "before.st_uid != 0",
        "stat.S_IMODE(before.st_mode) != 0o600",
        "before.st_nlink != 1",
        "0 < before.st_size <= 2048",
        "os.O_NOFOLLOW | os.O_NONBLOCK",
        "before != after",
        "handle.read(2049)",
        "len(raw) > 2048",
        "-5 <= time.time() - before.st_mtime <= 960",
    ):
        assert guard in script


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
        {"failure_lines": []},
        {"failure_lines": [False]},
        {"failure_lines": [1] * 9},
        {"failure_lines": [20001]},
        {"failure_lines": [0]},
        {"failure_lines": ["PRIVATE_FIXTURE"]},
        {"failure_lines": "PRIVATE_FIXTURE"},
        {"failure_flags": {"live": "PRIVATE_FIXTURE"}},
        {"failure_flags": {"private_url": True}},
        {"failure_flags": {"live": 1}},
        {"failure_flags": []},
        {"failure_flags": {}},
        {"failure_wait_seconds": True},
        {"failure_wait_seconds": float("nan")},
        {"failure_wait_seconds": float("inf")},
        {"failure_wait_seconds": 10**1000},
        {"failure_wait_seconds": -1},
        {"failure_wait_seconds": 661},
    ],
)
def test_progress_diagnostic_rejects_untrusted_or_stale_fields(change: dict) -> None:
    payload = {"job_id": "test-job", "stage": "sink-video", "elapsed_seconds": 287}
    payload.update(change)
    assert safe_self_test_progress(payload, job_id="test-job") == {"progress": "unavailable"}


def test_failure_checkpoint_contains_source_line_numbers_not_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = load_probe()
    persist = namespace["persist_self_test_failure_progress"]
    state = persist.__globals__
    checkpoint = {"job_id": "test-job", "stage": "sink-video", "elapsed_seconds": 12.0}
    writes = []
    monkeypatch.setitem(state, "SELF_TEST_STAGE_FILE", "configured")
    monkeypatch.setitem(state, "SELF_TEST_LAST_PROGRESS", checkpoint)
    monkeypatch.setitem(state, "mark_self_test_stage", lambda *args, **kwargs: None)
    monkeypatch.setitem(state, "atomic_json", lambda path, value: writes.append(value))
    # Give only the inner test-generated frame the exact self-test filename.
    # The outer pytest frame and exception text must not be serialized.
    code = compile("raise RuntimeError('PRIVATE_TRACEBACK_FIXTURE')", str(SELF_TEST), "exec")
    try:
        exec(code, {})  # noqa: S102 - fixed synthetic exception, no user input
    except RuntimeError as error:
        persist(error)
    assert writes == [{**checkpoint, "failure_lines": [1]}]
    assert "PRIVATE_TRACEBACK_FIXTURE" not in repr(writes)
    assert str(SELF_TEST) not in repr(writes)
