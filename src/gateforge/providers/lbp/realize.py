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
    LbpNote,
    LbpNoteId,
    LbpPlanDesign,
    LbpPlanMetadata,
    LbpPlanRealizationError,
    LbpSwitchSettings,
)
from gateforge.providers.lbp.configuration import (
    LBPCounterConfiguration,
    LBPObjectConfigurationCodec,
    LBPRandomizerConfiguration,
    LBPRandomizerInputAction,
    LBPRandomizerMode,
    LBPTimerConfiguration,
    LBPTimerMode,
)
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPCombinatorialVariableWidthGateType,
    LBPCounterType,
    LBPNotGateType,
    LBPOrGateType,
    LBPRandomizerType,
    LBPSelectorType,
    LBPXorGateType,
    LBPTimerType,
    decode_lbp_object_type,
)
from gateforge.target import PortDirection, PrefabValidationError


_GATE_SCALE_X = 1.4666667
_GATE_SCALE_STEP_Y = 0.73333334
_BOARD_GRID = 52.5
_BOARD_MARGIN = 105.0
_BOARD_MIN_X = 420.0
_BOARD_MIN_Y = 262.5
_NOTE_OFFSET_X = 105.0
_INPUT_PORT = re.compile(r"IN_([0-9]+)")
_OUTPUT_PORT = re.compile(r"OUT(?:_([0-9]+))?")


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
    notes: list[LbpNote] = []

    for material_object in material.objects:
        try:
            object_type = decode_lbp_object_type(material_object.type)
        except PrefabValidationError as error:
            raise LbpPlanRealizationError(str(error)) from error
        if isinstance(object_type, LBPTimerType):
            configuration = LBPObjectConfigurationCodec().decode(
                material_object.type,
                material_object.configuration,
            )
            if not isinstance(configuration, LBPTimerConfiguration):
                raise LbpPlanRealizationError(
                    "LBP Timer material object has invalid configuration"
                )
            kind = LbpGadgetKind.TIMER
            inverted = False
            arity = 2
            scale_x = _GATE_SCALE_X * 2
            scale_y = _GATE_SCALE_STEP_Y * 2
            settings = LbpSwitchSettings(
                radius=450.0,
                color_index=3,
                activation_hold_time=configuration.duration_frames,
                bullets_required=120,
                behavior=_timer_behavior(configuration.mode),
            )
            output_arity = 1
        elif isinstance(object_type, LBPCounterType):
            configuration = LBPObjectConfigurationCodec().decode(
                material_object.type,
                material_object.configuration,
            )
            if not isinstance(configuration, LBPCounterConfiguration):
                raise LbpPlanRealizationError(
                    "LBP Counter material object has invalid configuration"
                )
            kind = LbpGadgetKind.COUNTER
            inverted = False
            arity = 2
            output_arity = 1
            scale_x = _GATE_SCALE_X * 2
            scale_y = _GATE_SCALE_STEP_Y * 2
            settings = LbpSwitchSettings(
                radius=450.0,
                color_index=3,
                bullets_required=configuration.target,
                player_mode=2,
            )
        elif isinstance(object_type, LBPRandomizerType):
            configuration = LBPObjectConfigurationCodec().decode(
                material_object.type,
                material_object.configuration,
            )
            if not isinstance(configuration, LBPRandomizerConfiguration):
                raise LbpPlanRealizationError(
                    "LBP Randomizer material object has invalid configuration"
                )
            kind = LbpGadgetKind.RANDOMIZER
            inverted = False
            arity = 1
            output_arity = object_type.outputs
            scale_x = _GATE_SCALE_X
            scale_y = _GATE_SCALE_STEP_Y * 2
            settings = LbpSwitchSettings(
                random_behavior=_randomizer_action(configuration.input_action),
                random_pattern=_randomizer_pattern(configuration.mode),
                random_on_time_min=configuration.frames(configuration.on_min_ds),
                random_on_time_max=configuration.frames(configuration.on_max_ds),
                random_off_time_min=configuration.frames(configuration.off_min_ds),
                random_off_time_max=configuration.frames(configuration.off_max_ds),
                random_non_repeating=configuration.new_pick,
                player_mode=2,
            )
        elif isinstance(object_type, LBPSelectorType):
            kind = LbpGadgetKind.SELECTOR
            inverted = False
            arity = object_type.width + 1
            output_arity = object_type.width
            scale_x = _GATE_SCALE_X
            scale_y = _GATE_SCALE_STEP_Y * max(object_type.width, 2)
            settings = LbpSwitchSettings(
                bullets_required=object_type.width,
                player_mode=2,
            )
        elif isinstance(object_type, LBPCombinatorialVariableWidthGateType):
            kind, inverted = _realize_gate_type(object_type)
            arity = object_type.width
            scale_x = _GATE_SCALE_X
            scale_y = _GATE_SCALE_STEP_Y * max(arity, 2)
            settings = LbpSwitchSettings()
            output_arity = 1
        else:
            raise LbpPlanRealizationError(
                f"Unsupported LBP object type {type(object_type).__name__}"
            )
        identifier = lbp_object_gadget_id(material_object.identifier.value)
        gadget = LbpGadget(
            identifier=identifier,
            kind=kind,
            source=LbpGadgetSource.MATERIAL_OBJECT,
            arity=arity,
            inverted=inverted,
            output_arity=output_arity,
            settings=settings,
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
                scale_x=scale_x,
                scale_y=scale_y,
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
        identifier = lbp_module_gadget_id(
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
        notes.append(
            LbpNote(
                identifier=_module_note_id(
                    placement.module,
                    placement.port,
                    placement.bit,
                    placement.direction,
                ),
                text=name,
                x=(
                    placement.x - _NOTE_OFFSET_X
                    if placement.direction == PortDirection.INPUT
                    else placement.x + _NOTE_OFFSET_X
                ),
                y=placement.y,
                angle=placement.angle,
            )
        )

    for placement in placed.placements.constants:
        if placement.value not in {"0", "1"}:
            raise LbpPlanRealizationError(
                f"LBP export does not support constant {placement.value!r} on "
                f"material net {placement.net.value}"
            )
        identifier = lbp_constant_gadget_id(placement.net, placement.value)
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
            source=lbp_attachment_endpoint(
                dependency.source,
                dependency.net,
                source=True,
            ),
            target=lbp_attachment_endpoint(
                dependency.target,
                dependency.net,
                source=False,
            ),
        )
        for dependency in graph.dependencies
    )

    board_size = _board_size(tuple(placements), tuple(notes))
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
        notes=tuple(notes),
    )


