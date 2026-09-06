import unittest

from gateforge.graph import MaterialGraph
from gateforge.material import (
    MaterialDesign,
    MaterialModulePortRef,
    MaterialObjectPortRef,
)
from gateforge.placement import (
    PlacementError,
    TopologicalPlacementOptions,
    TopologicalPlacer,
)
from gateforge.providers.lbp.common import LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.types import LBPNotGateType
from gateforge.target import PortDirection
from tests.test_graph import (
    ADDITIVE_NODE,
    ADDITIVE_PROVIDER,
    ADDITIVE_WIRE,
    _additive_provider,
    _lbp_inverter_design,
    _material_net,
    _material_object,
)


def _coordinates(graph: MaterialGraph, options=None):
    placer = TopologicalPlacer(options or TopologicalPlacementOptions())
    placed = placer.place(graph).finalize(graph)
    return {
        item.object: (item.x, item.y) for item in placed.placements.objects
    }, placed


def _lbp_chain(length: int) -> MaterialDesign:
    gates = tuple(
        _material_object(
            f"gate_{index}",
            LBPNotGateType(width=1, invert=False).get_type(),
            chr(ord("a") + index * 2),
        )
        for index in range(length)
    )
    nets = [
        _material_net(
            LBP_WIRE,
            MaterialModulePortRef("test", "a", 0, PortDirection.INPUT),
            MaterialObjectPortRef(gates[0].identifier, "IN_0"),
        )
    ]
    nets.extend(
        _material_net(
            LBP_WIRE,
            MaterialObjectPortRef(left.identifier, "OUT"),
            MaterialObjectPortRef(right.identifier, "IN_0"),
        )
        for left, right in zip(gates, gates[1:])
    )
    nets.append(
        _material_net(
            LBP_WIRE,
            MaterialObjectPortRef(gates[-1].identifier, "OUT"),
            MaterialModulePortRef("test", "y", 0, PortDirection.OUTPUT),
        )
    )
    return MaterialDesign(gates, tuple(nets))


