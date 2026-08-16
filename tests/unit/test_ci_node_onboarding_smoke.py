"""Secret-safe diagnostics for the real Node onboarding smoke."""

from typing import Any

import pytest

from scripts.ci_node_onboarding_smoke import complete_command, safe_bootstrap_diagnostic_code
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


class _CompletedSelfTestClient:
    def request(self, method: str, path: str, *args: object, **kwargs: object) -> dict[str, Any]:
        if method == "POST":
            return {"id": "command-id"}
        return {
            "state": "completed",
            "safe_result": {
                "status": "failed",
                "checks": {
                    "control_https": False,
                    "dns": True,
                    "ffmpeg": True,
                    "ffprobe": True,
                    "memory": True,
                    "disk": True,
                    "data_writable": True,
                    "no_inbound_ports": True,
                },
            },
        }


def test_self_test_diagnostic_prints_only_fixed_check_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SmokeFailure):
        complete_command(_CompletedSelfTestClient(), "node-id", "SELF_TEST")  # type: ignore[arg-type]

    output = capsys.readouterr().out
    assert output == "SELF_TEST failed safe checks: control_https\n"
