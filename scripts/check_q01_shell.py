# Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_q01_shell.py
# Expected: [SUCCESS] N cases passed; visible native Windows required; auto-closes.
# SPDX-License-Identifier: GPL-3.0-only
"""Self-contained Q-01.0-A checks. --demo opens an interactive synthetic viewer.

No real GIS input, network, background job, guide, or core schema modification.
Native exposure is mandatory; offscreen/minimal platforms are not a substitute.
Outputs are local ignored artifacts; no failed/skipped case counts as passed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MISSING = [name for name in ("PySide6", "geopandas", "pyogrio", "pyproj", "shapely") if importlib.util.find_spec(name) is None]
if MISSING:
    print("FAIL: missing " + ", ".join(MISSING) + "; conda env create -f environment.yml")
    raise SystemExit(1)

import geopandas as gpd
import PySide6
from shapely.geometry import LineString, Polygon
from PySide6.QtCore import QPoint, QPointF, QTimer, Qt, qVersion
from PySide6.QtGui import QFontMetrics, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsItem

from prototypes.p0.contracts import ContractError
from prototypes.p0.job_contracts import DataState
from prototypes.q01.shell import (
    LAYER_NAME, ShellWindow, ViewInputError, choose_font, create_synthetic_fixture,
    load_features, resource_for,
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Interactive native viewer; close window to exit.")
    args = parser.parse_args()
    # Never force a headless platform and then claim Windows GUI success.
    application = QApplication.instance() or QApplication(sys.argv[:1])
    family = choose_font(application)
    checks: list[dict[str, object]] = []
    result = {"exit": 1}
    artifacts = ROOT / "outputs/q01a"
    context = ROOT / ".context"
    assert artifacts.resolve().is_relative_to(ROOT) and context.resolve().is_relative_to(ROOT)
    artifacts.mkdir(parents=True, exist_ok=True)
    context.mkdir(exist_ok=True)

    def check(name: str, condition: bool) -> None:
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            print("[FAIL] " + name)

    def reject(name: str, function: Callable[[], object]) -> None:
        try:
            function()
        except (ViewInputError, ContractError):
            check(name, True)
        except Exception as exc:
            check(name + ": unexpected " + type(exc).__name__, False)
        else:
            check(name + ": invalid input accepted", False)

    with tempfile.TemporaryDirectory(prefix="q01-native-") as temporary:
        root = Path(temporary) / "合成资源 空格"
        root.mkdir()
        layer = create_synthetic_fixture(root)
        window = ShellWindow(root, layer)
        window.show()
        window.raise_()
        window.activateWindow()
        QTimer.singleShot(150, window.view.fit_all)
        if args.demo:
            print("[DEMO] Synthetic only. Close the visible window to release temporary resources.")
            return application.exec()

        def perform() -> None:
            screenshot = None
            try:
                check("native Windows platform, not offscreen", application.platformName() == "windows")
                exposed = QTest.qWaitForWindowExposed(window, 5000)
                check("visible native window exposed", exposed and window.isVisible())
                check("native window handle created", bool(window.winId()))
                window.view.fit_all()
                QTest.qWait(100)
                view = window.view
                check("C0 layer consumed; three complete features", len(view.features) == 3 and len(view.items_by_id) == 3)
                check("Unicode + spaces resource path roundtrip", "合成 分区.gpkg" in layer.resource.relative_path and len(load_features(root, layer)) == 3)
                check("read-only details and non-movable polygons", window.details.isReadOnly() and all(not (i.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable) for i in view.items_by_id.values()))
                check("Chinese glyph available in chosen font", QFontMetrics(application.font()).inFont("规") and QFontMetrics(application.font()).inFont("划"))
                check("positive high-DPI device ratio", window.devicePixelRatioF() >= 1)
                north = view.mapFromScene(QPointF(500000, -4000100))
                south = view.mapFromScene(QPointF(500000, -4000000))
                check("explicit metric space rendered north-up", north.y() < south.y())
                baseline = view.transform().m11()
                QTest.mouseClick(window.zoom_in_button, Qt.MouseButton.LeftButton)
                check("toolbar zoom in changes transform", view.transform().m11() > baseline)
                QTest.mouseClick(window.zoom_out_button, Qt.MouseButton.LeftButton)
                check("toolbar zoom out reverses transform", abs(view.transform().m11() - baseline) < 1e-8)
                position = view.viewport().rect().center()
                event = QWheelEvent(QPointF(position), QPointF(view.viewport().mapToGlobal(position)), QPoint(), QPoint(0, 120),
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                QApplication.sendEvent(view.viewport(), event)
                check("wheel event zooms map", view.transform().m11() > baseline)
                QTest.mouseClick(window.fit_button, Qt.MouseButton.LeftButton)
                check("fit action restores bounded map scale", abs(view.transform().m11() - baseline) < 1e-8)
                center_before = view.mapToScene(view.viewport().rect().center())
                start = view.viewport().rect().center()
                end = start + QPoint(65, 40)
                QTest.mousePress(view.viewport(), Qt.MouseButton.MiddleButton, pos=start)
                QTest.mouseMove(view.viewport(), end, delay=30)
                QTest.mouseRelease(view.viewport(), Qt.MouseButton.MiddleButton, pos=end)
                center_after = view.mapToScene(view.viewport().rect().center())
                check("middle-drag pans viewport", (center_after - center_before).manhattanLength() > 1)
                check("pan cursor/drag state released", view._drag is None)
                QTest.mouseClick(window.fit_button, Qt.MouseButton.LeftButton)
                feature = view.features[0]
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(feature.center))
                check("left click selects feature", view.items_by_id[feature.identifier].isSelected())
                check("selection details update visibly", feature.name in window.details.toPlainText() and feature.identifier in window.details.toPlainText())
                reject("fixture registration rejects escape before reading", lambda: resource_for(root.parent / "outside.gpkg", root, layer.resource))
                reject("reject missing resource", lambda: load_features(root, replace(layer, resource=replace(layer.resource, relative_path="maps/missing.gpkg"))))
                reject("reject hash mismatch", lambda: load_features(root, replace(layer, resource=replace(layer.resource, sha256="b" * 64))))
                reject("reject size mismatch", lambda: load_features(root, replace(layer, resource=replace(layer.resource, size_bytes=layer.resource.size_bytes + 1))))
                reject("C0 rejects parent path", lambda: replace(layer.resource, relative_path="../outside.gpkg"))
                reject("C0 rejects absolute drive path", lambda: replace(layer.resource, relative_path="C:/outside.gpkg"))
                reject("unready data not silently drawn", lambda: load_features(root, replace(layer, data_state=DataState.DATA_INSUFFICIENT)))
                reject("CRS mismatch rejected without reinterpretation", lambda: load_features(root, replace(layer, space=replace(layer.space, crs="EPSG:32632"))))
                reject("missing source sublayer rejected", lambda: load_features(root, replace(layer, source_layer="missing_layer")))
                original_resolve = Path.resolve
                target = root / layer.resource.relative_path
                with patch.object(Path, "resolve", lambda self, *a, **kw: root.parent / "outside.gpkg" if self == target else original_resolve(self, *a, **kw)):
                    reject("simulated external reparse target rejected", lambda: load_features(root, layer))
                original_frame = gpd.read_file(target, layer=LAYER_NAME, engine="pyogrio")
                for index, kind in enumerate(("duplicate", "missing-name", "non-polygon", "hole")):
                    frame = original_frame.copy()
                    if kind == "duplicate":
                        frame.loc[1, "feature_id"] = frame.loc[0, "feature_id"]
                    elif kind == "missing-name":
                        frame = frame.drop(columns=["name"])
                    elif kind == "non-polygon":
                        frame = frame.iloc[:1].copy()
                        frame.at[frame.index[0], "geometry"] = LineString([(500000, 4000000), (500010, 4000010)])
                    else:
                        frame = frame.iloc[:1].copy()
                        frame.at[frame.index[0], "geometry"] = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(2, 2), (3, 2), (3, 3), (2, 3)]])
                    path = root / "maps" / f"negative-{index}.gpkg"
                    frame.to_file(path, layer=LAYER_NAME, engine="pyogrio", driver="GPKG", index=False)
                    bad = replace(layer, resource=resource_for(path, root, layer.resource))
                    reject("reject unsupported feature " + kind, lambda bad=bad: load_features(root, bad))
                reject("invalid zoom factor rejected", lambda: view.zoom(float("nan")))
                check("negative cases do not mutate visible scene", len(view.items_by_id) == 3 and view.items_by_id[feature.identifier].isSelected())
                QTest.qWait(150)
                if exposed and application.platformName() == "windows":
                    pixmap = window.grab()
                    path = artifacts / "native-window.png"
                    check("visible-window screenshot captured", not pixmap.isNull() and pixmap.save(str(path)))
                    screenshot = str(path)
            except Exception as exc:
                check("runtime exception: " + type(exc).__name__ + ": " + str(exc)[:160], False)
            finally:
                failed = sum(not c["passed"] for c in checks)
                report = {"checkpoint": "Q-01.0-A", "synthetic_only": True, "platform": application.platformName(),
                          "pyside6": PySide6.__version__, "qt": qVersion(), "font_family": family,
                          "c0_schema": layer.versions.schema_version, "passed": len(checks) - failed, "failed": failed,
                          "skipped": 0, "checks": checks, "screenshot": screenshot,
                          "not_verified": ["background job", "guide", "real GIS", "terrain safety", "real reparse point"]}
                (context / "q01a-native-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                result["exit"] = 1 if failed else 0
                print(f"[{'FAILURE' if failed else 'SUCCESS'}] Q-01.0-A: {len(checks) - failed} passed, {failed} failed, 0 skipped; synthetic only.")
                print(f"Platform={application.platformName()}; PySide6={PySide6.__version__}; Qt={qVersion()}; font={family}")
                window.close()
                application.quit()

        def watchdog() -> None:
            if window.isVisible():
                print("[FAIL] native test watchdog expired; no success claim.")
                window.close()
                application.quit()

        QTimer.singleShot(300, perform)
        QTimer.singleShot(25000, watchdog)
        application.exec()
        window.close()
    return result["exit"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print("[FAIL] Q-01 shell setup: " + type(exc).__name__ + ": " + str(exc)[:180])
        raise SystemExit(1)
