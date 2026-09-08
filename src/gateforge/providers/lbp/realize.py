from __future__ import annotations

import json
import re

from gateforge.graph import MaterialGraph
from gateforge.material import (
    MaterialAttachment,
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialNetId,
    MaterialObject,
    MaterialObjectPortRef,
)
from gateforge.placement import PlacedDesign, validate_placed_design
from gateforge.placement.model import PhysicalEndpoint, PhysicalEndpointKind
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
    LbpPlanHierarchy,
    LbpContainer,
    LbpRoutedConnection,
    LbpSwitchSettings,
    LbpThingEndpoint,
    LbpThingKind,
)
from gateforge.providers.lbp.configuration import (
    LBPCounterConfiguration,
    LBPObjectConfigurationCodec,
    LBPRandomizerConfiguration,
    LBPRandomizerInputAction,
    LBPRandomizerMode,
    LBPSelectorStateConfiguration,
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
    LBPPhaseSelectorType,
    LBPSelectorType,
    LBPStorageSelectorType,
    LBPXorGateType,
    LBPTimerType,
    decode_lbp_object_type,
)
from gateforge.target import PortDirection, PrefabValidationError


_GATE_SCALE_X = 1.4666667
_GATE_SCALE_STEP_Y = 0.73333334
_INPUT_PORT = re.compile(r"IN_([0-9]+)")
_OUTPUT_PORT = re.compile(r"OUT(?:_([0-9]+))?")


def serialize_lbp_placed_design(
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
            "LBP serialization graph does not contain the supplied material design"
        )
    try:
        validate_placed_design(placed, graph)
    except ValueError as error:
        raise LbpPlanRealizationError(str(error)) from error
    if placed.target != "lbp":
        raise LbpPlanRealizationError(
            f"Cannot serialize target {placed.target!r} as an LBP plan"
        )

    gadgets: list[LbpGadget] = []
    placements: list[LbpGadgetPlacement] = []
    for component in placed.components:
        gadget, scale_x, scale_y = decode_lbp_component(component)
        gadgets.append(gadget)
        placements.append(
            LbpGadgetPlacement(
                gadget.identifier,
                component.x,
                component.y,
                component.angle,
                scale_x,
                scale_y,
            )
        )
    notes = tuple(
        LbpNote(
            LbpNoteId(item.identifier),
            item.text,
            item.x,
            item.y,
            item.angle,
            item.scale_x,
            item.scale_y,
        )
        for item in placed.annotations
        if item.provider == "lbp"
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
    root = placed.root_container
    board_size = LbpBoardSize(
        max(abs(root.board.min_x), abs(root.board.max_x)),
        max(abs(root.board.min_y), abs(root.board.max_y)),
    )
    hierarchy = None
    if len(placed.containers) > 1:
        hierarchy = LbpPlanHierarchy(
            placed.root,
            tuple(
                LbpContainer(
                    path=container.path,
                    parent=container.parent,
                    name=container.name,
                    input_nets=tuple(
                        item.net.value
                        for item in sorted(
                            (
                                port
                                for port in container.boundary_ports
                                if port.direction == PortDirection.INPUT
                            ),
                            key=lambda item: item.index,
                        )
                    ),
                    output_nets=tuple(
                        item.net.value
                        for item in sorted(
                            (
                                port
                                for port in container.boundary_ports
                                if port.direction == PortDirection.OUTPUT
                            ),
                            key=lambda item: item.index,
                        )
                    ),
                    gadgets=tuple(
                        LbpGadgetId(item.identifier)
                        for item in container.components
                    ),
                    notes=tuple(
                        LbpNoteId(item.identifier)
                        for item in container.annotations
                        if item.provider == "lbp"
                    ),
                    children=container.children,
                    x=container.x,
                    y=container.y,
                    board_size=LbpBoardSize(
                        max(abs(container.board.min_x), abs(container.board.max_x)),
                        max(abs(container.board.min_y), abs(container.board.max_y)),
                    ),
                )
                for container in placed.containers
            ),
            tuple(
                LbpRoutedConnection(
                    _lbp_thing_endpoint(connection.source),
                    _lbp_thing_endpoint(connection.target),
                )
                for container in placed.containers
                for connection in container.connections
            ),
        )
    resolved_title = title or root.name
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
        notes=notes,
        hierarchy=hierarchy,
    )


def decode_lbp_component(component) -> tuple[LbpGadget, float, float]:
    if component.provider != "lbp":
        raise LbpPlanRealizationError(
            f"LBP placement contains component from {component.provider!r}"
        )
    data = component.payload.canonical_data()
    try:
        settings_data = data["settings"]
        if not isinstance(settings_data, dict):
            raise TypeError("settings must be an object")
        gadget = LbpGadget(
            identifier=LbpGadgetId(component.identifier),
            kind=LbpGadgetKind(component.kind),
            source=LbpGadgetSource(str(data["source"])),
            arity=int(data["arity"]),
            inverted=bool(data["inverted"]),
            name=str(data["name"]),
            manual_activation=bool(data["manual_activation"]),
            output_arity=int(data["output_arity"]),
            settings=LbpSwitchSettings(**settings_data),
        )
        scale_x = float(data["scale_x"])
        scale_y = float(data["scale_y"])
    except (KeyError, TypeError, ValueError) as error:
        raise LbpPlanRealizationError(
            f"Invalid LBP physical component payload for {component.identifier!r}"
        ) from error
    return gadget, scale_x, scale_y


def _lbp_thing_endpoint(endpoint: PhysicalEndpoint) -> LbpThingEndpoint:
    kind = {
        PhysicalEndpointKind.COMPONENT: LbpThingKind.GADGET,
        PhysicalEndpointKind.BOUNDARY: LbpThingKind.BOARD,
        PhysicalEndpointKind.CHILD: LbpThingKind.MICROCHIP,
    }[endpoint.kind]
    return LbpThingEndpoint(kind, endpoint.identifier, endpoint.port)


def realize_lbp_material_gadget(
    material_object: MaterialObject,
) -> tuple[LbpGadget, float, float]:
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
    elif isinstance(
        object_type,
        (LBPPhaseSelectorType, LBPStorageSelectorType),
    ):
        configuration = LBPObjectConfigurationCodec().decode(
            material_object.type,
            material_object.configuration,
        )
        if not isinstance(configuration, LBPSelectorStateConfiguration):
            raise LbpPlanRealizationError(
                "LBP state Selector has invalid configuration"
            )
        kind = LbpGadgetKind.SELECTOR
        inverted = False
        arity = 3
        output_arity = 2
        scale_x = _GATE_SCALE_X
        scale_y = _GATE_SCALE_STEP_Y * 2
        settings = LbpSwitchSettings(
            bullets_required=2,
            bullets_detected=configuration.selection,
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
    return (
        LbpGadget(
            identifier=lbp_object_gadget_id(material_object.identifier.value),
            kind=kind,
            source=LbpGadgetSource.MATERIAL_OBJECT,
            arity=arity,
            inverted=inverted,
            output_arity=output_arity,
            settings=settings,
        ),
        scale_x,
        scale_y,
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
