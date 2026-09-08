from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

from gateforge.target import PortDirection


class VisualizationError(ValueError):
    pass


def _finite(value: float, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualizationError(f"{context} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise VisualizationError(f"{context} must be finite")
    return normalized


@dataclass(frozen=True, slots=True, order=True)
class VisualPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "Visual point X"))
        object.__setattr__(self, "y", _finite(self.y, "Visual point Y"))


@dataclass(frozen=True, slots=True)
class VisualBounds:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        for name in ("min_x", "min_y", "max_x", "max_y"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.max_x < self.min_x or self.max_y < self.min_y:
            raise VisualizationError("Visual bounds must have nonnegative dimensions")

    @classmethod
    def centered(cls, width: float, height: float) -> "VisualBounds":
        width = _finite(width, "Visual width")
        height = _finite(height, "Visual height")
        if width <= 0 or height <= 0:
            raise VisualizationError("Visual dimensions must be positive")
        return cls(-width / 2, -height / 2, width / 2, height / 2)

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def contains(self, point: VisualPoint) -> bool:
        return (
            self.min_x <= point.x <= self.max_x
            and self.min_y <= point.y <= self.max_y
        )

    def intersects(self, other: "VisualBounds") -> bool:
        return not (
            self.max_x <= other.min_x
            or other.max_x <= self.min_x
            or self.max_y <= other.min_y
            or other.max_y <= self.min_y
        )


@dataclass(frozen=True, slots=True)
class VisualTransform:
    x: float
    y: float
    angle: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "Visual transform X"))
        object.__setattr__(self, "y", _finite(self.y, "Visual transform Y"))
        object.__setattr__(self, "angle", _finite(self.angle, "Visual transform angle"))

    def apply(self, point: VisualPoint) -> VisualPoint:
        radians = math.radians(self.angle)
        cosine = math.cos(radians)
        sine = math.sin(radians)
        return VisualPoint(
            self.x + point.x * cosine - point.y * sine,
            self.y + point.x * sine + point.y * cosine,
        )


@dataclass(frozen=True, slots=True)
class VisualStyle:
    fill: str | None = None
    stroke: str | None = "#263238"
    stroke_width: float = 1.5
    dash: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        width = _finite(self.stroke_width, "Visual stroke width")
        if width < 0:
            raise VisualizationError("Visual stroke width must be nonnegative")
        if any(not isinstance(item, int) or item <= 0 for item in self.dash):
            raise VisualizationError("Visual dash lengths must be positive integers")
        object.__setattr__(self, "stroke_width", width)


@dataclass(frozen=True, slots=True)
class VisualRectangle:
    bounds: VisualBounds
    style: VisualStyle = VisualStyle()


@dataclass(frozen=True, slots=True)
class VisualEllipse:
    bounds: VisualBounds
    style: VisualStyle = VisualStyle()


@dataclass(frozen=True, slots=True)
class VisualPolyline:
    points: tuple[VisualPoint, ...]
    style: VisualStyle = VisualStyle(fill=None)

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise VisualizationError("Visual polylines require at least two points")


@dataclass(frozen=True, slots=True)
class VisualPolygon:
    points: tuple[VisualPoint, ...]
    style: VisualStyle = VisualStyle()

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise VisualizationError("Visual polygons require at least three points")


@dataclass(frozen=True, slots=True)
class VisualText:
    position: VisualPoint
    text: str
    color: str = "#102027"
    anchor: str = "center"

    def __post_init__(self) -> None:
        if not self.text:
            raise VisualizationError("Visual text must not be empty")


type VisualPrimitive = (
    VisualRectangle | VisualEllipse | VisualPolyline | VisualPolygon | VisualText
)


@dataclass(frozen=True, slots=True, order=True)
class VisualReference:
    kind: str
    identifier: str

    def __post_init__(self) -> None:
        if not self.kind or not self.identifier:
            raise VisualizationError("Visual references require kind and identifier")


@dataclass(frozen=True, slots=True)
class VisualProperty:
    name: str
    value: str
    group: str = "General"

    def __post_init__(self) -> None:
        if not self.name or not self.group:
            raise VisualizationError("Visual properties require name and group")


@dataclass(frozen=True, slots=True)
class VisualPort:
    identifier: str
    label: str
    direction: PortDirection
    position: VisualPoint

    def __post_init__(self) -> None:
        if not self.identifier:
            raise VisualizationError("Visual port identifier must not be empty")


