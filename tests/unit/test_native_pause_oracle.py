"""Pause controls stop new sends, not existing media buffers or the recovery clock."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from test_moblin_relay_bundle import load_self_test
from test_native_observer_wait import bind_slate_wait, flow_sample, slate_sample


def paused_wait(samples, *, cutoff=108.0, now=100.2, baseline=None):
    wait, namespace, calls = bind_slate_wait(samples, now=now)
    return (
        lambda: wait(
            "fixture",
            100.0,
            ["PRIVATE_INGEST_ID"],
            pause_deadline=cutoff,
            pause_baseline=baseline or flow_sample(99.9),
        ),
        namespace,
        calls,
    )


@pytest.mark.parametrize("cutoff", [108.0, 109.0])
def test_buffered_tail_retains_post_growth_bound_and_fixed_total_cutoff(cutoff):
    samples = [
        flow_sample(100.3),
        flow_sample(101.9, normalized_bytes=80000, finished=102.0),
        flow_sample(104.5, normalized_bytes=80000, finished=104.6),
        slate_sample(104.9, normalized_ids=[], normalized_bytes=None),
    ]
    call, namespace, calls = paused_wait(samples, cutoff=cutoff)
    assert call() is samples[-1]
    assert calls == [(pytest.approx(cutoff - 100.2), 100.0)]
    assert namespace["LIVE_TO_SLATE_DEADLINE_SECONDS"] == 4.5
    assert namespace["PERSISTENT_INPUT_STALL_RESET_TIMEOUT_SECONDS"] == 9
    assert namespace["SRT_IDLE_LOWER_BOUND_SECONDS"] == 8


@pytest.mark.parametrize("cutoff", [108.0, 109.0])
def test_new_growth_cannot_extend_total_pause_cutoff(cutoff):
    call, namespace, calls = paused_wait(
        [
            flow_sample(cutoff - 0.1, normalized_bytes=999999, finished=cutoff - 0.05),
            slate_sample(cutoff - 0.01, finished=cutoff),
        ],
        cutoff=cutoff,
    )
    with pytest.raises(namespace["TestFailure"], match="expired observation"):
        call()
    assert calls[0][0] == pytest.approx(cutoff - 100.2)


@pytest.mark.parametrize("slate", [False, True])
def test_flat_output_cannot_outlive_original_post_growth_deadline(slate):
    latest = slate_sample(104.51) if slate else flow_sample(104.51)
    call, namespace, _ = paused_wait([flow_sample(100.3), latest], cutoff=109)
    with pytest.raises(namespace["TestFailure"], match="post-growth deadline"):
        call()


def test_first_completed_late_sample_is_not_accepted_by_an_early_start_time():
    call, namespace, _ = paused_wait([slate_sample(104.4, finished=104.501)])
    with pytest.raises(namespace["TestFailure"], match="post-growth deadline"):
        call()


@pytest.mark.parametrize(
    "updates",
    [
        {"ingest_ids": ["PRIVATE_REPLACEMENT"]},
        {"ingest_ids": []},
        {"ingest_live": False},
        {"normalized_ids": ["PRIVATE_REPLACEMENT"]},
        {"normalized_bytes": 1},
        {"normalized_bytes": None},
        {"normalized_bytes": True},
        {"normalized_bytes": 2**63},
    ],
)
def test_source_output_identity_and_counter_fail_closed(updates):
    call, namespace, _ = paused_wait([flow_sample(100.3, **updates), slate_sample(101)])
    with pytest.raises(namespace["TestFailure"]) as caught:
        call()
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "updates",
    [
        {"t": True},
        {"finished": None},
        {"finished": float("nan")},
        {"finished": float("inf")},
        {"finished": 100.1},
        {"dut_metrics_ok": False},
        {"sink_metrics_ok": False},
    ],
)
def test_actual_pause_evidence_rejects_invalid_or_unknown_observations(updates):
    namespace = load_self_test()
    proof = namespace["PauseSLATEEvidence"](100.0, 108.0, flow_sample(99.9))
    with pytest.raises(namespace["TestFailure"]):
        proof.observe(flow_sample(100.3, **updates))


def test_duplicate_samples_do_not_create_new_growth_or_slide_deadlines():
    namespace = load_self_test()
    proof = namespace["PauseSLATEEvidence"](100.0, 108.0, flow_sample(99.9))
    same = flow_sample(100.3)
    for _ in range(5):
        proof.observe(same)
    assert proof.last_growth == 100.0 and proof.deadline == 108.0
    with pytest.raises(namespace["TestFailure"], match="post-growth"):
        proof.observe(slate_sample(104.6))


@pytest.mark.parametrize("cutoff", [100.0, 109.001, float("nan"), True, "private"])
def test_pause_total_budget_cannot_exceed_existing_reset_limit(cutoff):
    namespace = load_self_test()
    with pytest.raises(namespace["TestFailure"]):
        namespace["PauseSLATEEvidence"](100.0, cutoff, flow_sample(99.9))


def test_only_two_control_pauses_use_the_buffered_media_oracle():
    namespace = load_self_test()
    source = Path(namespace["__file__"]).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "wait_slate_with_live_srt"
        and any(key.arg == "pause_deadline" for key in node.keywords)
    ]
    assert len(calls) == 2
    deadlines = {
        ast.unparse(key.value)
        for call in calls
        for key in call.keywords
        if key.arg == "pause_deadline"
    }
    assert deadlines == {
        "same_session_started + SRT_IDLE_LOWER_BOUND_SECONDS",
        "recovery_deadline",
    }
    assert (
        "recovery_deadline = stuck_started + PERSISTENT_INPUT_STALL_RESET_TIMEOUT_SECONDS" in source
    )
    assert "if reset_completed > recovery_deadline:" in source
    assert "if time.monotonic() - same_session_started >= SRT_IDLE_LOWER_BOUND_SECONDS:" in source
