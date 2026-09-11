from pathlib import Path
import unittest

from gateforge.compiler import compilation_backend, compile_material
from gateforge.graph import MaterialGraph
from gateforge.material import MaterialNetId
from gateforge.placement import TopologicalPlacer
from gateforge.providers.factorio.profile import factorio_entity_profile
from gateforge.providers.factorio.routed import (
    FactorioConnectorEndpoint,
    FactorioEntity,
    FactorioInputDriverMode,
    FactorioRoutingError,
    FactorioWireColor,
    connector_distance,
    entities_collide,
)
from gateforge.providers.factorio.routing import (
    _output_lamp_entity,
    _power_segments,
    _route_link,
    _within_reach,
    build_factorio_routed_design,
)
from gateforge.providers.factorio.visualization import _power_wire_element
from gateforge.visualization.build import build_visual_document
from gateforge.visualization.model import VisualPolyline


FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"


class FactorioRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _, _, cls.material = compile_material(str(FIXTURE), target="factorio")
        cls.backend = compilation_backend("factorio")
        cls.graph = MaterialGraph.from_design(
            cls.material,
            cls.backend.target_providers,
        )
        cls.placement = TopologicalPlacer(
            cls.backend.placement_options,
            providers=cls.backend.target_providers,
        ).place(cls.graph).finalize(cls.graph)

    def test_compact_add_routes_directly_with_isolated_input_colors(self) -> None:
        routed = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
        )

        self.assertEqual(len(routed.entities), 4)
        self.assertEqual(len(routed.wires), 3)
        self.assertFalse(
            any(item.identifier.startswith("factorio:relay:") for item in routed.entities)
        )
        assignments = {
            item.signal: item.color for item in routed.signal_assignments
        }
        self.assertEqual(
            assignments,
            {
                "signal-A": FactorioWireColor.RED,
                "signal-B": FactorioWireColor.GREEN,
                "signal-C": FactorioWireColor.RED,
            },
        )

    def test_constant_drivers_use_named_values_and_signed_counts(self) -> None:
        routed = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
            input_drivers=FactorioInputDriverMode.CONSTANT,
            input_values={"a": "0xffffffff", "b": "0x100000002"},
        )

        self.assertEqual(len(routed.entities), 6)
        self.assertEqual(len(routed.wires), 5)
        self.assertEqual(
            {
                item.port: (item.unsigned_value, item.signed_count)
                for item in routed.input_drivers
            },
            {"a": (0xFFFFFFFF, -1), "b": (2, 2)},
        )

    def test_output_lamps_are_optional_and_inherit_the_output_signal(self) -> None:
        routed = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
            output_lamps=True,
        )

        self.assertEqual(len(routed.output_lamps), 1)
        lamp = routed.output_lamps[0]
        self.assertEqual(lamp.port, "y")
        self.assertEqual(lamp.signal, "signal-C")
        self.assertEqual(lamp.color, FactorioWireColor.RED)
        self.assertEqual(
            next(
                entity.prototype
                for entity in routed.entities
                if entity.identifier == lamp.entity
            ),
            "small-lamp",
        )
        self.assertEqual(
            len(
                [
                    wire
                    for wire in routed.wires
                    if lamp.entity in {wire.source.entity, wire.target.entity}
                ]
            ),
            1,
        )
        self.assertEqual(
            routed.canonical_data(),
            build_factorio_routed_design(
                self.material,
                self.graph,
                self.placement,
                output_lamps=True,
            ).canonical_data(),
        )

    def test_output_lamp_uses_collision_free_fallback_position(self) -> None:
        terminal = FactorioEntity(
            "terminal", "medium-electric-pole", 0.5, 0.5
        )
        blocker = FactorioEntity("blocker", "small-lamp", 1.5, 0.5)

        lamp = _output_lamp_entity("y", terminal, (terminal, blocker))

        self.assertEqual((lamp.x, lamp.y), (0.5, -0.5))
        self.assertFalse(entities_collide(lamp, terminal))
        self.assertFalse(entities_collide(lamp, blocker))

    def test_output_lamp_visualization_shows_signal_condition(self) -> None:
        document = build_visual_document(
            self.material,
            self.graph,
            self.placement,
            self.backend.target_providers,
            provider_options={"factorio": {"output_lamps": True}},
        )
        routed_scene = next(
            view for view in document.views if view.identifier == "factorio:routed"
        ).scenes[0]
        lamp = next(
            item
            for item in routed_scene.elements
            if item.identifier.startswith("factorio-entity:factorio:output-lamp:")
        )

        properties = {
            item.name: item.value for item in lamp.descriptor.properties
        }
        self.assertEqual(properties["Signal"], "signal-C")
        self.assertEqual(properties["Condition"], "> 0")

    def test_input_values_require_drivers_and_known_ports(self) -> None:
        with self.assertRaisesRegex(FactorioRoutingError, "require constant"):
            build_factorio_routed_design(
                self.material,
                self.graph,
                self.placement,
                input_values={"a": 1},
            )
        with self.assertRaisesRegex(FactorioRoutingError, "Unknown.*missing"):
            build_factorio_routed_design(
                self.material,
                self.graph,
                self.placement,
                input_drivers="constant",
                input_values={"missing": 1},
            )

    def test_routing_is_deterministic(self) -> None:
        options = {
            "input_drivers": "constant",
            "input_values": {"a": 1, "b": 2},
        }

        first = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
            **options,
        )
        second = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
            **options,
        )

        self.assertEqual(first.canonical_data(), second.canonical_data())
        self.assertEqual(first.get_digest(), second.get_digest())

    def test_visualization_shows_logical_ports_and_physical_wires(self) -> None:
        document = build_visual_document(
            self.material,
            self.graph,
            self.placement,
            self.backend.target_providers,
            provider_options={
                "factorio": {
                    "input_drivers": "constant",
                    "input_values": {"a": "0xffffffff", "b": 2},
                }
            },
        )
        views = {item.identifier: item for item in document.views}

        material_scene = views["material"].scenes[0]
        arithmetic = next(
            item
            for item in material_scene.elements
            if item.identifier.startswith("object:")
        )
        self.assertEqual(len(arithmetic.descriptor.ports), 96)
        self.assertIn(
            "a[0]",
            {item.identifier for item in arithmetic.descriptor.ports},
        )
        placed_scene = views["factorio:placed"].scenes[0]
        routed_scene = views["factorio:routed"].scenes[0]
        self.assertEqual(len(placed_scene.elements), 6)
        self.assertFalse(
            any(
                item.identifier.startswith("factorio-wire:")
                for item in placed_scene.elements
            )
        )
        wires = tuple(
            item
            for item in routed_scene.elements
            if item.identifier.startswith("factorio-wire:")
        )
        self.assertEqual(len(wires), 5)
        self.assertEqual(
            {
                primitive.style.stroke
                for item in wires
                for primitive in item.descriptor.primitives
                if isinstance(primitive, VisualPolyline)
            },
            {"#d64b45", "#3f9b55"},
        )
        self.assertFalse(routed_scene.nets)

    def test_reach_uses_the_smaller_endpoint_limit(self) -> None:
        left = FactorioEntity("left", "medium-electric-pole", 0.0, 0.0)
        exact = FactorioEntity("exact", "big-electric-pole", 9.0, 0.0)
        over = FactorioEntity("over", "big-electric-pole", 9.001, 0.0)

        self.assertTrue(_within_reach(left, 1, exact, 1))
        self.assertFalse(_within_reach(left, 1, over, 1))

    def test_blocked_direct_lane_uses_deterministic_lateral_offset(self) -> None:
        net = MaterialNetId("a" * 64)
        source = FactorioEntity("source", "arithmetic-combinator", 0.0, 0.0)
        target = FactorioEntity("target", "arithmetic-combinator", 16.0, 0.0)
        blocker = FactorioEntity("blocker", "medium-electric-pole", 8.0, 0.0)
        entities = {item.identifier: item for item in (source, target, blocker)}

        relays, wires = _route_link(
            net,
            FactorioWireColor.RED,
            FactorioConnectorEndpoint("source", 2),
            FactorioConnectorEndpoint("target", 1),
            entities,
            0,
        )

        self.assertEqual(len(relays), 1)
        self.assertNotEqual(relays[0].y, 0.0)
        self.assertEqual(len(wires), 2)

    def test_long_route_uses_medium_endpoints_and_big_trunk(self) -> None:
        net = MaterialNetId("b" * 64)
        source = FactorioEntity("source", "arithmetic-combinator", 0.0, 0.0)
        target = FactorioEntity("target", "arithmetic-combinator", 100.0, 0.0)
        entities = {item.identifier: item for item in (source, target)}

        relays, wires = _route_link(
            net,
            FactorioWireColor.GREEN,
            FactorioConnectorEndpoint("source", 2),
            FactorioConnectorEndpoint("target", 1),
            entities,
            0,
        )

        self.assertEqual(
            [item.prototype for item in relays].count("medium-electric-pole"),
            2,
        )
        self.assertGreaterEqual(
            [item.prototype for item in relays].count("big-electric-pole"),
            2,
        )
        routed_entities = {**entities, **{item.identifier: item for item in relays}}
        for wire in wires:
            left = routed_entities[wire.source.entity]
            right = routed_entities[wire.target.entity]
            distance = connector_distance(
                left,
                wire.source.connector,
                right,
                wire.target.connector,
            )
            reach = min(
                factorio_entity_profile(left.prototype).circuit_wire_reach,
                factorio_entity_profile(right.prototype).circuit_wire_reach,
            )
            self.assertLessEqual(distance, reach)
        power_segments = _power_segments(routed_entities, wires)
        self.assertEqual(len(power_segments), len(relays) - 1)
        self.assertEqual(
            {
                endpoint
                for segment in power_segments
                for endpoint in (segment.source, segment.target)
            },
            {item.identifier for item in relays},
        )

    def test_copper_power_segment_has_distinct_visualization(self) -> None:
        element = _power_wire_element(
            0,
            "left",
            "right",
            (0.5, 0.5),
            (8.5, 0.5),
        )

        self.assertEqual(element.identifier, "factorio-power-wire:0")
        self.assertFalse(element.collision_enabled)
        self.assertEqual(
            {
                primitive.style.stroke
                for primitive in element.descriptor.primitives
                if isinstance(primitive, VisualPolyline)
            },
            {"#b87333"},
        )
        self.assertEqual(
            {(item.kind, item.identifier) for item in element.references},
            {("factorio_entity", "left"), ("factorio_entity", "right")},
        )


if __name__ == "__main__":
    unittest.main()