from __future__ import annotations

from dataclasses import dataclass, replace

from gateforge.providers.lbp.plan import (
    LbpContainer,
    LbpGadget,
    LbpGadgetId,
    LbpGadgetKind,
    LbpNote,
    LbpPlanDesign,
    LbpSwitchSettings,
    LbpThingEndpoint,
    LbpThingKind,
)


LBP_PLAN_REVISION = 35128313
_MICROCHIP_PLAN_GUID = 75319
_NOTE_PLAN_GUID = 95485
_NOTE_SCRIPT_GUID = 95484
_GATE_PLAN_GUIDS = {
    LbpGadgetKind.NOT: 73941,
    LbpGadgetKind.AND: 72708,
    LbpGadgetKind.OR: 73154,
    LbpGadgetKind.XOR: 73155,
    LbpGadgetKind.BATTERY: 78607,
    LbpGadgetKind.TIMER: 73790,
    LbpGadgetKind.COUNTER: 73789,
    LbpGadgetKind.RANDOMIZER: 73799,
    LbpGadgetKind.SELECTOR: 100663,
}


@dataclass(frozen=True, slots=True)
class _ThingRef:
    uid: int


def encode_lbp_toolkit_plan(plan: LbpPlanDesign) -> dict[str, object]:
    if plan.hierarchy is not None:
        return _encode_hierarchical_plan(plan)
    gadget_uids = {
        gadget.identifier: uid
        for uid, gadget in enumerate(plan.gadgets, start=5)
    }
    note_uids = {
        note.identifier: uid
        for uid, note in enumerate(plan.notes, start=5 + len(plan.gadgets))
    }
    targets: dict[LbpGadgetId, dict[int, list[tuple[int, int]]]] = {
        gadget.identifier: {
            output: [] for output in range(gadget.output_count)
        }
        for gadget in plan.gadgets
    }
    for connection in plan.connections:
        targets[connection.source.gadget][connection.source.port].append(
            (gadget_uids[connection.target.gadget], connection.target.port)
        )
    for outputs in targets.values():
        for values in outputs.values():
            values.sort()

    placements = {item.gadget: item for item in plan.placements}
    things: dict[int, dict[str, object]] = {
        4: _group_thing(4, plan.metadata.creator),
        3: _board_thing(3),
    }
    for gadget in plan.gadgets:
        uid = gadget_uids[gadget.identifier]
        things[uid] = _gadget_thing(
            uid,
            gadget,
            targets[gadget.identifier],
            parent_uid=3,
            group_uid=4,
        )
    for note in plan.notes:
        uid = note_uids[note.identifier]
        things[uid] = _note_thing(uid, note, parent_uid=3, group_uid=4)

    components = [
        {
            "thing": _ThingRef(gadget_uids[gadget.identifier]),
            "x": placements[gadget.identifier].x,
            "y": placements[gadget.identifier].y,
            "angle": placements[gadget.identifier].angle,
            "scaleX": placements[gadget.identifier].scale_x,
            "scaleY": placements[gadget.identifier].scale_y,
            "flipped": False,
        }
        for gadget in plan.gadgets
    ]
    components.extend(
        {
            "thing": _ThingRef(note_uids[note.identifier]),
            "x": note.x,
            "y": note.y,
            "angle": note.angle,
            "scaleX": note.scale_x,
            "scaleY": note.scale_y,
            "flipped": False,
        }
        for note in plan.notes
    )
    things[2] = _microchip_thing(
        2,
        plan,
        components,
        board_uid=3,
        parent_uid=None,
        group_uid=4,
        output_targets={},
        output_count=0,
        root=True,
    )

    serializer = _ThingGraphSerializer(things)
    serialized_things: list[object] = [serializer.encode_thing(2)]
    serialized_things.extend(
        serializer.encode_thing(uid) for uid in sorted(things) if uid != 2
    )
    return {
        "revision": LBP_PLAN_REVISION,
        "type": "PLAN",
        "resource": {
            "isUsedForStreaming": False,
            "things": serialized_things,
            "inventoryData": _inventory_data(plan),
        },
    }


