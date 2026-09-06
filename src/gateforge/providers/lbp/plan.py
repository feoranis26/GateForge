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


class LbpGadgetSource(StrEnum):
    MATERIAL_OBJECT = "material_object"
    MODULE_PORT = "module_port"
    CONSTANT = "constant"


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
        if self.kind == LbpGadgetKind.NOT and self.arity != 1:
            raise LbpPlanRealizationError("LBP NOT gadgets require exactly one input")

    @property
    def input_count(self) -> int:
        return self.arity

    @property
    def output_count(self) -> int:
        return 1


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


@dataclass(frozen=True, slots=True)
class LbpPlanDesign:
    gadgets: tuple[LbpGadget, ...]
    placements: tuple[LbpGadgetPlacement, ...]
    connections: tuple[LbpConnection, ...]
    metadata: LbpPlanMetadata
    board_size: LbpBoardSize

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

        gadgets_by_id = {item.identifier: item for item in gadgets}
        placement_ids = {item.gadget for item in placements}
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

        for placement in placements:
            if abs(placement.x) > self.board_size.x:
                raise LbpPlanRealizationError(
                    f"LBP gadget {placement.gadget.value!r} lies outside board X extent"
                )
            if abs(placement.y) > self.board_size.y:
                raise LbpPlanRealizationError(
                    f"LBP gadget {placement.gadget.value!r} lies outside board Y extent"
                )

        object.__setattr__(self, "gadgets", gadgets)
        object.__setattr__(self, "placements", placements)
        object.__setattr__(self, "connections", connections)


def _require_unique(values, context: str) -> None:
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise LbpPlanRealizationError(f"Duplicate {context} {value!r}")
        seen.add(value)
