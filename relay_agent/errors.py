"""Deliberately terse errors that are safe to write to a service journal."""

from __future__ import annotations


class RelayAgentError(Exception):
    """An operational failure identified only by a non-secret allowlisted code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
