"""Explicit bootstrap state transitions and safe progress projection."""

from __future__ import annotations

from bootstrap_worker.errors import InvalidTransitionError
from bootstrap_worker.models import (
    TERMINAL_JOB_STATES,
    BootstrapStep,
    JobState,
    StepState,
    StepView,
)

_NEXT: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RESOLVING}),
    JobState.RESOLVING: frozenset({JobState.CONNECTING}),
    JobState.CONNECTING: frozenset({JobState.VERIFYING_HOST_KEY}),
    JobState.VERIFYING_HOST_KEY: frozenset({JobState.AUTHENTICATING}),
    JobState.AUTHENTICATING: frozenset({JobState.CHECKING_PRIVILEGES}),
    JobState.CHECKING_PRIVILEGES: frozenset(
        {JobState.NEEDS_SUDO_PASSWORD, JobState.CHECKING_SYSTEM}
    ),
    JobState.NEEDS_SUDO_PASSWORD: frozenset({JobState.CHECKING_PRIVILEGES}),
    JobState.CHECKING_SYSTEM: frozenset({JobState.CHECKING_RESOURCES}),
    JobState.CHECKING_RESOURCES: frozenset({JobState.CHECKING_DOCKER}),
    JobState.CHECKING_DOCKER: frozenset(
        {JobState.INSTALLING_DOCKER, JobState.NEEDS_ENROLLMENT_TOKEN}
    ),
    JobState.INSTALLING_DOCKER: frozenset({JobState.NEEDS_ENROLLMENT_TOKEN}),
    JobState.NEEDS_ENROLLMENT_TOKEN: frozenset({JobState.PREPARING_AGENT}),
    JobState.PREPARING_AGENT: frozenset({JobState.INSTALLING_AGENT}),
    JobState.INSTALLING_AGENT: frozenset({JobState.WAITING_FOR_ENROLLMENT}),
    JobState.WAITING_FOR_ENROLLMENT: frozenset({JobState.RUNNING_SELF_TEST}),
    JobState.RUNNING_SELF_TEST: frozenset({JobState.COMPLETED}),
    JobState.CANCELLING: frozenset({JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.FAILED: frozenset(),
}

for _state in tuple(_NEXT):
    if _state not in TERMINAL_JOB_STATES and _state is not JobState.CANCELLING:
        _NEXT[_state] = _NEXT[_state] | {JobState.CANCELLING, JobState.FAILED}
_NEXT[JobState.CANCELLING] = _NEXT[JobState.CANCELLING] | {JobState.FAILED}

_STATE_STEP: dict[JobState, BootstrapStep | None] = {
    JobState.QUEUED: None,
    JobState.RESOLVING: BootstrapStep.SSH_CONNECT,
    JobState.CONNECTING: BootstrapStep.SSH_CONNECT,
    JobState.VERIFYING_HOST_KEY: BootstrapStep.SSH_CONNECT,
    JobState.AUTHENTICATING: BootstrapStep.SSH_CONNECT,
    JobState.CHECKING_PRIVILEGES: BootstrapStep.SSH_CONNECT,
    JobState.NEEDS_SUDO_PASSWORD: BootstrapStep.SSH_CONNECT,
    JobState.CHECKING_SYSTEM: BootstrapStep.SYSTEM_CHECK,
    JobState.CHECKING_RESOURCES: BootstrapStep.RESOURCES_CHECK,
    JobState.CHECKING_DOCKER: BootstrapStep.DOCKER_CHECK,
    JobState.INSTALLING_DOCKER: BootstrapStep.DOCKER_CHECK,
    JobState.NEEDS_ENROLLMENT_TOKEN: BootstrapStep.AGENT_INSTALL,
    JobState.PREPARING_AGENT: BootstrapStep.AGENT_INSTALL,
    JobState.INSTALLING_AGENT: BootstrapStep.AGENT_INSTALL,
    JobState.WAITING_FOR_ENROLLMENT: BootstrapStep.PANEL_CONNECT,
    JobState.RUNNING_SELF_TEST: BootstrapStep.FINAL_CHECK,
    JobState.COMPLETED: None,
    JobState.CANCELLING: None,
    JobState.CANCELLED: None,
    JobState.FAILED: None,
}

_STEP_ORDER = tuple(BootstrapStep)
_PROGRESS = {
    None: 0,
    BootstrapStep.SSH_CONNECT: 10,
    BootstrapStep.SYSTEM_CHECK: 25,
    BootstrapStep.RESOURCES_CHECK: 38,
    BootstrapStep.DOCKER_CHECK: 52,
    BootstrapStep.AGENT_INSTALL: 70,
    BootstrapStep.PANEL_CONNECT: 86,
    BootstrapStep.FINAL_CHECK: 95,
}


class JobStateMachine:
    """Small synchronous machine; all mutation occurs on the ASGI event loop."""

    def __init__(self, initial: JobState = JobState.QUEUED) -> None:
        self._state = initial
        self._last_active_step = _STATE_STEP[initial]

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def current_step(self) -> BootstrapStep | None:
        if self._state in {JobState.FAILED, JobState.CANCELLED, JobState.CANCELLING}:
            return self._last_active_step
        return _STATE_STEP[self._state]

    def can_transition(self, target: JobState) -> bool:
        return target in _NEXT[self._state]

    def transition(self, target: JobState) -> None:
        if not self.can_transition(target):
            raise InvalidTransitionError(f"invalid transition: {self._state} -> {target}")
        self._state = target
        active_step = _STATE_STEP[target]
        if active_step is not None:
            self._last_active_step = active_step

    def step_views(self) -> tuple[StepView, ...]:
        if self._state is JobState.COMPLETED:
            return tuple(StepView(name=step, state=StepState.COMPLETED) for step in _STEP_ORDER)

        active = self.current_step
        active_index = _STEP_ORDER.index(active) if active is not None else -1
        views: list[StepView] = []
        for index, step in enumerate(_STEP_ORDER):
            if index < active_index:
                state = StepState.COMPLETED
            elif index > active_index or active is None:
                state = StepState.PENDING
            elif self._state is JobState.FAILED:
                state = StepState.FAILED
            elif self._state in {JobState.CANCELLED, JobState.CANCELLING}:
                state = StepState.SKIPPED
            else:
                state = StepState.RUNNING
            views.append(StepView(name=step, state=state))
        return tuple(views)

    def progress_percent(self) -> int:
        if self._state is JobState.COMPLETED:
            return 100
        return _PROGRESS[self.current_step]


__all__ = ["JobStateMachine"]
