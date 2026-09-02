from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from relay_agent.broker_client import BrokerResponse
from relay_agent.journal import CommandJournal
from relay_agent.models import (
    RelayCommand,
    RelaySnapshot,
    YouTubeConfiguration,
    YouTubeKeyConfiguration,
)
from relay_agent.processor import CommandProcessor
from relay_agent.security import SensitiveToken


def safe_snapshot() -> RelaySnapshot:
    return RelaySnapshot(
        service_state="inactive",
        enabled=False,
        main_process="stopped",
        srt_listener="closed",
        source="NONE",
        youtube_forward="inactive",
        overall="offline",
        youtube_url_configured=True,
        youtube_key_configured=True,
        healthy=False,
        portrait_profile=True,
        error_code=None,
    )


class FakeBroker:
    def __init__(self, srt_secret: str) -> None:
        self.calls: list[str] = []
        self.payloads: list[tuple[str, dict[str, object] | None]] = []
        self.srt_secret = srt_secret

    def call(self, action: str, payload: dict[str, object] | None = None) -> BrokerResponse:
        self.calls.append(action)
        self.payloads.append((action, payload))
        return BrokerResponse(
            "ok",
            safe_snapshot(),
            self.srt_secret if action == "reveal_moblin_url" else None,
        )


def command(
    action: str,
    youtube: YouTubeConfiguration | None = None,
    youtube_key: YouTubeKeyConfiguration | None = None,
) -> RelayCommand:
    return RelayCommand(
        command_id=str(uuid4()),
        action=action,  # type: ignore[arg-type]
        lease_seconds=30,
        attempt_count=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        youtube=youtube,
        youtube_key=youtube_key,
    )


def test_configure_is_idempotent_and_journal_never_contains_secrets(tmp_path: Path) -> None:
    youtube_key = "YT_FIXTURE_DO_NOT_STORE_8fcb1b"
    youtube_url = "rtmps://a.rtmps.youtube.com/live2"
    srt_secret = "srt://203.0.113.10:8890?passphrase=SRT_FIXTURE_DO_NOT_STORE"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    journal = CommandJournal(private / "commands.json")
    broker = FakeBroker(srt_secret)
    processor = CommandProcessor(broker, journal)

    configure = command("CONFIGURE_YOUTUBE", YouTubeConfiguration(youtube_url, youtube_key))
    first = processor.process(configure)
    second = processor.process(configure)
    assert first == second
    assert broker.calls.count("configure_youtube") == 1
    journal_bytes = (private / "commands.json").read_bytes()
    assert youtube_key.encode() not in journal_bytes
    assert youtube_url.encode() not in journal_bytes

    reveal = command("REVEAL_MOBLIN_URL")
    first_reveal = processor.process(reveal)
    assert first_reveal.secret_result == srt_secret
    assert srt_secret.encode() not in (private / "commands.json").read_bytes()
    repeated_reveal = processor.process(reveal)
    assert repeated_reveal.secret_result == srt_secret
    assert repeated_reveal.completed_at == first_reveal.completed_at
    assert repeated_reveal.safe_result == first_reveal.safe_result
    assert broker.calls.count("reveal_moblin_url") == 2


def test_key_only_configure_is_idempotent_redacted_and_has_exact_broker_payload(
    tmp_path: Path,
) -> None:
    marker = "KEY_ONLY_AGENT_SECRET_7f4c"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    journal = CommandJournal(private / "commands.json")
    broker = FakeBroker("unused")
    processor = CommandProcessor(broker, journal)
    configure = command(
        "CONFIGURE_YOUTUBE_KEY",
        youtube_key=YouTubeKeyConfiguration(marker),
    )

    first = processor.process(configure)
    second = processor.process(configure)

    assert first == second
    assert broker.calls.count("configure_youtube_key") == 1
    assert broker.payloads == [("configure_youtube_key", {"youtube_stream_key": marker})]
    assert marker.encode() not in (private / "commands.json").read_bytes()
    assert marker not in repr(configure)
    assert marker not in repr(configure.youtube_key)


def test_secret_wrappers_argv_environment_and_logs_are_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SUPER_SECRET_STREAM_KEY_4fe38a"
    token = SensitiveToken.parse(f"token-{sentinel}-012345678901234567890123456789")
    youtube = YouTubeConfiguration("rtmps://a.rtmps.youtube.com/live2", sentinel)
    relay_command = command("CONFIGURE_YOUTUBE", youtube)
    assert sentinel not in repr(token)
    assert sentinel not in str(token)
    assert sentinel not in repr(youtube)
    assert sentinel not in repr(relay_command)
    assert sentinel not in "\0".join(sys.argv)
    assert sentinel not in "\0".join(f"{key}={value}" for key, value in os.environ.items())

    logger = logging.getLogger("relay_agent.test")
    logger.warning("Command failed safely (%s)", "invalid_configuration")
    assert sentinel not in "\n".join(record.getMessage() for record in caplog.records)
