from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Iterable

from gateforge.visualization.model import (
    VisualBounds,
    VisualElement,
    VisualPoint,
    VisualScene,
    VisualizationError,
)


@dataclass(frozen=True, slots=True)
class Viewport:
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        for name in ("scale", "offset_x", "offset_y"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise VisualizationError(f"Viewport {name} must be finite")
            object.__setattr__(self, name, value)
        if self.scale <= 0:
            raise VisualizationError("Viewport scale must be positive")

    @classmethod
    def fit(
        cls,
        bounds: VisualBounds,
        width: float,
        height: float,
        padding: float = 32.0,
    ) -> "Viewport":
        usable_width = max(1.0, float(width) - 2 * padding)
        usable_height = max(1.0, float(height) - 2 * padding)
        scale = min(
            usable_width / max(bounds.width, 1.0),
            usable_height / max(bounds.height, 1.0),
        )
        center_x = (bounds.min_x + bounds.max_x) / 2
        center_y = (bounds.min_y + bounds.max_y) / 2
        return cls(scale, -center_x * scale, -center_y * scale)

    def world_to_screen(
        self,
        point: VisualPoint,
        width: float,
        height: float,
    ) -> VisualPoint:
        return VisualPoint(
            width / 2 + self.offset_x + point.x * self.scale,
            height / 2 + self.offset_y + point.y * self.scale,
        )

    def screen_to_world(
        self,
        point: VisualPoint,
        width: float,
        height: float,
    ) -> VisualPoint:
        return VisualPoint(
            (point.x - width / 2 - self.offset_x) / self.scale,
            (point.y - height / 2 - self.offset_y) / self.scale,
        )

    def pan(self, delta_x: float, delta_y: float) -> "Viewport":
        return Viewport(
            self.scale,
            self.offset_x + delta_x,
            self.offset_y + delta_y,
        )

    def zoom_at(
        self,
        factor: float,
        focus: VisualPoint,
        width: float,
        height: float,
    ) -> "Viewport":
        factor = float(factor)
        if not math.isfinite(factor) or factor <= 0:
            raise VisualizationError("Viewport zoom factor must be positive")
        world_focus = self.screen_to_world(focus, width, height)
        scale = min(20.0, max(0.02, self.scale * factor))
        return Viewport(
            scale,
            focus.x - width / 2 - world_focus.x * scale,
            focus.y - height / 2 - world_focus.y * scale,
        )


def pick_element(scene: VisualScene, point: VisualPoint) -> VisualElement | None:
    for element in reversed(scene.elements):
        local = _inverse_transform(element, point)
        if element.descriptor.bounds.contains(local):
            return element
    return None


def find_collisions(scene: VisualScene) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for index, left in enumerate(scene.elements):
        if not _is_physical(left):
            continue
        for right in scene.elements[index + 1 :]:
            if _is_physical(right) and left.world_bounds.intersects(right.world_bounds):
                result.append(tuple(sorted((left.identifier, right.identifier))))
    return tuple(sorted(result))


def flatten_point_coordinates(points: Iterable[VisualPoint]) -> tuple[float, ...]:
    return tuple(coordinate for point in points for coordinate in (point.x, point.y))


def _inverse_transform(element: VisualElement, point: VisualPoint) -> VisualPoint:
    translated_x = point.x - element.transform.x
    translated_y = point.y - element.transform.y
    radians = math.radians(-element.transform.angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return VisualPoint(
        translated_x * cosine - translated_y * sine,
        translated_x * sine + translated_y * cosine,
    )


def _is_physical(element: VisualElement) -> bool:
    return element.collision_enabled