class TopologicalPlacementTests(unittest.TestCase):
    def test_linear_chain_places_every_dependency_left_to_right(self) -> None:
        design = _lbp_chain(3)
        graph = MaterialGraph.from_design(
            design,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        coordinates, placed = _coordinates(graph)

        gates_by_role = {item.role: item for item in design.objects}
        ordered_x = [
            coordinates[gates_by_role[f"gate_{index}"].identifier][0]
            for index in range(3)
        ]
        self.assertEqual(ordered_x, sorted(ordered_x))
        self.assertEqual(len(set(ordered_x)), 3)
        terminal_x = [item.x for item in placed.placements.module_ports]
        self.assertLess(min(terminal_x), ordered_x[0])
        self.assertGreater(max(terminal_x), ordered_x[-1])
        self.assertEqual((placed.bounds.min_x + placed.bounds.max_x) / 2, 0.0)

    def test_additive_multi_source_net_places_both_sources_before_sink(self) -> None:
        left = _material_object("left", ADDITIVE_NODE, "a")
        right = _material_object("right", ADDITIVE_NODE, "c")
        sink = _material_object("sink", ADDITIVE_NODE, "e")
        net = _material_net(
            ADDITIVE_WIRE,
            MaterialObjectPortRef(left.identifier, "OUT"),
            MaterialObjectPortRef(right.identifier, "OUT"),
            MaterialObjectPortRef(sink.identifier, "IN"),
        )
        graph = MaterialGraph.from_design(
            MaterialDesign((left, right, sink), (net,)),
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        coordinates, _ = _coordinates(graph)

        self.assertLess(coordinates[left.identifier][0], coordinates[sink.identifier][0])
        self.assertLess(coordinates[right.identifier][0], coordinates[sink.identifier][0])
        self.assertNotEqual(coordinates[left.identifier][1], coordinates[right.identifier][1])

    def test_unequal_paths_are_output_biased(self) -> None:
        short_source = _material_object("short_source", ADDITIVE_NODE, "a")
        short_inner = _material_object("short_inner", ADDITIVE_NODE, "c")
        long_source = _material_object("long_source", ADDITIVE_NODE, "e")
        long_first = _material_object("long_first", ADDITIVE_NODE, "g")
        long_second = _material_object("long_second", ADDITIVE_NODE, "i")
        sink = _material_object("sink", ADDITIVE_NODE, "k")
        nets = (
            _material_net(
                ADDITIVE_WIRE,
                MaterialObjectPortRef(short_source.identifier, "OUT"),
                MaterialObjectPortRef(short_inner.identifier, "IN"),
            ),
            _material_net(
                ADDITIVE_WIRE,
                MaterialObjectPortRef(long_source.identifier, "OUT"),
                MaterialObjectPortRef(long_first.identifier, "IN"),
            ),
            _material_net(
                ADDITIVE_WIRE,
                MaterialObjectPortRef(long_first.identifier, "OUT"),
                MaterialObjectPortRef(long_second.identifier, "IN"),
            ),
            _material_net(
                ADDITIVE_WIRE,
                MaterialObjectPortRef(short_inner.identifier, "OUT"),
                MaterialObjectPortRef(long_second.identifier, "OUT"),
                MaterialObjectPortRef(sink.identifier, "IN"),
            ),
        )
        design = MaterialDesign(
            (short_source, short_inner, long_source, long_first, long_second, sink),
            nets,
        )
        graph = MaterialGraph.from_design(
            design,
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        coordinates, _ = _coordinates(graph)

        self.assertEqual(
            coordinates[short_source.identifier][0],
            coordinates[long_source.identifier][0],
        )
        self.assertEqual(
            coordinates[short_inner.identifier][0],
            coordinates[long_second.identifier][0],
        )
        self.assertLess(
            coordinates[long_first.identifier][0],
            coordinates[long_second.identifier][0],
        )

    def test_boundary_only_design_uses_two_centered_columns(self) -> None:
        net = _material_net(
            LBP_WIRE,
            MaterialModulePortRef("test", "a", 0, PortDirection.INPUT),
            MaterialModulePortRef("test", "y", 0, PortDirection.OUTPUT),
        )
        graph = MaterialGraph.from_design(
            MaterialDesign((), (net,)),
            {LBP_PROVIDER: make_lbp_provider()},
        )

        _, placed = _coordinates(graph)

        self.assertEqual(
            [item.x for item in placed.placements.module_ports],
            [-131.25, 131.25],
        )

    def test_isolated_object_uses_exact_midpoint_of_object_span(self) -> None:
        chain = _lbp_chain(3)
        isolated = _material_object(
            "isolated",
            LBPNotGateType(width=1, invert=False).get_type(),
            "q",
        )
        graph = MaterialGraph.from_design(
            MaterialDesign((*chain.objects, isolated), chain.nets),
            {LBP_PROVIDER: make_lbp_provider()},
        )

        coordinates, _ = _coordinates(graph)

        gates_by_role = {item.role: item for item in chain.objects}
        first_x = coordinates[gates_by_role["gate_0"].identifier][0]
        last_x = coordinates[gates_by_role["gate_2"].identifier][0]
        self.assertEqual(
            coordinates[isolated.identifier][0],
            (first_x + last_x) / 2,
        )

    def test_cycle_error_reports_the_complete_component(self) -> None:
        left = _material_object("left", ADDITIVE_NODE, "a")
        right = _material_object("right", ADDITIVE_NODE, "c")
        design = MaterialDesign(
            (left, right),
            (
                _material_net(
                    ADDITIVE_WIRE,
                    MaterialObjectPortRef(left.identifier, "OUT"),
                    MaterialObjectPortRef(right.identifier, "IN"),
                ),
                _material_net(
                    ADDITIVE_WIRE,
                    MaterialObjectPortRef(right.identifier, "OUT"),
                    MaterialObjectPortRef(left.identifier, "IN"),
                ),
            ),
        )
        graph = MaterialGraph.from_design(
            design,
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        with self.assertRaises(PlacementError) as raised:
            TopologicalPlacer().place(graph)

        self.assertIn(left.identifier.value, str(raised.exception))
        self.assertIn(right.identifier.value, str(raised.exception))

    def test_empty_graph_is_rejected(self) -> None:
        graph = MaterialGraph.from_design(MaterialDesign((), ()), {})

        with self.assertRaises(PlacementError):
            TopologicalPlacer().place(graph)

    def test_pitch_must_be_positive_and_is_applied(self) -> None:
        graph = MaterialGraph.from_design(
            _lbp_inverter_design(),
            {LBP_PROVIDER: make_lbp_provider()},
        )

        _, placed = _coordinates(
            graph,
            TopologicalPlacementOptions(column_pitch=100.0, row_pitch=50.0),
        )

        self.assertEqual(placed.bounds.min_x, -100.0)
        self.assertEqual(placed.bounds.max_x, 100.0)
        with self.assertRaises(PlacementError):
            TopologicalPlacementOptions(column_pitch=0)

    def test_repeated_placement_is_deterministic(self) -> None:
        graph = MaterialGraph.from_design(
            _lbp_chain(3),
            {LBP_PROVIDER: make_lbp_provider()},
        )
        placer = TopologicalPlacer()

        first = placer.place(graph).finalize(graph)
        second = placer.place(graph).finalize(graph)

        self.assertEqual(first.canonical_data(), second.canonical_data())


if __name__ == "__main__":
    unittest.main()
