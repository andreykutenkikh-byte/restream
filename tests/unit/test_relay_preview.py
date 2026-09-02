from __future__ import annotations

from uuid import uuid4

import pytest

import app.services.relay_preview as preview_module
from app.services.relay_preview import (
    MAX_SEGMENTS_PER_NODE,
    RelayPreviewCapacityExceeded,
    RelayPreviewInvalidSegment,
    RelayPreviewRateLimited,
    RelayPreviewStore,
    RelayPreviewUnavailable,
    validate_mpegts,
)


def ts_segment(packets: int = 3, marker: int = 0) -> bytes:
    packet = bytes((0x47, marker)) + bytes(186)
    return packet * packets


def test_preview_requires_live_lease_and_purges_media_when_it_expires() -> None:
    now = 100.0
    store = RelayPreviewStore(clock=lambda: now, lease_seconds=5, media_ttl_seconds=20)
    node_id = str(uuid4())
    generation = str(uuid4())

    with pytest.raises(RelayPreviewUnavailable):
        store.put(node_id, generation, 1, ts_segment())

    store.renew(node_id)
    store.put(node_id, generation, 1, ts_segment())
    assert store.requested(node_id) is True
    assert store.stored_bytes == len(ts_segment())

    now += 6
    assert store.requested(node_id) is False
    assert store.stored_bytes == 0
    with pytest.raises(RelayPreviewUnavailable):
        store.segment(node_id, generation, 1)


def test_playlist_is_server_generated_and_uses_only_latest_contiguous_suffix() -> None:
    store = RelayPreviewStore()
    node_id = str(uuid4())
    generation = str(uuid4())
    store.renew(node_id)
    for sequence in (40, 42, 43):
        store.put(node_id, generation, sequence, ts_segment(marker=sequence))

    playlist = store.playlist(node_id).decode("ascii")

    assert "#EXT-X-MEDIA-SEQUENCE:42" in playlist
    assert f"segment/{generation}/42.ts" in playlist
    assert f"segment/{generation}/43.ts" in playlist
    assert f"segment/{generation}/40.ts" not in playlist
    assert "http:" not in playlist and "https:" not in playlist


def test_generation_change_and_fixed_ring_remove_stale_assets() -> None:
    store = RelayPreviewStore()
    node_id = str(uuid4())
    old_generation = str(uuid4())
    new_generation = str(uuid4())
    store.renew(node_id)
    for sequence in range(MAX_SEGMENTS_PER_NODE + 2):
        store.put(node_id, old_generation, sequence, ts_segment(marker=sequence))
    with pytest.raises(RelayPreviewUnavailable):
        store.segment(node_id, old_generation, 0)

    store.put(node_id, new_generation, 0, ts_segment(marker=99))
    with pytest.raises(RelayPreviewUnavailable):
        store.segment(node_id, old_generation, MAX_SEGMENTS_PER_NODE + 1)
    assert store.segment(node_id, new_generation, 0) == ts_segment(marker=99)


def test_duplicate_is_idempotent_but_changed_replay_and_stale_sequence_fail() -> None:
    store = RelayPreviewStore()
    node_id = str(uuid4())
    generation = str(uuid4())
    original = ts_segment(marker=1)
    store.renew(node_id)
    store.put(node_id, generation, 10, original)
    store.put(node_id, generation, 10, original)

    with pytest.raises(RelayPreviewInvalidSegment):
        store.put(node_id, generation, 10, ts_segment(marker=2))
    with pytest.raises(RelayPreviewInvalidSegment):
        store.put(node_id, generation, 9, ts_segment(marker=3))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x46" + bytes(187),
        bytes((0x47,)) + bytes(186),
        bytes((0x47,)) + bytes(187) + bytes(188),
    ],
)
def test_mpegts_validator_rejects_malformed_packet_streams(payload: bytes) -> None:
    with pytest.raises(RelayPreviewInvalidSegment):
        validate_mpegts(payload)


def test_mpegts_validator_accepts_complete_sync_aligned_packets() -> None:
    validate_mpegts(ts_segment())


def test_upload_reservation_bounds_concurrency_and_attempt_rate() -> None:
    store = RelayPreviewStore()
    node_id = str(uuid4())
    store.renew(node_id)

    with (
        store.reserve_upload(node_id, len(ts_segment())),
        pytest.raises(RelayPreviewRateLimited),
        store.reserve_upload(node_id, len(ts_segment())),
    ):
        pass

    # The rejected concurrent reservation is not charged; the initial one is.
    for _ in range(11):
        with store.reserve_upload(node_id, len(ts_segment())):
            pass
    with (
        pytest.raises(RelayPreviewRateLimited),
        store.reserve_upload(node_id, len(ts_segment())),
    ):
        pass


def test_global_memory_cap_rejects_new_media_without_evicting_other_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = ts_segment()
    monkeypatch.setattr(preview_module, "MAX_GLOBAL_BYTES", len(body) * 2)
    store = RelayPreviewStore()
    generation = str(uuid4())
    nodes = [str(uuid4()) for _ in range(3)]
    for node_id in nodes:
        store.renew(node_id)
    store.put(nodes[0], generation, 1, body)
    store.put(nodes[1], generation, 1, body)

    with pytest.raises(RelayPreviewCapacityExceeded):
        store.put(nodes[2], generation, 1, body)
    assert store.segment(nodes[0], generation, 1) == body
    assert store.segment(nodes[1], generation, 1) == body


def test_media_ttl_drops_stale_segment_even_while_viewer_renews_lease() -> None:
    now = 100.0
    store = RelayPreviewStore(clock=lambda: now, lease_seconds=10, media_ttl_seconds=3)
    node_id = str(uuid4())
    generation = str(uuid4())
    store.renew(node_id)
    store.put(node_id, generation, 1, ts_segment())

    now += 4
    store.renew(node_id)
    with pytest.raises(RelayPreviewUnavailable):
        store.segment(node_id, generation, 1)
