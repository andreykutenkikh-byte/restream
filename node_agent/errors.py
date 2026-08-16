"""Secret-safe node agent errors."""

from __future__ import annotations


class AgentError(RuntimeError):
    """Base error whose text is safe to write to the agent log."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigurationError(AgentError):
    """The local non-secret agent configuration is invalid."""


class CredentialError(AgentError):
    """A local credential file is absent, unsafe, or invalid."""


class CredentialsRejected(AgentError):
    """The control plane rejected the permanent node credential."""


class EnrollmentRejected(AgentError):
    """The control plane permanently rejected the one-time enrollment credential."""


class ProtocolRejected(AgentError):
    """The control plane permanently rejected this agent protocol version."""


class ControlAPIError(AgentError):
    """A control-plane request failed without exposing request details."""


class ProtocolError(AgentError):
    """A v1 response or command does not match the fixed protocol."""
