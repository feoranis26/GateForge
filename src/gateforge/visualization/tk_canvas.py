from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from gateforge.visualization.interaction import (
    Viewport,
    find_collisions,
    flatten_point_coordinates,
    pick_element,
)
from gateforge.visualization.model import (
    VisualElement,
    VisualEllipse,
    VisualNet,
    VisualPoint,
    VisualPolygon,
    VisualPolyline,
    VisualRectangle,
    VisualScene,
    VisualText,
)


class SceneCanvas(tk.Canvas):
    def __init__(
        self,
        parent,
        scene: VisualScene,
        *,
        on_select: Callable[[VisualElement | None], None],
        on_activate: Callable[[VisualElement], None],
    ) -> None:
        super().__init__(
            parent,
            background="#101816",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.scene = scene
        self.on_select = on_select
        self.on_activate = on_activate
        self.viewport = Viewport()
        self.selected_element: str | None = None
        self.highlighted_net: str | None = None
        self.show_wires = True
        self.show_labels = True
        self.show_bounds = False
        self.show_collisions = True
        self._fitted = False
        self._pan_origin: tuple[int, int] | None = None
        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<ButtonPress-2>", self._start_pan)
        self.bind("<B2-Motion>", self._pan)
        self.bind("<ButtonPress-3>", self._start_pan)
        self.bind("<B3-Motion>", self._pan)
        self.bind("<MouseWheel>", self._zoom)
        self.bind("<Button-4>", self._zoom)
        self.bind("<Button-5>", self._zoom)

    def fit_scene(self) -> None:
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self.viewport = Viewport.fit(self.scene.bounds, width, height, 42.0)
        self._fitted = True
        self.redraw()

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
        self.redraw()

    def highlight_net(self, identifier: str | None) -> None:
        self.highlighted_net = identifier
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self._draw_grid(width, height)
        board_min = self._screen(VisualPoint(self.scene.bounds.min_x, self.scene.bounds.min_y))
        board_max = self._screen(VisualPoint(self.scene.bounds.max_x, self.scene.bounds.max_y))
        self.create_rectangle(
            board_min.x,
            board_min.y,
            board_max.x,
            board_max.y,
            fill="#183a31",
            outline="#5b9677",
            width=2,
        )
        self._draw_grid(width, height, clipped=True)
        if self.show_wires:
            for net in self.scene.nets:
                self._draw_net(net)
        collisions = {
            identifier
            for pair in find_collisions(self.scene)
            for identifier in pair
        } if self.show_collisions else set()
        for element in self.scene.elements:
            self._draw_element(element, element.identifier in collisions)

    def _draw_grid(self, width: int, height: int, *, clipped: bool = False) -> None:
        if self.scene.grid_size is None:
            return
        step = self.scene.grid_size
        while step * self.viewport.scale < 14.0:
            step *= 2
        world_min = self.viewport.screen_to_world(VisualPoint(0.0, 0.0), width, height)
        world_max = self.viewport.screen_to_world(
            VisualPoint(float(width), float(height)), width, height
        )
        min_x = max(world_min.x, self.scene.bounds.min_x) if clipped else world_min.x
        max_x = min(world_max.x, self.scene.bounds.max_x) if clipped else world_max.x
        min_y = max(world_min.y, self.scene.bounds.min_y) if clipped else world_min.y
        max_y = min(world_max.y, self.scene.bounds.max_y) if clipped else world_max.y
        color = "#285247" if clipped else "#17231f"
        cursor = int(min_x // step) * step
        while cursor <= max_x:
            top = self._screen(VisualPoint(cursor, min_y))
            bottom = self._screen(VisualPoint(cursor, max_y))
            self.create_line(top.x, top.y, bottom.x, bottom.y, fill=color)
            cursor += step
        cursor = int(min_y // step) * step
        while cursor <= max_y:
            left = self._screen(VisualPoint(min_x, cursor))
            right = self._screen(VisualPoint(max_x, cursor))
            self.create_line(left.x, left.y, right.x, right.y, fill=color)
            cursor += step

    def _draw_net(self, net: VisualNet) -> None:
        points = tuple(
            self.scene.element(endpoint.element).port_position(endpoint.port)
            for endpoint in net.endpoints
        )
        if len(points) < 2:
            return
        junction = VisualPoint(
            sum(point.x for point in points) / len(points),
            sum(point.y for point in points) / len(points),
        )
        color = "#ffd34e" if net.identifier == self.highlighted_net else "#6f9589"
        width = 3 if net.identifier == self.highlighted_net else 1
        for point in points:
            start = self._screen(point)
            bend = self._screen(VisualPoint(junction.x, point.y))
            end = self._screen(junction)
            self.create_line(
                start.x,
                start.y,
                bend.x,
                bend.y,
                end.x,
                end.y,
                fill=color,
                width=width,
                smooth=False,
            )

    def _draw_element(self, element: VisualElement, colliding: bool) -> None:
        for primitive in element.descriptor.primitives:
            if isinstance(primitive, VisualText) and not self.show_labels:
                continue
            self._draw_primitive(element, primitive)
        port_radius = max(2.0, min(5.0, 3.0 * self.viewport.scale))
        for port in element.descriptor.ports:
            point = self._screen(element.transform.apply(port.position))
            self.create_oval(
                point.x - port_radius,
                point.y - port_radius,
                point.x + port_radius,
                point.y + port_radius,
                fill="#f8df72",
                outline="#2d3c3f",
                width=1,
            )
        if (
            self.show_bounds
            or colliding
            or element.identifier == self.selected_element
        ):
            bounds = element.world_bounds
            minimum = self._screen(VisualPoint(bounds.min_x, bounds.min_y))
            maximum = self._screen(VisualPoint(bounds.max_x, bounds.max_y))
            color = (
                "#ff5d5d"
                if colliding
                else "#ffd34e" if element.identifier == self.selected_element else "#9eb8b0"
            )
            self.create_rectangle(
                minimum.x,
                minimum.y,
                maximum.x,
                maximum.y,
                outline=color,
                width=3 if element.identifier == self.selected_element else 2,
                dash=(6, 4) if self.show_bounds else (),
            )

    def _draw_primitive(self, element: VisualElement, primitive) -> None:
        if isinstance(primitive, (VisualRectangle, VisualEllipse)):
            bounds = primitive.bounds
            corners = tuple(
                self._screen(point)
                for point in (
                element.transform.apply(VisualPoint(bounds.min_x, bounds.min_y)),
                element.transform.apply(VisualPoint(bounds.max_x, bounds.min_y)),
                element.transform.apply(VisualPoint(bounds.max_x, bounds.max_y)),
                element.transform.apply(VisualPoint(bounds.min_x, bounds.max_y)),
                )
            )
            options = _style_options(primitive.style, self.viewport.scale)
            if isinstance(primitive, VisualRectangle):
                self.create_polygon(*flatten_point_coordinates(corners), **options)
            else:
                self.create_oval(
                    min(point.x for point in corners),
                    min(point.y for point in corners),
                    max(point.x for point in corners),
                    max(point.y for point in corners),
                    **options,
                )
            return
        if isinstance(primitive, (VisualPolyline, VisualPolygon)):
            points = tuple(
                self._screen(element.transform.apply(point))
                for point in primitive.points
            )
            options = _style_options(primitive.style, self.viewport.scale)
            if isinstance(primitive, VisualPolygon):
                self.create_polygon(*flatten_point_coordinates(points), **options)
            else:
                options.pop("fill", None)
                self.create_line(
                    *flatten_point_coordinates(points),
                    fill=primitive.style.stroke or "",
                    width=options["width"],
                )
            return
        if isinstance(primitive, VisualText):
            point = self._screen(element.transform.apply(primitive.position))
            self.create_text(
                point.x,
                point.y,
                text=primitive.text,
                fill=primitive.color,
                anchor=primitive.anchor,
                font=("TkDefaultFont", max(7, min(14, round(9 * self.viewport.scale)))),
            )

    def _screen(self, point: VisualPoint) -> VisualPoint:
        return self.viewport.world_to_screen(
            point,
            max(1, self.winfo_width()),
            max(1, self.winfo_height()),
        )

    def _on_configure(self, _event) -> None:
        if not self._fitted:
            self.fit_scene()
        else:
            self.redraw()

    def _on_click(self, event) -> None:
        point = self.viewport.screen_to_world(
            VisualPoint(event.x, event.y),
            max(1, self.winfo_width()),
            max(1, self.winfo_height()),
        )
        selected = pick_element(self.scene, point)
        self.selected_element = None if selected is None else selected.identifier
        self.highlighted_net = None
        self.redraw()
        self.on_select(selected)

    def _on_double_click(self, event) -> None:
        point = self.viewport.screen_to_world(
            VisualPoint(event.x, event.y),
            max(1, self.winfo_width()),
            max(1, self.winfo_height()),
        )
        selected = pick_element(self.scene, point)
        if selected is not None:
            self.on_activate(selected)

    def _start_pan(self, event) -> None:
        self._pan_origin = (event.x, event.y)

    def _pan(self, event) -> None:
        if self._pan_origin is None:
            return
        old_x, old_y = self._pan_origin
        self.viewport = self.viewport.pan(event.x - old_x, event.y - old_y)
        self._pan_origin = (event.x, event.y)
        self.redraw()

    def _zoom(self, event) -> None:
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            factor = 1.15
        else:
            factor = 1 / 1.15
        self.viewport = self.viewport.zoom_at(
            factor,
            VisualPoint(event.x, event.y),
            max(1, self.winfo_width()),
            max(1, self.winfo_height()),
        )
        self.redraw()


def _style_options(style, scale: float) -> dict[str, object]:
    return {
        "fill": style.fill or "",
        "outline": style.stroke or "",
        "width": max(1.0, style.stroke_width * min(scale, 2.0)),
        "dash": style.dash,
    }