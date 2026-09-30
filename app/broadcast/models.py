"""Public domain inputs and fail-closed platform validation."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

CAPABILITIES = frozenset(
    {
        "multi_output_v1",
        "inter_relay_srt_v1",
        "route_switch_v1",
        "youtube_dual_ingest_v1",
        "egress_credential_lease_v1",
    }
)
YOUTUBE_HOSTS = frozenset(
    {"a.rtmps.youtube.com", "b.rtmps.youtube.com", "a.rtmp.youtube.com", "b.rtmp.youtube.com"}
)


class BroadcastError(Exception):
    """Only a constant safe code crosses an API or logging boundary."""

    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class SwitchState(StrEnum):
    REQUESTED = "REQUESTED"
    VALIDATING_TARGET = "VALIDATING_TARGET"
    PREPARING_TARGET = "PREPARING_TARGET"
    TARGET_MEDIA_READY = "TARGET_MEDIA_READY"
    TARGET_CREDENTIAL_LEASED = "TARGET_CREDENTIAL_LEASED"
    TARGET_EGRESS_STARTING = "TARGET_EGRESS_STARTING"
    TARGET_YOUTUBE_CONNECTED = "TARGET_YOUTUBE_CONNECTED"
    CUTOVER_ARMED = "CUTOVER_ARMED"
    OLD_EGRESS_DRAINING = "OLD_EGRESS_DRAINING"
    OLD_CREDENTIAL_REVOKED = "OLD_CREDENTIAL_REVOKED"
    EGRESS_SWITCH_COMPLETED = "EGRESS_SWITCH_COMPLETED"
    AWAITING_DIRECT_SOURCE = "AWAITING_DIRECT_SOURCE"
    DIRECT_SOURCE_CONFIRMED = "DIRECT_SOURCE_CONFIRMED"
    DIRECT_SOURCE_SEEN = "DIRECT_SOURCE_SEEN"
    REWARMING_OLD_ROUTE = "REWARMING_OLD_ROUTE"
    COMPLETED = "COMPLETED"
    ROLLING_BACK = "ROLLING_BACK"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MediaProfile(Input):
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    width: int = Field(default=1080, ge=128, le=3840)
    height: int = Field(default=1920, ge=128, le=3840)
    fps: int = Field(default=30, ge=1, le=60)
    gop: int = Field(default=60, ge=1, le=240)
    expected_bitrate_bps: int = Field(default=6_000_000, ge=128_000, le=50_000_000)


class SessionCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    ingress_node_id: str = Field(min_length=1, max_length=128)
    policy: Literal["manual", "assisted"] = "manual"
    profile: MediaProfile = Field(default_factory=MediaProfile)


def youtube_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "rtmps"
            and parsed.hostname in YOUTUBE_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/live2"
            and parsed.query in ("", "backup=1")
            and not parsed.fragment
            and not any(ord(c) < 33 or c in "\\%" for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Official YouTube RTMPS ingest endpoint required")
    return value


class OutputCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["manual", "youtube_api"] = "manual"
    channel_id: str | None = Field(default=None, max_length=128)
    primary_url: str | None = Field(default=None, max_length=256)
    backup_url: str | None = Field(default=None, max_length=256)
    stream_key: SecretStr | None = Field(default=None, repr=False)
    visibility: Literal["private", "unlisted", "public"] = "unlisted"
    scheduled_start: str | None = Field(default=None, max_length=40)

    @field_validator("scheduled_start")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("UTC timestamp required")
        return value

    @field_validator("primary_url", "backup_url")
    @classmethod
    def endpoint(cls, value: str | None) -> str | None:
        return youtube_endpoint(value) if value is not None else None

    @field_validator("stream_key")
    @classmethod
    def key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{6,256}", value.get_secret_value()
        ):
            raise ValueError("Invalid stream key")
        return value


class RouteCreate(Input):
    node_id: str = Field(min_length=1, max_length=128)


class OutputIntent(Input):
    enabled: bool


class ResourceLimits(Input):
    max_outputs_per_source: int = Field(default=4, ge=1, le=32)
    max_forwarded_routes: int = Field(default=4, ge=1, le=32)
    max_publishers_per_node: int = Field(default=4, ge=1, le=32)
    max_expected_egress_bps: int = Field(default=40_000_000, ge=128_000, le=1_000_000_000)