@dataclass(frozen=True, slots=True)
class VisualElementDescriptor:
    label: str
    bounds: VisualBounds
    primitives: tuple[VisualPrimitive, ...]
    ports: tuple[VisualPort, ...] = ()
    properties: tuple[VisualProperty, ...] = ()

    def __post_init__(self) -> None:
        if not self.label:
            raise VisualizationError("Visual element label must not be empty")
        if not self.primitives:
            raise VisualizationError("Visual elements require at least one primitive")
        identifiers = [item.identifier for item in self.ports]
        if len(identifiers) != len(set(identifiers)):
            raise VisualizationError("Visual element has duplicate port identifiers")


@dataclass(frozen=True, slots=True)
class VisualElement:
    identifier: str
    descriptor: VisualElementDescriptor
    transform: VisualTransform
    references: tuple[VisualReference, ...] = ()
    layer: int = 0
    collision_enabled: bool = True
    linked_scene: str | None = None

    def __post_init__(self) -> None:
        if not self.identifier:
            raise VisualizationError("Visual element identifier must not be empty")
        if not isinstance(self.layer, int) or isinstance(self.layer, bool):
            raise VisualizationError("Visual element layer must be an integer")
        if not isinstance(self.collision_enabled, bool):
            raise VisualizationError("Visual element collision flag must be boolean")
        if self.linked_scene == "":
            raise VisualizationError("Visual element linked scene must not be empty")
        if len(self.references) != len(set(self.references)):
            raise VisualizationError("Visual element has duplicate references")

    @property
    def world_bounds(self) -> VisualBounds:
        bounds = self.descriptor.bounds
        corners = (
            VisualPoint(bounds.min_x, bounds.min_y),
            VisualPoint(bounds.max_x, bounds.min_y),
            VisualPoint(bounds.max_x, bounds.max_y),
            VisualPoint(bounds.min_x, bounds.max_y),
        )
        transformed = tuple(self.transform.apply(point) for point in corners)
        return VisualBounds(
            min(item.x for item in transformed),
            min(item.y for item in transformed),
            max(item.x for item in transformed),
            max(item.y for item in transformed),
        )

    def port_position(self, identifier: str) -> VisualPoint:
        try:
            port = next(
                item for item in self.descriptor.ports if item.identifier == identifier
            )
        except StopIteration as error:
            raise VisualizationError(
                f"Visual element {self.identifier!r} has no port {identifier!r}"
            ) from error
        return self.transform.apply(port.position)


@dataclass(frozen=True, slots=True, order=True)
class VisualEndpoint:
    element: str
    port: str

    def __post_init__(self) -> None:
        if not self.element or not self.port:
            raise VisualizationError("Visual endpoints require element and port IDs")


@dataclass(frozen=True, slots=True)
class VisualNet:
    identifier: str
    label: str
    endpoints: tuple[VisualEndpoint, ...]
    properties: tuple[VisualProperty, ...] = ()

    def __post_init__(self) -> None:
        if not self.identifier:
            raise VisualizationError("Visual net identifier must not be empty")
        endpoints = tuple(sorted(set(self.endpoints)))
        if len(endpoints) < 2:
            raise VisualizationError("Visual nets require at least two endpoints")
        object.__setattr__(self, "endpoints", endpoints)


@dataclass(frozen=True, slots=True)
class VisualScene:
    identifier: str
    label: str
    bounds: VisualBounds
    elements: tuple[VisualElement, ...]
    nets: tuple[VisualNet, ...] = ()
    parent: str | None = None
    grid_size: float | None = None

    def __post_init__(self) -> None:
        if not self.identifier or not self.label:
            raise VisualizationError("Visual scenes require identifier and label")
        if self.grid_size is not None:
            grid_size = _finite(self.grid_size, "Visual scene grid size")
            if grid_size <= 0:
                raise VisualizationError("Visual scene grid size must be positive")
            object.__setattr__(self, "grid_size", grid_size)
        elements = tuple(sorted(self.elements, key=lambda item: (item.layer, item.identifier)))
        nets = tuple(sorted(self.nets, key=lambda item: item.identifier))
        element_by_id = {item.identifier: item for item in elements}
        if len(element_by_id) != len(elements):
            raise VisualizationError("Visual scene has duplicate element identifiers")
        if len({item.identifier for item in nets}) != len(nets):
            raise VisualizationError("Visual scene has duplicate net identifiers")
        for net in nets:
            for endpoint in net.endpoints:
                try:
                    element = element_by_id[endpoint.element]
                except KeyError as error:
                    raise VisualizationError(
                        f"Visual net {net.identifier!r} references unknown element "
                        f"{endpoint.element!r}"
                    ) from error
                element.port_position(endpoint.port)
        object.__setattr__(self, "elements", elements)
        object.__setattr__(self, "nets", nets)

    def element(self, identifier: str) -> VisualElement:
        try:
            return next(item for item in self.elements if item.identifier == identifier)
        except StopIteration as error:
            raise VisualizationError(
                f"Visual scene {self.identifier!r} has no element {identifier!r}"
            ) from error

    def nets_for_element(self, identifier: str) -> tuple[VisualNet, ...]:
        return tuple(
            net
            for net in self.nets
            if any(endpoint.element == identifier for endpoint in net.endpoints)
        )