def _encode_hierarchical_plan(plan: LbpPlanDesign) -> dict[str, object]:
    hierarchy = plan.hierarchy
    if hierarchy is None:
        raise AssertionError("Hierarchical encoder received a flat plan")
    containers = {item.path: item for item in hierarchy.containers}
    root = containers[hierarchy.root]
    gadget_uids = {
        gadget.identifier: uid
        for uid, gadget in enumerate(plan.gadgets, start=5)
    }
    next_uid = 5 + len(gadget_uids)
    note_uids = {
        note.identifier: uid
        for uid, note in enumerate(plan.notes, start=next_uid)
    }
    next_uid += len(note_uids)
    child_paths = [item.path for item in hierarchy.containers if item.path != hierarchy.root]
    microchip_uids: dict[str, int] = {hierarchy.root: 2}
    board_uids: dict[str, int] = {hierarchy.root: 3}
    for path in sorted(child_paths):
        microchip_uids[path] = next_uid
        board_uids[path] = next_uid + 1
        next_uid += 2

    def endpoint_uid(endpoint: LbpThingEndpoint) -> int:
        if endpoint.kind == LbpThingKind.GADGET:
            return gadget_uids[LbpGadgetId(endpoint.identifier)]
        if endpoint.kind == LbpThingKind.MICROCHIP:
            return microchip_uids[endpoint.identifier]
        return board_uids[endpoint.identifier]

    target_map: dict[tuple[LbpThingKind, str, int], list[tuple[int, int]]] = {}
    for connection in hierarchy.connections:
        key = (
            connection.source.kind,
            connection.source.identifier,
            connection.source.port,
        )
        target_map.setdefault(key, []).append(
            (endpoint_uid(connection.target), connection.target.port)
        )
    for targets in target_map.values():
        targets.sort()

    centers: dict[str, tuple[float, float]] = {hierarchy.root: (0.0, 0.0)}

    def populate_centers(path: str) -> None:
        parent_x, parent_y = centers[path]
        for child in containers[path].children:
            child_container = containers[child]
            centers[child] = (
                parent_x + child_container.x,
                parent_y + child_container.y,
            )
            populate_centers(child)

    populate_centers(hierarchy.root)
    placements = {item.gadget: item for item in plan.placements}
    notes = {item.identifier: item for item in plan.notes}
    gadgets = {item.identifier: item for item in plan.gadgets}
    things: dict[int, dict[str, object]] = {
        4: _group_thing(4, plan.metadata.creator),
    }

    for path in sorted(containers):
        container = containers[path]
        board_uid = board_uids[path]
        chip_uid = microchip_uids[path]
        board_targets = {
            output: target_map.get((LbpThingKind.BOARD, path, output), [])
            for output in range(len(container.input_nets))
        }
        things[board_uid] = _board_thing(
            board_uid,
            parent_uid=chip_uid,
            output_targets=board_targets,
            output_count=len(container.input_nets),
        )
        center_x, center_y = centers[path]
        components: list[dict[str, object]] = []
        for gadget_id in container.gadgets:
            gadget = gadgets[gadget_id]
            placement = placements[gadget_id]
            uid = gadget_uids[gadget_id]
            gadget_targets = {
                output: target_map.get(
                    (LbpThingKind.GADGET, gadget_id.value, output),
                    [],
                )
                for output in range(gadget.output_count)
            }
            things[uid] = _gadget_thing(
                uid,
                gadget,
                gadget_targets,
                parent_uid=board_uid,
                group_uid=4,
            )
            components.append(
                _component(
                    uid,
                    placement.x - center_x,
                    placement.y - center_y,
                    placement.angle,
                    placement.scale_x,
                    placement.scale_y,
                )
            )
        for note_id in container.notes:
            note = notes[note_id]
            uid = note_uids[note_id]
            things[uid] = _note_thing(
                uid,
                note,
                parent_uid=board_uid,
                group_uid=4,
            )
            components.append(
                _component(
                    uid,
                    note.x - center_x,
                    note.y - center_y,
                    note.angle,
                    note.scale_x,
                    note.scale_y,
                )
            )
        for child in container.children:
            child_container = containers[child]
            components.append(
                _component(
                    microchip_uids[child],
                    child_container.x,
                    child_container.y,
                    0.0,
                    1.4666667,
                    1.4666667,
                )
            )
        chip_targets = {
            output: target_map.get((LbpThingKind.MICROCHIP, path, output), [])
            for output in range(len(container.output_nets))
        }
        parent_uid = (
            None
            if container.parent is None
            else board_uids[container.parent]
        )
        things[chip_uid] = _microchip_thing(
            chip_uid,
            plan,
            components,
            board_uid=board_uid,
            parent_uid=parent_uid,
            group_uid=4,
            output_targets=chip_targets,
            output_count=len(container.output_nets),
            root=container.parent is None,
            name=plan.metadata.title if container.parent is None else container.name,
            board_size=container.board_size,
        )

    serializer = _ThingGraphSerializer(things)
    serialized_things: list[object] = [serializer.encode_thing(2)]
    serialized_things.extend(
        serializer.encode_thing(uid) for uid in sorted(things) if uid != 2
    )
    return {
        "revision": LBP_PLAN_REVISION,
        "type": "PLAN",
        "resource": {
            "isUsedForStreaming": False,
            "things": serialized_things,
            "inventoryData": _inventory_data(plan),
        },
    }


