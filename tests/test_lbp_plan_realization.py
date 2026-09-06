import unittest

from gateforge.graph import MaterialGraph
from gateforge.material import (
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialObjectPortRef,
)
from gateforge.placement import TopologicalPlacer
from gateforge.providers.lbp.common import LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.plan import (
    LbpGadgetKind,
    LbpGadgetSource,
    LbpPlanRealizationError,
)
from gateforge.providers.lbp.realize import realize_lbp_plan
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPNotGateType,
    LBPOrGateType,
    LBPXorGateType,
)
from gateforge.target import PortDirection
from tests.test_graph import _material_net, _material_object


PROVIDERS = {LBP_PROVIDER: make_lbp_provider()}


def _realize(design: MaterialDesign, **metadata):
    graph = MaterialGraph.from_design(design, PROVIDERS)
    placed = TopologicalPlacer().place(graph).finalize(graph)
    return realize_lbp_plan(design, graph, placed, **metadata)


class LbpPlanRealizationTests(unittest.TestCase):
    def test_realizes_all_gate_families_polarities_and_widths(self) -> None:
        cases = (
            ("not", LBPNotGateType(1, False), LbpGadgetKind.NOT, True, 1),
            ("buffer", LBPNotGateType(1, True), LbpGadgetKind.NOT, False, 1),
            ("and", LBPAndGateType(5, False), LbpGadgetKind.AND, False, 5),
            ("nand", LBPAndGateType(2, True), LbpGadgetKind.AND, True, 2),
            ("or", LBPOrGateType(5, False), LbpGadgetKind.OR, False, 5),
            ("nor", LBPOrGateType(2, True), LbpGadgetKind.OR, True, 2),
            ("xor", LBPXorGateType(4, False), LbpGadgetKind.XOR, False, 4),
            ("xnor", LBPXorGateType(2, True), LbpGadgetKind.XOR, True, 2),
        )
        objects = tuple(
            _material_object(role, gate_type.get_type(), chr(ord("a") + index * 2))
            for index, (role, gate_type, _, _, _) in enumerate(cases)
        )
        realization = _realize(MaterialDesign(objects, ()))
        gadgets = {item.identifier.value: item for item in realization.gadgets}
        placements = {item.gadget.value: item for item in realization.placements}

        for material_object, (_, _, kind, inverted, width) in zip(objects, cases):
            identifier = f"object:{material_object.identifier.value}"
            with self.subTest(identifier=identifier):
                gadget = gadgets[identifier]
                self.assertEqual(gadget.kind, kind)
                self.assertEqual(gadget.inverted, inverted)
                self.assertEqual(gadget.arity, width)
                self.assertEqual(gadget.source, LbpGadgetSource.MATERIAL_OBJECT)
                self.assertAlmostEqual(
                    placements[identifier].scale_y,
                    0.73333334 * max(width, 2),
                )

    def test_rejects_wide_not_gate(self) -> None:
        gate = _material_object(
            "not",
            LBPNotGateType(2, False).get_type(),
            "a",
        )

        with self.assertRaises(LbpPlanRealizationError):
            _realize(MaterialDesign((gate,), ()))

    def test_synthesizes_scalar_io_buffers_and_rewrites_connections(self) -> None:
        gate = _material_object(
            "gate",
            LBPNotGateType(1, False).get_type(),
            "a",
        )
        design = MaterialDesign(
            (gate,),
            (
                _material_net(
                    LBP_WIRE,
                    MaterialModulePortRef("top", "a", 0, PortDirection.INPUT),
                    MaterialObjectPortRef(gate.identifier, "IN_0"),
                ),
                _material_net(
                    LBP_WIRE,
                    MaterialObjectPortRef(gate.identifier, "OUT"),
                    MaterialModulePortRef("top", "y", 0, PortDirection.OUTPUT),
                ),
            ),
        )

        realization = _realize(design)
        io_gadgets = {
            item.name: item
            for item in realization.gadgets
            if item.source == LbpGadgetSource.MODULE_PORT
        }

        self.assertEqual(set(io_gadgets), {"a", "y"})
        self.assertTrue(all(item.kind == LbpGadgetKind.NOT for item in io_gadgets.values()))
        self.assertTrue(all(not item.inverted for item in io_gadgets.values()))
        connections = {
            (
                item.source.gadget.value,
                item.source.port,
                item.target.gadget.value,
                item.target.port,
            )
            for item in realization.connections
        }
        gate_id = f"object:{gate.identifier.value}"
        input_id = io_gadgets["a"].identifier.value
        output_id = io_gadgets["y"].identifier.value
        self.assertIn((input_id, 0, gate_id, 0), connections)
        self.assertIn((gate_id, 0, output_id, 0), connections)

    def test_vector_io_buffers_use_bit_suffixes(self) -> None:
        left = _material_object(
            "left",
            LBPNotGateType(1, False).get_type(),
            "a",
        )
        right = _material_object(
            "right",
            LBPNotGateType(1, False).get_type(),
            "c",
        )
        design = MaterialDesign(
            (left, right),
            (
                _material_net(
                    LBP_WIRE,
                    MaterialModulePortRef("top", "data", 0, PortDirection.INPUT),
                    MaterialObjectPortRef(left.identifier, "IN_0"),
                ),
                _material_net(
                    LBP_WIRE,
                    MaterialModulePortRef("top", "data", 1, PortDirection.INPUT),
                    MaterialObjectPortRef(right.identifier, "IN_0"),
                ),
            ),
        )

        realization = _realize(design)
        names = {
            item.name
            for item in realization.gadgets
            if item.source == LbpGadgetSource.MODULE_PORT
        }

        self.assertEqual(names, {"data[0]", "data[1]"})

    def test_synthesizes_binary_constant_batteries(self) -> None:
        for value in ("0", "1"):
            with self.subTest(value=value):
                gate = _material_object(
                    "gate",
                    LBPNotGateType(1, False).get_type(),
                    "e",
                )
                design = MaterialDesign(
                    (gate,),
                    (
                        _material_net(
                            LBP_WIRE,
                            MaterialConstantRef(value),
                            MaterialObjectPortRef(gate.identifier, "IN_0"),
                        ),
                    ),
                )

                realization = _realize(design)
                battery = next(
                    item
                    for item in realization.gadgets
                    if item.source == LbpGadgetSource.CONSTANT
                )

                self.assertEqual(battery.kind, LbpGadgetKind.BATTERY)
                self.assertEqual(battery.manual_activation, value == "1")
                self.assertTrue(
                    any(
                        connection.source.gadget == battery.identifier
                        for connection in realization.connections
                    )
                )

    def test_rejects_unknown_constant_state(self) -> None:
        gate = _material_object(
            "gate",
            LBPNotGateType(1, False).get_type(),
            "g",
        )
        design = MaterialDesign(
            (gate,),
            (
                _material_net(
                    LBP_WIRE,
                    MaterialConstantRef("x"),
                    MaterialObjectPortRef(gate.identifier, "IN_0"),
                ),
            ),
        )

        with self.assertRaisesRegex(LbpPlanRealizationError, "constant 'x'"):
            _realize(design)

    def test_metadata_and_board_size_use_selected_defaults(self) -> None:
        gate = _material_object(
            "gate",
            LBPNotGateType(1, False).get_type(),
            "i",
        )
        design = MaterialDesign(
            (gate,),
            (
                _material_net(
                    LBP_WIRE,
                    MaterialModulePortRef("my_top", "a", 0, PortDirection.INPUT),
                    MaterialObjectPortRef(gate.identifier, "IN_0"),
                ),
            ),
        )

        realization = _realize(design)

        self.assertEqual(realization.metadata.title, "my_top")
        self.assertEqual(realization.metadata.creator, "GateForge")
        self.assertIn("1 material objects", realization.metadata.description)
        self.assertGreaterEqual(realization.board_size.x, 420.0)
        self.assertGreaterEqual(realization.board_size.y, 262.5)
        self.assertEqual(realization.board_size.x % 52.5, 0.0)
        self.assertEqual(realization.board_size.y % 52.5, 0.0)

    def test_metadata_overrides_are_applied(self) -> None:
        gate = _material_object(
            "gate",
            LBPNotGateType(1, False).get_type(),
            "k",
        )

        realization = _realize(
            MaterialDesign((gate,), ()),
            title="Title",
            description="Description",
            creator="Creator",
        )

        self.assertEqual(realization.metadata.title, "Title")
        self.assertEqual(realization.metadata.description, "Description")
        self.assertEqual(realization.metadata.creator, "Creator")


if __name__ == "__main__":
    unittest.main()
