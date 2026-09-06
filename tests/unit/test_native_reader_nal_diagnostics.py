"""Passive parser-log observations are bounded diagnostics, never media proof."""

from __future__ import annotations

import io
import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test

from scripts.ci_node_onboarding_smoke import (
    _safe_failure_media,
    safe_self_test_progress,
    safe_strict_sink_reader_timings,
)

NAL_TYPES = {
    "sps": "7(SPS)",
    "pps": "8(PPS)",
    "idr": "5(IDR)",
    "non_idr": "1(Coded slice of a non-IDR picture)",
}


def nal_line(name="idr", pointer="0x123abc"):
    return f"[h264 @ {pointer}] nal_unit_type: {NAL_TYPES[name]}, nal_ref_idc: 3".encode()


@pytest.fixture
def reader(monkeypatch):
    state = load_self_test()
    scope = state["CaptureReaderProgress"].__init__.__globals__
    clock = SimpleNamespace(now=100.1254)
    monkeypatch.setitem(scope, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return state["CaptureReaderProgress"](started=100.0), clock, state


@pytest.mark.parametrize("name", NAL_TYPES)
@pytest.mark.parametrize("pointer", ["0x123abc", "000001AB12"])
def test_exact_linux_and_windows_nal_log_types_have_private_context(reader, name, pointer):
    diagnostic, clock, _state = reader
    diagnostic.observe_line(nal_line(name, pointer), progress=False)
    clock.now = 101.23456
    diagnostic.observe_line(nal_line(name, pointer), progress=False)
    value = diagnostic.snapshot()
    assert value["reader_nal_events"] == {
        name: {"count": 2, "first_seconds": 0.125, "last_seconds": 1.235}
    }
    assert not value["reader_input"] and not value["reader_output"] and value["reader_frames"] == 0
    assert pointer not in json.dumps(value) and "nal_ref_idc" not in json.dumps(value)


@pytest.mark.parametrize(
    "line",
    [
        b"[extract_extradata @ 0x123] nal_unit_type: 5(IDR), nal_ref_idc: 3",
        b"[h264 @ 0x123] nal_unit_type: 7(IDR), nal_ref_idc: 3",
        b"[h264 @ 0x123] nal_unit_type: 5(SPS), nal_ref_idc: 3",
        b"[h264 @ 0x123] nal_unit_type: 6(SEI), nal_ref_idc: 3",
        b"[h264 @ 0x123] nal_unit_type: 5(IDR), nal_ref_idc: 4",
        b"[h264 @ 0x123] nal_unit_type: 5(IDR), nal_ref_idc: -1",
        b"[h264 @ PRIVATE] nal_unit_type: 5(IDR), nal_ref_idc: 3",
        b"[h264 @ 0x12345678901234567] nal_unit_type: 5(IDR), nal_ref_idc: 3",
        b"PRIVATE " + nal_line(),
        nal_line() + b" rtmp://PRIVATE/key",
        b"[h264 @ 0x123] decode_slice_header error",
    ],
)
def test_lookalike_errors_and_non_allowlisted_nals_are_not_reflected(reader, line):
    diagnostic, _clock, _state = reader
    diagnostic.observe_line(line, progress=False)
    assert "reader_nal_events" not in diagnostic.snapshot()


def test_observations_stop_at_input_and_do_not_mutate_previous_snapshots(reader):
    diagnostic, clock, _state = reader
    diagnostic.observe_line(nal_line(), progress=True)
    assert "reader_nal_events" not in diagnostic.snapshot()
    diagnostic.observe_line(nal_line(), progress=False)
    previous = diagnostic.snapshot()
    clock.now = 101
    diagnostic.observe_line(nal_line(), progress=False)
    assert previous["reader_nal_events"]["idr"]["count"] == 1
    diagnostic.observe_line(b"Input #0, flv, from 'PRIVATE':", progress=False)
    before = diagnostic.snapshot()
    clock.now = 102
    diagnostic.observe_line(nal_line(), progress=False)
    assert diagnostic.snapshot() == before
    assert "PRIVATE" not in json.dumps(before)


def test_counts_saturate_but_last_observation_continues(reader):
    diagnostic, clock, _state = reader
    for _ in range(300):
        diagnostic.observe_line(nal_line(), progress=False)
    clock.now = 102
    diagnostic.observe_line(nal_line(), progress=False)
    assert diagnostic.snapshot()["reader_nal_events"]["idr"] == {
        "count": 255,
        "first_seconds": 0.125,
        "last_seconds": 2.0,
    }


@pytest.mark.parametrize("now", [99, 761, float("nan"), float("inf")])
def test_invalid_observation_times_do_not_create_evidence(reader, now):
    diagnostic, clock, _state = reader
    clock.now = now
    diagnostic.observe_line(nal_line(), progress=False)
    assert "reader_nal_events" not in diagnostic.snapshot()


@pytest.mark.parametrize("progress", [False, True])
def test_inspection_saturation_is_explicit_and_keeps_draining(reader, progress):
    diagnostic, _clock, _state = reader
    payload = b"PRIVATE" * 200_000 + b"\n" + nal_line() + b"\n"
    pipe = io.BytesIO(payload)
    diagnostic.drain(pipe, progress=progress)
    assert pipe.tell() == len(payload)
    result = diagnostic.snapshot()
    assert result["reader_inspection_limited"] is True
    assert "reader_nal_events" not in result and "PRIVATE" not in json.dumps(result)


def test_exact_inspection_budget_remains_observable_and_never_invents_false(reader):
    diagnostic, _clock, _state = reader
    line = nal_line() + b"\n"
    payload = b"X" * (1024 * 1024 - len(line) - 1) + b"\n" + line
    diagnostic.drain(io.BytesIO(payload), progress=False)
    result = diagnostic.snapshot()
    assert result["reader_nal_events"]["idr"]["count"] == 1
    assert "reader_inspection_limited" not in result


def media():
    return {
        "scope": "capture",
        "elapsed_seconds": 15.0,
        "log_ok": True,
        "markers": {},
        "first_seen": {},
        "reader_input": False,
        "reader_output": False,
        "reader_frames": 0,
        "reader_nal_events": {
            "idr": {"count": 2, "first_seconds": 1.12345, "last_seconds": 2.45678}
        },
        "reader_inspection_limited": True,
    }


def test_projection_is_copied_rounded_and_reused_for_successful_reader_reports():
    value = media()
    original = deepcopy(value)
    result = _safe_failure_media(value)
    assert result["reader_nal_events"]["idr"] == {
        "count": 2,
        "first_seconds": 1.123,
        "last_seconds": 2.457,
    }
    assert result["reader_inspection_limited"] is True
    assert value == original
    result["reader_nal_events"]["idr"]["count"] = 255
    assert value == original
    projected = safe_strict_sink_reader_timings([{"segment": 1, "diagnostic": value}])
    assert projected[0]["diagnostic"] == _safe_failure_media(value)


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(scope="crash"),
        lambda x: x.update(reader_inspection_limited=False),
        lambda x: x.update(reader_inspection_limited=1),
        lambda x: x.update(reader_inspection_limited="PRIVATE"),
        lambda x: x.update(reader_nal_events={}),
        lambda x: x.update(reader_nal_events="PRIVATE"),
        lambda x: x["reader_nal_events"].update(PRIVATE={}),
        lambda x: x["reader_nal_events"]["idr"].update(pointer="PRIVATE"),
        lambda x: x["reader_nal_events"]["idr"].update(count=True),
        lambda x: x["reader_nal_events"]["idr"].update(count=0),
        lambda x: x["reader_nal_events"]["idr"].update(count=256),
        lambda x: x["reader_nal_events"]["idr"].update(first_seconds=float("nan")),
        lambda x: x["reader_nal_events"]["idr"].update(last_seconds=float("inf")),
        lambda x: x["reader_nal_events"]["idr"].update(last_seconds=10**500),
        lambda x: x["reader_nal_events"]["idr"].update(last_seconds=True),
        lambda x: x["reader_nal_events"]["idr"].update(first_seconds=-1),
        lambda x: x["reader_nal_events"]["idr"].update(last_seconds=15.001),
        lambda x: x["reader_nal_events"]["idr"].update(first_seconds=3),
        lambda x: x["reader_nal_events"]["idr"].pop("count"),
        lambda x: x.update(reader_input=True, reader_input_seconds=1),
    ],
)
def test_unknown_cross_scope_unbounded_and_inconsistent_evidence_fails_closed(change):
    value = media()
    change(value)
    assert _safe_failure_media(value) is None
    assert safe_strict_sink_reader_timings([{"segment": 1, "diagnostic": value}]) is None


