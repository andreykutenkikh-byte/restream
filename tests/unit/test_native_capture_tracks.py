"""Failure-only FLV framing evidence, with actual bounded descriptor reads."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_moblin_relay_bundle import load_self_test

HEADER = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00" * 4


def tag(kind, payload, dts=0):
    size = len(payload)
    return (
        bytes([kind])
        + size.to_bytes(3, "big")
        + (dts & 0xFFFFFF).to_bytes(3, "big")
        + bytes([dts >> 24])
        + b"\x00" * 3
        + payload
        + (size + 11).to_bytes(4, "big")
    )


def video(dts=0, composition=0, *, packet_type=1, body=b"video"):
    return tag(
        9, b"\x17" + bytes([packet_type]) + composition.to_bytes(3, "big", signed=True) + body, dts
    )


def audio(dts=0, *, packet_type=1, body=b"audio"):
    return tag(8, b"\xaf" + bytes([packet_type]) + body, dts)


def sample(age, size, **changes):
    return {"t": 100 + age, "capture_ok": True, "capture_size": size, **changes}


@pytest.fixture
def probe(monkeypatch, tmp_path):
    namespace = load_self_test()
    function = namespace["capture_track_failure_summary"]
    scope = function.__globals__
    path = tmp_path / "capture.flv"
    opened, closed, reads = [], [], []

    def metadata(value, *, directory=False):
        return SimpleNamespace(
            st_mode=(stat.S_IFDIR | 0o700) if directory else stat.S_IFREG | 0o600,
            st_uid=0,
            st_nlink=1,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_size=value.st_size,
        )

    def open_file(target, flags):
        descriptor = os.open(target, flags)
        opened.append((descriptor, flags))
        return descriptor

    def close_file(descriptor):
        closed.append(descriptor)
        os.close(descriptor)

    def read_file(descriptor, length):
        reads.append(length)
        return os.read(descriptor, length)

    proxy = SimpleNamespace(
        O_RDONLY=os.O_RDONLY,
        O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 0),
        O_NONBLOCK=getattr(os, "O_NONBLOCK", 0),
        O_CLOEXEC=getattr(os, "O_CLOEXEC", 0),
        O_BINARY=getattr(os, "O_BINARY", 0),
        SEEK_SET=os.SEEK_SET,
        lstat=lambda target: metadata(os.lstat(target), directory=target == path.parent),
        fstat=lambda descriptor: metadata(os.fstat(descriptor)),
        open=open_file,
        close=close_file,
        read=read_file,
        lseek=os.lseek,
    )
    monkeypatch.setitem(scope, "os", proxy)

    def invoke(blob, samples=None):
        path.write_bytes(blob)
        observations = samples or [sample(0.1, 13), sample(0.9, len(blob))]
        result = function(path, observations, 100.0, 101.0)
        assert sorted(fd for fd, _ in opened) == sorted(closed)
        return result

    return SimpleNamespace(
        invoke=invoke,
        function=function,
        path=path,
        scope=scope,
        os=proxy,
        opened=opened,
        closed=closed,
        reads=reads,
    )


def test_tracks_count_only_complete_avc_aac_payloads_and_exclude_baseline(probe):
    baseline = HEADER + video(0) + audio(0)
    first = baseline + video(1000, -10) + audio(1000)
    ignored = video(packet_type=0) + video(packet_type=2, body=b"") + audio(packet_type=0)
    last = (
        first
        + ignored
        + tag(18, b"PRIVATE rtmp://private.invalid/key")
        + video(1030, 10)
        + audio(1040)
    )
    result = probe.invoke(
        last, [sample(0.1, len(baseline)), sample(0.4, len(first)), sample(0.8, len(last))]
    )
    assert result == {
        "state": "known",
        "partial_tail": False,
        "sample_window_seconds": [0.1, 0.8],
        "video": {"packets": 2, "last_observed_seconds": 0.8, "pts_span_seconds": 0.05},
        "audio": {"packets": 2, "last_observed_seconds": 0.8, "pts_span_seconds": 0.04},
    }
    assert "PRIVATE" not in json.dumps(result) and "rtmp" not in json.dumps(result)


def test_one_packet_span_zero_and_absent_track_are_explicit(probe):
    assert probe.invoke(HEADER + audio(123)) == {
        "state": "known",
        "partial_tail": False,
        "sample_window_seconds": [0.1, 0.9],
        "video": {"packets": 0},
        "audio": {"packets": 1, "last_observed_seconds": 0.9, "pts_span_seconds": 0.0},
    }


def test_tag_end_maps_to_first_observed_file_size_not_later_samples(probe):
    blob = HEADER + video(20)
    result = probe.invoke(blob, [sample(0.1, 13), sample(0.3, len(blob)), sample(0.9, len(blob))])
    assert result["video"]["last_observed_seconds"] == 0.3


def test_reads_skip_payload_and_file_bytes_beyond_last_sample(probe):
    prefix = HEADER + video(body=b"PRIVATE" * 100000)
    blob = prefix + b"not-yet-sampled"
    result = probe.invoke(blob, [sample(0.1, 13), sample(0.9, len(prefix))])
    assert result["state"] == "known" and result["video"]["packets"] == 1
    assert max(probe.reads) <= 11 and sum(probe.reads) == 33


@pytest.mark.parametrize(
    "blob",
    [
        b"BAD" + HEADER[3:],
        HEADER[:8] + b"\x08" + HEADER[9:],
        HEADER[:9] + b"\x00\x00\x00\x01",
        HEADER + video()[:-4] + b"\x00" * 4,
        HEADER + tag(7, b"unknown"),
        HEADER + tag(9, b"tiny"),
        HEADER + tag(9, b"\x16\x01\x00\x00\x00x"),
        HEADER + tag(9, b"\x17\x03\x00\x00\x00x"),
        HEADER + video(body=b""),
        HEADER + tag(8, b"x"),
        HEADER + tag(8, b"\x9f\x01x"),
        HEADER + tag(8, b"\xaf\x03x"),
        HEADER + audio(body=b""),
    ],
)
def test_malformed_complete_framing_is_unknown_not_fabricated_counts(probe, blob):
    assert probe.invoke(blob) == {"state": "unknown"}


@pytest.mark.parametrize("tail", [b"\x09", video()[:11], video()[:-1]])
def test_partial_tail_retains_only_completed_prefix_as_explicit_lower_bound(probe, tail):
    result = probe.invoke(HEADER + audio(10) + tail)
    assert result == {
        "state": "known",
        "partial_tail": True,
        "sample_window_seconds": [0.1, 0.9],
        "video": {"packets": 0},
        "audio": {"packets": 1, "last_observed_seconds": 0.9, "pts_span_seconds": 0.0},
    }


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "boolean_size",
        "regressed_size",
        "duplicate_time",
        "nan_time",
        "missing_time",
        "no_window",
        "one_sample",
        "size_past_file",
        "too_many_samples",
    ],
)
def test_bad_sample_windows_fail_closed(probe, change):
    blob = HEADER + video()
    samples = [sample(0.1, 13), sample(0.9, len(blob))]
    if change == "failed":
        samples[1]["capture_ok"] = False
    elif change == "boolean_size":
        samples[1]["capture_size"] = True
    elif change == "regressed_size":
        samples[0]["capture_size"], samples[1]["capture_size"] = len(blob), 13
    elif change == "duplicate_time":
        samples[1]["t"] = samples[0]["t"]
    elif change == "nan_time":
        samples[1]["t"] = float("nan")
    elif change == "missing_time":
        del samples[1]["t"]
    elif change == "no_window":
        samples = [sample(-2, 13), sample(-1, len(blob))]
    elif change == "one_sample":
        samples = samples[:1]
    elif change == "size_past_file":
        samples[1]["capture_size"] += 1
    elif change == "too_many_samples":
        samples *= 8193
    assert probe.invoke(blob, samples) == {"state": "unknown"}


@pytest.mark.parametrize(
    "location,change",
    [
        ("parent", "owner"),
        ("parent", "symlink"),
        ("parent", "public"),
        ("path", "owner"),
        ("path", "symlink"),
        ("path", "hardlink"),
        ("path", "writable"),
        ("opened", "owner"),
        ("opened", "identity"),
        ("opened", "fifo"),
        ("final", "identity"),
        ("final", "truncated"),
    ],
)
def test_ownership_identity_and_regular_file_guards(probe, monkeypatch, location, change):
    original_lstat, original_fstat = probe.os.lstat, probe.os.fstat
    fstats = 0

    def corrupt(metadata):
        if change == "owner":
            metadata.st_uid = 1000
        elif change == "symlink":
            metadata.st_mode = stat.S_IFLNK | 0o700
        elif change == "public":
            metadata.st_mode |= 0o055
        elif change == "hardlink":
            metadata.st_nlink = 2
        elif change == "writable":
            metadata.st_mode |= 0o020
        elif change == "identity":
            metadata.st_ino += 1
        elif change == "fifo":
            metadata.st_mode = stat.S_IFIFO | 0o600
        elif change == "truncated":
            metadata.st_size = 13
        return metadata

    def lstat(target):
        value = original_lstat(target)
        return (
            corrupt(value)
            if (location == "parent" and target == probe.path.parent)
            or (location == "path" and target == probe.path)
            else value
        )

    def fstat(descriptor):
        nonlocal fstats
        fstats += 1
        value = original_fstat(descriptor)
        return (
            corrupt(value)
            if (location == "opened" and fstats == 1) or (location == "final" and fstats == 2)
            else value
        )

    monkeypatch.setattr(probe.os, "lstat", lstat)
    monkeypatch.setattr(probe.os, "fstat", fstat)
    assert probe.invoke(HEADER + video()) == {"state": "unknown"}


def test_missing_nofollow_is_unknown_before_open(probe, monkeypatch):
    monkeypatch.delattr(probe.os, "O_NOFOLLOW")
    assert probe.invoke(HEADER + video()) == {"state": "unknown"}
    assert not probe.opened


def test_io_exception_closes_owned_descriptor(probe, monkeypatch):
    def fail(*_args):
        raise OSError("PRIVATE")

    monkeypatch.setattr(probe.os, "read", fail)
    assert probe.invoke(HEADER + video()) == {"state": "unknown"}
    assert len(probe.opened) == len(probe.closed) == 1


def test_packet_and_work_deadlines_are_hard_bounds(probe, monkeypatch):
    monkeypatch.setitem(probe.scope, "time", SimpleNamespace(monotonic=lambda: 0))
    empty_script = tag(18, b"")
    assert probe.invoke(HEADER + empty_script * 65536)["state"] == "known"
    assert probe.invoke(HEADER + empty_script * 65537) == {"state": "unknown"}
    times = iter([0, 0, 0, 3])
    monkeypatch.setitem(probe.scope, "time", SimpleNamespace(monotonic=lambda: next(times, 3)))
    assert probe.invoke(HEADER + video()) == {"state": "unknown"}


@pytest.mark.parametrize("packets", [video(1000) + video(999), audio(0) + audio(660001)])
def test_regressed_or_out_of_bound_pts_span_is_unknown(probe, packets):
    assert probe.invoke(HEADER + packets) == {"state": "unknown"}


def test_failure_scans_have_no_shared_cursor_between_concurrent_calls(probe):
    blob = HEADER + video(0) + audio(0) + video(33) + audio(21)
    probe.path.write_bytes(blob)
    samples = [sample(0.1, 13), sample(0.9, len(blob))]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(probe.function, probe.path, samples, 100, 101) for _ in range(8)]
    results = [future.result() for future in futures]
    assert all(result == results[0] for result in results)
    assert results[0]["video"]["packets"] == results[0]["audio"]["packets"] == 2
    assert sorted(fd for fd, _ in probe.opened) == sorted(probe.closed)
