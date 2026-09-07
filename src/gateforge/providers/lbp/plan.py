from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class LbpPlanRealizationError(ValueError):
    pass


class LbpGadgetKind(StrEnum):
    NOT = "NOT"
    AND = "AND"
    OR = "OR"
    XOR = "XOR"
    BATTERY = "ALWAYS_ON"
    TIMER = "TIMER"
    COUNTER = "COUNTDOWN"
    RANDOMIZER = "RANDOM"
    SELECTOR = "SELECTOR"


class LbpGadgetSource(StrEnum):
    MATERIAL_OBJECT = "material_object"
    MODULE_PORT = "module_port"
    CONSTANT = "constant"


@dataclass(frozen=True, slots=True)
class LbpSwitchSettings:
    radius: float = 250.0
    color_index: int = 0
    activation_hold_time: int = 0
    bullets_required: int | None = None
    bullets_detected: int = 0
    bullet_refresh_time: int = 0
    reset_when_full: bool = False
    timer_count: float = 0.0
    behavior: str | int | None = "OFF_ON"
    random_behavior: int = 1
    random_pattern: int = 0
    random_on_time_min: int = 30
    random_on_time_max: int = 30
    random_off_time_min: int = 0
    random_off_time_max: int | None = None
    random_non_repeating: bool = False
    player_mode: int = 1

    def __post_init__(self) -> None:
        integer_fields = (
            "activation_hold_time",
            "bullets_detected",
            "bullet_refresh_time",
            "random_behavior",
            "random_pattern",
            "random_on_time_min",
            "random_on_time_max",
            "random_off_time_min",
            "color_index",
            "player_mode",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LbpPlanRealizationError(
                    f"LBP switch setting {name} must be a nonnegative integer"
                )
        for name in ("bullets_required", "random_off_time_max"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise LbpPlanRealizationError(
                    f"LBP switch setting {name} must be a nonnegative integer"
                )
        if not math.isfinite(float(self.timer_count)) or self.timer_count < 0:
            raise LbpPlanRealizationError(
                "LBP switch timer_count must be finite and nonnegative"
            )
        if not math.isfinite(float(self.radius)) or self.radius <= 0:
            raise LbpPlanRealizationError(
                "LBP switch radius must be finite and positive"
            )


@dataclass(frozen=True, slots=True, order=True)
class LbpGadgetId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise LbpPlanRealizationError("LBP gadget ID must not be empty")


@dataclass(frozen=True, slots=True)
class LbpGadget:
    identifier: LbpGadgetId
    kind: LbpGadgetKind
    source: LbpGadgetSource
    arity: int
    inverted: bool = False
    name: str = ""
    manual_activation: bool = False
    output_arity: int = 1
    settings: LbpSwitchSettings = LbpSwitchSettings()

    def __post_init__(self) -> None:
        if not isinstance(self.arity, int) or isinstance(self.arity, bool):
            raise LbpPlanRealizationError("LBP gadget arity must be an integer")
        if self.kind == LbpGadgetKind.BATTERY:
            if self.arity != 0:
                raise LbpPlanRealizationError("LBP batteries must have zero inputs")
            if self.inverted:
                raise LbpPlanRealizationError("LBP batteries cannot be inverted")
        elif self.arity <= 0:
            raise LbpPlanRealizationError("LBP logic gadget arity must be positive")
        if (
            not isinstance(self.output_arity, int)
            or isinstance(self.output_arity, bool)
            or self.output_arity <= 0
        ):
            raise LbpPlanRealizationError(
                "LBP gadget output arity must be a positive integer"
            )
        if self.kind == LbpGadgetKind.NOT and self.arity != 1:
            raise LbpPlanRealizationError("LBP NOT gadgets require exactly one input")
        if self.kind == LbpGadgetKind.TIMER and self.arity != 2:
            raise LbpPlanRealizationError("LBP Timer gadgets require two inputs")
        if self.kind == LbpGadgetKind.COUNTER and self.arity != 2:
            raise LbpPlanRealizationError("LBP Counter gadgets require two inputs")
        if self.kind == LbpGadgetKind.RANDOMIZER and self.arity != 1:
            raise LbpPlanRealizationError(
                "LBP Randomizer gadgets require one input"
            )
        if (
            self.kind == LbpGadgetKind.SELECTOR
            and self.arity != self.output_arity + 1
        ):
            raise LbpPlanRealizationError(
                "LBP Selector gadgets require CYCLE plus one input per output"
            )

    @property
    def input_count(self) -> int:
        return self.arity

    @property
    def output_count(self) -> int:
        return self.output_arity


@dataclass(frozen=True, slots=True)
class LbpGadgetPlacement:
    gadget: LbpGadgetId
    x: float
    y: float
    angle: float
    scale_x: float
    scale_y: float

    def __post_init__(self) -> None:
        for attribute in ("x", "y", "angle", "scale_x", "scale_y"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LbpPlanRealizationError(
                    f"LBP gadget placement {attribute} must be numeric"
                )
            normalized = float(value)
            if not math.isfinite(normalized):
                raise LbpPlanRealizationError(
                    f"LBP gadget placement {attribute} must be finite"
                )
            object.__setattr__(self, attribute, normalized)
        if self.scale_x <= 0 or self.scale_y <= 0:
            raise LbpPlanRealizationError("LBP gadget scale must be positive")


@dataclass(frozen=True, slots=True, order=True)
class LbpEndpoint:
    gadget: LbpGadgetId
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise LbpPlanRealizationError("LBP endpoint port must be an integer")
        if self.port < 0:
            raise LbpPlanRealizationError("LBP endpoint port must be nonnegative")


@dataclass(frozen=True, slots=True, order=True)
class LbpConnection:
    source: LbpEndpoint
    target: LbpEndpoint


@dataclass(frozen=True, slots=True)
class LbpPlanMetadata:
    title: str
    description: str
    creator: str

    def __post_init__(self) -> None:
        if not self.title:
            raise LbpPlanRealizationError("LBP plan title must not be empty")
        if not self.creator:
            raise LbpPlanRealizationError("LBP plan creator must not be empty")


@dataclass(frozen=True, slots=True)
class LbpBoardSize:
    x: float
    y: float

    def __post_init__(self) -> None:
        for attribute in ("x", "y"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LbpPlanRealizationError(
                    f"LBP board size {attribute} must be numeric"
                )
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0:
                raise LbpPlanRealizationError(
                    f"LBP board size {attribute} must be finite and positive"
                )
            object.__setattr__(self, attribute, normalized)


@dataclass(frozen=True, slots=True, order=True)
class LbpNoteId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise LbpPlanRealizationError("LBP note ID must not be empty")


@dataclass(frozen=True, slots=True)
class LbpNote:
    identifier: LbpNoteId
    text: str
    x: float
    y: float
    angle: float = 0.0
    scale_x: float = 1.4666667
    scale_y: float = 1.4666667

    def __post_init__(self) -> None:
        if not self.text:
            raise LbpPlanRealizationError("LBP note text must not be empty")
        for attribute in ("x", "y", "angle", "scale_x", "scale_y"):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LbpPlanRealizationError(
                    f"LBP note {attribute} must be numeric"
                )
            normalized = float(value)
            if not math.isfinite(normalized):
                raise LbpPlanRealizationError(
                    f"LBP note {attribute} must be finite"
                )
            object.__setattr__(self, attribute, normalized)
        if self.scale_x <= 0 or self.scale_y <= 0:
            raise LbpPlanRealizationError("LBP note scale must be positive")


class LbpThingKind(StrEnum):
    GADGET = "gadget"
    MICROCHIP = "microchip"
    BOARD = "board"


@dataclass(frozen=True, slots=True, order=True)
class LbpThingEndpoint:
    kind: LbpThingKind
    identifier: str
    port: int

    def __post_init__(self) -> None:
        if not self.identifier:
            raise LbpPlanRealizationError("LBP Thing endpoint ID must not be empty")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or self.port < 0:
            raise LbpPlanRealizationError(
                "LBP Thing endpoint port must be a nonnegative integer"
            )


@dataclass(frozen=True, slots=True, order=True)
class LbpRoutedConnection:
    source: LbpThingEndpoint
    target: LbpThingEndpoint


@dataclass(frozen=True, slots=True)
class LbpContainer:
    path: str
    parent: str | None
    name: str
    input_nets: tuple[str, ...]
    output_nets: tuple[str, ...]
    gadgets: tuple[LbpGadgetId, ...]
    notes: tuple[LbpNoteId, ...]
    children: tuple[str, ...]
    x: float
    y: float
    board_size: LbpBoardSize

    def __post_init__(self) -> None:
        if not self.path or not self.name:
            raise LbpPlanRealizationError("LBP container names must not be empty")
        if len(set(self.input_nets)) != len(self.input_nets):
            raise LbpPlanRealizationError(
                f"LBP container {self.path!r} has duplicate input nets"
            )
        if len(set(self.output_nets)) != len(self.output_nets):
            raise LbpPlanRealizationError(
                f"LBP container {self.path!r} has duplicate output nets"
            )
        if not math.isfinite(float(self.x)) or not math.isfinite(float(self.y)):
            raise LbpPlanRealizationError("LBP container placement must be finite")
        object.__setattr__(self, "x", float(self.x))
        object.__setattr__(self, "y", float(self.y))


@dataclass(frozen=True, slots=True)
class LbpPlanHierarchy:
    root: str
    containers: tuple[LbpContainer, ...]
    connections: tuple[LbpRoutedConnection, ...]

    def __post_init__(self) -> None:
        containers = tuple(sorted(self.containers, key=lambda item: item.path))
        connections = tuple(sorted(set(self.connections)))
        by_path = {item.path: item for item in containers}
        if len(by_path) != len(containers):
            raise LbpPlanRealizationError("LBP hierarchy has duplicate container paths")
        try:
            root = by_path[self.root]
        except KeyError as error:
            raise LbpPlanRealizationError("LBP hierarchy root is missing") from error
        if root.parent is not None:
            raise LbpPlanRealizationError("LBP hierarchy root must not have a parent")
        for container in containers:
            if container.parent is not None and container.parent not in by_path:
                raise LbpPlanRealizationError(
                    f"LBP container {container.path!r} has missing parent"
                )
            for child in container.children:
                if child not in by_path or by_path[child].parent != container.path:
                    raise LbpPlanRealizationError(
                        f"LBP container {container.path!r} has invalid child {child!r}"
                    )
        object.__setattr__(self, "containers", containers)
        object.__setattr__(self, "connections", connections)


@dataclass(frozen=True, slots=True)
class LbpPlanDesign:
    gadgets: tuple[LbpGadget, ...]
    placements: tuple[LbpGadgetPlacement, ...]
    connections: tuple[LbpConnection, ...]
    metadata: LbpPlanMetadata
    board_size: LbpBoardSize
    notes: tuple[LbpNote, ...] = ()
    hierarchy: LbpPlanHierarchy | None = None

    def __post_init__(self) -> None:
        gadgets = tuple(sorted(self.gadgets, key=lambda item: item.identifier.value))
        placements = tuple(
            sorted(self.placements, key=lambda item: item.gadget.value)
        )
        connections = tuple(
            sorted(
                self.connections,
                key=lambda item: (
                    item.source.gadget.value,
                    item.source.port,
                    item.target.gadget.value,
                    item.target.port,
                ),
            )
        )
        notes = tuple(sorted(self.notes, key=lambda item: item.identifier.value))
        _require_unique(
            (item.identifier for item in gadgets),
            "LBP gadget ID",
        )
        _require_unique(
            (item.gadget for item in placements),
            "LBP gadget placement",
        )
        if len(set(connections)) != len(connections):
            raise LbpPlanRealizationError("LBP plan contains duplicate connections")
        _require_unique(
            (item.identifier for item in notes),
            "LBP note ID",
        )

        gadgets_by_id = {item.identifier: item for item in gadgets}
        placements_by_id = {item.gadget: item for item in placements}
        placement_ids = set(placements_by_id)
        gadget_ids = set(gadgets_by_id)
        if placement_ids != gadget_ids:
            raise LbpPlanRealizationError(
                "Every LBP gadget must have exactly one placement"
            )

        driven_inputs: set[LbpEndpoint] = set()
        for connection in connections:
            try:
                source = gadgets_by_id[connection.source.gadget]
            except KeyError as error:
                raise LbpPlanRealizationError(
                    f"LBP connection references unknown source "
                    f"{connection.source.gadget.value!r}"
                ) from error
            try:
                target = gadgets_by_id[connection.target.gadget]
            except KeyError as error:
                raise LbpPlanRealizationError(
                    f"LBP connection references unknown target "
                    f"{connection.target.gadget.value!r}"
                ) from error
            if connection.source.port >= source.output_count:
                raise LbpPlanRealizationError(
                    f"LBP connection references invalid output {connection.source}"
                )
            if connection.target.port >= target.input_count:
                raise LbpPlanRealizationError(
                    f"LBP connection references invalid input {connection.target}"
                )
            if connection.target in driven_inputs:
                raise LbpPlanRealizationError(
                    f"LBP input {connection.target} has more than one driver"
                )
            driven_inputs.add(connection.target)

        if self.hierarchy is None:
            _validate_component_extents(
                self.board_size,
                placements,
                notes,
            )
        else:
            _validate_hierarchical_extents(
                self.hierarchy,
                placements_by_id,
                {item.identifier: item for item in notes},
            )

        object.__setattr__(self, "gadgets", gadgets)
        object.__setattr__(self, "placements", placements)
        object.__setattr__(self, "connections", connections)
        object.__setattr__(self, "notes", notes)


def _validate_component_extents(
    board_size: LbpBoardSize,
    placements: tuple[LbpGadgetPlacement, ...],
    notes: tuple[LbpNote, ...],
    *,
    center_x: float = 0.0,
    center_y: float = 0.0,
    container: str | None = None,
) -> None:
    context = "" if container is None else f" in container {container!r}"
    for placement in placements:
        if abs(placement.x - center_x) > board_size.x:
            raise LbpPlanRealizationError(
                f"LBP gadget {placement.gadget.value!r} lies outside board X "
                f"extent{context}"
            )
        if abs(placement.y - center_y) > board_size.y:
            raise LbpPlanRealizationError(
                f"LBP gadget {placement.gadget.value!r} lies outside board Y "
                f"extent{context}"
            )
    for note in notes:
        if (
            abs(note.x - center_x) > board_size.x
            or abs(note.y - center_y) > board_size.y
        ):
            raise LbpPlanRealizationError(
                f"LBP note {note.identifier.value!r} lies outside board extent{context}"
            )


def _validate_hierarchical_extents(
    hierarchy: LbpPlanHierarchy,
    placements: dict[LbpGadgetId, LbpGadgetPlacement],
    notes: dict[LbpNoteId, LbpNote],
) -> None:
    containers = {item.path: item for item in hierarchy.containers}
    centers: dict[str, tuple[float, float]] = {}

    def visit(path: str, parent_x: float, parent_y: float) -> None:
        container = containers[path]
        center_x = parent_x + container.x
        center_y = parent_y + container.y
        centers[path] = (center_x, center_y)
        for child_path in container.children:
            child = containers[child_path]
            if abs(child.x) > container.board_size.x or abs(child.y) > container.board_size.y:
                raise LbpPlanRealizationError(
                    f"LBP child container {child_path!r} lies outside board extent "
                    f"in container {path!r}"
                )
            visit(child_path, center_x, center_y)

    visit(hierarchy.root, 0.0, 0.0)
    if len(centers) != len(containers):
        raise LbpPlanRealizationError(
            "Every LBP container must descend from the hierarchy root"
        )

    assigned_gadgets: set[LbpGadgetId] = set()
    assigned_notes: set[LbpNoteId] = set()
    for container in hierarchy.containers:
        center_x, center_y = centers[container.path]
        try:
            local_placements = tuple(
                placements[identifier] for identifier in container.gadgets
            )
            local_notes = tuple(notes[identifier] for identifier in container.notes)
        except KeyError as error:
            raise LbpPlanRealizationError(
                f"LBP container {container.path!r} references an unknown component"
            ) from error
        if assigned_gadgets.intersection(container.gadgets):
            raise LbpPlanRealizationError(
                "An LBP gadget cannot belong to multiple containers"
            )
        if assigned_notes.intersection(container.notes):
            raise LbpPlanRealizationError(
                "An LBP note cannot belong to multiple containers"
            )
        assigned_gadgets.update(container.gadgets)
        assigned_notes.update(container.notes)
        _validate_component_extents(
            container.board_size,
            local_placements,
            local_notes,
            center_x=center_x,
            center_y=center_y,
            container=container.path,
        )

    if assigned_gadgets != set(placements):
        raise LbpPlanRealizationError(
            "Every LBP gadget must belong to exactly one container"
        )
    if assigned_notes != set(notes):
        raise LbpPlanRealizationError(
            "Every LBP note must belong to exactly one container"
        )


def _require_unique(values, context: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise LbpPlanRealizationError(f"Duplicate {context} {value!r}")
        seen.add(value)
