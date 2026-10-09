# SPDX-License-Identifier: GPL-3.0-only
"""Q-owned Qt/IPC adapter. P owns all shared job/lifecycle/acceptance contracts."""
from __future__ import annotations

import math
import multiprocessing as mp
import time
from pathlib import Path
from dataclasses import replace
from multiprocessing.connection import Connection

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDockWidget, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget
from PySide6.QtCore import Qt

from prototypes.p0.contracts import ContractError
from prototypes.p0.job_acceptance import ResultAcceptance, assess_result_acceptance
from prototypes.p0.job_contracts import (
    CheckState, Diagnostic, JobEvent, JobSpec, JobState, MapLayer, ProjectSummary, ResultManifest, Severity, TERMINAL_STATES,
)
from prototypes.p0.job_fixtures import build_job, build_project
from prototypes.p0.job_lifecycle import CancelStatus, JobLifecycle
from .shell import ShellWindow
from .worker import MODES, run_child


class JobController(QObject):
    event_received = Signal(object)
    finished = Signal(object)
    project_changed = Signal(object)
    started = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.project = build_project()
        self.current_result: ResultManifest | None = None
        self.spec = None
        self.lifecycle = None
        self.outcome: dict | None = None
        self.last_acceptance = None
        self._process = None
        self._receiver: Connection | None = None
        self._cancel_event = None
        self._counter = 0
        self._revision = 0
        self._candidate = None
        self._evidence: dict = {}
        self._cancel_intent = False
        self._protocol_error: str | None = None
        self._closing = False
        self._launched = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll)

    @property
    def active(self) -> bool:
        return self._process is not None

    def start(self, mode: str = "success", *, steps: int = 18, delay: float = 0.025) -> None:
        if self.active or self._closing:
            raise ValueError("A job is already active or the controller is shutting down.")
        if mode not in MODES or type(steps) is not int or not 3 <= steps <= 100:
            raise ValueError("Unsupported or unbounded synthetic plan.")
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 0.1:
            raise ValueError("Synthetic delay must be finite and bounded.")
        self._expected_steps = steps
        self._counter += 1
        base = build_job()
        identity = replace(base.identity, project_id=self.project.project_id,
                           scenario_id=self.project.scenario_id, job_id=f"q01b-job-{self._counter}")
        self.spec = replace(base, identity=identity, versions=self.project.versions, space=self.project.space,
                            input_resources=self.project.resources, cancel_token=f"q01b-cancel-{self._counter}")
        self.lifecycle = JobLifecycle(self.spec)
        self.outcome = None
        self.last_acceptance = None
        self._candidate, self._evidence = None, {}
        self._protocol_error, self._cancel_intent = None, False
        context = mp.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        cancellation = context.Event()
        process = context.Process(target=run_child, args=(self.spec, sender, cancellation, mode, steps, delay),
                                  name=f"q01b-worker-{self._counter}")
        self._receiver, self._cancel_event, self._process = receiver, cancellation, process
        try:
            process.start()
        except Exception:
            receiver.close()
            sender.close()
            process.close()
            self._receiver = self._cancel_event = self._process = None
            raise
        sender.close()
        self._launched = time.monotonic()
        self.timer.start()
        self.started.emit(self.spec)

    def request_cancel(self, *, token: str | None = None) -> CancelStatus:
        if self.spec is None or not self.active or self.lifecycle.state in TERMINAL_STATES:
            return CancelStatus.JOB_FINISHED
        # Use P's validation on a throwaway lifecycle; don't prematurely mark a
        # worker cancellation as confirmed in the live GUI lifecycle.
        validator = JobLifecycle(self.spec)
        validator.request_cancel(self.spec.identity, self.spec.cancel_token if token is None else token)
        if self._cancel_intent:
            return CancelStatus.ALREADY_REQUESTED
        self._cancel_intent = True
        self._cancel_event.set()
        return CancelStatus.REQUESTED

    def simulate_input_edit(self) -> None:
        self._revision += 1
        self.project = replace(self.project, versions=replace(self.project.versions,
                               input_revision=f"q01b-input-edit-{self._revision}"))
        self.project_changed.emit(self.project)

    def evaluate_candidate(self, manifest: ResultManifest) -> ResultAcceptance:
        if self.spec is None or self.lifecycle is None:
            raise ValueError("No registered job context.")
        return assess_result_acceptance(self.project, self.spec, self.lifecycle, manifest)

    def _receive(self, message: object) -> None:
        if not isinstance(message, tuple) or not message:
            raise ValueError("Malformed private IPC message.")
        kind = message[0]
        if kind == "event" and len(message) == 2 and isinstance(message[1], JobEvent):
            if self._protocol_error is None:
                self.lifecycle.accept(message[1])
                self.event_received.emit(message[1])
        elif kind == "cancel-ack" and len(message) == 3:
            self.lifecycle.request_cancel(message[1], message[2])
        elif kind == "result" and len(message) == 3 and isinstance(message[1], ResultManifest) and isinstance(message[2], dict):
            if self._candidate is not None:
                raise ValueError("Duplicate result delivery.")
            if message[1].identity != self.spec.identity or message[1].versions != self.spec.versions:
                raise ValueError("Result identity/version mismatch.")
            self._candidate, self._evidence = message[1], message[2]
        else:
            raise ValueError("Unknown or mistyped private IPC message.")

    def _pipe_has_data(self) -> bool:
        try:
            return self._receiver.poll()
        except (EOFError, BrokenPipeError):
            return False  # Windows closed sender is EOF, not recursive recovery.
        except OSError as exc:
            if getattr(exc, "winerror", None) in (109, 232, 233):
                return False
            raise

    def poll(self) -> None:
        if not self.active:
            return
        try:
            for _ in range(256):
                if not self._pipe_has_data():
                    break
                try:
                    message = self._receiver.recv()
                except (EOFError, BrokenPipeError):
                    break
                try:
                    self._receive(message)
                except (ContractError, ValueError, TypeError) as exc:
                    self._protocol_error = getattr(exc, "code", type(exc).__name__)
                    self._cancel_event.set()
            if self._process.is_alive() and time.monotonic() - self._launched > 15:
                self._protocol_error = "Q01_WORKER_TIMEOUT"
                self._process.terminate()
                self._process.join(1)
            if not self._process.is_alive():
                self._finalize()
        except (OSError, RuntimeError) as exc:
            self.timer.stop()
            self._protocol_error = type(exc).__name__
            self._closing = True
            if self.active:
                self._cancel_event.set()
                self._process.join(0.5)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(1)
                if self._process.is_alive():
                    raise RuntimeError("IPC failure: worker exit unconfirmed.")
                self._finalize(drain=False)

    def _failed_manifest(self) -> ResultManifest:
        if self.lifecycle.state not in TERMINAL_STATES:
            if self.lifecycle.state is None:
                self.lifecycle.accept(JobEvent(self.spec.identity, self.spec.versions, 0, JobState.QUEUED))
            self.lifecycle.accept(JobEvent(self.spec.identity, self.spec.versions, len(self.lifecycle.events),
                                          JobState.FAILED, error_code="Q01_WORKER_EXIT"))
        return ResultManifest(self.spec.identity, self.spec.module_id, self.spec.versions, self.spec.space,
                              JobState.FAILED, (), diagnostics=(Diagnostic("Q01_WORKER_EXIT", "Worker/protocol failed; no success claim.", Severity.ERROR),))

    def _finalize(self, *, drain: bool = True) -> None:
        process = self._process
        process.join(0)
        # Drain once more after process exit: completion can race the first poll.
        for _ in range(256):
            if not drain or not self._pipe_has_data():
                break
            try:
                self._receive(self._receiver.recv())
            except (EOFError, BrokenPipeError):
                break
            except (ContractError, ValueError, TypeError) as exc:
                self._protocol_error = getattr(exc, "code", type(exc).__name__)
        exitcode = process.exitcode
        self._receiver.close()
        process.close()
        self._process = self._receiver = self._cancel_event = None
        self.timer.stop()
        cleanup_verified = (type(self._evidence.get("cleanup_count")) is int and self._evidence["cleanup_count"] == 1 and self._evidence.get("resource_closed") is True)
        manifest = self._candidate
        if exitcode != 0 or manifest is None or self._protocol_error:
            manifest = self._failed_manifest()
        decision = self.evaluate_candidate(manifest)
        self.last_acceptance = decision
        fixture_verified = (self._evidence.get("fixture_passed") is True
                            and type(self._evidence.get("steps_completed")) is int
                            and self._evidence["steps_completed"] == self._expected_steps)
        accepted = (decision.acceptable and cleanup_verified and fixture_verified and exitcode == 0
                    and not self._closing and not self._protocol_error)
        if accepted:
            self.current_result = manifest
        reasons = [issue.code for issue in decision.issues]
        if not cleanup_verified:
            reasons.append("Q01_CLEANUP_UNVERIFIED")
        if manifest.job_state == JobState.SUCCEEDED and not fixture_verified:
            reasons.append("Q01_SYNTHETIC_CHECKS_UNVERIFIED")
        if self._closing:
            reasons.append("GUI_SHUTDOWN")
        if self._protocol_error:
            reasons.append(self._protocol_error)
        self.outcome = {"state": manifest.job_state.value, "accepted": bool(accepted),
                        "decision": decision.state.value, "reasons": reasons, "diagnostic_codes": [d.code for d in manifest.diagnostics], "cleanup_verified": cleanup_verified,
                        "cleanup_count": self._evidence.get("cleanup_count"), "resource_closed": self._evidence.get("resource_closed"),
                        "worker_pid": self._evidence.get("worker_pid"), "exitcode": exitcode,
                        "resources_released": True, "cancel_confirmed": self.lifecycle.cancellation_requested,
                        "cancel_requested": self._cancel_intent, "synthetic_only": True}
        self.finished.emit(self.outcome)

    def shutdown(self) -> None:
        self._closing = True
        if not self.active:
            self.timer.stop()
            return
        self.request_cancel()
        self._process.join(1.5)
        if self._process.is_alive():
            self._protocol_error = "Q01_FORCED_EXIT"
            self._process.terminate()
            self._process.join(1)
        if self._process.is_alive():
            # Do not close a live process handle or fabricate release.
            raise RuntimeError("Worker did not exit after bounded shutdown.")
        self.poll()


