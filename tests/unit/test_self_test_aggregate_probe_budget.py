"""The long full-test recording gets a bounded offline budget, never live gates."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_moblin_relay_bundle import SELF_TEST, load_self_test


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (0.001, 60),
        (12, 60),
        (72.0, 60),
        (180, 60),
        (180.001, 61),
        (300, 100),
        (600.1, 201),
        (897, 299),
        (897.001, 300),
        (900, 300),
        (936.021, 300),
        (3600, 300),
        (10**400, 300),
    ],
)
def test_full_aggregate_budget_scales_only_with_valid_duration(duration, expected):
    budget = load_self_test()["aggregate_probe_timeout_seconds"]
    assert budget(duration, quick=False) == expected


@pytest.mark.parametrize(
    "duration",
    [None, True, False, "PRIVATE_DURATION", 0, -1, float("nan"), float("inf"), -float("inf")],
)
def test_invalid_full_duration_fails_closed_without_echoing_metadata(duration):
    namespace = load_self_test()
    with pytest.raises(
        namespace["TestFailure"],
        match="^invalid aggregate capture duration for offline probe budget$",
    ):
        namespace["aggregate_probe_timeout_seconds"](duration, quick=False)


@pytest.mark.parametrize(
    "duration", [None, "PRIVATE_DURATION", float("nan"), 0, 12, 936.021, 10**400]
)
def test_quick_budget_remains_sixty_even_without_duration(duration):
    assert load_self_test()["aggregate_probe_timeout_seconds"](duration, quick=True) == 60


@pytest.mark.parametrize("quick", [False, True])
def test_main_uses_capture_duration_and_records_selected_budget_only_for_full(quick):
    tree = ast.parse(SELF_TEST.read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    select = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "aggregate_probe_timeout"
            for target in node.targets
        )
    )
    save = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "not args.quick"
        and "result['aggregate_probe_timeout_seconds']" in ast.unparse(node)
    )
    namespace = load_self_test()
    namespace.update(
        normalized_signature={"duration_seconds": 936.021},
        args=SimpleNamespace(quick=quick),
        result={},
    )
    exec(  # noqa: S102 - fixed, repository-owned setup AST
        compile(ast.Module(body=[select, save], type_ignores=[]), str(SELF_TEST), "exec"),
        namespace,
    )
    assert namespace["aggregate_probe_timeout"] == (60 if quick else 300)
    assert namespace["result"] == ({} if quick else {"aggregate_probe_timeout_seconds": 300})


@pytest.mark.parametrize("helper", ["video_gop_signature", "analyze_decoded_video_frames"])
@pytest.mark.parametrize("timeout", [None, 60, 300])
def test_video_helpers_keep_default_or_explicit_deadline_and_complete_frame_data(helper, timeout):
    namespace = load_self_test()
    gop = [{"key_frame": int(index % 60 == 0)} for index in range(120)]
    decoded = "".join(
        f"width=1080|height=1920|pix_fmt=yuv420p|pts_time={index / 30:.6f}|"
        f"best_effort_timestamp_time={index / 30:.6f}\n"
        for index in range(120)
    )
    stdout = json.dumps({"frames": gop}) if helper == "video_gop_signature" else decoded
    kwargs = {} if timeout is None else {"timeout": timeout}
    with patch(
        "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    ) as run:
        actual = namespace[helper](Path("isolated-aggregate.flv"), **kwargs)
    command = run.call_args.args[0]
    assert run.call_args.kwargs["timeout"] == (60 if timeout is None else timeout)
    assert command[-1] == "isolated-aggregate.flv"
    assert "-show_frames" in command
    assert "-read_intervals" not in command
    assert "-skip_frame" not in command
    assert actual["frame_count"] == 120
    if helper == "video_gop_signature":
        assert actual == {
            "frame_count": 120,
            "keyframe_indexes": [0, 60],
            "keyframe_count": 2,
            "interval_frames": [60],
        }
    else:
        assert actual["presentation_timestamp_count"] == 120
        assert actual["dimensions"] == {"1080x1920": 120}
        assert actual["decode_error_flags"] == {}


@pytest.mark.parametrize("helper", ["video_gop_signature", "analyze_decoded_video_frames"])
@pytest.mark.parametrize("timeout", [60, 300])
def test_video_probe_timeout_remains_fatal_and_never_uses_partial_frames(helper, timeout):
    namespace = load_self_test()
    error = subprocess.TimeoutExpired(
        ["PRIVATE_COMMAND"], timeout, output='{"frames":[{"key_frame":1}]}', stderr="PRIVATE_STDERR"
    )
    with (
        patch("subprocess.run", side_effect=error),
        pytest.raises(namespace["TestFailure"], match="^local media probe timed out$"),
    ):
        namespace[helper](Path("PRIVATE_PATH"), timeout=timeout)


def test_scaled_budget_has_exactly_three_aggregate_consumers_and_no_live_consumers():
    tree = ast.parse(SELF_TEST.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    selected = []
    for name, function in functions.items():
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if any(
                isinstance(keyword.value, ast.Name)
                and keyword.value.id == "aggregate_probe_timeout"
                for keyword in node.keywords
            ):
                assert [(keyword.arg, ast.unparse(keyword.value)) for keyword in node.keywords] == [
                    ("timeout", "aggregate_probe_timeout")
                ]
                selected.append((name, ast.unparse(node.func)))
    assert sorted(selected) == [
        ("main", "analyze_decoded_video_frames"),
        ("main", "run_probe"),
        ("main", "video_gop_signature"),
    ]
    for helper in ("video_gop_signature", "analyze_decoded_video_frames"):
        definition = functions[helper]
        assert [arg.arg for arg in definition.args.kwonlyargs] == ["timeout"]
        assert [ast.literal_eval(value) for value in definition.args.kw_defaults] == [60]
        for name, function in functions.items():
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == helper
                ):
                    assert not node.keywords or name == "main"
    for name, expected in (
        ("stream_signature", 60),
        ("analyze_decoded_audio_timestamps", 60),
        ("analyze_timestamps", 30),
    ):
        calls = [
            node
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_probe"
        ]
        assert len(calls) == 1
        assert (
            next(
                ast.literal_eval(keyword.value)
                for keyword in calls[0].keywords
                if keyword.arg == "timeout"
            )
            == expected
        )
    namespace = load_self_test()
    assert namespace["STRICT_SINK_REQUIRED_VIDEO_FRAMES"] == 90
    assert namespace["CAPTURE_NO_GROWTH_LIMIT_SECONDS"] == 3.0
    strict = functions["validate_final_sink_media_segment"]
    decode = next(
        node
        for node in ast.walk(strict)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "decoded" for target in node.targets)
    )
    assert (
        next(
            ast.literal_eval(keyword.value)
            for keyword in decode.value.keywords
            if keyword.arg == "timeout"
        )
        == 15
    )
