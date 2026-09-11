from pathlib import Path
from typing import Any, cast
import unittest

from gateforge.compiler import compilation_backend, compile_material, design_preprocess
from gateforge.graph import MaterialGraph
from gateforge.intrinsics import DEFAULT_INTRINSICS, IntrinsicKind
from gateforge.placement import TopologicalPlacer
from gateforge.providers.factorio.common import FACTORIO_LAMP
from gateforge.providers.factorio.configuration import FactorioLampConfiguration
from gateforge.providers.factorio.intrinsics import FactorioLampIntrinsicMapper
from gateforge.providers.factorio.objects import make_factorio_provider
from gateforge.providers.factorio.blueprint import build_factorio_blueprint
from gateforge.providers.factorio.routing import build_factorio_routed_design
from gateforge.providers.lbp.intrinsics import LBPIntrinsicMapper
from gateforge.visualization.build import build_visual_document


FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "lamp_intrinsic.v"


class FactorioLampIntrinsicTests(unittest.TestCase):
    def test_lamp_is_recognized_and_mapped_as_a_required_sink(self) -> None:
        context = design_preprocess(str(FIXTURE))
        snapshot = context.snapshot()
        cell = snapshot.module("top").cells["lamp"]

        instance = DEFAULT_INTRINSICS.recognize(snapshot, cell)
        if instance is None:
            self.fail("GF_Lamp was not recognized as an intrinsic")
        self.assertEqual(instance.definition.kind, IntrinsicKind.LAMP)

        proposals = FactorioLampIntrinsicMapper().map(snapshot)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal.mapper, "factorio.intrinsic.lamp")
        self.assertEqual(len(proposal.boundary), 32)
        self.assertEqual(
            {item.name: item.width for item in proposal.prefab.ports},
            {"in": 32},
        )
        self.assertEqual(
            {item.type for item in proposal.prefab.objects},
            {FACTORIO_LAMP},
        )
        make_factorio_provider().validate(proposal.prefab)

    def test_lbp_mapper_ignores_factorio_lamp(self) -> None:
        context = design_preprocess(str(FIXTURE))

        self.assertEqual(LBPIntrinsicMapper().map(context.snapshot()), ())

    def test_lamp_materializes_as_required_factorio_object(self) -> None:
        _, state, material = compile_material(str(FIXTURE), target="factorio")

        self.assertEqual(
            {claim.mapper for claim in state.claims.values()},
            {"factorio.arithmetic.add32", "factorio.intrinsic.lamp"},
        )
        lamp = next(item for item in material.objects if item.type == FACTORIO_LAMP)
        self.assertEqual(
            make_factorio_provider().decode_object_configuration(
                lamp.type,
                lamp.configuration,
            ),
            FactorioLampConfiguration(),
        )

    def test_lamp_elaborates_as_a_connected_physical_component(self) -> None:
        backend = compilation_backend("factorio")
        _, _, material = compile_material(str(FIXTURE), target="factorio")
        graph = MaterialGraph.from_design(material, backend.target_providers)

        placement = TopologicalPlacer(
            backend.placement_options,
            providers=backend.target_providers,
        ).place(graph).finalize(graph)

        lamp = next(
            item for item in placement.components if item.kind == "small-lamp"
        )
        self.assertEqual((lamp.bounds.width, lamp.bounds.height), (1.0, 1.0))
        self.assertEqual(
            len(
                [
                    connection
                    for connection in placement.root_container.connections
                    if connection.target.identifier == lamp.identifier
                ]
            ),
            1,
        )

    def test_lamp_routes_on_the_producer_signal_and_exports_condition(self) -> None:
        backend = compilation_backend("factorio")
        _, _, material = compile_material(str(FIXTURE), target="factorio")
        graph = MaterialGraph.from_design(material, backend.target_providers)
        placement = TopologicalPlacer(
            backend.placement_options,
            providers=backend.target_providers,
        ).place(graph).finalize(graph)

        routed = build_factorio_routed_design(material, graph, placement)
        lamp = next(item for item in routed.entities if item.prototype == "small-lamp")
        incident = [
            wire
            for wire in routed.wires
            if lamp.identifier in {wire.source.entity, wire.target.entity}
        ]
        self.assertEqual(len(incident), 1)
        assignment = next(
            item for item in routed.signal_assignments if item.net == incident[0].net
        )
        self.assertEqual(assignment.signal, "signal-C")
        self.assertFalse(routed.output_lamps)

        data = cast(
            dict[str, Any],
            build_factorio_blueprint(routed).canonical_data()["blueprint"],
        )
        lamp_data = next(
            item
            for item in cast(list[dict[str, Any]], data["entities"])
            if item["name"] == "small-lamp"
        )
        self.assertEqual(
            cast(dict[str, Any], lamp_data["control_behavior"])[
                "circuit_condition"
            ],
            {
                "first_signal": {"type": "virtual", "name": "signal-C"},
                "comparator": ">",
                "constant": 0,
            },
        )

    def test_lamp_is_rendered_in_material_and_routed_views(self) -> None:
        backend = compilation_backend("factorio")
        _, _, material = compile_material(str(FIXTURE), target="factorio")
        graph = MaterialGraph.from_design(material, backend.target_providers)
        placement = TopologicalPlacer(
            backend.placement_options,
            providers=backend.target_providers,
        ).place(graph).finalize(graph)

        document = build_visual_document(
            material,
            graph,
            placement,
            backend.target_providers,
        )
        views = {item.identifier: item for item in document.views}
        material_lamp = next(
            item
            for item in views["material"].scenes[0].elements
            if item.descriptor.label == "LAMP"
        )
        self.assertEqual(
            {item.identifier for item in material_lamp.descriptor.ports},
            {"in[0]"},
        )
        routed_lamp = next(
            item
            for item in views["factorio:routed"].scenes[0].elements
            if item.descriptor.label == "C > 0"
        )
        self.assertEqual(
            {item.name: item.value for item in routed_lamp.descriptor.properties}[
                "Condition"
            ],
            "> 0",
        )


if __name__ == "__main__":
    unittest.main()