@dataclass(frozen=True, slots=True)
class VisualView:
    identifier: str
    label: str
    scenes: tuple[VisualScene, ...]

    def __post_init__(self) -> None:
        if not self.identifier or not self.label:
            raise VisualizationError("Visual views require identifier and label")
        scenes = tuple(
            sorted(
                self.scenes,
                key=lambda item: (item.parent is not None, item.identifier),
            )
        )
        by_id = {item.identifier: item for item in scenes}
        if len(by_id) != len(scenes):
            raise VisualizationError("Visual view has duplicate scene identifiers")
        for scene in scenes:
            if scene.parent is not None and scene.parent not in by_id:
                raise VisualizationError(
                    f"Visual scene {scene.identifier!r} has unknown parent "
                    f"{scene.parent!r}"
                )
            for element in scene.elements:
                if element.linked_scene is not None and element.linked_scene not in by_id:
                    raise VisualizationError(
                        f"Visual element {element.identifier!r} links to unknown scene "
                        f"{element.linked_scene!r}"
                    )
        object.__setattr__(self, "scenes", scenes)


@dataclass(frozen=True, slots=True)
class VisualDocument:
    views: tuple[VisualView, ...]

    def __post_init__(self) -> None:
        views = tuple(sorted(self.views, key=lambda item: item.identifier))
        if not views:
            raise VisualizationError("Visual documents require at least one view")
        if len({item.identifier for item in views}) != len(views):
            raise VisualizationError("Visual document has duplicate view identifiers")
        object.__setattr__(self, "views", views)

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "views": [_view_data(view) for view in self.views],
        }

    @classmethod
    def from_canonical_data(cls, value: object) -> "VisualDocument":
        data = _object(value, "visual document")
        _keys(data, {"schema_version", "views"}, "visual document")
        if _integer(data["schema_version"], "schema_version") != 1:
            raise VisualizationError("Unsupported visual document schema version")
        return cls(tuple(_view(item) for item in _array(data["views"], "views")))


def _point_data(value: VisualPoint) -> dict[str, object]:
    return {"x": value.x, "y": value.y}


def _point(value: object) -> VisualPoint:
    data = _object(value, "point")
    _keys(data, {"x", "y"}, "point")
    return VisualPoint(_number(data["x"], "point.x"), _number(data["y"], "point.y"))


def _bounds_data(value: VisualBounds) -> dict[str, object]:
    return {
        "min_x": value.min_x,
        "min_y": value.min_y,
        "max_x": value.max_x,
        "max_y": value.max_y,
    }


def _bounds(value: object) -> VisualBounds:
    data = _object(value, "bounds")
    _keys(data, {"min_x", "min_y", "max_x", "max_y"}, "bounds")
    return VisualBounds(
        _number(data["min_x"], "bounds.min_x"),
        _number(data["min_y"], "bounds.min_y"),
        _number(data["max_x"], "bounds.max_x"),
        _number(data["max_y"], "bounds.max_y"),
    )


def _style_data(value: VisualStyle) -> dict[str, object]:
    return {
        "fill": value.fill,
        "stroke": value.stroke,
        "stroke_width": value.stroke_width,
        "dash": list(value.dash),
    }


def _style(value: object) -> VisualStyle:
    data = _object(value, "style")
    _keys(data, {"fill", "stroke", "stroke_width", "dash"}, "style")
    return VisualStyle(
        fill=_optional_string(data["fill"], "style.fill"),
        stroke=_optional_string(data["stroke"], "style.stroke"),
        stroke_width=_number(data["stroke_width"], "style.stroke_width"),
        dash=tuple(
            _integer(item, "style.dash")
            for item in _array(data["dash"], "style.dash")
        ),
    )


