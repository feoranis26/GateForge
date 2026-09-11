from pathlib import Path
import json
from typing import Any, cast
import unittest

from gateforge.compiler import compilation_backend, compile_material
from gateforge.graph import MaterialGraph
from gateforge.placement import TopologicalPlacer
from gateforge.providers.factorio.blueprint import (
    FACTORIO_BLUEPRINT_VERSION,
    build_factorio_blueprint,
)
from gateforge.providers.factorio.routed import (
    FactorioEntity,
    FactorioPowerSegment,
    FactorioRoutedDesign,
)
from gateforge.providers.factorio.routing import build_factorio_routed_design


FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"
GOLDEN = Path(__file__).parent / "fixtures" / "factorio" / "SAMPLE.json"


class FactorioBlueprintTests(unittest.TestCase):
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

    def test_add32_export_uses_factorio_2_blueprint_contract(self) -> None:
        routed = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
            input_drivers="constant",
            input_values={"a": "0xffffffff", "b": 2},
        )

        encoded = build_factorio_blueprint(
            routed,
            label="GateForge add32",
        ).canonical_data()
        blueprint = cast(dict[str, Any], encoded["blueprint"])

        self.assertEqual(blueprint["item"], "blueprint")
        self.assertEqual(blueprint["label"], "GateForge add32")
        self.assertEqual(blueprint["version"], FACTORIO_BLUEPRINT_VERSION)
        entities = cast(list[dict[str, Any]], blueprint["entities"])
        self.assertEqual(
            [item["entity_number"] for item in entities],
            list(range(1, 7)),
        )
        self.assertEqual(
            [item["name"] for item in entities],
            [
                "constant-combinator",
                "constant-combinator",
                "arithmetic-combinator",
                "medium-electric-pole",
                "medium-electric-pole",
                "medium-electric-pole",
            ],
        )
        self.assertEqual(
            entities[0]["control_behavior"],
            {
                "sections": {
                    "sections": [
                        {
                            "index": 1,
                            "filters": [
                                {
                                    "index": 1,
                                    "type": "virtual",
                                    "name": "signal-A",
                                    "quality": "normal",
                                    "comparator": "=",
                                    "count": -1,
                                }
                            ],
                        }
                    ]
                }
            },
        )
        self.assertEqual(entities[2]["direction"], 4)
        self.assertEqual(
            entities[2]["control_behavior"]["arithmetic_conditions"],
            {
                "first_signal": {"type": "virtual", "name": "signal-A"},
                "second_signal": {"type": "virtual", "name": "signal-B"},
                "operation": "+",
                "output_signal": {"type": "virtual", "name": "signal-C"},
                "first_signal_networks": {"red": True, "green": False},
                "second_signal_networks": {"red": False, "green": True},
            },
        )
        self.assertEqual(
            blueprint["wires"],
            [
                [1, 1, 4, 1],
                [2, 2, 5, 2],
                [3, 1, 4, 1],
                [3, 2, 5, 2],
                [3, 3, 6, 1],
            ],
        )

    def test_golden_fixture_records_the_factorio_2_contract(self) -> None:
        decoded = json.loads(GOLDEN.read_text(encoding="utf-8"))
        blueprint = cast(dict[str, Any], decoded["blueprint"])
        entities = cast(list[dict[str, Any]], blueprint["entities"])

        self.assertEqual(blueprint["version"], FACTORIO_BLUEPRINT_VERSION)
        self.assertEqual(len(entities), 10)
        self.assertEqual(len(cast(list[object], blueprint["wires"])), 8)
        self.assertEqual(entities[4]["direction"], 4)
        self.assertEqual(
            cast(dict[str, Any], entities[0]["control_behavior"])["sections"],
            {
                "sections": [
                    {
                        "index": 1,
                        "filters": [
                            {
                                "index": 1,
                                "type": "virtual",
                                "name": "signal-A",
                                "quality": "normal",
                                "comparator": "=",
                                "count": -1,
                            }
                        ],
                    }
                ]
            },
        )
        self.assertEqual(
            cast(list[list[int]], blueprint["wires"])[-3:],
            [[6, 5, 7, 5], [7, 5, 8, 5], [8, 5, 9, 5]],
        )
        self.assertNotIn("control_behavior", entities[9])

    def test_power_segments_export_as_copper_connector_five(self) -> None:
        routed = FactorioRoutedDesign(
            material_digest=self.material.get_digest(),
            placement_digest="0" * 64,
            entities=(
                FactorioEntity("left", "medium-electric-pole", 0.5, 0.5),
                FactorioEntity("right", "medium-electric-pole", 8.5, 0.5),
            ),
            signal_assignments=(),
            wires=(),
            power_segments=(FactorioPowerSegment("left", "right"),),
        )

        blueprint = cast(
            dict[str, Any],
            build_factorio_blueprint(routed).canonical_data()["blueprint"],
        )

        self.assertEqual(blueprint["wires"], [[1, 5, 2, 5]])

    def test_generated_output_lamp_uses_positive_signal_condition(self) -> None:
        routed = build_factorio_routed_design(
            self.material,
            self.graph,
            self.placement,
            output_lamps=True,
        )

        blueprint = cast(
            dict[str, Any],
            build_factorio_blueprint(routed).canonical_data()["blueprint"],
        )
        lamp = next(
            entity
            for entity in cast(list[dict[str, Any]], blueprint["entities"])
            if entity["name"] == "small-lamp"
        )

        self.assertEqual(
            lamp["control_behavior"],
            {
                "circuit_enabled": True,
                "circuit_condition": {
                    "first_signal": {"type": "virtual", "name": "signal-C"},
                    "comparator": ">",
                    "constant": 0,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()