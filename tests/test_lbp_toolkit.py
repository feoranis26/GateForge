import json
from pathlib import Path
import unittest

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
)
from gateforge.providers.lbp.toolkit import (
    LBP_PLAN_REVISION,
    encode_lbp_toolkit_plan,
)


FIXTURES = Path(__file__).parent / "fixtures" / "lbp"


def _gadget(
    name: str,
    kind: LbpGadgetKind,
    arity: int,
    *,
    inverted: bool = False,
    active: bool = False,
) -> LbpGadget:
    return LbpGadget(
        LbpGadgetId(name),
        kind,
        LbpGadgetSource.MATERIAL_OBJECT,
        arity,
        inverted=inverted,
        manual_activation=active,
    )


def _placement(gadget: LbpGadget, x: float, y: float, scale_y: float) -> LbpGadgetPlacement:
    return LbpGadgetPlacement(
        gadget.identifier,
        x,
        y,
        0.0,
        1.4666667,
        scale_y,
    )


def _plan(
    gadgets: tuple[LbpGadget, ...],
    placements: tuple[LbpGadgetPlacement, ...],
    connections: tuple[LbpConnection, ...] = (),
    notes: tuple[LbpNote, ...] = (),
) -> LbpPlanDesign:
    return LbpPlanDesign(
        gadgets,
        placements,
        connections,
        LbpPlanMetadata("Test", "Description", "GateForge"),
        LbpBoardSize(420.0, 262.5),
        notes,
    )


def _full_things(value: object) -> dict[int, dict[str, object]]:
    things: dict[int, dict[str, object]] = {}

    def visit(item: object) -> None:
        if isinstance(item, dict):
            uid = item.get("UID")
            if isinstance(uid, int):
                things[uid] = item
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return things


