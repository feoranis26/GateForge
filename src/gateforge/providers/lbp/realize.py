from __future__ import annotations

from collections import Counter
import json
import math
import re

from gateforge.graph import MaterialGraph
from gateforge.material import (
    MaterialAttachment,
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialNetId,
    MaterialObjectPortRef,
)
from gateforge.placement import PlacedDesign, validate_placed_design
from gateforge.providers.lbp.plan import (
    LbpBoardSize,
    LbpConnection,
    LbpEndpoint,
    LbpGadget,
    LbpGadgetId,
    LbpGadgetKind,
    LbpGadgetPlacement,
    LbpGadgetSource,
    LbpPlanDesign,
    LbpPlanMetadata,
    LbpPlanRealizationError,
)
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPNotGateType,
    LBPOrGateType,
    LBPXorGateType,
    decode_lbp_object_type,
)
from gateforge.target import PortDirection, PrefabValidationError


_GATE_SCALE_X = 1.4666667
_GATE_SCALE_STEP_Y = 0.73333334
_BOARD_GRID = 52.5
_BOARD_MARGIN = 105.0
_BOARD_MIN_X = 420.0
_BOARD_MIN_Y = 262.5
_INPUT_PORT = re.compile(r"IN_([0-9]+)")


def realize_lbp_plan(
    material: MaterialDesign,
    graph: MaterialGraph,
    placed: PlacedDesign,
    *,
    title: str | None = None,
    description: str | None = None,
    creator: str | None = None,
) -> LbpPlanDesign:
    if graph.design != material:
        raise LbpPlanRealizationError(
            "LBP realization graph does not contain the supplied material design"
        )
    try:
        validate_placed_design(placed, graph)
    except ValueError as error:
        raise LbpPlanRealizationError(str(error)) from error

    object_placements = {
        item.object: item for item in placed.placements.objects
    }
    module_placements = {
        (item.module, item.port, item.bit, item.direction): item
        for item in placed.placements.module_ports
    }
    constant_placements = {
        (item.net, item.value): item for item in placed.placements.constants
    }

    gadgets: list[LbpGadget] = []
    placements: list[LbpGadgetPlacement] = []

    for material_object in material.objects:
        try:
            gate_type = decode_lbp_object_type(material_object.type)
        except PrefabValidationError as error:
            raise LbpPlanRealizationError(str(error)) from error
        kind, inverted = _realize_gate_type(gate_type)
        identifier = _object_gadget_id(material_object.identifier.value)
        gadget = LbpGadget(
            identifier=identifier,
            kind=kind,
            source=LbpGadgetSource.MATERIAL_OBJECT,
            arity=gate_type.width,
            inverted=inverted,
        )
        try:
            placement = object_placements[material_object.identifier]
        except KeyError as error:
            raise LbpPlanRealizationError(
                f"Material object {material_object.identifier.value} has no placement"
            ) from error
        gadgets.append(gadget)
        placements.append(
            _gadget_placement(
                identifier,
                placement.x,
                placement.y,
                placement.angle,
                gadget.arity,
            )
        )

    port_counts = Counter(
        (item.module, item.port) for item in placed.placements.module_ports
    )
    modules = {item.module for item in placed.placements.module_ports}
    if len(modules) > 1:
        raise LbpPlanRealizationError(
            f"LBP export requires one top module, found {sorted(modules)!r}"
        )
    for placement in placed.placements.module_ports:
        identifier = _module_gadget_id(
            placement.module,
            placement.port,
            placement.bit,
            placement.direction,
        )
        name = (
            placement.port
            if port_counts[(placement.module, placement.port)] == 1
            else f"{placement.port}[{placement.bit}]"
        )
        # Both boundaries use a non-inverting NOT as an inspectable temporary pin.
        gadgets.append(
            LbpGadget(
                identifier=identifier,
                kind=LbpGadgetKind.NOT,
                source=LbpGadgetSource.MODULE_PORT,
                arity=1,
                inverted=False,
                name=name,
            )
        )
        placements.append(
            _gadget_placement(
                identifier,
                placement.x,
                placement.y,
                placement.angle,
                1,
            )
        )

    for placement in placed.placements.constants:
        if placement.value not in {"0", "1"}:
            raise LbpPlanRealizationError(
                f"LBP export does not support constant {placement.value!r} on "
                f"material net {placement.net.value}"
            )
        identifier = _constant_gadget_id(placement.net, placement.value)
        gadgets.append(
            LbpGadget(
                identifier=identifier,
                kind=LbpGadgetKind.BATTERY,
                source=LbpGadgetSource.CONSTANT,
                arity=0,
                manual_activation=placement.value == "1",
            )
        )
        placements.append(
            LbpGadgetPlacement(
                identifier,
                placement.x,
                placement.y,
                placement.angle,
                _GATE_SCALE_X,
                _GATE_SCALE_X,
            )
        )

    connections = tuple(
        LbpConnection(
            source=_attachment_endpoint(
                dependency.source,
                dependency.net,
                source=True,
            ),
            target=_attachment_endpoint(
                dependency.target,
                dependency.net,
                source=False,
            ),
        )
        for dependency in graph.dependencies
    )

    board_size = _board_size(tuple(placements))
    resolved_title = title or (next(iter(modules)) if modules else "GateForge Export")
    resolved_creator = creator or "GateForge"
    resolved_description = description or (
        f"Generated by GateForge from {len(material.objects)} material objects, "
        f"{len(material.nets)} nets, and {len(gadgets)} LBP gadgets."
    )
    return LbpPlanDesign(
        gadgets=tuple(gadgets),
        placements=tuple(placements),
        connections=connections,
        metadata=LbpPlanMetadata(
            title=resolved_title,
            description=resolved_description,
            creator=resolved_creator,
        ),
        board_size=board_size,
    )


