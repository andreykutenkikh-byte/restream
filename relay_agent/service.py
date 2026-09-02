"""Concurrent heartbeat and long-poll loops for the native relay agent."""

from __future__ import annotations

import logging
import random
import socket
import threading
from typing import Protocol

from relay_agent.errors import RelayAgentError
from relay_agent.metrics import HostMetricsCollector
from relay_agent.models import HostMetrics, RelayCommand, RelayCompletion, RelaySnapshot
from relay_agent.processor import CommandProcessor

logger = logging.getLogger("relay_agent")


class ControlPlane(Protocol):
    def heartbeat(
        self,
        *,
        hostname: str,
        relay: RelaySnapshot,
        host: HostMetrics,
        current_command_id: str | None,
    ) -> object: ...

    def next_command(self, *, wait_seconds: int = 20) -> RelayCommand | None: ...

    def acknowledge(self, command_id: str) -> None: ...

    def complete(self, command_id: str, completion: RelayCompletion) -> None: ...


class PreviewController(Protocol):
    def set_requested(self, requested: bool) -> None: ...

    def run(self, stop_event: threading.Event) -> None: ...


class Backoff:
    def __init__(self, minimum: float = 1.0, maximum: float = 30.0) -> None:
        self._minimum = minimum
        self._maximum = maximum
        self._cap = minimum
        self._random = random.SystemRandom()

    def next_delay(self) -> float:
        delay = self._random.uniform(0.0, self._cap)
        self._cap = min(self._maximum, self._cap * 2.0)
        return delay

    def reset(self) -> None:
        self._cap = self._minimum


class AgentService:
    def __init__(
        self,
        *,
        control: ControlPlane,
        processor: CommandProcessor,
        metrics: HostMetricsCollector,
        stop_event: threading.Event,
        preview: PreviewController | None = None,
    ) -> None:
        hostname = socket.gethostname()
        if not 1 <= len(hostname) <= 253 or not all(
            character.isprintable() for character in hostname
        ):
            raise RelayAgentError("invalid_hostname")
        self._hostname = hostname
        self._control = control
        self._processor = processor
        self._metrics = metrics
        self._stop = stop_event
        self._preview = preview
        self._current_lock = threading.Lock()
        self._current_command_id: str | None = None

    def run(self) -> None:
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="relay-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        preview_thread = None
        if self._preview is not None:
            preview_thread = threading.Thread(
                target=self._preview.run,
                args=(self._stop,),
                name="relay-preview",
                daemon=True,
            )
            preview_thread.start()
        try:
            self._command_loop()
        finally:
            self._stop.set()
            heartbeat.join(timeout=20.0)
            if preview_thread is not None:
                preview_thread.join(timeout=10.0)

    def _heartbeat_loop(self) -> None:
        backoff = Backoff()
        delay = 0.0
        while not self._stop.wait(delay):
            try:
                relay = self._processor.status()
                host = self._metrics.collect()
                intervals = self._control.heartbeat(
                    hostname=self._hostname,
                    relay=relay,
                    host=host,
                    current_command_id=self._current_id(),
                )
                if self._preview is not None:
                    requested = getattr(intervals, "preview_requested", False)
                    self._preview.set_requested(requested is True and relay.source == "LIVE")
            except RelayAgentError as exc:
                if self._preview is not None:
                    self._preview.set_requested(False)
                logger.warning("Heartbeat failed safely (%s)", exc.code)
                delay = backoff.next_delay()
            except Exception:
                if self._preview is not None:
                    self._preview.set_requested(False)
                logger.error("Heartbeat failed safely (internal_error)")
                delay = backoff.next_delay()
            else:
                backoff.reset()
                delay = 5.0

    def _command_loop(self) -> None:
        backoff = Backoff()
        while not self._stop.is_set():
            try:
                command = self._control.next_command(wait_seconds=20)
                if command is None:
                    backoff.reset()
                    continue
                self._set_current_id(command.command_id)
                self._control.acknowledge(command.command_id)
                completion = self._processor.process(command)
                self._control.complete(command.command_id, completion)
            except RelayAgentError as exc:
                logger.warning("Command cycle failed safely (%s)", exc.code)
                if self._stop.wait(backoff.next_delay()):
                    return
            except Exception:
                logger.error("Command cycle failed safely (internal_error)")
                if self._stop.wait(backoff.next_delay()):
                    return
            else:
                backoff.reset()
            finally:
                self._set_current_id(None)

    def _current_id(self) -> str | None:
        with self._current_lock:
            return self._current_command_id

    def _set_current_id(self, value: str | None) -> None:
        with self._current_lock:
            self._current_command_id = value
