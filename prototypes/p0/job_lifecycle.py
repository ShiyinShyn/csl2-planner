# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from tempfile import TemporaryFile
from typing import BinaryIO, Callable, Iterator

from .contracts import ContractError, identifier, require
from .job_contracts import (
    Diagnostic, JobEvent, JobIdentity, JobSpec, JobState, ProgressUnit,
    ResultManifest, Severity, TERMINAL_STATES,
)

MAX_FAKE_STEPS = 10_000
MAX_FAKE_STAGES = 64


class CancelStatus(str, Enum):
    REQUESTED = "requested"
    ALREADY_REQUESTED = "already_requested"
    JOB_FINISHED = "job_finished"


class JobLifecycle:
    def __init__(self, spec: JobSpec) -> None:
        require(isinstance(spec, JobSpec), "INVALID_JOB_CONTEXT", "typed job specification required")
        self._spec = spec
        self._events: tuple[JobEvent, ...] = ()
        self._progress: dict[str, tuple[int, int | None, ProgressUnit]] = {}
        self._cancellation_requested = False

    @property
    def spec(self) -> JobSpec:
        return self._spec

    @property
    def events(self) -> tuple[JobEvent, ...]:
        return self._events

    @property
    def state(self) -> JobState | None:
        return self._events[-1].state if self._events else None

    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation_requested

    def accept(self, event: JobEvent) -> None:
        require(isinstance(event, JobEvent), "INVALID_JOB_EVENT", "typed event required")
        require(event.identity == self.spec.identity and event.versions == self.spec.versions,
                "JOB_EVENT_CONTEXT_MISMATCH", "event must match the bound job identity and versions")
        require(self.state not in TERMINAL_STATES, "JOB_ALREADY_TERMINAL", "terminal event history is closed")
        require(event.sequence == len(self._events), "JOB_EVENT_SEQUENCE", "event sequence must be contiguous from zero")
        allowed = {
            None: frozenset({JobState.QUEUED}),
            JobState.QUEUED: frozenset({JobState.RUNNING, JobState.FAILED, JobState.CANCELLED}),
            JobState.RUNNING: frozenset({JobState.RUNNING, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}),
        }
        require(event.state in allowed[self.state], "INVALID_JOB_TRANSITION", "invalid lifecycle transition")
        if event.state == JobState.CANCELLED:
            require(self.cancellation_requested, "JOB_CANCELLATION_NOT_REQUESTED", "cancelled requires a matching request")
        if event.state == JobState.SUCCEEDED:
            require(not self.cancellation_requested, "JOB_CANCELLATION_PENDING", "pending cancellation precedes success")
        progress = self._progress.copy()
        if event.completed is not None:
            previous = progress.get(event.stage_id)
            if previous is not None:
                completed, total, unit = previous
                require(event.completed >= completed, "JOB_PROGRESS_REGRESSION", "stage progress cannot decrease")
                require(event.progress_unit == unit, "JOB_PROGRESS_UNIT_CHANGED", "stage counter unit cannot change")
                require(total is None or event.total == total,
                        "JOB_PROGRESS_TOTAL_CHANGED", "confirmed stage total cannot change or disappear")
            progress[event.stage_id] = (event.completed, event.total, event.progress_unit)
        self._progress = progress
        self._events += (event,)

    def request_cancel(self, identity: JobIdentity, cancel_token: str) -> CancelStatus:
        require(isinstance(identity, JobIdentity) and identity == self.spec.identity,
                "JOB_CANCEL_CONTEXT_MISMATCH", "cancellation must target the bound job")
        require(isinstance(cancel_token, str) and cancel_token == self.spec.cancel_token,
                "INVALID_CANCEL_TOKEN", "cancellation token does not match")
        if self.state in TERMINAL_STATES:
            return CancelStatus.JOB_FINISHED
        if self.cancellation_requested:
            return CancelStatus.ALREADY_REQUESTED
        self._cancellation_requested = True
        return CancelStatus.REQUESTED


