"""Secret-safe diagnostics for the native relay onboarding smoke."""

from typing import Any

import pytest

from scripts.ci_node_onboarding_smoke import (
    safe_api_payload,
    safe_bootstrap_diagnostic_code,
)
from scripts.ci_output_smoke import SmokeFailure


def test_bootstrap_diagnostic_code_is_strictly_allowlisted() -> None:
    assert (
        safe_bootstrap_diagnostic_code(
            {"safe_error": {"code": "remote_command_failed", "message": "ignored"}}
        )
        == "remote_command_failed"
    )
    assert (
        safe_bootstrap_diagnostic_code(
            {
                "safe_error": {
                    "code": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A",
                    "message": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A",
                }
            }
        )
        == "unknown"
    )
    assert safe_bootstrap_diagnostic_code({"safe_error": "invalid"}) == "unknown"


def test_native_safe_failure_code_is_allowlisted() -> None:
    assert (
        safe_bootstrap_diagnostic_code({"safe_error": {"code": "relay_self_test_failed"}})
        == "relay_self_test_failed"
    )


def test_api_payload_password_marker_fails_closed() -> None:
    with pytest.raises(SmokeFailure):
        safe_api_payload(
            {"unexpected": "CI_SSH_PASSWORD_MUST_NEVER_PERSIST_9F3A"},
            "unit test payload",
        )

    clean: dict[str, Any] = {"install_profile": "moblin_relay"}
    assert safe_api_payload(clean, "unit test payload") is clean
