from __future__ import annotations

import hashlib

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from gateforge.target import PortDirection
from gateforge.visualization.interaction import find_collisions
from gateforge.visualization.model import (
    VisualElement,
    VisualEllipse,
    VisualPoint,
    VisualPolygon,
    VisualPolyline,
    VisualRectangle,
    VisualScene,
    VisualStyle,
    VisualText,
)


ELEMENT_ID_ROLE = 1
LINKED_SCENE_ROLE = 2
_WIRE_COLOR = "#427a78"
_DIMMED_WIRE_COLOR = "#aebbb7"
_HIGHLIGHT_LANE_SPACING = 6.0
_HIGHLIGHT_LEAD_LENGTH = 10.0


class ZoomableGraphicsView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#f4f3ef"))

    def fit_scene(self) -> None:
        scene = self.scene()
        if scene is None or scene.itemsBoundingRect().isEmpty():
            return
        self.fitInView(
            scene.itemsBoundingRect().adjusted(-24.0, -24.0, 24.0, 24.0),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.angleDelta().y() == 0:
            return
        before = self.mapToScene(event.position().toPoint())
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        self.scale(factor, factor)
        after = self.mapToScene(event.position().toPoint())
        delta = after - before
        self.translate(delta.x(), delta.y())
        event.accept()


class YosysSvgView(ZoomableGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self._renderer: QSvgRenderer | None = None
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

    def load_svg(self, svg: str) -> None:
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")), self)
        if not renderer.isValid():
            raise ValueError("Yosys schematic is not valid SVG")
        self._scene.clear()
        item = QGraphicsSvgItem()
        item.setSharedRenderer(renderer)
        self._scene.addItem(item)
        self._renderer = renderer
        self._scene.setSceneRect(item.boundingRect())
        self.fit_scene()


class VisualSceneView(ZoomableGraphicsView):
    element_selected = Signal(str)
    scene_activated = Signal(str)

    def __init__(self, scene: VisualScene | None = None) -> None:
        super().__init__()
        self._graphics_scene = QGraphicsScene(self)
        self._graphics_scene.selectionChanged.connect(self._selection_changed)
        self.setScene(self._graphics_scene)
        self.visual_scene: VisualScene | None = None
        self.show_wires = True
        self.show_labels = True
        self.show_bounds = False
        self.show_collisions = True
        self._net_items: dict[str, list[QGraphicsPathItem]] = {}
        self._net_routes: dict[
            str,
            list[tuple[QGraphicsPathItem, VisualPoint, VisualPoint, float]],
        ] = {}
        self._element_items: dict[str, QGraphicsItemGroup] = {}
        self._port_items: dict[
            tuple[str, str],
            tuple[QGraphicsEllipseItem, PortDirection],
        ] = {}
        self._label_items: list[QGraphicsSimpleTextItem] = []
        self._bound_items: list[QGraphicsRectItem] = []
        self._collision_items: list[QGraphicsRectItem] = []
        if scene is not None:
            self.set_visual_scene(scene)

    def set_visual_scene(self, scene: VisualScene) -> None:
        self.visual_scene = scene
        self._net_items.clear()
        self._net_routes.clear()
        self._element_items.clear()
        self._port_items.clear()
        self._label_items.clear()
        self._bound_items.clear()
        self._collision_items.clear()
        self._graphics_scene.clear()
        self._draw_nets(scene)
        colliding = {
            identifier for pair in find_collisions(scene) for identifier in pair
        }
        for element in scene.elements:
            self._graphics_scene.addItem(self._element_item(element))
            self._add_element_overlay(element, element.identifier in colliding)
        bounds = scene.bounds
        margin = max(24.0, (bounds.width + bounds.height) * 0.025)
        self._graphics_scene.setSceneRect(
            QRectF(
                bounds.min_x - margin,
                bounds.min_y - margin,
                bounds.width + margin * 2,
                bounds.height + margin * 2,
            )
        )
        self.set_overlays(
            wires=self.show_wires,
            labels=self.show_labels,
            bounds=self.show_bounds,
            collisions=self.show_collisions,
        )
        self.fit_scene()

    def set_overlays(
        self,
        *,
        wires: bool,
        labels: bool,
        bounds: bool,
        collisions: bool,
    ) -> None:
        self.show_wires = wires
        self.show_labels = labels
        self.show_bounds = bounds
        self.show_collisions = collisions
        for items in self._net_items.values():
            for item in items:
                item.setVisible(wires)
        for item in self._label_items:
            item.setVisible(labels)
        for item in self._bound_items:
            item.setVisible(bounds)
        for item in self._collision_items:
            item.setVisible(collisions)

    def highlight_net(self, identifier: str | None) -> None:
        selected = set() if identifier is None else {identifier}
        self.highlight_nets(selected)

    def highlight_nets(self, identifiers: set[str]) -> None:
        selected = identifiers & self._net_items.keys()
        colors = _highlight_colors(selected)
        lanes = _highlight_lanes(selected)
        for identifier in self._net_items:
            highlighted = identifier in selected
            color = QColor(
                colors[identifier]
                if highlighted
                else _DIMMED_WIRE_COLOR if selected else _WIRE_COLOR
            )
            width = 3.0 if highlighted else 1.0 if selected else 1.5
            for item, start, junction, side in self._net_routes[identifier]:
                item.setPen(QPen(color, width))
                item.setZValue(
                    -800.0 if highlighted else -1100.0 if selected else -1000.0
                )
                item.setPath(
                    _highlighted_wire_path(
                        start,
                        junction,
                        side,
                        lanes[identifier],
                    )
                    if highlighted
                    else _wire_path(start, junction)
                )

        connected_elements = set()
        net_by_id = (
            {}
            if self.visual_scene is None
            else {item.identifier: item for item in self.visual_scene.nets}
        )
        for identifier in selected:
            connected_elements.update(
                endpoint.element for endpoint in net_by_id[identifier].endpoints
            )
        for identifier, item in self._element_items.items():
            item.setOpacity(
                1.0 if not selected or identifier in connected_elements else 0.28
            )
        for marker, direction in self._port_items.values():
            marker.setBrush(QBrush(_port_color(direction)))
        for identifier in sorted(selected):
            color = QBrush(QColor(colors[identifier]))
            for endpoint in net_by_id[identifier].endpoints:
                port = self._port_items.get((endpoint.element, endpoint.port))
                if port is not None:
                    port[0].setBrush(color)

    def selected_element(self) -> VisualElement | None:
        if self.visual_scene is None:
            return None
        selected = self._graphics_scene.selectedItems()
        if not selected:
            return None
        identifier = selected[0].data(ELEMENT_ID_ROLE)
        if not isinstance(identifier, str):
            return None
        return self.visual_scene.element(identifier)

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(event)
        element = self.selected_element()
        if element is not None and element.linked_scene is not None:
            self.scene_activated.emit(element.linked_scene)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        scene = self.visual_scene
        if scene is None or scene.grid_size is None:
            return
        grid = scene.grid_size
        left = math_floor(rect.left() / grid) * grid
        top = math_floor(rect.top() / grid) * grid
        painter.save()
        painter.setPen(QPen(QColor("#deddd7"), 0.0))
        x = left
        while x <= rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += grid
        y = top
        while y <= rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += grid
        painter.restore()

    def _selection_changed(self) -> None:
        element = self.selected_element()
        if self.visual_scene is not None and element is not None:
            self.highlight_nets(
                {
                    net.identifier
                    for net in self.visual_scene.nets_for_element(element.identifier)
                }
            )
        else:
            self.highlight_nets(set())
        self.element_selected.emit("" if element is None else element.identifier)

    def _draw_nets(self, scene: VisualScene) -> None:
        for net in scene.nets:
            endpoints = tuple(
                (
                    scene.element(endpoint.element),
                    endpoint.port,
                )
                for endpoint in net.endpoints
            )
            points = tuple(
                element.port_position(port)
                for element, port in endpoints
            )
            if len(points) < 2:
                continue
            junction = VisualPoint(
                sum(point.x for point in points) / len(points),
                sum(point.y for point in points) / len(points),
            )
            items = []
            routes = []
            for (element, _port), point in zip(endpoints, points):
                side = _horizontal_port_side(element, point, junction)
                item = QGraphicsPathItem(_wire_path(point, junction))
                item.setPen(QPen(QColor(_WIRE_COLOR), 1.5))
                item.setZValue(-1000.0)
                item.setToolTip(net.label)
                self._graphics_scene.addItem(item)
                items.append(item)
                routes.append((item, point, junction, side))
            self._net_items[net.identifier] = items
            self._net_routes[net.identifier] = routes

    def _element_item(self, element: VisualElement) -> QGraphicsItemGroup:
        group = QGraphicsItemGroup()
        group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        group.setData(ELEMENT_ID_ROLE, element.identifier)
        group.setData(LINKED_SCENE_ROLE, element.linked_scene)
        group.setToolTip(element.descriptor.label)
        self._element_items[element.identifier] = group
        for primitive in element.descriptor.primitives:
            item = _primitive_item(primitive)
            if isinstance(item, QGraphicsSimpleTextItem):
                bounds = element.descriptor.bounds
                text_bounds = item.boundingRect()
                scale = min(1.0, bounds.width * 0.85 / max(1.0, text_bounds.width()), bounds.height * 0.85 / max(1.0, text_bounds.height()))
                item.setScale(scale)
                if isinstance(primitive, VisualText):
                    origin = _text_origin(primitive.position, QRectF(0, 0, text_bounds.width() * scale, text_bounds.height() * scale), primitive.anchor)
                    item.setPos(*origin)
                self._label_items.append(item)
            group.addToGroup(item)
        for port in element.descriptor.ports:
            marker = QGraphicsEllipseItem(-2.75, -2.75, 5.5, 5.5)
            marker.setPos(port.position.x, port.position.y)
            marker.setPen(QPen(QColor("#f4f3ef"), 0.8))
            marker.setBrush(QBrush(_port_color(port.direction)))
            marker.setToolTip(port.label)
            group.addToGroup(marker)
            self._port_items[(element.identifier, port.identifier)] = (
                marker,
                port.direction,
            )
        group.setPos(element.transform.x, element.transform.y)
        group.setRotation(element.transform.angle)
        group.setZValue(float(element.layer))
        return group

    def _add_element_overlay(self, element: VisualElement, colliding: bool) -> None:
        bounds = element.world_bounds
        rectangle = QRectF(bounds.min_x, bounds.min_y, bounds.width, bounds.height)
        bound_item = QGraphicsRectItem(rectangle)
        bound_pen = QPen(QColor("#6f817a"), 1.5, Qt.PenStyle.DashLine)
        bound_item.setPen(bound_pen)
        bound_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        bound_item.setZValue(1000.0)
        self._graphics_scene.addItem(bound_item)
        self._bound_items.append(bound_item)
        if colliding:
            collision_item = QGraphicsRectItem(rectangle)
            collision_item.setPen(QPen(QColor("#c3483f"), 3.0))
            collision_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            collision_item.setZValue(1001.0)
            self._graphics_scene.addItem(collision_item)
            self._collision_items.append(collision_item)


def _wire_path(start: VisualPoint, junction: VisualPoint) -> QPainterPath:
    path = QPainterPath(QPointF(start.x, start.y))
    path.lineTo(QPointF(junction.x, start.y))
    path.lineTo(QPointF(junction.x, junction.y))
    return path


def _highlighted_wire_path(
    start: VisualPoint,
    junction: VisualPoint,
    side: float,
    lane: float,
) -> QPainterPath:
    lead_x = start.x + side * _HIGHLIGHT_LEAD_LENGTH
    lane_y = start.y + lane
    trunk_x = junction.x + lane
    path = QPainterPath(QPointF(start.x, start.y))
    path.lineTo(QPointF(lead_x, start.y))
    path.lineTo(QPointF(lead_x, lane_y))
    path.lineTo(QPointF(trunk_x, lane_y))
    path.lineTo(QPointF(trunk_x, junction.y + lane))
    return path


def _horizontal_port_side(
    element: VisualElement,
    point: VisualPoint,
    junction: VisualPoint,
) -> float:
    bounds = element.world_bounds
    center_x = (bounds.min_x + bounds.max_x) / 2
    if point.x > center_x:
        return 1.0
    if point.x < center_x:
        return -1.0
    return 1.0 if junction.x >= point.x else -1.0


def _highlight_colors(identifiers: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    digests = {
        identifier: hashlib.sha256(identifier.encode("utf-8")).digest()
        for identifier in identifiers
    }
    used_hues: set[int] = set()
    minimum_distance = max(1, min(24, 300 // max(1, len(identifiers))))
    for identifier in sorted(identifiers, key=lambda item: digests[item]):
        digest = digests[identifier]
        initial_hue = int.from_bytes(digest[:2], "big") % 360
        hue = initial_hue
        for attempt in range(360):
            candidate = (initial_hue + attempt * 137) % 360
            if all(
                min(abs(candidate - used), 360 - abs(candidate - used))
                >= minimum_distance
                for used in used_hues
            ):
                hue = candidate
                break
        used_hues.add(hue)
        saturation = 180 + digest[2] % 51
        value = 165 + digest[3] % 46
        result[identifier] = QColor.fromHsv(hue, saturation, value).name()
    return result


def _highlight_lanes(identifiers: set[str]) -> dict[str, float]:
    ordered = sorted(identifiers)
    midpoint = (len(ordered) - 1) / 2
    return {
        identifier: (index - midpoint) * _HIGHLIGHT_LANE_SPACING
        for index, identifier in enumerate(ordered)
    }


def _primitive_item(primitive):
    if isinstance(primitive, VisualRectangle):
        bounds = primitive.bounds
        item = QGraphicsRectItem(
            bounds.min_x,
            bounds.min_y,
            bounds.width,
            bounds.height,
        )
        _apply_style(item, primitive.style)
        return item
    if isinstance(primitive, VisualEllipse):
        bounds = primitive.bounds
        item = QGraphicsEllipseItem(
            bounds.min_x,
            bounds.min_y,
            bounds.width,
            bounds.height,
        )
        _apply_style(item, primitive.style)
        return item
    if isinstance(primitive, VisualPolyline):
        path = _polyline_path(primitive.points)
        item = QGraphicsPathItem(path)
        item.setPen(_pen(primitive.style))
        return item
    if isinstance(primitive, VisualPolygon):
        item = QGraphicsPolygonItem(
            QPolygonF([QPointF(point.x, point.y) for point in primitive.points])
        )
        _apply_style(item, primitive.style)
        return item
    if isinstance(primitive, VisualText):
        item = QGraphicsSimpleTextItem(primitive.text)
        item.setBrush(QBrush(QColor(primitive.color)))
        bounds = item.boundingRect()
        x, y = _text_origin(primitive.position, bounds, primitive.anchor)
        item.setPos(x, y)
        return item
    raise TypeError(f"Unsupported visual primitive {type(primitive).__name__}")


def _polyline_path(points: tuple[VisualPoint, ...]) -> QPainterPath:
    path = QPainterPath(QPointF(points[0].x, points[0].y))
    for point in points[1:]:
        path.lineTo(QPointF(point.x, point.y))
    return path


def _apply_style(item, style: VisualStyle) -> None:
    item.setPen(_pen(style))
    item.setBrush(
        QBrush(Qt.BrushStyle.NoBrush)
        if style.fill is None
        else QBrush(QColor(style.fill))
    )


def _pen(style: VisualStyle) -> QPen:
    if style.stroke is None:
        return QPen(Qt.PenStyle.NoPen)
    pen = QPen(QColor(style.stroke), style.stroke_width)
    if style.dash:
        pen.setDashPattern([float(item) for item in style.dash])
    return pen


def _port_color(direction: PortDirection) -> QColor:
    if direction == PortDirection.INPUT:
        return QColor("#d28f2c")
    if direction == PortDirection.OUTPUT:
        return QColor("#20827f")
    return QColor("#7d5b8c")


def _text_origin(
    position: VisualPoint,
    bounds: QRectF,
    anchor: str,
) -> tuple[float, float]:
    horizontal = 0.0
    vertical = 0.0
    normalized = anchor.lower()
    if normalized == "center":
        return position.x - bounds.width() / 2, position.y - bounds.height() / 2
    if "e" in normalized:
        horizontal = bounds.width()
    elif "w" not in normalized:
        horizontal = bounds.width() / 2
    if "s" in normalized:
        vertical = bounds.height()
    elif "n" not in normalized:
        vertical = bounds.height() / 2
    return position.x - horizontal, position.y - vertical


def math_floor(value: float) -> int:
    return int(value // 1)