@dataclass(frozen=True, slots=True)
class FakeStage:
    stage_id: str
    steps: int
    known_total: bool = True

    def __post_init__(self) -> None:
        identifier(self.stage_id, "stage_id")
        require(len(self.stage_id) <= 128 and self.stage_id == self.stage_id.strip() and
                not any(ord(character) < 32 for character in self.stage_id), "INVALID_ID", "stage_id")
        require(type(self.steps) is int and 1 <= self.steps <= MAX_FAKE_STEPS,
                "INVALID_FAKE_PLAN", "positive bounded synthetic step count required")
        require(type(self.known_total) is bool, "INVALID_FAKE_PLAN", "explicit total visibility required")


class FakeWorker:
    def __init__(self, stages: tuple[FakeStage, ...] = (FakeStage("synthetic-work", 3),), *,
                 fail_initialization: bool = False, fail_at_step: int | None = None,
                 fail_cleanup: bool = False, resource_factory: Callable[[], BinaryIO] | None = None) -> None:
        require(isinstance(stages, tuple) and 0 < len(stages) <= MAX_FAKE_STAGES and
                all(isinstance(stage, FakeStage) for stage in stages),
                "INVALID_FAKE_PLAN", "immutable bounded synthetic stages required")
        require(len({stage.stage_id for stage in stages}) == len(stages) and
                sum(stage.steps for stage in stages) <= MAX_FAKE_STEPS,
                "INVALID_FAKE_PLAN", "unique stages and bounded total work required")
        require(type(fail_initialization) is bool and type(fail_cleanup) is bool,
                "INVALID_FAKE_PLAN", "explicit fault flags required")
        require(fail_at_step is None or (type(fail_at_step) is int and
                1 <= fail_at_step <= sum(stage.steps for stage in stages)),
                "INVALID_FAKE_PLAN", "fault step must be within the synthetic work")
        require(resource_factory is None or callable(resource_factory),
                "INVALID_FAKE_PLAN", "trusted local resource factory required")
        self.stages = stages
        self.fail_initialization = fail_initialization
        self.fail_at_step = fail_at_step
        self.fail_cleanup = fail_cleanup
        self.steps_completed = 0
        self.cleanup_count = 0
        self._started = False
        self._resource: BinaryIO | None = None
        self._resource_factory = TemporaryFile if resource_factory is None else resource_factory

    @property
    def resource_closed(self) -> bool:
        return self._resource is None or self._resource.closed

    @property
    def started(self) -> bool:
        return self._started

    def prepare(self) -> None:
        require(not self.started and self.cleanup_count == 0,
                "FAKE_WORKER_ALREADY_USED", "synthetic worker is single-use")
        self._started = True
        self._resource = self._resource_factory()
        if self.fail_initialization:
            raise ContractError("SYNTHETIC_INITIALIZATION_FAILED", "injected failure after acquiring resource")

    def execute(self) -> Iterator[tuple[FakeStage, int]]:
        require(self.started and self._resource is not None and not self.resource_closed,
                "FAKE_WORKER_NOT_PREPARED", "open synthetic resource required")
        for stage in self.stages:
            for completed in range(1, stage.steps + 1):
                if self.fail_at_step == self.steps_completed + 1:
                    raise ContractError("SYNTHETIC_EXECUTION_FAILED", "injected synthetic step failure")
                self._resource.write(b"\0")
                self.steps_completed += 1
                yield stage, completed

    def cleanup(self) -> None:
        require(self.cleanup_count == 0, "FAKE_WORKER_ALREADY_CLEANED", "cleanup must run exactly once")
        self.cleanup_count += 1
        if self._resource is not None:
            self._resource.close()
        if self.fail_cleanup:
            raise ContractError("SYNTHETIC_CLEANUP_FAILED", "injected failure after closing resource")


@dataclass(frozen=True, slots=True)
class FakeJobRun:
    events: tuple[JobEvent, ...]
    manifest: ResultManifest


Checkpoint = Callable[[str, JobLifecycle], None]


