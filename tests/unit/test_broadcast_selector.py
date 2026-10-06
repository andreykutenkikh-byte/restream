from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace
from typing import cast

import pytest

from app.broadcast.selector import FLV_HEADER, MAX_PACKETS, Input, Selector, Tag, read_tag


def source(name: str, *, config: bytes = b"same") -> Input:
    return cast(
        Input,
        SimpleNamespace(
            identity=name,
            config={8: Tag(8, 0, b"\xaf\x00\x11\x90"), 9: Tag(9, 0, b"\x17\0\0\0\0" + config)},
            counts={8: 200, 9: 200},
            length_size=4,
            audio_step=1024 * 1000 / 48000,
            preselection_packets=0,
            started=time.monotonic(),
        ),
    )


def video(stamp: int, *, idr: bool = True) -> Tag:
    return Tag(
        9,
        stamp,
        bytes([0x17 if idr else 0x27, 1, 0, 0, 0])
        + b"\0\0\0\2"
        + bytes([0x65 if idr else 0x41, 0]),
    )


def wait(predicate: object) -> None:
    for _ in range(100):
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.005)
    raise AssertionError("selector did not advance")


def test_tag_roundtrip_and_real_idr_validation() -> None:
    original = video(0x12345678)
    assert original.idr(4)
    assert read_tag(io.BytesIO(original.encode())) == original
    assert not video(10, idr=False).idr(4)
    assert not Tag(9, 10, original.data[:-1]).idr(4)
    with pytest.raises(ValueError, match="timestamp"):
        original.encode(-0x20000000)
    with pytest.raises(ValueError, match="size"):
        read_tag(io.BytesIO(original.encode()[:-4] + b"\0\0\0\0"))


def test_splice_preserves_both_clocks_without_replacing_sink() -> None:
    sink = io.BytesIO()
    selector = Selector(sink)
    a, b = source("forward"), source("direct")
    try:
        selector.select("forward")
        selector.offer(a, video(1000))
        selector.offer(a, Tag(8, 1021, b"\xaf\x01abc"))
        wait(lambda: selector.frames == 1 and selector.audio_packets == 1)
        selector.offer(a, video(1033, idr=False))
        selector.offer(a, Tag(8, 1042, b"\xaf\x01def"))
        wait(lambda: selector.frames == 2 and selector.audio_packets == 2)
        selector.select("direct")
        selector.offer(b, video(0, idr=False))
        assert selector.selected == "forward"
        selector.offer(b, video(5000))
        selector.offer(b, Tag(8, 5021, b"\xaf\x01ghi"))
        wait(lambda: selector.frames == 3 and selector.audio_packets == 3)
        assert selector.selected == "direct" and selector.old_tail_packets == 0
        assert selector.error is None and len(selector.events) == 1
        data = io.BytesIO(sink.getvalue())
        assert data.read(13) == FLV_HEADER
        tags = []
        while data.tell() < len(data.getvalue()):
            tag = read_tag(data)
            if not tag.configuration:
                tags.append(tag)
        for kind in (8, 9):
            stamps = [p.dts for p in tags if p.kind == kind]
            assert all(y > x for x, y in zip(stamps, stamps[1:], strict=False))
        assert tags[-1].dts - tags[-2].dts == 21
    finally:
        selector.close()


def test_incompatible_or_unaligned_direct_keeps_forwarded() -> None:
    selector = Selector(io.BytesIO())
    a = source("forward")
    try:
        selector.select("forward")
        selector.offer(a, video(0))
        selector.offer(a, Tag(8, 1, b"\xaf\x01x"))
        wait(lambda: selector.frames == 1 and selector.audio_packets == 1)
        selector.select("direct")
        selector.offer(source("direct", config=b"different"), video(100))
        assert selector.rejection == "direct_codec_configuration_incompatible"
        selector.offer(source("direct"), video(1000))
        selector.offer(source("direct"), Tag(8, 1300, b"\xaf\x01x"))
        assert selector.selected == "forward"
        assert selector.rejection == "direct_av_alignment_rejected"
        selector.offer(a, video(33, idr=False))
        wait(lambda: selector.frames == 2)
        assert selector.error is None
    finally:
        selector.close()


def test_blocked_publisher_does_not_hold_control_lock_or_discard_queued_tail() -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockedSink(io.BytesIO):
        def flush(self) -> None:
            entered.set()
            assert release.wait(3), "test did not release blocked publisher"

    selector = Selector(BlockedSink())
    a, b = source("forward"), source("direct")
    try:
        selector.select("forward")
        selector.offer(a, video(0))
        selector.offer(a, Tag(8, 1, b"\xaf\x01x"))
        assert entered.wait(1)
        # These calls must complete even while sink I/O is blocked. Otherwise an
        # expired lease cannot acquire this lock to close the media pipeline.
        selector.select("direct")
        selector.offer(b, video(1000))
        selector.offer(b, Tag(8, 1001, b"\xaf\x01x"))
        assert selector.selected == "forward" and selector.inflight
        for stamp in range(1, MAX_PACKETS + 2):
            selector.offer(a, video(stamp * 33, idr=False))
        assert selector.error == "active_queue_overflow"
        assert len(selector.queue) == MAX_PACKETS
        assert selector.old_tail_packets == 0
        assert selector.events == []
    finally:
        release.set()
        selector.close()
    assert not selector.thread.is_alive()


def test_unprepared_direct_and_unrequested_old_source_cannot_steal_selection() -> None:
    selector = Selector(io.BytesIO())
    a, b = source("forward"), source("direct")
    try:
        selector.select("forward")
        selector.offer(a, video(1000))
        selector.offer(a, Tag(8, 1021, b"\xaf\x01x"))
        wait(lambda: selector.audio_packets == 1)
        selector.select("direct")
        b.counts = {8: 89, 9: 89}
        selector.offer(b, video(0))
        selector.offer(b, Tag(8, 21, b"\xaf\x01x"))
        assert selector.selected == "forward"
        b.counts = {8: 90, 9: 90}
        selector.offer(b, video(2000))
        selector.offer(b, Tag(8, 2021, b"\xaf\x01x"))
        wait(lambda: selector.audio_packets == 2)
        selector.offer(a, video(3000))
        selector.offer(a, Tag(8, 3021, b"\xaf\x01x"))
        assert selector.selected == "direct" and len(selector.events) == 1
        assert not selector.queue
    finally:
        selector.close()