def _primitive_data(value: VisualPrimitive) -> dict[str, object]:
    if isinstance(value, VisualRectangle):
        return {
            "kind": "rectangle",
            "bounds": _bounds_data(value.bounds),
            "style": _style_data(value.style),
        }
    if isinstance(value, VisualEllipse):
        return {
            "kind": "ellipse",
            "bounds": _bounds_data(value.bounds),
            "style": _style_data(value.style),
        }
    if isinstance(value, VisualPolyline):
        return {
            "kind": "polyline",
            "points": [_point_data(item) for item in value.points],
            "style": _style_data(value.style),
        }
    if isinstance(value, VisualPolygon):
        return {
            "kind": "polygon",
            "points": [_point_data(item) for item in value.points],
            "style": _style_data(value.style),
        }
    if isinstance(value, VisualText):
        return {
            "kind": "text",
            "position": _point_data(value.position),
            "text": value.text,
            "color": value.color,
            "anchor": value.anchor,
        }
    raise AssertionError(f"Unsupported visual primitive {type(value).__name__}")


def _primitive(value: object) -> VisualPrimitive:
    data = _object(value, "primitive")
    kind = _string(data.get("kind"), "primitive.kind")
    if kind in {"rectangle", "ellipse"}:
        _keys(data, {"kind", "bounds", "style"}, "primitive")
        bounds = _bounds(data["bounds"])
        style = _style(data["style"])
        return (
            VisualRectangle(bounds, style)
            if kind == "rectangle"
            else VisualEllipse(bounds, style)
        )
    if kind in {"polyline", "polygon"}:
        _keys(data, {"kind", "points", "style"}, "primitive")
        points = tuple(_point(item) for item in _array(data["points"], "points"))
        style = _style(data["style"])
        return (
            VisualPolyline(points, style)
            if kind == "polyline"
            else VisualPolygon(points, style)
        )
    if kind == "text":
        _keys(data, {"kind", "position", "text", "color", "anchor"}, "primitive")
        return VisualText(
            _point(data["position"]),
            _string(data["text"], "primitive.text"),
            _string(data["color"], "primitive.color"),
            _string(data["anchor"], "primitive.anchor"),
        )
    raise VisualizationError(f"Unknown visual primitive kind {kind!r}")


def _property_data(value: VisualProperty) -> dict[str, object]:
    return {"name": value.name, "value": value.value, "group": value.group}


def _property(value: object) -> VisualProperty:
    data = _object(value, "property")
    _keys(data, {"name", "value", "group"}, "property")
    return VisualProperty(
        _string(data["name"], "property.name"),
        _string(data["value"], "property.value"),
        _string(data["group"], "property.group"),
    )


def _port_data(value: VisualPort) -> dict[str, object]:
    return {
        "identifier": value.identifier,
        "label": value.label,
        "direction": value.direction.value,
        "position": _point_data(value.position),
    }


def _port(value: object) -> VisualPort:
    data = _object(value, "port")
    _keys(data, {"identifier", "label", "direction", "position"}, "port")
    try:
        direction = PortDirection(_string(data["direction"], "port.direction"))
    except ValueError as error:
        raise VisualizationError(f"Unknown port direction {data['direction']!r}") from error
    return VisualPort(
        _string(data["identifier"], "port.identifier"),
        _string(data["label"], "port.label"),
        direction,
        _point(data["position"]),
    )


def _descriptor_data(value: VisualElementDescriptor) -> dict[str, object]:
    return {
        "label": value.label,
        "bounds": _bounds_data(value.bounds),
        "primitives": [_primitive_data(item) for item in value.primitives],
        "ports": [_port_data(item) for item in value.ports],
        "properties": [_property_data(item) for item in value.properties],
    }


def _descriptor(value: object) -> VisualElementDescriptor:
    data = _object(value, "descriptor")
    _keys(data, {"label", "bounds", "primitives", "ports", "properties"}, "descriptor")
    return VisualElementDescriptor(
        _string(data["label"], "descriptor.label"),
        _bounds(data["bounds"]),
        tuple(_primitive(item) for item in _array(data["primitives"], "primitives")),
        tuple(_port(item) for item in _array(data["ports"], "ports")),
        tuple(_property(item) for item in _array(data["properties"], "properties")),
    )


def _element_data(value: VisualElement) -> dict[str, object]:
    return {
        "identifier": value.identifier,
        "descriptor": _descriptor_data(value.descriptor),
        "transform": {
            "x": value.transform.x,
            "y": value.transform.y,
            "angle": value.transform.angle,
        },
        "references": [
            {"kind": item.kind, "identifier": item.identifier}
            for item in value.references
        ],
        "layer": value.layer,
        "collision_enabled": value.collision_enabled,
        "linked_scene": value.linked_scene,
    }


