from __future__ import annotations

from app.core.redaction import (
    REDACTION_MARKER,
    is_sensitive_key,
    mask_secret,
    redact_command,
    redact_destination_url,
    redact_mapping,
    redact_text,
    redact_url,
)


def test_redact_url_masks_credentials_and_sensitive_query_only() -> None:
    value = "rtmps://alice:s3cr3t@example.com/live?token=abc123&quality=high&api_key=key456"

    redacted = redact_url(value)

    assert "alice" not in redacted
    assert "s3cr3t" not in redacted
    assert "abc123" not in redacted
    assert "key456" not in redacted
    assert "example.com/live" in redacted
    assert "quality=high" in redacted
    assert redacted.count(REDACTION_MARKER) == 3


def test_destination_url_masks_final_stream_key_path_segment() -> None:
    redacted = redact_destination_url("rtmp://example.com/live/very-secret-key")

    assert redacted == f"rtmp://example.com/live/{REDACTION_MARKER}"


def test_destination_url_can_redact_exact_key_without_path_guessing() -> None:
    redacted = redact_destination_url(
        "rtmp://example.com/live?name=secret-value",
        stream_key="secret-value",
        hide_last_path_segment=False,
    )

    assert "secret-value" not in redacted


def test_redact_text_masks_known_secret_assignments_bearer_and_embedded_url() -> None:
    text = (
        "password=hunter2 stream_key=my-stream-key "
        "Authorization: Bearer bearer-value "
        "target=rtmp://user:pass@example.com/live?token=query-secret. "
        "master=known-master-secret"
    )

    redacted = redact_text(text, secrets=("known-master-secret",))

    for secret in (
        "hunter2",
        "my-stream-key",
        "bearer-value",
        "query-secret",
        "known-master-secret",
    ):
        assert secret not in redacted
    assert "//user:" not in redacted
    assert ":pass@" not in redacted
    assert "example.com/live" in redacted


def test_redact_text_masks_quoted_structured_values() -> None:
    redacted = redact_text('{"password": "secret", "event": "login"}')

    assert "secret" not in redacted
    assert '"event": "login"' in redacted


def test_redact_mapping_recurses_and_preserves_non_secret_data() -> None:
    source = {
        "event": "destination_started",
        "youtube_stream_key": "top-secret",
        "details": {
            "session_cookie": "cookie-value",
            "url": "rtmp://user:pass@example.com/live?auth=abc",
        },
        "items": ["password=hunter2", 42],
    }

    redacted = redact_mapping(source)

    assert redacted["event"] == "destination_started"
    assert redacted["youtube_stream_key"] == REDACTION_MARKER
    assert redacted["details"]["session_cookie"] == REDACTION_MARKER
    assert "pass" not in redacted["details"]["url"]
    assert "abc" not in redacted["details"]["url"]
    assert "hunter2" not in redacted["items"][0]
    assert redacted["items"][1] == 42


def test_redact_command_hides_rtmp_path_and_explicit_secrets() -> None:
    arguments = [
        "ffmpeg",
        "-i",
        "rtmp://mediamtx/live/input-key",
        "rtmps://example.com/live/destination-key",
    ]

    safe = redact_command(arguments, secrets=("input-key", "destination-key"))

    rendered = " ".join(safe)
    assert "input-key" not in rendered
    assert "destination-key" not in rendered
    assert rendered.count(REDACTION_MARKER) >= 2


def test_mask_secret_may_expose_only_an_explicit_short_tail() -> None:
    assert mask_secret("abcdef") == REDACTION_MARKER
    assert mask_secret("abcdef", visible_tail=2) == f"{REDACTION_MARKER}…ef"


def test_sensitive_key_detection_avoids_unrelated_key_suffixes() -> None:
    assert is_sensitive_key("master_encryption_key")
    assert is_sensitive_key("csrf-token")
    assert not is_sensitive_key("monkey")
    assert not is_sensitive_key("destination_name")