class _ThingGraphSerializer:
    def __init__(self, things: dict[int, dict[str, object]]) -> None:
        self._things = things
        self._seen: set[int] = set()

    def encode_thing(self, uid: int) -> object:
        if uid in self._seen:
            return uid
        try:
            thing = self._things[uid]
        except KeyError as error:
            raise ValueError(f"Unknown LBP Thing UID {uid}") from error
        self._seen.add(uid)
        return self._encode_value(thing)

    def _encode_value(self, value: object) -> object:
        if isinstance(value, _ThingRef):
            return self.encode_thing(value.uid)
        if isinstance(value, dict):
            return {key: self._encode_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._encode_value(item) for item in value]
        return value


def _microchip_thing(
    uid: int,
    plan: LbpPlanDesign,
    components: list[dict[str, object]],
    *,
    board_uid: int,
    parent_uid: int | None,
    group_uid: int,
    output_targets: dict[int, list[tuple[int, int]]],
    output_count: int,
    root: bool,
    name: str | None = None,
    board_size=None,
) -> dict[str, object]:
    size = board_size or plan.board_size
    thing: dict[str, object] = {
        "UID": uid,
        "planGUID": _MICROCHIP_PLAN_GUID,
        "parent": _ThingRef(parent_uid) if parent_uid is not None else None,
        "group": _ThingRef(group_uid),
        "PStickers": _microchip_stickers(),
        "PSwitch": _switch(
            switch_type="MICROCHIP",
            inverted=False,
            arity=1,
            outputs=output_count,
            is_lbp3=True,
            colour=-2130738945,
            targets_by_output=output_targets,
        ),
        "PGroup": {
            "planDescriptor": {"value": _MICROCHIP_PLAN_GUID, "type": "PLAN"},
            "creator": "MM_Studio",
            "emitter": None,
            "lifetime": 0,
            "aliveFrames": 11,
            "flags": 2,
        },
        "PMicrochip": {
            "circuitBoardThing": _ThingRef(board_uid),
            "hideInPlayMode": True,
            "wiresVisible": True,
            "lastTouched": 776616,
            "offset": [0.0, 533.2588, 590.0, 0.0],
            "name": name or plan.metadata.title,
            "components": components,
            "circuitBoardSizeX": size.x,
            "circuitBoardSizeY": size.y,
            "keepVisualVertical": False,
            "broadcastType": 0,
        },
    }
    if root:
        thing["PPos"] = {
            "thingOfWhichIAmABone": None,
            "animHash": 0,
            "worldPosition": {
                "translation": [-8504.0, 9399.0, 0.0],
                "rotation": [0.0, 0.0, -4.0280046e-09, 1.0],
                "scale": [1.4666667, 1.4666667, 1.4666667],
            },
        }
    return thing


def _group_thing(uid: int, creator: str) -> dict[str, object]:
    return {
        "UID": uid,
        "planGUID": None,
        "parent": None,
        "group": None,
        "PGroup": {
            "planDescriptor": None,
            "creator": creator,
            "emitter": None,
            "lifetime": 0,
            "aliveFrames": 0,
            "flags": 0,
        },
    }


def _board_thing(
    uid: int,
    *,
    parent_uid: int = 2,
    output_targets: dict[int, list[tuple[int, int]]] | None = None,
    output_count: int = 0,
) -> dict[str, object]:
    return {
        "UID": uid,
        "planGUID": _MICROCHIP_PLAN_GUID,
        "parent": _ThingRef(parent_uid),
        "group": _ThingRef(parent_uid),
        "PSwitch": _switch(
            switch_type="CIRCUIT_BOARD",
            inverted=False,
            arity=1,
            outputs=output_count,
            is_lbp3=True,
            colour=-2130738945,
            targets_by_output=output_targets,
        ),
    }


