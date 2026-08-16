"""Secret-safe diagnostics for the real Node onboarding smoke."""

from scripts.ci_node_onboarding_smoke import safe_bootstrap_diagnostic_code


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
