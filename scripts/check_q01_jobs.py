# Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_q01_jobs.py
# Expected: [SUCCESS] N checks; real spawn workers, native window; auto-cleans.
# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic native GUI/worker integration only; no real model, data or network."""
from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
missing = [name for name in ("PySide6", "geopandas", "pyogrio", "pyproj") if importlib.util.find_spec(name) is None]
if missing:
    print("FAIL: missing " + ", ".join(missing) + "; conda env create -f environment.yml")
    raise SystemExit(1)

import PySide6
from PySide6.QtCore import QTimer, Qt, qVersion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from prototypes.p0.contracts import ContractError
from prototypes.p0.job_lifecycle import CancelStatus
from prototypes.q01.jobs import JobWindow
from prototypes.q01.shell import choose_font, create_synthetic_fixture


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    choose_font(app)
    checks = []
    runs = []
    result = {"exit": 1}
    screenshot = None
    heartbeat = {"ticks": 0}
    pulse = QTimer()
    pulse.setInterval(20)
    pulse.timeout.connect(lambda: heartbeat.update(ticks=heartbeat["ticks"] + 1))
    pulse.start()

    def check(name: str, condition: bool) -> None:
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            print("[FAIL] " + name)

    def wait_until(predicate, timeout: float = 12) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            QTest.qWait(20)
        return bool(predicate())

    with tempfile.TemporaryDirectory(prefix="q01b-native-") as temporary:
        root = Path(temporary) / "合成资源 空格"
        root.mkdir()
        layer = create_synthetic_fixture(root)
        window = JobWindow(root, layer)
        controller = window.controller
        window.show()
        QTimer.singleShot(100, window.view.fit_all)
        if args.demo:
            app.setQuitOnLastWindowClosed(True)
            print("[DEMO] Synthetic spawn jobs only. Closing cancels/reaps the worker.")
            return app.exec()

        def completed(name: str) -> dict:
            check(name + " finishes within bound", wait_until(lambda: not controller.active))
            if controller.active or controller.outcome is None:
                raise RuntimeError("Worker did not complete; no success claim.")
            outcome = controller.outcome.copy()
            # Persist only meaningful evidence, not transient PIDs.
            runs.append({**{k: v for k, v in outcome.items() if k != "worker_pid"}, "case": name,
                         "distinct_process": outcome["worker_pid"] is not None and outcome["worker_pid"] != os.getpid()})
            check(name + " process/pipe/timer released", outcome["resources_released"] and not controller.active and not controller.timer.isActive())
            return outcome

        def perform() -> None:
            nonlocal screenshot
            try:
                check("native Windows platform", app.platformName() == "windows")
                check("visible window exposed", QTest.qWaitForWindowExposed(window, 5000))
                QTest.mouseClick(window.success_button, Qt.MouseButton.LeftButton)
                start_ticks = heartbeat["ticks"]
                first = completed("success")
                check("success typed lifecycle and result accepted", first["state"] == "succeeded" and first["accepted"])
                check("actual distinct background process", first["worker_pid"] != os.getpid())
                check("GUI heartbeat continues while worker runs", heartbeat["ticks"] - start_ticks >= 3)
                check("worker cleans temporary resource exactly once", first["cleanup_count"] == 1 and first["resource_closed"] and first["cleanup_verified"])
                events = controller.lifecycle.events
                check("contiguous C0 event sequence", [e.sequence for e in events] == list(range(len(events))))
                progress = [e.completed for e in events if e.completed is not None]
                check("real progress monotonic", progress == sorted(progress) and progress[-1] == 18)
                old = controller.current_result
                check("GUI says synthetic, not planning-approved", "合成" in window.job_status.text() and "不代表算法或规划" in window.job_status.text())

                QTest.mouseClick(window.unknown_button, Qt.MouseButton.LeftButton)
                check("unknown-total progress reaches GUI", wait_until(lambda: controller.lifecycle.events and any(e.completed for e in controller.lifecycle.events)))
                check("unknown total uses busy range, no fabricated percent", window.progress.minimum() == 0 and window.progress.maximum() == 0 and "总量未知" in window.job_status.text())
                feature = window.view.features[0]
                QTest.mouseClick(window.view.viewport(), Qt.MouseButton.LeftButton, pos=window.view.mapFromScene(feature.center))
                check("map remains interactive during worker", window.view.items_by_id[feature.identifier].isSelected())
                unknown = completed("unknown-total")
                check("unknown-total completion accepted", unknown["accepted"])
                old = controller.current_result

                for mode in ("fail-init", "fail-exec", "fail-cleanup"):
                    controller.start(mode)
                    outcome = completed(mode)
                    check(mode + " is failed, never success", outcome["state"] == "failed" and not outcome["accepted"])
                    check(mode + " preserves previous result", controller.current_result is old)
                    check(mode + " closes resource once", outcome["cleanup_count"] == 1 and outcome["resource_closed"])
                check("GUI failure/old-result state visible", "旧结果保留" in window.job_status.text())

                controller.start("success")
                check("cancel requested before first GUI event", not controller.lifecycle.events and controller.request_cancel() == CancelStatus.REQUESTED)
                early = completed("early-cancel")
                check("early cancellation confirmed, no result applied", early["state"] == "cancelled" and early["cancel_confirmed"] and not early["accepted"])
                check("early cancellation still cleans once", early["cleanup_count"] == 1 and early["resource_closed"] and controller.current_result is old)

                controller.start("success", steps=60, delay=0.03)
                check("cancel test reaches running progress", wait_until(lambda: any(e.completed for e in controller.lifecycle.events)))
                try:
                    controller.request_cancel(token="wrong-token")
                except ContractError:
                    check("wrong cancel token rejected", not controller._cancel_intent)
                else:
                    check("wrong cancel token rejected", False)
                try:
                    controller.start("success")
                except ValueError:
                    check("second concurrent job rejected", True)
                else:
                    check("second concurrent job rejected", False)
                QTest.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
                check("repeat cancellation is idempotent", controller.request_cancel() == CancelStatus.ALREADY_REQUESTED)
                cancelled = completed("cancel-running")
                check("cancelled only after worker confirmation", cancelled["state"] == "cancelled" and cancelled["cancel_confirmed"] and not cancelled["accepted"])
                check("cancel preserves previous result and verifies cleanup", controller.current_result is old and cancelled["cleanup_verified"])
                check("cancel after terminal is no-op", controller.request_cancel() == CancelStatus.JOB_FINISHED)

                controller.start("success", steps=40, delay=0.025)
                check("stale test running", wait_until(lambda: any(e.completed for e in controller.lifecycle.events)))
                QTest.mouseClick(window.edit_button, Qt.MouseButton.LeftButton)
                stale = completed("stale-input")
                check("successful old revision rejected", stale["state"] == "succeeded" and not stale["accepted"] and stale["decision"] == "rejected_stale" and "VERSION_CHANGED" in stale["reasons"])
                check("stale result does not overwrite previous result", controller.current_result is old)
                check("GUI marks old accepted result as reference only", "已过期" in window.result_status.text())

                controller.start("success")
                fresh = completed("fresh-input")
                check("fresh revision accepted", fresh["accepted"] and controller.current_result.versions == controller.project.versions)
                new = controller.current_result
                decision = controller.evaluate_candidate(old)
                check("replaced job candidate rejected independently", not decision.acceptable and any(i.code == "JOB_REPLACED" for i in decision.issues))
                check("assessment cannot mutate accepted result", controller.current_result is new)

                controller.start("crash")
                crash = completed("abnormal-exit")
                check("abnormal exit is failed and rejected", crash["state"] == "failed" and not crash["accepted"] and crash["exitcode"] == 7)
                check("abnormal exit does not fabricate normal cleanup", not crash["cleanup_verified"] and crash["cleanup_count"] is None)
                check("abnormal exit preserves previous result", controller.current_result is new)

                controller.start("success", steps=3, delay=0)
                controller.timer.stop()  # Deliberately delay GUI delivery, not worker completion.
                check("late-cancel worker already exited", wait_until(lambda: not controller._process.is_alive()))
                check("late cancellation is only an intent", controller.request_cancel() == CancelStatus.REQUESTED)
                controller.poll()
                late = completed("late-cancel-after-exit")
                check("late cancel does not falsely invalidate completed job", late["state"] == "succeeded" and late["accepted"] and not late["cancel_confirmed"])

                controller.start("success")
                completed("final-success")
                window.view.fit_all()
                QTest.qWait(100)
                artifacts = ROOT / "outputs/q01b"
                assert artifacts.resolve().is_relative_to(ROOT)
                artifacts.mkdir(parents=True, exist_ok=True)
                screenshot = artifacts / "native-jobs.png"
                check("native job window screenshot", window.grab().save(str(screenshot)))
                old = controller.current_result
                controller.start("success", steps=60, delay=0.03)
                check("close test running", wait_until(lambda: any(e.completed for e in controller.lifecycle.events)))
                window.close()
                runs.append({**{k: v for k, v in controller.outcome.items() if k != "worker_pid"}, "case": "close-window",
                             "distinct_process": controller.outcome["worker_pid"] is not None and controller.outcome["worker_pid"] != os.getpid()})
                check("window close reaps worker and pipe", not controller.active and controller.outcome["resources_released"] and not window.isVisible())
                check("shutdown cannot apply pending result", controller.current_result is old and not controller.outcome["accepted"])
                check("shutdown regular resource closure verified", controller.outcome["cleanup_verified"])
                check("no multiprocessing children remain", not mp.active_children())
            except Exception as exc:
                check("runtime exception: " + type(exc).__name__ + ": " + str(exc)[:160], False)
            finally:
                controller.shutdown()
                window.close()
                pulse.stop()
                failed = sum(not c["passed"] for c in checks)
                report = {"checkpoint": "Q-01.0-B", "platform": app.platformName(), "synthetic_only": True,
                          "pyside6": PySide6.__version__, "qt": qVersion(), "passed": len(checks) - failed,
                          "failed": failed, "skipped": 0, "checks": checks, "runs": runs,
                          "screenshot": str(screenshot) if screenshot else None,
                          "not_verified": ["real compute kernel", "real GIS jobs", "project persistence", "guide", "user UAT", "general untrusted IPC"]}
                (ROOT / ".context/q01b-native-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                result["exit"] = 1 if failed else 0
                print(f"[{'FAILURE' if failed else 'SUCCESS'}] Q-01.0-B: {len(checks)-failed} passed, {failed} failed, 0 skipped; {len(runs)} spawn runs; synthetic only.")
                app.quit()

        def watchdog() -> None:
            check("integration watchdog expired", False)
            controller.shutdown()
            window.close()
            app.quit()

        QTimer.singleShot(300, perform)
        QTimer.singleShot(70000, watchdog)
        app.exec()
    return result["exit"]


if __name__ == "__main__":
    mp.freeze_support()
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print("[FAIL] Q-01 jobs setup: " + type(exc).__name__ + ": " + str(exc)[:180])
        raise SystemExit(1)