class LbpToolkitEncodingTests(unittest.TestCase):
    def test_encodes_captured_gate_guids_defaults_and_scales(self) -> None:
        gadgets = (
            _gadget("and5", LbpGadgetKind.AND, 5),
            _gadget("nand", LbpGadgetKind.AND, 2, inverted=True),
            _gadget("or5", LbpGadgetKind.OR, 5),
            _gadget("nor", LbpGadgetKind.OR, 2, inverted=True),
            _gadget("xor", LbpGadgetKind.XOR, 2),
            _gadget("xnor", LbpGadgetKind.XOR, 2, inverted=True),
            _gadget("battery0", LbpGadgetKind.BATTERY, 0),
            _gadget("battery1", LbpGadgetKind.BATTERY, 0, active=True),
            _gadget("not", LbpGadgetKind.NOT, 1, inverted=True),
            _gadget("buffer", LbpGadgetKind.NOT, 1),
        )
        placements = tuple(
            _placement(gadget, 0.0, 0.0, 0.73333334 * max(gadget.arity, 2))
            if gadget.kind != LbpGadgetKind.BATTERY
            else _placement(gadget, 0.0, 0.0, 1.4666667)
            for gadget in gadgets
        )

        encoded = encode_lbp_toolkit_plan(_plan(gadgets, placements))
        things = _full_things(encoded)
        switches = [
            thing
            for uid, thing in things.items()
            if uid >= 5 and "PSwitch" in thing
        ]
        by_signature = {
            (
                item["PSwitch"]["type"],
                item["PSwitch"]["inverted"],
                item["PSwitch"]["bulletsRequired"],
                item["PSwitch"]["manualActivation"]["activation"],
            ): item
            for item in switches
        }

        expected = {
            ("AND", False, 5, 0.0): (72708, 1),
            ("AND", True, 2, 0.0): (72708, 1),
            ("OR", False, 5, 0.0): (73154, 1),
            ("OR", True, 2, 0.0): (73154, 1),
            ("XOR", False, 2, 0.0): (73155, 0),
            ("XOR", True, 2, 0.0): (73155, 0),
            ("ALWAYS_ON", False, 1, 0.0): (78607, 0),
            ("ALWAYS_ON", False, 1, 1.0): (78607, 0),
            ("NOT", True, 1, 0.0): (73941, 0),
            ("NOT", False, 1, 0.0): (73941, 0),
        }
        for signature, (guid, off_max) in expected.items():
            with self.subTest(signature=signature):
                thing = by_signature[signature]
                self.assertEqual(thing["planGUID"], guid)
                self.assertEqual(thing["PSwitch"]["randomOffTimeMax"], off_max)
                self.assertEqual(
                    thing["PSwitch"]["outputs"][0]["activation"],
                    {"activation": 0.0, "ternary": 0, "player": -1},
                )

        captured = json.loads((FIXTURES / "GATE_TYPES.json").read_text())
        captured_components = captured["resource"]["things"][0]["PMicrochip"]["components"]
        captured_fields = {
            (
                item["thing"]["PSwitch"]["type"],
                item["thing"]["PSwitch"]["inverted"],
                item["thing"]["PSwitch"]["bulletsRequired"],
            ): (
                item["thing"]["planGUID"],
                item["thing"]["PSwitch"]["randomOffTimeMax"],
                item["scaleX"],
                item["scaleY"],
            )
            for item in captured_components
            if isinstance(item["thing"], dict)
            and item["thing"]["PSwitch"]["type"]
            in {"AND", "OR", "XOR", "ALWAYS_ON"}
        }
        self.assertEqual(captured_fields[("AND", False, 5)], (72708, 1, 1.4666667, 3.6666667))
        self.assertEqual(captured_fields[("OR", False, 5)], (73154, 1, 1.4666667, 3.6666667))
        self.assertEqual(captured_fields[("XOR", False, 2)], (73155, 0, 1.4666667, 1.4666667))

    def test_uses_known_good_microchip_shell(self) -> None:
        gadget = _gadget("not", LbpGadgetKind.NOT, 1, inverted=True)
        encoded = encode_lbp_toolkit_plan(
            _plan((gadget,), (_placement(gadget, 0.0, 0.0, 1.4666667),))
        )
        root = encoded["resource"]["things"][0]
        captured = json.loads((FIXTURES / "SDFF.json").read_text())
        captured_root = captured["resource"]["things"][0]

        self.assertEqual(encoded["revision"], LBP_PLAN_REVISION)
        self.assertEqual(root["UID"], 2)
        self.assertEqual(root["planGUID"], 75319)
        self.assertEqual(root["PPos"], captured_root["PPos"])
        self.assertEqual(root["PStickers"], captured_root["PStickers"])
        self.assertEqual(root["PSwitch"], captured_root["PSwitch"])
        self.assertEqual(root["PGroup"], captured_root["PGroup"])
        self.assertEqual(root["PMicrochip"]["circuitBoardSizeX"], 420.0)
        self.assertEqual(root["PMicrochip"]["circuitBoardSizeY"], 262.5)

    def test_fanout_becomes_multiple_targets_on_one_output(self) -> None:
        source = _gadget("source", LbpGadgetKind.NOT, 1)
        left = _gadget("left", LbpGadgetKind.NOT, 1)
        right = _gadget("right", LbpGadgetKind.NOT, 1)
        plan = _plan(
            (source, left, right),
            (
                _placement(source, -100.0, 0.0, 1.4666667),
                _placement(left, 100.0, -50.0, 1.4666667),
                _placement(right, 100.0, 50.0, 1.4666667),
            ),
            (
                LbpConnection(LbpEndpoint(source.identifier, 0), LbpEndpoint(left.identifier, 0)),
                LbpConnection(LbpEndpoint(source.identifier, 0), LbpEndpoint(right.identifier, 0)),
            ),
        )

        encoded = encode_lbp_toolkit_plan(plan)
        source_uid = 5 + [item.identifier for item in plan.gadgets].index(source.identifier)
        source_thing = _full_things(encoded)[source_uid]
        targets = source_thing["PSwitch"]["outputs"][0]["targetList"]

        self.assertEqual(len(targets), 2)
        self.assertEqual({item["port"] for item in targets}, {0})

    def test_cycle_serialization_terminates_and_reuses_uids(self) -> None:
        left = _gadget("left", LbpGadgetKind.NOT, 1)
        right = _gadget("right", LbpGadgetKind.NOT, 1)
        plan = _plan(
            (left, right),
            (
                _placement(left, -100.0, 0.0, 1.4666667),
                _placement(right, 100.0, 0.0, 1.4666667),
            ),
            (
                LbpConnection(LbpEndpoint(left.identifier, 0), LbpEndpoint(right.identifier, 0)),
                LbpConnection(LbpEndpoint(right.identifier, 0), LbpEndpoint(left.identifier, 0)),
            ),
        )

        encoded = encode_lbp_toolkit_plan(plan)
        things = _full_things(encoded)

        self.assertEqual(set(things), {2, 3, 4, 5, 6})
        self.assertEqual(len(encoded["resource"]["things"]), 5)
        self.assertTrue(
            any(
                isinstance(item, int)
                for item in encoded["resource"]["things"][1:]
            )
        )

    def test_output_is_deterministic(self) -> None:
        gadget = _gadget("not", LbpGadgetKind.NOT, 1)
        plan = _plan((gadget,), (_placement(gadget, 0.0, 0.0, 1.4666667),))

        self.assertEqual(
            encode_lbp_toolkit_plan(plan),
            encode_lbp_toolkit_plan(plan),
        )

    def test_encodes_captured_note_script_and_component(self) -> None:
        gadget = _gadget("input", LbpGadgetKind.NOT, 1)
        note = LbpNote(
            LbpNoteId("input-note"),
            "data[3]",
            -105.0,
            52.5,
        )
        plan = _plan(
            (gadget,),
            (_placement(gadget, 0.0, 52.5, 1.4666667),),
            notes=(note,),
        )

        encoded = encode_lbp_toolkit_plan(plan)
        things = _full_things(encoded)
        note_thing = next(item for item in things.values() if "PScript" in item)
        fields = {
            item["name"]: item
            for item in note_thing["PScript"]["instance"]["instanceLayout"]["fields"]
        }
        components = encoded["resource"]["things"][0]["PMicrochip"]["components"]
        note_component = next(
            item
            for item in components
            if (item["thing"] if isinstance(item["thing"], int) else item["thing"]["UID"])
            == note_thing["UID"]
        )

        self.assertEqual(note_thing["planGUID"], 95485)
        self.assertEqual(
            note_thing["PScript"]["instance"]["script"],
            {"value": 95484, "type": "SCRIPT"},
        )
        self.assertEqual(
            note_thing["PScript"]["instance"]["instanceLayout"]["instanceSize"],
            249,
        )
        self.assertEqual(
            fields["Text"]["value"],
            {"type": "STRINGW", "value": "data[3]"},
        )
        self.assertTrue(fields["UserEntered"]["value"])
        self.assertEqual(fields["FontSize"]["value"], 32.0)
        self.assertEqual(fields["NoteVisibility"]["value"], 2)
        self.assertEqual(note_component["x"], -105.0)
        self.assertEqual(note_component["y"], 52.5)


if __name__ == "__main__":
    unittest.main()