class JobWindow(ShellWindow):
    """Native map shell plus synthetic worker controls; no real input editor."""

    def __init__(self, root: Path, layer: MapLayer) -> None:
        super().__init__(root, layer)
        self.setWindowTitle("CSL2 Planner · Q-01.0-B 合成后台作业 PoC")
        self.controller = JobController(self)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        row = QHBoxLayout()
        self.success_button = QPushButton("合成成功作业")
        self.unknown_button = QPushButton("未知总量作业")
        self.failure_button = QPushButton("合成失败作业")
        self.cancel_button = QPushButton("请求取消")
        self.edit_button = QPushButton("模拟输入版本变化")
        self.start_buttons = (self.success_button, self.unknown_button, self.failure_button)
        for button in (*self.start_buttons, self.cancel_button, self.edit_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.job_status = QLabel("未运行 · 仅合成作业，不代表规划结果")
        self.job_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.result_status = QLabel("尚无已接受的合成结果")
        self.result_status.setWordWrap(True)
        self.version_status = QLabel("当前输入版本：" + self.controller.project.versions.input_revision)
        for widget in (self.job_status, self.progress, self.result_status, self.version_status):
            layout.addWidget(widget)
        dock = QDockWidget("后台作业与结果门禁（合成）", self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.cancel_button.setEnabled(False)
        self.success_button.clicked.connect(lambda: self._start("success"))
        self.unknown_button.clicked.connect(lambda: self._start("unknown-total"))
        self.failure_button.clicked.connect(lambda: self._start("fail-exec"))
        self.cancel_button.clicked.connect(self._cancel)
        self.edit_button.clicked.connect(self.controller.simulate_input_edit)
        self.controller.started.connect(self._started)
        self.controller.event_received.connect(self._event)
        self.controller.finished.connect(self._finished)
        self.controller.project_changed.connect(self._project_changed)

    def _start(self, mode: str) -> None:
        try:
            self.controller.start(mode)
        except (ValueError, OSError, RuntimeError) as exc:
            self.job_status.setText("作业启动失败；旧结果未修改：" + type(exc).__name__)

    def _started(self, spec: JobSpec) -> None:
        for button in self.start_buttons:
            button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.job_status.setText("合成作业启动中：" + spec.identity.job_id)

    def _cancel(self) -> None:
        status = self.controller.request_cancel()
        self.job_status.setText("取消请求已发送，等待后台确认：" + status.value)
        self.cancel_button.setEnabled(False)

    def _event(self, event: JobEvent) -> None:
        if event.completed is not None:
            if event.total is None:
                self.progress.setRange(0, 0)
                self.progress.setTextVisible(False)
                self.job_status.setText(f"合成 {event.stage_id}：{event.completed} 步（总量未知）")
            else:
                self.progress.setRange(0, event.total)
                self.progress.setTextVisible(True)
                self.progress.setValue(event.completed)
                self.job_status.setText(f"合成 {event.stage_id}：{event.completed}/{event.total} 步")
        else:
            self.job_status.setText("合成作业状态：" + event.state.value)

    def _finished(self, outcome: dict) -> None:
        for button in self.start_buttons:
            button.setEnabled(not self.controller._closing)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setTextVisible(False)
        if outcome["accepted"]:
            text = "合成结果已接受；仅通过假作业检查，不代表算法或规划已通过"
        elif outcome["state"] == "succeeded":
            text = "后台完成但结果已拒绝；旧结果保留：" + ", ".join(outcome["reasons"])
        else:
            text = "合成作业 " + outcome["state"] + "；旧结果保留"
        if outcome["diagnostic_codes"]:
            text += "；诊断：" + ", ".join(outcome["diagnostic_codes"])
        if not outcome["cleanup_verified"]:
            text += "；常规清理未确认"
        self.job_status.setText(text)
        self._refresh_result()

    def _refresh_result(self) -> None:
        manifest = self.controller.current_result
        if manifest is None:
            self.result_status.setText("尚无已接受的合成结果")
        elif manifest.versions != self.controller.project.versions:
            self.result_status.setText("旧合成结果已过期，仅作参考，不覆盖当前输入：" + manifest.identity.job_id)
        else:
            self.result_status.setText("已接受的合成结果：" + manifest.identity.job_id)

    def _project_changed(self, project: ProjectSummary) -> None:
        self.version_status.setText("当前输入版本：" + project.versions.input_revision)
        self._refresh_result()

    def closeEvent(self, event) -> None:
        try:
            self.controller.shutdown()
        except RuntimeError:
            self.job_status.setText("后台进程尚未退出；关闭未完成")
            event.ignore()
        else:
            super().closeEvent(event)