def test_legacy_diagnostics_without_nal_observations_stay_compatible():
    value = media()
    del value["reader_nal_events"], value["reader_inspection_limited"]
    assert _safe_failure_media(value) == value


def test_maximal_media_checkpoint_is_atomically_written_under_two_kib(monkeypatch, tmp_path):
    state = load_self_test()
    value = media()
    value.update(
        elapsed_seconds=659.999,
        markers=dict.fromkeys(state["MEDIA_DIAGNOSTIC_MARKERS"], 255),
        first_seen=dict.fromkeys({"attached", "active", "start-timeout", "child-exit"}, 659.999),
        supervisor_count=32,
        child_count=32,
        supervisor_seen_seconds=659.999,
        child_seen_seconds=659.999,
        reader_input=True,
        reader_output=True,
        reader_frames=10000,
        reader_input_seconds=659.999,
        reader_output_seconds=659.999,
        reader_first_frame_seconds=659.999,
        reader_last_frame_seconds=659.999,
        reader_probe_start_seconds=659.999,
        reader_probe_end_seconds=659.999,
        reader_media_seconds=659.999,
        source_clock={
            "state": "known",
            "packets": 1_000_000,
            "seconds": 659.999,
            "ratio": 4.0,
            "last_age": 659.999,
            "max_gap": 659.999,
            "discarded": 659.999,
            "rebases": 1_000_000,
        },
        reader_nal_events={
            name: {"count": 255, "first_seconds": 659.999, "last_seconds": 659.999}
            for name in NAL_TYPES
        },
    )
    progress = {
        "job_id": "f" * 36,
        "stage": "stuck-live",
        "elapsed_seconds": 659.999,
        "failure_lines": [20000] * 8,
        "failure_wait_seconds": 659.999,
        "failure_flags": dict.fromkeys(
            (
                "live",
                "normalized",
                "path_ready",
                "ingest_live",
                "metrics_ok",
                "core_alive",
                "ingest_one",
                "sink_one",
                "sink_growth",
                "state_ok",
                "ingest_match",
            ),
            False,
        ),
        "failure_media": value,
    }
    target = tmp_path / "progress.json"
    portable_os = SimpleNamespace(**vars(os))
    if os.name == "nt":
        portable_os.fchmod = lambda *_args: None
        portable_os.O_DIRECTORY = 0
        portable_os.open = lambda path, flags: (
            os.open(target, os.O_RDWR) if path == tmp_path else os.open(path, flags)
        )
    monkeypatch.setitem(state["atomic_json"].__globals__, "os", portable_os)
    state["atomic_json"](target, progress, compact=True)
    encoded = target.read_bytes()
    assert len(encoded) <= 2048
    assert json.loads(encoded) == progress
    assert safe_self_test_progress(progress, job_id=progress["job_id"])["failure_media"] == value
    assert list(tmp_path.iterdir()) == [target]