def _realize_gate_type(gate_type) -> tuple[LbpGadgetKind, bool]:
    if isinstance(gate_type, LBPNotGateType):
        if gate_type.width != 1:
            raise LbpPlanRealizationError(
                f"LBP NOT/BUF export requires width 1, got {gate_type.width}"
            )
        # Inverting an LBP NOT gadget cancels its native inversion.
        return LbpGadgetKind.NOT, not gate_type.invert_output
    if isinstance(gate_type, LBPAndGateType):
        return LbpGadgetKind.AND, gate_type.invert_output
    if isinstance(gate_type, LBPOrGateType):
        return LbpGadgetKind.OR, gate_type.invert_output
    if isinstance(gate_type, LBPXorGateType):
        return LbpGadgetKind.XOR, gate_type.invert_output
    raise LbpPlanRealizationError(
        f"Unsupported LBP gate type {type(gate_type).__name__}"
    )


def _attachment_endpoint(
    attachment: MaterialAttachment,
    net: MaterialNetId,
    *,
    source: bool,
) -> LbpEndpoint:
    if isinstance(attachment, MaterialObjectPortRef):
        identifier = _object_gadget_id(attachment.object.value)
        if source:
            if attachment.port != "OUT" or attachment.bit != 0:
                raise LbpPlanRealizationError(
                    f"Material source {attachment} is not an LBP object output"
                )
            return LbpEndpoint(identifier, 0)
        match = _INPUT_PORT.fullmatch(attachment.port)
        if match is None or attachment.bit != 0:
            raise LbpPlanRealizationError(
                f"Material target {attachment} is not an LBP object input"
            )
        return LbpEndpoint(identifier, int(match.group(1)))

    if isinstance(attachment, MaterialModulePortRef):
        identifier = _module_gadget_id(
            attachment.module,
            attachment.port,
            attachment.bit,
            attachment.direction,
        )
        if source and attachment.direction != PortDirection.INPUT:
            raise LbpPlanRealizationError(
                f"Material module source {attachment} is not an input port"
            )
        if not source and attachment.direction != PortDirection.OUTPUT:
            raise LbpPlanRealizationError(
                f"Material module target {attachment} is not an output port"
            )
        return LbpEndpoint(identifier, 0)

    if isinstance(attachment, MaterialConstantRef):
        if not source:
            raise LbpPlanRealizationError(
                f"Material constant {attachment.value!r} cannot be a target"
            )
        return LbpEndpoint(_constant_gadget_id(net, attachment.value), 0)

    raise LbpPlanRealizationError(f"Unsupported material attachment {attachment!r}")


def _gadget_placement(
    identifier: LbpGadgetId,
    x: float,
    y: float,
    angle: float,
    arity: int,
) -> LbpGadgetPlacement:
    return LbpGadgetPlacement(
        identifier,
        x,
        y,
        angle,
        _GATE_SCALE_X,
        _GATE_SCALE_STEP_Y * max(arity, 2),
    )


def _board_size(
    placements: tuple[LbpGadgetPlacement, ...],
) -> LbpBoardSize:
    if not placements:
        raise LbpPlanRealizationError("LBP plan requires at least one gadget")
    max_x = max(abs(item.x) for item in placements)
    max_y = max(abs(item.y) for item in placements)
    return LbpBoardSize(
        max(_BOARD_MIN_X, _snap_up(max_x + _BOARD_MARGIN)),
        max(_BOARD_MIN_Y, _snap_up(max_y + _BOARD_MARGIN)),
    )


def _snap_up(value: float) -> float:
    return math.ceil(value / _BOARD_GRID - 1e-12) * _BOARD_GRID


def _object_gadget_id(identifier: str) -> LbpGadgetId:
    return LbpGadgetId(f"object:{identifier}")


def _module_gadget_id(
    module: str,
    port: str,
    bit: int,
    direction: PortDirection,
) -> LbpGadgetId:
    identity = json.dumps(
        [module, port, bit, direction.value],
        separators=(",", ":"),
    )
    return LbpGadgetId(f"module:{identity}")


def _constant_gadget_id(net: MaterialNetId, value: str) -> LbpGadgetId:
    return LbpGadgetId(f"constant:{net.value}:{value}")