def _gadget_thing(
    uid: int,
    gadget: LbpGadget,
    targets: dict[int, list[tuple[int, int]]],
    *,
    parent_uid: int,
    group_uid: int,
) -> dict[str, object]:
    random_off_time_max = (
        1 if gadget.kind in {LbpGadgetKind.AND, LbpGadgetKind.OR} else 0
    )
    settings = gadget.settings
    if gadget.kind == LbpGadgetKind.RANDOMIZER:
        settings = replace(settings, bullet_refresh_time=uid)
    colour = -2130738945 if gadget.kind == LbpGadgetKind.BATTERY else -32513
    return {
        "UID": uid,
        "planGUID": _GATE_PLAN_GUIDS[gadget.kind],
        "parent": _ThingRef(parent_uid),
        "group": _ThingRef(group_uid),
        "PSwitch": _switch(
            switch_type=gadget.kind.value,
            inverted=gadget.inverted,
            arity=max(gadget.arity, 1),
            outputs=gadget.output_count,
            is_lbp3=False,
            colour=colour,
            name=gadget.name,
            manual_activation=gadget.manual_activation,
            random_off_time_max=random_off_time_max,
            targets_by_output=targets,
            settings=settings,
        ),
    }


def _note_thing(
    uid: int,
    note: LbpNote,
    *,
    parent_uid: int,
    group_uid: int,
) -> dict[str, object]:
    return {
        "UID": uid,
        "planGUID": _NOTE_PLAN_GUID,
        "parent": _ThingRef(parent_uid),
        "group": _ThingRef(group_uid),
        "PScript": {
            "instance": {
                "script": {"value": _NOTE_SCRIPT_GUID, "type": "SCRIPT"},
                "instanceLayout": {
                    "fields": _note_fields(note.text),
                    "instanceSize": 249,
                },
            }
        },
    }