def _element(value: object) -> VisualElement:
    data = _object(value, "element")
    _keys(
        data,
        {
            "identifier",
            "descriptor",
            "transform",
            "references",
            "layer",
            "collision_enabled",
            "linked_scene",
        },
        "element",
    )
    transform = _object(data["transform"], "transform")
    _keys(transform, {"x", "y", "angle"}, "transform")
    references = []
    for value in _array(data["references"], "references"):
        reference = _object(value, "reference")
        _keys(reference, {"kind", "identifier"}, "reference")
        references.append(
            VisualReference(
                _string(reference["kind"], "reference.kind"),
                _string(reference["identifier"], "reference.identifier"),
            )
        )
    collision_enabled = data["collision_enabled"]
    if type(collision_enabled) is not bool:
        raise VisualizationError("element.collision_enabled must be a boolean")
    return VisualElement(
        _string(data["identifier"], "element.identifier"),
        _descriptor(data["descriptor"]),
        VisualTransform(
            _number(transform["x"], "transform.x"),
            _number(transform["y"], "transform.y"),
            _number(transform["angle"], "transform.angle"),
        ),
        tuple(references),
        _integer(data["layer"], "element.layer"),
        collision_enabled,
        _optional_string(data["linked_scene"], "element.linked_scene"),
    )


def _net_data(value: VisualNet) -> dict[str, object]:
    return {
        "identifier": value.identifier,
        "label": value.label,
        "endpoints": [
            {"element": item.element, "port": item.port} for item in value.endpoints
        ],
        "properties": [_property_data(item) for item in value.properties],
    }


def _net(value: object) -> VisualNet:
    data = _object(value, "net")
    _keys(data, {"identifier", "label", "endpoints", "properties"}, "net")
    endpoints = []
    for value in _array(data["endpoints"], "endpoints"):
        endpoint = _object(value, "endpoint")
        _keys(endpoint, {"element", "port"}, "endpoint")
        endpoints.append(
            VisualEndpoint(
                _string(endpoint["element"], "endpoint.element"),
                _string(endpoint["port"], "endpoint.port"),
            )
        )
    return VisualNet(
        _string(data["identifier"], "net.identifier"),
        _string(data["label"], "net.label"),
        tuple(endpoints),
        tuple(_property(item) for item in _array(data["properties"], "properties")),
    )


def _scene_data(value: VisualScene) -> dict[str, object]:
    return {
        "identifier": value.identifier,
        "label": value.label,
        "bounds": _bounds_data(value.bounds),
        "elements": [_element_data(item) for item in value.elements],
        "nets": [_net_data(item) for item in value.nets],
        "parent": value.parent,
        "grid_size": value.grid_size,
    }


def _scene(value: object) -> VisualScene:
    data = _object(value, "scene")
    _keys(
        data,
        {"identifier", "label", "bounds", "elements", "nets", "parent", "grid_size"},
        "scene",
    )
    grid_size = data["grid_size"]
    return VisualScene(
        _string(data["identifier"], "scene.identifier"),
        _string(data["label"], "scene.label"),
        _bounds(data["bounds"]),
        tuple(_element(item) for item in _array(data["elements"], "elements")),
        tuple(_net(item) for item in _array(data["nets"], "nets")),
        _optional_string(data["parent"], "scene.parent"),
        None if grid_size is None else _number(grid_size, "scene.grid_size"),
    )


def _view_data(value: VisualView) -> dict[str, object]:
    return {
        "identifier": value.identifier,
        "label": value.label,
        "scenes": [_scene_data(item) for item in value.scenes],
    }


def _view(value: object) -> VisualView:
    data = _object(value, "view")
    _keys(data, {"identifier", "label", "scenes"}, "view")
    return VisualView(
        _string(data["identifier"], "view.identifier"),
        _string(data["label"], "view.label"),
        tuple(_scene(item) for item in _array(data["scenes"], "scenes")),
    )


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise VisualizationError(f"{context} must be an object")
    return dict(value)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise VisualizationError(f"{context} must be an array")
    return value


def _keys(data: Mapping[str, object], expected: set[str], context: str) -> None:
    actual = set(data)
    if actual != expected:
        raise VisualizationError(
            f"Invalid {context} fields; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise VisualizationError(f"{context} must be a string")
    return value


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _number(value: object, context: str) -> float:
    try:
        return _finite(value, context)  # type: ignore[arg-type]
    except VisualizationError:
        raise


def _integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise VisualizationError(f"{context} must be an integer")
    return value