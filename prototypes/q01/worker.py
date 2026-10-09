# SPDX-License-Identifier: GPL-3.0-only
"""Private Q fake-worker process; no Qt application, network, or real inputs."""
from __future__ import annotations

import os
from dataclasses import replace
from multiprocessing.connection import Connection

from prototypes.p0.job_contracts import CheckState, JobSpec, JobState
from prototypes.p0.job_lifecycle import FakeStage, FakeWorker, JobLifecycle, run_fake_job

MODES = frozenset({"success", "unknown-total", "fail-init", "fail-exec", "fail-cleanup", "crash"})


def run_child(spec: JobSpec, sender: Connection, cancellation, mode: str, steps: int, delay: float) -> None:
    """Only typed C0 objects cross the private, parent-owned pipe."""
    worker = FakeWorker((FakeStage("q01b-synthetic-work", steps, mode != "unknown-total"),),
                        fail_initialization=mode == "fail-init", fail_at_step=3 if mode == "fail-exec" else None,
                        fail_cleanup=mode == "fail-cleanup")
    lifecycle = JobLifecycle(spec)
    cursor = 0

    def flush_events() -> None:
        nonlocal cursor
        while cursor < len(lifecycle.events):
            sender.send(("event", lifecycle.events[cursor]))
            cursor += 1

    def checkpoint(name: str, current: JobLifecycle) -> None:
        flush_events()
        if name == "after_prepare" and mode == "crash":
            os._exit(7)  # Deliberate abnormal-exit fixture; never a normal cleanup claim.
        if name == "after_progress":
            cancellation.wait(delay)
        if cancellation.is_set() and not current.cancellation_requested:
            current.request_cancel(spec.identity, spec.cancel_token)
            sender.send(("cancel-ack", spec.identity, spec.cancel_token))

    try:
        run = run_fake_job(lifecycle, worker, checkpoint=checkpoint)
        flush_events()
        manifest = run.manifest
        # This validates only the explicit synthetic fixture, NOT a planning algorithm.
        fixture_passed = (manifest.job_state == JobState.SUCCEEDED and worker.steps_completed == steps
                          and worker.cleanup_count == 1 and worker.resource_closed)
        if fixture_passed:
            manifest = replace(manifest, validation_state=CheckState.PASSED)
        evidence = {"cleanup_count": worker.cleanup_count, "resource_closed": worker.resource_closed,
                    "started": worker.started, "steps_completed": worker.steps_completed,
                    "worker_pid": os.getpid(), "fixture_passed": fixture_passed}
        sender.send(("result", manifest, evidence))
    finally:
        sender.close()