def _notify(checkpoint: Checkpoint | None, name: str, lifecycle: JobLifecycle) -> None:
    if checkpoint is not None:
        checkpoint(name, lifecycle)


def _failure(exc: Exception, code: str, message: str) -> tuple[Diagnostic, ...]:
    diagnostics = (Diagnostic(code, f"{message}: {type(exc).__name__}", Severity.ERROR),)
    if isinstance(exc, ContractError) and exc.code != code:
        diagnostics += (Diagnostic(exc.code, "Underlying worker reason", Severity.ERROR),)
    return diagnostics


def run_fake_job(lifecycle: JobLifecycle, worker: FakeWorker, *,
                 checkpoint: Checkpoint | None = None) -> FakeJobRun:
    require(isinstance(lifecycle, JobLifecycle) and isinstance(worker, FakeWorker),
            "INVALID_FAKE_RUN", "typed lifecycle and synthetic worker required")
    require(not lifecycle.events and not worker.started and worker.cleanup_count == 0,
            "FAKE_RUN_ALREADY_STARTED", "runner requires a fresh lifecycle and worker")
    require(checkpoint is None or callable(checkpoint), "INVALID_FAKE_RUN", "trusted checkpoint callback required")
    spec = lifecycle.spec
    lifecycle.accept(JobEvent(spec.identity, spec.versions, 0, JobState.QUEUED))
    pending = JobState.SUCCEEDED
    diagnostics: tuple[Diagnostic, ...] = ()
    error_code: str | None = None
    failure_code = "WORKER_CHECKPOINT_FAILED"
    try:
        _notify(checkpoint, "before_start", lifecycle)
        if not lifecycle.cancellation_requested:
            lifecycle.accept(JobEvent(spec.identity, spec.versions, len(lifecycle.events), JobState.RUNNING))
            failure_code = "WORKER_INITIALIZATION_FAILED"
            worker.prepare()
            failure_code = "WORKER_CHECKPOINT_FAILED"
            _notify(checkpoint, "after_prepare", lifecycle)
            if not lifecycle.cancellation_requested:
                iterator = worker.execute()
                while not lifecycle.cancellation_requested:
                    failure_code = "WORKER_EXECUTION_FAILED"
                    try:
                        stage, completed = next(iterator)
                    except StopIteration:
                        break
                    lifecycle.accept(JobEvent(spec.identity, spec.versions, len(lifecycle.events), JobState.RUNNING,
                                              stage.stage_id, completed, stage.steps if stage.known_total else None,
                                              ProgressUnit.STEPS))
                    failure_code = "WORKER_CHECKPOINT_FAILED"
                    _notify(checkpoint, "after_progress", lifecycle)
    except Exception as exc:
        pending = JobState.FAILED
        error_code = failure_code
        diagnostics += _failure(exc, failure_code, "Synthetic job failed")
    finally:
        try:
            worker.cleanup()
        except Exception as exc:
            pending = JobState.FAILED
            error_code = "WORKER_CLEANUP_FAILED"
            diagnostics += _failure(exc, error_code, "Synthetic resource cleanup failed")
    try:
        _notify(checkpoint, "before_result", lifecycle)
    except Exception as exc:
        pending = JobState.FAILED
        if error_code is None:
            error_code = "WORKER_CHECKPOINT_FAILED"
        diagnostics += _failure(exc, "WORKER_CHECKPOINT_FAILED", "Final checkpoint failed")
    if pending != JobState.FAILED and lifecycle.cancellation_requested:
        pending = JobState.CANCELLED
        diagnostics += (Diagnostic("JOB_CANCELLED", "Cancellation confirmed after cleanup", Severity.INFO),)
    lifecycle.accept(JobEvent(spec.identity, spec.versions, len(lifecycle.events), pending, error_code=error_code))
    manifest = ResultManifest(spec.identity, spec.module_id, spec.versions, spec.space,
                              pending, (), diagnostics=diagnostics)
    return FakeJobRun(lifecycle.events, manifest)
