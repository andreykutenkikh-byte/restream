from __future__ import annotations

import ipaddress
import socket

import pytest

from app.core.validation import (
    CodecCompatibilityError,
    URLValidationError,
    check_codec_compatibility,
    check_stream_compatibility,
    ensure_stream_compatible,
    is_public_address,
    parse_destination_url,
    validate_destination_url,
    validate_public_host,
    validate_rtmp_url,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def public_resolver(_: str) -> tuple[str, ...]:
    return (PUBLIC_V4, PUBLIC_V6)


def test_valid_rtmp_url_resolves_all_addresses_with_injected_resolver() -> None:
    observed: list[str] = []

    def resolver(hostname: str) -> tuple[str, ...]:
        observed.append(hostname)
        return (PUBLIC_V4, PUBLIC_V6)

    result = validate_destination_url("rtmp://Stream.Example.COM:1936/live", resolver=resolver)

    assert observed == ["stream.example.com"]
    assert result.value == "rtmp://Stream.Example.COM:1936/live"
    assert result.scheme == "rtmp"
    assert result.hostname == "stream.example.com"
    assert result.port == 1936
    assert result.resolved_addresses == (
        ipaddress.ip_address(PUBLIC_V4),
        ipaddress.ip_address(PUBLIC_V6),
    )


def test_rtmps_default_port_and_schema_hook_return_value() -> None:
    parsed = validate_destination_url("rtmps://stream.example.com/live", resolver=public_resolver)

    assert parsed.port == 443
    assert (
        validate_rtmp_url("rtmps://stream.example.com/live", resolver=public_resolver)
        == "rtmps://stream.example.com/live"
    )


def test_direct_public_ip_does_not_call_dns_resolver() -> None:
    def unexpected_resolver(_: str) -> tuple[str, ...]:
        raise AssertionError("literal IP must not be resolved")

    result = validate_destination_url("rtmp://8.8.8.8/live", resolver=unexpected_resolver)

    assert result.resolved_addresses == (ipaddress.ip_address("8.8.8.8"),)


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/stream",
        "http://example.com/live",
        "https://example.com/live",
        "ftp://example.com/live",
        "ssh://example.com/live",
        "/tmp/local-stream",  # noqa: S108 - intentionally rejected user input
        r"C:\\stream\\file",
    ],
)
def test_only_rtmp_and_rtmps_schemes_are_allowed(value: str) -> None:
    with pytest.raises(URLValidationError):
        parse_destination_url(value)


@pytest.mark.parametrize(
    "value",
    [
        " rtmp://example.com/live",
        "rtmp://example.com/live key",
        "rtmp://example.com/live\n",
        "rtmp://example.com/live%0Aheader",
        "rtmp://example.com/live;touch",
        "rtmp://example.com/live|command",
        "rtmp://example.com/live`command`",
        "rtmp://example.com/live?one=1&two=2",
    ],
)
def test_whitespace_control_and_shell_characters_are_rejected(value: str) -> None:
    with pytest.raises(URLValidationError):
        parse_destination_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "rtmp://user:password@example.com/live",
        "rtmp://bad_host.example/live",
        "rtmp://999.999.999.999/live",
        "rtmp://example.com:0/live",
        "rtmp://example.com:65536/live",
        "rtmp://example.com/live#fragment",
        "rtmp:///missing-host",
    ],
)
def test_malformed_or_ambiguous_url_is_rejected(value: str) -> None:
    with pytest.raises(URLValidationError):
        parse_destination_url(value)


def test_excessively_long_url_is_rejected() -> None:
    with pytest.raises(URLValidationError, match="exceeds"):
        parse_destination_url("rtmp://example.com/" + "x" * 100, max_length=64)


@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "host.docker.internal",
        "gateway.docker.internal",
        "metadata.google.internal",
        "service.internal",
        "printer.local",
    ],
)
def test_local_metadata_and_docker_hostnames_are_rejected_without_dns(
    hostname: str,
) -> None:
    with pytest.raises(URLValidationError):
        validate_public_host(hostname, resolver=lambda _: (PUBLIC_V4,))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.10.20.30",
        "0.0.0.0",  # noqa: S104 - SSRF rejection fixture
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "224.0.0.1",
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_address(address)
    with pytest.raises(URLValidationError, match="non-public"):
        validate_destination_url(
            f"rtmp://[{address}]/live" if ":" in address else f"rtmp://{address}/live"
        )


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "8.8.8.8"])
def test_public_unicast_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address)


def test_one_private_dns_answer_rejects_the_entire_hostname() -> None:
    with pytest.raises(URLValidationError, match="10.0.0.5"):
        validate_public_host(
            "stream.example.com",
            resolver=lambda _: (PUBLIC_V4, "10.0.0.5"),
        )


def test_empty_and_failed_dns_answers_are_rejected() -> None:
    with pytest.raises(URLValidationError, match="no addresses"):
        validate_public_host("stream.example.com", resolver=lambda _: ())

    def failed(_: str) -> tuple[str, ...]:
        raise socket.gaierror("not found")

    with pytest.raises(URLValidationError, match="could not be resolved"):
        validate_public_host("stream.example.com", resolver=failed)


def test_ffprobe_h264_aac_flv_is_compatible() -> None:
    result = check_stream_compatibility(
        {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"format_name": "flv"},
        }
    )

    assert result.compatible
    assert result.video_codecs == ("h264",)
    assert result.audio_codecs == ("aac",)
    assert result.reason is None


def test_h264_without_audio_is_compatible() -> None:
    assert check_codec_compatibility("H.264", None, container="live_flv").compatible


@pytest.mark.parametrize(
    ("metadata", "issue"),
    [
        (
            {"video_codec": "vp9", "audio_codec": "aac"},
            "unsupported video codec",
        ),
        (
            {"video_codec": "h264", "audio_codec": "opus"},
            "unsupported audio codec",
        ),
        ({"audio_codec": "aac"}, "video stream is missing"),
        (
            {"video_codec": "h264", "audio_codec": "aac", "container": "matroska"},
            "unsupported container",
        ),
    ],
)
def test_incompatible_codec_metadata_has_a_clear_reason(
    metadata: dict[str, str], issue: str
) -> None:
    result = check_stream_compatibility(metadata)

    assert not result.compatible
    assert result.reason is not None and issue in result.reason
    assert "H.264" in result.message
    assert "AAC" in result.message


def test_ensure_stream_compatible_raises_for_worker_boundary() -> None:
    with pytest.raises(CodecCompatibilityError, match="unsupported video codec"):
        ensure_stream_compatible({"video_codec": "hevc", "audio_codec": "aac"})
