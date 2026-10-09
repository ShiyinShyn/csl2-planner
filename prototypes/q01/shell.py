# Run: conda run -n csl2-planner --no-capture-output python -s scripts/check_q01_shell.py --demo
# Expected: visible native synthetic map; close window to release temporary resources.
# SPDX-License-Identifier: GPL-3.0-only
"""Q-01.0-A: native synthetic polygon viewer, not an editor or solver.

Consumes frozen C0 objects without changing them. This PoC only reads a bounded
GeoPackage layer with explicit matching metric CRS and simple 2D polygons. It
makes no general import, terrain/safety, background job, or guide claim.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import Polygon
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import (
    QApplication, QDockWidget, QGraphicsItem, QGraphicsPolygonItem, QGraphicsScene,
    QGraphicsSimpleTextItem, QGraphicsView, QLabel, QListWidget, QMainWindow,
    QPlainTextEdit, QToolBar, QToolButton,
)

from prototypes.p0.job_contracts import (
    DataState, GeometryType, MapLayer, ResourceFormat, ResourceRef,
)
from prototypes.p0.job_fixtures import build_resource, build_space, build_versions

LAYER_NAME = "q01_synthetic_zones"
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_FEATURES = 128
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z", re.ASCII)


class ViewInputError(ValueError):
    """Rejected before any feature is exposed to the scene."""


@dataclass(frozen=True)
class Feature:
    identifier: str
    name: str
    color: str
    points: tuple[tuple[float, float], ...]

    @property
    def center(self) -> QPointF:
        ring = self.points[:-1]
        return QPointF(sum(p[0] for p in ring) / len(ring), -sum(p[1] for p in ring) / len(ring))


def resource_for(path: Path, root: Path, template: ResourceRef) -> ResourceRef:
    """Register an owned fixture's actual bytes, not a fabricated resource hash."""
    from dataclasses import replace
    root = root.resolve()
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ViewInputError("Fixture resource escapes its owned root; not read.")
    if not path.is_file() or path.stat().st_size > MAX_RESOURCE_BYTES:
        raise ViewInputError("Fixture resource is missing or exceeds its bound.")
    raw = path.read_bytes()
    return replace(template, relative_path=path.relative_to(root).as_posix(),
                   size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def create_synthetic_fixture(root: Path) -> MapLayer:
    """Create only new, tiny data in the caller's owned temporary directory."""
    path = root / "maps" / "合成 分区.gpkg"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ViewInputError("Fixture output already exists; refusing to overwrite.")
    rings = [
        [(500000, 4000000), (500240, 4000000), (500240, 4000240), (500000, 4000240)],
        [(500320, 4000120), (500560, 4000120), (500560, 4000340), (500320, 4000340)],
        [(500140, 4000360), (500340, 4000360), (500340, 4000520), (500140, 4000520)],
    ]
    frame = gpd.GeoDataFrame({"feature_id": ["SYN-A", "SYN-B", "SYN-C"],
                             "name": ["合成分区 A", "合成分区 B", "合成分区 C"],
                             "color": ["#98c6dc", "#b9d9b4", "#edc691"]},
                            geometry=[Polygon(ring) for ring in rings], crs="EPSG:32631")
    frame.to_file(path, layer=LAYER_NAME, driver="GPKG", engine="pyogrio", index=False)
    template = build_resource("maps/synthetic.gpkg", fmt=ResourceFormat.GEOPACKAGE)
    resource = resource_for(path, root, template)
    return MapLayer("q01-synthetic-polygons", build_versions(), build_space(), resource,
                    GeometryType.POLYGON, source_layer=LAYER_NAME)


def load_features(root: Path, layer: MapLayer) -> tuple[Feature, ...]:
    """All-or-nothing checked view consumption; no CRS guessing or conversion."""
    if (not isinstance(layer, MapLayer) or layer.data_state != DataState.READY
            or layer.geometry_type != GeometryType.POLYGON
            or layer.resource.format != ResourceFormat.GEOPACKAGE or not layer.source_layer):
        raise ViewInputError("This PoC requires a ready C0 polygon GeoPackage layer.")
    root = root.resolve()
    relative = layer.resource.relative_path
    if ("\\" in relative or ":" in relative or relative.startswith("/")
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ViewInputError("Resource path must be repository/fixture-relative.")
    try:
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ViewInputError("Resolved resource escapes its declared root.")
        if not path.is_file() or path.stat().st_size > MAX_RESOURCE_BYTES:
            raise ViewInputError("Resource is missing, not a file, or too large.")
        raw = path.read_bytes()
        if len(raw) != layer.resource.size_bytes or hashlib.sha256(raw).hexdigest() != layer.resource.sha256:
            raise ViewInputError("Resource size/hash does not match C0 manifest.")
        frame = gpd.read_file(path, layer=layer.source_layer, engine="pyogrio")
    except ViewInputError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise ViewInputError("Resource could not be safely read as a GeoPackage layer.") from exc
    if frame.crs is None or not CRS.from_user_input(frame.crs).equals(CRS.from_user_input(layer.space.crs)):
        raise ViewInputError("Explicit resource CRS must equal the C0 view CRS.")
    if not 0 < len(frame) <= MAX_FEATURES or not {"feature_id", "name", "color"} <= set(frame.columns):
        raise ViewInputError("Missing fixture fields or unsupported feature count.")
    result: list[Feature] = []
    seen: set[str] = set()
    for _, row in frame.iterrows():
        identifier, name, color, geometry = row["feature_id"], row["name"], row["color"], row.geometry
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in seen:
            raise ViewInputError("Feature IDs must be stable, unique ASCII identifiers.")
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ViewInputError("Feature display name is missing or unbounded.")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ViewInputError("Fixture color must be an explicit RGB hex value.")
        if (geometry is None or geometry.geom_type != "Polygon" or geometry.is_empty
                or not geometry.is_valid or geometry.has_z or len(geometry.interiors)):
            raise ViewInputError("Only valid, nonempty, simple 2D polygons are supported.")
        points = tuple((float(x), float(y)) for x, y in geometry.exterior.coords)
        if not 4 <= len(points) <= 4096 or not all(math.isfinite(n) for p in points for n in p):
            raise ViewInputError("Polygon coordinate count/values are invalid.")
        seen.add(identifier)
        result.append(Feature(identifier, name, color, points))
    return tuple(result)


def choose_font(application: QApplication) -> str:
    families = set(QFontDatabase.families())
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "SimSun"):
        if family in families:
            application.setFont(QFont(family, 10))
            return family
    return application.font().family()


class MapView(QGraphicsView):
    selection_changed = Signal(tuple)

    def __init__(self, features: tuple[Feature, ...]) -> None:
        super().__init__()
        self.features = features
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#f4f7fa"))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._drag: QPoint | None = None
        self._fit_scale = 1.0
        self.items_by_id: dict[str, QGraphicsPolygonItem] = {}
        for feature in features:
            polygon = QPolygonF([QPointF(x, -y) for x, y in feature.points])
            item = scene.addPolygon(polygon, QPen(QColor("#37516a"), 2), QBrush(QColor(feature.color)))
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            item.setData(0, feature.identifier)
            item.setToolTip(feature.name)
            self.items_by_id[feature.identifier] = item
            label = QGraphicsSimpleTextItem(feature.name, item)
            label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            label.setBrush(QBrush(QColor("#233c50")))
            label.setFont(QFont(QApplication.font().family(), 13))
            label.setPos(feature.center - label.boundingRect().center())
        self.feature_bounds = scene.itemsBoundingRect()
        scene.setSceneRect(self.feature_bounds.adjusted(-700, -700, 700, 700))
        scene.selectionChanged.connect(self._selection)
        self.setAccessibleName("合成地图：左键选中，中键平移，滚轮缩放")

    def _selection(self) -> None:
        identifiers = tuple(sorted(item.data(0) for item in self.scene().selectedItems()))
        self.selection_changed.emit(identifiers)

    def fit_all(self) -> None:
        self.resetTransform()
        self.fitInView(self.feature_bounds.adjusted(-45, -45, 45, 45), Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = self.transform().m11()

    def zoom(self, factor: float) -> None:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
            raise ViewInputError("Zoom factor must be finite and positive.")
        scale = self.transform().m11() * factor
        if self._fit_scale / 8 <= scale <= self._fit_scale * 16:
            self.scale(factor, factor)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.zoom(1.25 if delta > 0 else 0.8)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._drag = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            current = event.position().toPoint()
            delta = current - self._drag
            self._drag = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._drag is not None:
            self._drag = None
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class ShellWindow(QMainWindow):
    """Visible native window with read-only selection details, never a core editor."""

    def __init__(self, root: Path, layer: MapLayer) -> None:
        features = load_features(root, layer)
        super().__init__()
        self.layer = layer
        self.setWindowTitle("CSL2 Planner · Q-01.0-A 原生地图 PoC")
        self.resize(1180, 780)
        self.view = MapView(features)
        self.setCentralWidget(self.view)
        toolbar = QToolBar("地图工具", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.zoom_in_button = QToolButton()
        self.zoom_in_button.setText("放大 +")
        self.zoom_in_button.clicked.connect(lambda: self.view.zoom(1.25))
        self.zoom_out_button = QToolButton()
        self.zoom_out_button.setText("缩小 −")
        self.zoom_out_button.clicked.connect(lambda: self.view.zoom(0.8))
        self.fit_button = QToolButton()
        self.fit_button.setText("全图")
        self.fit_button.clicked.connect(self.view.fit_all)
        for button in (self.zoom_in_button, self.zoom_out_button, self.fit_button):
            toolbar.addWidget(button)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("  左键选中 · 中键平移 · 滚轮缩放  "))
        self.layers = QListWidget()
        self.layers.addItem("合成分区 · 3 个多边形")
        self.layers.addItem("坐标：EPSG:32631 / 米")
        self.layers.addItem("来源：临时合成 GeoPackage")
        dock = QDockWidget("图层与数据边界", self)
        dock.setWidget(self.layers)
        dock.setMinimumWidth(240)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText("请选择合成分区。\n\n仅验证渲染与交互：\n不计算需求或交通结果；\n不编辑分区或工程；\n不判断三维安全；\n不接后台作业或指南。")
        information = QDockWidget("选中详情（只读）", self)
        information.setWidget(self.details)
        information.setMinimumWidth(250)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, information)
        self.view.selection_changed.connect(self._details)
        self.statusBar().showMessage("合成数据 · C0 " + layer.versions.schema_version + " · 地图交互 PoC，非规划结果")
        self.menuBar().addMenu("文件").addAction("关闭", self.close)
        self.menuBar().addMenu("视图").addAction("全图", self.view.fit_all)

    def _details(self, identifiers: tuple[str, ...]) -> None:
        selected = [f for f in self.view.features if f.identifier in identifiers]
        self.details.setPlainText("\n\n".join(f"{f.name}\nID: {f.identifier}\n来源：合成数据\n坐标单位：米\n只读选中，不修改 P 核心数据" for f in selected) or "未选中分区。")
