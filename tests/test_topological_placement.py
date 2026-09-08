import unittest

from gateforge.graph import MaterialGraph, ModulePortSubject, ObjectSubject
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
from gateforge.providers.lbp.types import LBPAndGateType, LBPNotGateType
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
        item.source.object: (item.x, item.y)
        for item in placed.prefabs
        if isinstance(item.source, ObjectSubject)
    }, placed


def _object_placements(placed):
    return {
        item.source.object: item
        for item in placed.prefabs
        if isinstance(item.source, ObjectSubject)
    }


def _module_port_placements(placed):
    return tuple(
        item
        for item in placed.prefabs
        if isinstance(item.source, ModulePortSubject)
    )


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
        terminal_x = [item.x for item in _module_port_placements(placed)]
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
            [item.x for item in _module_port_placements(placed)],
            [-105.0, 105.0],
        )

    def test_routing_gaps_follow_physical_content_height(self) -> None:
        sources = tuple(
            _material_object(
                f"source_{index}",
                LBPNotGateType(width=1, invert=False).get_type(),
                chr(ord("a") + index * 2),
            )
            for index in range(6)
        )
        sinks = tuple(
            _material_object(
                f"sink_{index}",
                LBPNotGateType(width=1, invert=False).get_type(),
                chr(ord("m") + index * 2),
            )
            for index in range(6)
        )
        nets = tuple(
            _material_net(
                LBP_WIRE,
                MaterialObjectPortRef(source.identifier, "OUT"),
                MaterialObjectPortRef(sink.identifier, "IN_0"),
            )
            for source, sink in zip(sources, sinks)
        )
        graph = MaterialGraph.from_design(
            MaterialDesign((*sources, *sinks), nets),
            {LBP_PROVIDER: make_lbp_provider()},
        )

        placed = TopologicalPlacer(
            providers={LBP_PROVIDER: make_lbp_provider()}
        ).place(graph).finalize(graph)
        by_object = _object_placements(placed)
        source_y = sorted(by_object[item.identifier].y for item in sources)
        sink_y = sorted(by_object[item.identifier].y for item in sinks)

        self.assertEqual(source_y, sink_y)
        self.assertEqual(
            [right - left for left, right in zip(source_y, source_y[1:])],
            [52.5, 52.5, 52.5, 145.0, 52.5],
        )

    def test_provider_geometry_reserves_multiple_rows_for_wide_gates(self) -> None:
        wide = _material_object(
            "wide",
            LBPAndGateType(width=5, invert=False).get_type(),
            "a",
        )
        narrow = _material_object(
            "narrow",
            LBPNotGateType(width=1, invert=False).get_type(),
            "c",
        )
        graph = MaterialGraph.from_design(
            MaterialDesign((wide, narrow), ()),
            {LBP_PROVIDER: make_lbp_provider()},
        )

        placed = TopologicalPlacer(
            providers={LBP_PROVIDER: make_lbp_provider()}
        ).place(graph).finalize(graph)
        by_object = _object_placements(placed)

        self.assertEqual(
            abs(by_object[wide.identifier].y - by_object[narrow.identifier].y),
            91.875,
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

    def test_cycle_is_layered_with_one_feedback_edge(self) -> None:
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

        first = TopologicalPlacer().place(graph).finalize(graph)
        second = TopologicalPlacer().place(graph).finalize(graph)
        by_object = {
            identifier: (item.x, item.y)
            for identifier, item in _object_placements(first).items()
        }

        self.assertNotEqual(by_object[left.identifier][0], by_object[right.identifier][0])
        self.assertEqual(first.canonical_data(), second.canonical_data())
        self.assertEqual(len(graph.dependencies), 2)

    def test_self_loop_is_placed_without_dropping_dependency(self) -> None:
        gate = _material_object("gate", ADDITIVE_NODE, "e")
        loop = _material_net(
            ADDITIVE_WIRE,
            MaterialObjectPortRef(gate.identifier, "OUT"),
            MaterialObjectPortRef(gate.identifier, "IN"),
        )
        graph = MaterialGraph.from_design(
            MaterialDesign((gate,), (loop,)),
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        coordinates, _ = _coordinates(graph)

        self.assertEqual(set(coordinates), {gate.identifier})
        self.assertEqual(len(graph.dependencies), 1)

    def test_fan_in_and_fan_out_surround_feedback_component(self) -> None:
        source = _material_object("source", ADDITIVE_NODE, "g")
        left = _material_object("left", ADDITIVE_NODE, "i")
        right = _material_object("right", ADDITIVE_NODE, "k")
        sink = _material_object("sink", ADDITIVE_NODE, "m")
        design = MaterialDesign(
            (source, left, right, sink),
            (
                _material_net(
                    ADDITIVE_WIRE,
                    MaterialObjectPortRef(left.identifier, "OUT"),
                    MaterialObjectPortRef(right.identifier, "IN"),
                ),
                _material_net(
                    ADDITIVE_WIRE,
                    MaterialObjectPortRef(source.identifier, "OUT"),
                    MaterialObjectPortRef(right.identifier, "OUT"),
                    MaterialObjectPortRef(left.identifier, "IN"),
                    MaterialObjectPortRef(sink.identifier, "IN"),
                ),
            ),
        )
        graph = MaterialGraph.from_design(
            design,
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        coordinates, _ = _coordinates(graph)

        feedback_x = (
            coordinates[left.identifier][0],
            coordinates[right.identifier][0],
        )
        self.assertLess(coordinates[source.identifier][0], min(feedback_x))
        self.assertNotEqual(*feedback_x)
        self.assertLess(max(feedback_x), coordinates[sink.identifier][0])

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
        with self.assertRaises(PlacementError):
            TopologicalPlacementOptions(routing_group_height=0)

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
