"""Service-layer integrations and process management."""

from .mediamtx import (
    IngestState,
    IngestStatus,
    MediaMTXClient,
    StreamMetadata,
    map_ingest_status,
    normalize_stream_metadata,
)
from .workers import (
    DestinationSpec,
    IngestSnapshot,
    ReconnectPolicy,
    WorkerManager,
    WorkerRuntimeConfig,
    WorkerState,
    WorkerStatus,
    build_ffmpeg_argv,
    check_stream_compatibility,
)

__all__ = [
    "DestinationSpec",
    "IngestSnapshot",
    "IngestState",
    "IngestStatus",
    "MediaMTXClient",
    "ReconnectPolicy",
    "StreamMetadata",
    "WorkerManager",
    "WorkerRuntimeConfig",
    "WorkerState",
    "WorkerStatus",
    "build_ffmpeg_argv",
    "check_stream_compatibility",
    "map_ingest_status",
    "normalize_stream_metadata",
]
