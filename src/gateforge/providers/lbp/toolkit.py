from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gateforge.providers.lbp.plan import (
    LbpGadget,
    LbpGadgetId,
    LbpGadgetKind,
    LbpPlanDesign,
)


LBP_PLAN_REVISION = 35128313
_MICROCHIP_PLAN_GUID = 75319
_GATE_PLAN_GUIDS = {
    LbpGadgetKind.NOT: 73941,
    LbpGadgetKind.AND: 72708,
    LbpGadgetKind.OR: 73154,
    LbpGadgetKind.XOR: 73155,
    LbpGadgetKind.BATTERY: 78607,
}


@dataclass(frozen=True, slots=True)
class _ThingRef:
    uid: int


def encode_lbp_toolkit_plan(plan: LbpPlanDesign) -> dict[str, object]:
    gadget_uids = {
        gadget.identifier: uid
        for uid, gadget in enumerate(plan.gadgets, start=5)
    }
    targets: dict[LbpGadgetId, list[tuple[int, int]]] = {
        gadget.identifier: [] for gadget in plan.gadgets
    }
    for connection in plan.connections:
        targets[connection.source.gadget].append(
            (gadget_uids[connection.target.gadget], connection.target.port)
        )
    for values in targets.values():
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
        )

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
    things[2] = _microchip_thing(
        2,
        plan,
        components,
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
) -> dict[str, object]:
    return {
        "UID": uid,
        "planGUID": _MICROCHIP_PLAN_GUID,
        "parent": None,
        "group": _ThingRef(4),
        "PPos": {
            "thingOfWhichIAmABone": None,
            "animHash": 0,
            "worldPosition": {
                "translation": [-8504.0, 9399.0, 0.0],
                "rotation": [0.0, 0.0, -4.0280046e-09, 1.0],
                "scale": [1.4666667, 1.4666667, 1.4666667],
            },
        },
        "PStickers": _microchip_stickers(),
        "PSwitch": _switch(
            switch_type="MICROCHIP",
            inverted=False,
            arity=1,
            outputs=0,
            is_lbp3=True,
            colour=-2130738945,
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
            "circuitBoardThing": _ThingRef(3),
            "hideInPlayMode": True,
            "wiresVisible": True,
            "lastTouched": 776616,
            "offset": [0.0, 533.2588, 590.0, 0.0],
            "name": plan.metadata.title,
            "components": components,
            "circuitBoardSizeX": plan.board_size.x,
            "circuitBoardSizeY": plan.board_size.y,
            "keepVisualVertical": False,
            "broadcastType": 0,
        },
    }


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


def _board_thing(uid: int) -> dict[str, object]:
    return {
        "UID": uid,
        "planGUID": _MICROCHIP_PLAN_GUID,
        "parent": _ThingRef(2),
        "group": _ThingRef(2),
        "PSwitch": _switch(
            switch_type="CIRCUIT_BOARD",
            inverted=False,
            arity=1,
            outputs=0,
            is_lbp3=True,
            colour=-2130738945,
        ),
    }


def _gadget_thing(
    uid: int,
    gadget: LbpGadget,
    targets: list[tuple[int, int]],
) -> dict[str, object]:
    random_off_time_max = (
        1 if gadget.kind in {LbpGadgetKind.AND, LbpGadgetKind.OR} else 0
    )
    colour = -2130738945 if gadget.kind == LbpGadgetKind.BATTERY else -32513
    return {
        "UID": uid,
        "planGUID": _GATE_PLAN_GUIDS[gadget.kind],
        "parent": _ThingRef(3),
        "group": _ThingRef(4),
        "PSwitch": _switch(
            switch_type=gadget.kind.value,
            inverted=gadget.inverted,
            arity=max(gadget.arity, 1),
            outputs=1,
            is_lbp3=False,
            colour=colour,
            name=gadget.name,
            manual_activation=gadget.manual_activation,
            random_off_time_max=random_off_time_max,
            targets=targets,
        ),
    }


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
    targets: list[tuple[int, int]] | None = None,
) -> dict[str, object]:
    target_values = targets or []
    output_values = []
    for output_index in range(outputs):
        output_targets = target_values if output_index == 0 else []
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
        "radius": 250.0,
        "minRadius": 0.0,
        "colorIndex": 0,
        "name": name,
        "crappyOldLbp1Switch": False,
        "behaviorOld": 0,
        "outputs": output_values,
        "stickerPlan": None,
        "hideInPlayMode": False,
        "type": switch_type,
        "referenceThing": None,
        "manualActivation": _activation(manual_activation),
        "activationHoldTime": 0,
        "requireAll": False,
        "angleRange": 180.0,
        "includeTouching": 0,
        "bulletsRequired": arity,
        "bulletsDetected": 0,
        "bulletRefreshTime": 0,
        "resetWhenFull": False,
        "inputList": [],
        "includeRigidConnectors": False,
        "timerCount": 0.0,
        "teamFilter": 0,
        "behavior": "OFF_ON",
        "randomBehavior": 1,
        "randomPattern": 0,
        "randomOnTimeMin": 30,
        "randomOnTimeMax": 30,
        "randomOffTimeMin": 0,
        "randomOffTimeMax": random_off_time_max,
        "retardedOldJoint": False,
        "keySensorMode": 0,
        "userDefinedColour": colour,
        "wiresVisible": False,
        "bulletTypes": 0,
        "detectUnspawnedPlayers": False,
        "unspawnedBehavior": 0,
        "playSwitchAudio": True,
        "playerMode": 1,
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
        "randomNonRepeating": False,
        "stickerSwitchMode": 1,
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
