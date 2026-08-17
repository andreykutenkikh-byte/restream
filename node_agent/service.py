"""Agent enrollment, heartbeat, and command loops."""

from __future__ import annotations

import logging
import random
import threading
from typing import Protocol

from node_agent.commands import CommandProcessor
from node_agent.credentials import CredentialStore, SensitiveToken
from node_agent.errors import (
    AgentError,
    CredentialsRejected,
    EnrollmentRejected,
    ProtocolRejected,
)
from node_agent.metrics import MetricsCollector
from node_agent.models import CommandCompletion, EnrollmentResponse, NodeCommand, NodeSnapshot
from node_agent.settings import HEARTBEAT_INTERVAL_SECONDS, AgentSettings

logger = logging.getLogger("node_agent")


class NodeControlClient(Protocol):
    def enroll(
        self, enrollment_token: SensitiveToken, snapshot: NodeSnapshot
    ) -> EnrollmentResponse: ...

    def heartbeat(
        self,
        node_token: SensitiveToken,
        snapshot: NodeSnapshot,
        *,
        current_command_id: str | None,
    ) -> None: ...

    def next_command(
        self, node_token: SensitiveToken, *, wait_seconds: int
    ) -> NodeCommand | None: ...

    def ack_command(self, node_token: SensitiveToken, command_id: str) -> None: ...

    def complete_command(
        self,
        node_token: SensitiveToken,
        command_id: str,
        completion: CommandCompletion,
    ) -> None: ...


class ExponentialBackoff:
    """Bounded full-jitter backoff with an injectable random source."""

    def __init__(
        self,
        initial_seconds: float,
        maximum_seconds: float,
        *,
        random_source: random.Random | None = None,
    ) -> None:
        self._initial = initial_seconds
        self._maximum = maximum_seconds
        self._random = random_source or random.SystemRandom()
        self._next_cap = initial_seconds

    def next_delay(self) -> float:
        delay = self._random.uniform(0.0, self._next_cap)
        self._next_cap = min(self._maximum, self._next_cap * 2)
        return delay

    def reset(self) -> None:
        self._next_cap = self._initial


class AgentService:
    """Runs outbound loops and becomes quiescent after permanent rejection."""

    def __init__(
        self,
        *,
        settings: AgentSettings,
        client: NodeControlClient,
        credentials: CredentialStore,
        metrics: MetricsCollector,
        commands: CommandProcessor,
        stop_event: threading.Event,
    ) -> None:
        self._settings = settings
        self._client = client
        self._credentials = credentials
        self._metrics = metrics
        self._commands = commands
        self._stop_event = stop_event
        self._loop_stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._current_command_id: str | None = None
        self._fatal_error: AgentError | None = None
        self._quiescent_error: AgentError | None = None

    def run(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            token = self._initialize_credentials()
        except (CredentialsRejected, EnrollmentRejected, ProtocolRejected) as exc:
            self._set_quiescent(exc)
            self._wait_quiescent()
            return
        if self._stop_event.is_set():
            return
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(token,),
            name="node-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            self._command_loop(token)
        finally:
            self._loop_stop_event.set()
            heartbeat_thread.join(
                timeout=(
                    self._settings.connect_timeout_seconds
                    + self._settings.request_timeout_seconds
                    + 1
                )
            )
        if self._fatal_error is not None:
            raise self._fatal_error
        if self._quiescent_error is not None:
            self._wait_quiescent()

    def _initialize_credentials(self) -> SensitiveToken:
        permanent = self._credentials.load_permanent()
        if permanent is not None:
            return permanent
        enrollment = self._credentials.load_enrollment()
        snapshot = self._metrics.collect()
        response = self._client.enroll(enrollment, snapshot)
        self._credentials.promote(response.node_token)
        return response.node_token

    def _heartbeat_loop(self, token: SensitiveToken) -> None:
        backoff = ExponentialBackoff(
            self._settings.backoff_initial_seconds, self._settings.backoff_max_seconds
        )
        delay = 0.0
        while not self._wait_for_loop_stop(delay):
            try:
                snapshot = self._metrics.collect()
                self._client.heartbeat(
                    token,
                    snapshot,
                    current_command_id=self._get_current_command_id(),
                )
            except (CredentialsRejected, ProtocolRejected) as exc:
                self._set_quiescent(exc)
                return
            except AgentError as exc:
                logger.warning("Heartbeat failed safely (%s); retrying", exc.code)
                delay = backoff.next_delay()
            except Exception:
                self._set_fatal(
                    AgentError("heartbeat_internal_error", "heartbeat loop failed safely")
                )
                return
            else:
                backoff.reset()
                delay = HEARTBEAT_INTERVAL_SECONDS

    def _command_loop(self, token: SensitiveToken) -> None:
        backoff = ExponentialBackoff(
            self._settings.backoff_initial_seconds, self._settings.backoff_max_seconds
        )
        while not self._stopping():
            try:
                command = self._client.next_command(
                    token, wait_seconds=self._settings.command_wait_seconds
                )
                if command is None:
                    backoff.reset()
                    continue
                if self._stopping():
                    return
                self._set_current_command_id(command.command_id)
                self._client.ack_command(token, command.command_id)
                if self._stopping():
                    return
                completion = self._commands.process(command)
                if self._stopping():
                    return
                self._client.complete_command(token, command.command_id, completion)
                backoff.reset()
            except (CredentialsRejected, ProtocolRejected) as exc:
                self._set_quiescent(exc)
                return
            except AgentError as exc:
                logger.warning("Command cycle failed safely (%s); retrying", exc.code)
                if self._wait_for_loop_stop(backoff.next_delay()):
                    return
            except Exception:
                self._set_fatal(AgentError("command_internal_error", "command loop failed safely"))
                return
            finally:
                self._set_current_command_id(None)

    def _get_current_command_id(self) -> str | None:
        with self._state_lock:
            return self._current_command_id

    def _set_current_command_id(self, command_id: str | None) -> None:
        with self._state_lock:
            self._current_command_id = command_id

    def _set_fatal(self, error: AgentError) -> None:
        with self._state_lock:
            if self._fatal_error is None and self._quiescent_error is None:
                self._fatal_error = error
        self._loop_stop_event.set()

    def _set_quiescent(self, error: AgentError) -> None:
        first_rejection = False
        with self._state_lock:
            if self._fatal_error is None and self._quiescent_error is None:
                self._quiescent_error = error
                first_rejection = True
        if first_rejection:
            logger.error("Agent entered quiescent state (%s)", error.code)
        self._loop_stop_event.set()

    def _stopping(self) -> bool:
        return self._stop_event.is_set() or self._loop_stop_event.is_set()

    def _wait_for_loop_stop(self, delay: float) -> bool:
        if self._stopping():
            return True
        return self._loop_stop_event.wait(delay) or self._stop_event.is_set()

    def _wait_quiescent(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.wait()
