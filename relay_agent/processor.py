"""Idempotent mapping from leased control-plane commands to broker actions."""

from __future__ import annotations

from typing import Protocol

from relay_agent.broker_client import BrokerResponse
from relay_agent.errors import RelayAgentError
from relay_agent.journal import CommandJournal
from relay_agent.models import (
    JsonObject,
    RelayCommand,
    RelayCompletion,
    RelaySnapshot,
    utc_timestamp,
)


class Broker(Protocol):
    def call(self, action: str, payload: JsonObject | None = None) -> BrokerResponse: ...


_BROKER_ACTIONS = {
    "STATUS": "status",
    "START": "start",
    "STOP": "stop",
    "CONFIGURE_YOUTUBE": "configure_youtube",
    "CLEAR_YOUTUBE": "clear_youtube",
    "REVEAL_MOBLIN_URL": "reveal_moblin_url",
}


class CommandProcessor:
    def __init__(self, broker: Broker, journal: CommandJournal) -> None:
        self._broker = broker
        self._journal = journal

    def status(self) -> RelaySnapshot:
        response = self._broker.call("status", {})
        if response.status != "ok" or response.secret_result is not None:
            raise RelayAgentError("invalid_broker_response")
        return response.safe_result

    def process(self, command: RelayCommand) -> RelayCompletion:
        stored = self._journal.lookup(command)
        if stored is not None:
            if command.action != "REVEAL_MOBLIN_URL" or stored.status != "ok":
                return stored
            response = self._broker.call("reveal_moblin_url", {})
            self._validate_secret_semantics(command, response)
            return RelayCompletion(
                stored.status,
                stored.completed_at,
                stored.safe_result,
                response.secret_result,
            )
        if command.expired():
            completion = RelayCompletion(
                "failed",
                utc_timestamp(),
                self._safe_snapshot("command_expired"),
                None,
            )
            self._journal.record(command, completion)
            return completion
        payload: JsonObject = {}
        if command.action == "CONFIGURE_YOUTUBE":
            if command.youtube is None:
                raise RelayAgentError("invalid_protocol")
            payload = command.youtube.to_broker_payload()
        try:
            response = self._broker.call(_BROKER_ACTIONS[command.action], payload)
            self._validate_secret_semantics(command, response)
            completion = RelayCompletion(
                response.status,
                utc_timestamp(),
                response.safe_result,
                response.secret_result,
            )
        except (KeyError, RelayAgentError):
            completion = RelayCompletion(
                "failed",
                utc_timestamp(),
                self._safe_snapshot("internal_error"),
                None,
            )
        self._journal.record(command, completion)
        return completion

    @staticmethod
    def _validate_secret_semantics(command: RelayCommand, response: BrokerResponse) -> None:
        reveal_success = command.action == "REVEAL_MOBLIN_URL" and response.status == "ok"
        if reveal_success != (response.secret_result is not None):
            raise RelayAgentError("invalid_broker_response")

    def _safe_snapshot(self, code: str) -> RelaySnapshot:
        try:
            return self.status().with_error(code)
        except RelayAgentError:
            return RelaySnapshot.unavailable(code)