_MISSING = object()
_NOTE_TEXT = object()
_NOTE_FIELD_SPECS = (
    ("TweakPlayerNumber", ("PROTECTED",), "S32", "S32", 0, 0),
    ("IsTweaking", ("PROTECTED", "DIVERGENT"), "BOOL", "BOOL", 4, _MISSING),
    ("IsAdvanced", ("PROTECTED", "DIVERGENT"), "BOOL", "BOOL", 5, _MISSING),
    ("ColourTex", ("PROTECTED", "DIVERGENT"), "OBJECT_REF", "VOID", 8, _MISSING),
    ("TweakTexture", ("PROTECTED", "DIVERGENT"), "OBJECT_REF", "VOID", 12, _MISSING),
    ("SideIndicatorTexture", ("PROTECTED", "DIVERGENT"), "OBJECT_REF", "VOID", 16, _MISSING),
    ("NeedUpdate", ("PUBLIC", "DIVERGENT"), "S32", "S32", 20, _MISSING),
    ("LastTweakResult", ("PUBLIC", "DIVERGENT"), "S32", "S32", 24, _MISSING),
    ("BigLeft", ("PRIVATE", "DIVERGENT"), "BOOL", "BOOL", 28, _MISSING),
    ("BigRight", ("PRIVATE", "DIVERGENT"), "BOOL", "BOOL", 29, _MISSING),
    ("SelectingName", ("PROTECTED", "DIVERGENT"), "S32", "S32", 32, _MISSING),
    ("SpeechTex", ("PRIVATE", "DIVERGENT"), "OBJECT_REF", "VOID", 36, _MISSING),
    ("NoteTex", ("PRIVATE", "DIVERGENT"), "OBJECT_REF", "VOID", 40, _MISSING),
    ("UserEntered", ("PRIVATE",), "BOOL", "BOOL", 44, True),
    ("Text", ("PUBLIC",), "OBJECT_REF", "VOID", 48, _NOTE_TEXT),
    ("OriginalText", ("PRIVATE",), "OBJECT_REF", "VOID", 52, {"type": "STRINGW", "value": ""}),
    ("OldUserEnteredText", ("PRIVATE",), "OBJECT_REF", "VOID", 56, {"type": "NULL"}),
    ("FontSize", ("PRIVATE",), "F32", "F32", 60, 32.0),
    ("BubbleVisible", ("PRIVATE",), "BOOL", "BOOL", 64, True),
    ("Size", ("PRIVATE", "DIVERGENT"), "F32", "F32", 68, _MISSING),
    ("TargetSize", ("PRIVATE", "DIVERGENT"), "F32", "F32", 72, _MISSING),
    ("Speed", ("PRIVATE", "DIVERGENT"), "F32", "F32", 76, _MISSING),
    ("Offset", ("PRIVATE", "DIVERGENT"), "V4", "V2", 80, _MISSING),
    ("TargetOffset", ("PRIVATE", "DIVERGENT"), "V4", "V2", 96, _MISSING),
    ("ExpandTicks", ("PRIVATE", "DIVERGENT"), "S32", "S32", 112, _MISSING),
    ("TextHalfSize", ("PRIVATE", "DIVERGENT"), "V4", "V2", 128, _MISSING),
    ("BubbleHalfSize", ("PRIVATE", "DIVERGENT"), "V4", "V2", 144, _MISSING),
    ("Alpha", ("PRIVATE", "DIVERGENT"), "F32", "F32", 160, _MISSING),
    ("TargetAlpha", ("PRIVATE", "DIVERGENT"), "F32", "F32", 164, _MISSING),
    ("NeedInit", ("PRIVATE", "DIVERGENT"), "BOOL", "BOOL", 168, _MISSING),
    ("Colour", ("PRIVATE",), "V4", "V4", 176, [1.0, 1.0, 0.0, 1.0]),
    ("BGColour", ("PUBLIC",), "S32", "S32", 192, -65281),
    ("BGBrightness", ("PRIVATE",), "F32", "F32", 196, 1.0),
    ("NoteVisibility", ("PUBLIC",), "S32", "S32", 200, 2),
    ("Opacity", ("PRIVATE",), "F32", "F32", 204, 1.0),
    ("bubbleVisibleNow", ("PRIVATE", "DIVERGENT"), "BOOL", "BOOL", 208, _MISSING),
    ("FontColour", ("PRIVATE",), "S32", "S32", 212, 0),
    ("FontBrightness", ("PRIVATE",), "F32", "F32", 216, 1.0),
    ("FontFace", ("PRIVATE",), "S32", "S32", 220, 4),
    ("ColourFixup", ("PRIVATE",), "BOOL", "BOOL", 224, True),
    ("LocalSpace", ("PRIVATE",), "BOOL", "BOOL", 225, False),
    ("Behaviour", ("PRIVATE",), "S32", "S32", 228, 0),
    ("lastAnalogueValue", ("PRIVATE", "DIVERGENT"), "F32", "F32", 232, _MISSING),
    ("MaxValue", ("PUBLIC",), "S32", "S32", 236, 0),
    ("NumDecimals", ("PUBLIC",), "S32", "S32", 240, 0),
    ("Suffix", ("PUBLIC",), "OBJECT_REF", "VOID", 244, {"type": "NULL"}),
    ("IsSelectingFont", ("PRIVATE", "DIVERGENT"), "BOOL", "BOOL", 248, _MISSING),
)


def _note_fields(text: str) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    for name, modifiers, machine_type, fish_type, offset, value in _NOTE_FIELD_SPECS:
        field: dict[str, object] = {
            "name": name,
            "modifiers": list(modifiers),
            "machineType": machine_type,
            "fishType": fish_type,
            "arrayBaseMachineType": "VOID",
            "instanceOffset": offset,
        }
        if value is _NOTE_TEXT:
            field["value"] = {"type": "STRINGW", "value": text}
        elif value is not _MISSING:
            field["value"] = value
        fields.append(field)
    return fields