def _timer_behavior(mode: LBPTimerMode) -> str | int:
    return {
        LBPTimerMode.ON_OFF: "OFF_ON",
        LBPTimerMode.SPEED_SCALE: "SPEED_SCALE",
        LBPTimerMode.FORWARD_BACKWARD: "DIRECTION",
        LBPTimerMode.START_COUNT_UP: "ONE_SHOT",
        LBPTimerMode.START_COUNT_DOWN: 4,
        LBPTimerMode.POSITIONAL: 5,
    }[mode]


def _randomizer_action(action: LBPRandomizerInputAction) -> int:
    return {
        LBPRandomizerInputAction.TRIGGER: 0,
        LBPRandomizerInputAction.OVERRIDE_PATTERN: 1,
    }[action]


def _randomizer_pattern(mode: LBPRandomizerMode) -> int:
    return {
        LBPRandomizerMode.ADD: 0,
        LBPRandomizerMode.ONE_AT_A_TIME: 1,
        LBPRandomizerMode.TOGGLE: 2,
        LBPRandomizerMode.ADD_AND_RESET: 3,
    }[mode]


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


def lbp_attachment_endpoint(
    attachment: MaterialAttachment,
    net: MaterialNetId,
    *,
    source: bool,
) -> LbpEndpoint:
    if isinstance(attachment, MaterialObjectPortRef):
        identifier = lbp_object_gadget_id(attachment.object.value)
        if source:
            match = _OUTPUT_PORT.fullmatch(attachment.port)
            if match is None or attachment.bit != 0:
                raise LbpPlanRealizationError(
                    f"Material source {attachment} is not an LBP object output"
                )
            return LbpEndpoint(
                identifier,
                0 if match.group(1) is None else int(match.group(1)),
            )
        match = _INPUT_PORT.fullmatch(attachment.port)
        if match is None or attachment.bit != 0:
            raise LbpPlanRealizationError(
                f"Material target {attachment} is not an LBP object input"
            )
        return LbpEndpoint(identifier, int(match.group(1)))

    if isinstance(attachment, MaterialModulePortRef):
        identifier = lbp_module_gadget_id(
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
        return LbpEndpoint(lbp_constant_gadget_id(net, attachment.value), 0)

    raise LbpPlanRealizationError(f"Unsupported material attachment {attachment!r}")


def _gadget_placement(
    identifier: LbpGadgetId,
    x: float,
    y: float,
    angle: float,
    arity: int,
    *,
    scale_x: float = _GATE_SCALE_X,
    scale_y: float | None = None,
) -> LbpGadgetPlacement:
    return LbpGadgetPlacement(
        identifier,
        x,
        y,
        angle,
        scale_x,
        _GATE_SCALE_STEP_Y * max(arity, 2) if scale_y is None else scale_y,
    )


def _board_size(
    placements: tuple[LbpGadgetPlacement, ...],
    notes: tuple[LbpNote, ...],
) -> LbpBoardSize:
    if not placements:
        raise LbpPlanRealizationError("LBP plan requires at least one gadget")
    max_x = max(
        [abs(item.x) for item in placements] + [abs(item.x) for item in notes]
    )
    max_y = max(
        [abs(item.y) for item in placements] + [abs(item.y) for item in notes]
    )
    return LbpBoardSize(
        max(_BOARD_MIN_X, _snap_up(max_x + _BOARD_MARGIN)),
        max(_BOARD_MIN_Y, _snap_up(max_y + _BOARD_MARGIN)),
    )


def _snap_up(value: float) -> float:
    return math.ceil(value / _BOARD_GRID - 1e-12) * _BOARD_GRID


def lbp_object_gadget_id(identifier: str) -> LbpGadgetId:
    return LbpGadgetId(f"object:{identifier}")


def lbp_module_gadget_id(
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


def _module_note_id(
    module: str,
    port: str,
    bit: int,
    direction: PortDirection,
) -> LbpNoteId:
    identity = json.dumps(
        [module, port, bit, direction.value],
        separators=(",", ":"),
    )
    return LbpNoteId(f"module-note:{identity}")


def lbp_constant_gadget_id(net: MaterialNetId, value: str) -> LbpGadgetId:
    return LbpGadgetId(f"constant:{net.value}:{value}")