def _switch(
    *,
    switch_type: str,
    inverted: bool,
    arity: int,
    outputs: int,
    is_lbp3: bool,
    colour: int,
    name: str = "",
    manual_activation: bool = False,
    random_off_time_max: int = 0,
    targets_by_output: dict[int, list[tuple[int, int]]] | None = None,
    settings: LbpSwitchSettings = LbpSwitchSettings(),
) -> dict[str, object]:
    target_values = targets_by_output or {}
    output_values = []
    for output_index in range(outputs):
        output_targets = target_values.get(output_index, [])
        output_values.append(
            {
                "activation": _activation(False),
                "targetList": [
                    {"thing": _ThingRef(uid), "port": port}
                    for uid, port in output_targets
                ],
                "userDefinedName": "",
            }
        )
    return {
        "inverted": inverted,
        "radius": settings.radius,
        "minRadius": 0.0,
        "colorIndex": settings.color_index,
        "name": name,
        "crappyOldLbp1Switch": False,
        "behaviorOld": 0,
        "outputs": output_values,
        "stickerPlan": None,
        "hideInPlayMode": False,
        "type": switch_type,
        "referenceThing": None,
        "manualActivation": _activation(manual_activation),
        "activationHoldTime": settings.activation_hold_time,
        "requireAll": False,
        "angleRange": 180.0,
        "includeTouching": 0,
        "bulletsRequired": (
            arity if settings.bullets_required is None else settings.bullets_required
        ),
        "bulletsDetected": settings.bullets_detected,
        "bulletRefreshTime": settings.bullet_refresh_time,
        "resetWhenFull": settings.reset_when_full,
        "inputList": [],
        "includeRigidConnectors": False,
        "timerCount": settings.timer_count,
        "teamFilter": 0,
        "behavior": settings.behavior,
        "randomBehavior": settings.random_behavior,
        "randomPattern": settings.random_pattern,
        "randomOnTimeMin": settings.random_on_time_min,
        "randomOnTimeMax": settings.random_on_time_max,
        "randomOffTimeMin": settings.random_off_time_min,
        "randomOffTimeMax": (
            random_off_time_max
            if settings.random_off_time_max is None
            else settings.random_off_time_max
        ),
        "retardedOldJoint": False,
        "keySensorMode": 0,
        "userDefinedColour": colour,
        "wiresVisible": False,
        "bulletTypes": 0,
        "detectUnspawnedPlayers": False,
        "unspawnedBehavior": 0,
        "playSwitchAudio": True,
        "playerMode": settings.player_mode,
        "value": {
            "creatorID": None,
            "labelName": None,
            "labelIndex": 0,
            "analogue": None,
            "ternary": None,
        },
        "relativeToSequencer": False,
        "layerRange": 0,
        "breakSound": False,
        "colorTimer": 0,
        "isLbp3Switch": is_lbp3,
        "randomNonRepeating": settings.random_non_repeating,
        "stickerSwitchMode": 1,
    }


def _component(
    uid: int,
    x: float,
    y: float,
    angle: float,
    scale_x: float,
    scale_y: float,
) -> dict[str, object]:
    return {
        "thing": _ThingRef(uid),
        "x": x,
        "y": y,
        "angle": angle,
        "scaleX": scale_x,
        "scaleY": scale_y,
        "flipped": False,
    }


def _activation(active: bool) -> dict[str, object]:
    return {
        "activation": 1.0 if active else 0.0,
        "ternary": 1 if active else 0,
        "player": -1,
    }


def _microchip_stickers() -> dict[str, object]:
    return {
        "decals": [
            {
                "texture": {"value": 108715, "type": "TEXTURE"},
                "u": 0.08130286,
                "v": 0.07663398,
                "xvecu": -0.058889322,
                "xvecv": 0.0,
                "yvecu": -0.0,
                "yvecv": 0.055062078,
                "color": -1,
                "type": "STICKER",
                "metadataIndex": -1,
                "placedBy": -1,
                "playModeFrame": 0,
                "scorchMark": False,
                "plan": None,
            }
        ],
        "costumeDecals": [[] for _ in range(15)],
        "eyetoyData": [],
    }


def _inventory_data(plan: LbpPlanDesign) -> dict[str, object]:
    metadata = plan.metadata
    return {
        "dateAdded": 0,
        "levelUnlockSlotID": "NONE",
        "highlightSound": None,
        "colour": 10329776,
        "type": ["USER_OBJECT"],
        "subType": 0,
        "titleKey": 0,
        "descriptionKey": 0,
        "userCreatedDetails": {
            "name": metadata.title,
            "description": metadata.description,
        },
        "creationHistory": ["MM_Studio", metadata.creator],
        "icon": {
            "value": "b0e0891c8966ef04de37fb09e3885793dec557d5",
            "type": "TEXTURE",
        },
        "photoData": None,
        "eyetoyData": None,
        "locationIndex": -1,
        "categoryIndex": -1,
        "primaryIndex": 0,
        "creator": metadata.creator,
        "toolType": "NONE",
        "flags": 0,
        "location": 0,
        "category": 3423480070,
    }
