from dataclasses import replace
from pathlib import Path
import unittest

from gateforge.compiler import compile_material, design_preprocess
from gateforge.graph import MaterialGraph, ModuleValueSubject, ObjectSubject
from gateforge.material import MaterialModuleValueRef
from gateforge.placement import TopologicalPlacementOptions, TopologicalPlacer
from gateforge.providers.factorio.common import FACTORIO_PROVIDER
from gateforge.providers.factorio.configuration import (
    FactorioArithmeticConfiguration,
)
from gateforge.providers.factorio.mapping import FactorioAddMapper
from gateforge.providers.factorio.objects import make_factorio_provider


FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"


class FactorioMappingTests(unittest.TestCase):
    def test_mapper_accepts_exact_unsigned_add32(self) -> None:
        context = design_preprocess(str(FIXTURE))

        proposals = FactorioAddMapper().map(context.snapshot())

        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(len(proposal.boundary), 96)
        self.assertEqual(
            {port.name: port.width for port in proposal.prefab.ports},
            {"A": 32, "B": 32, "Y": 32},
        )
        self.assertEqual(
            {
                target.name: sorted(
                    binding.target.bit
                    for binding in proposal.boundary
                    if binding.target.port == target.name
                )
                for target in proposal.prefab.ports
            },
            {name: list(range(32)) for name in ("A", "B", "Y")},
        )
        make_factorio_provider().validate(proposal.prefab)

    def test_mapper_rejects_signed_operand(self) -> None:
        context = design_preprocess(str(FIXTURE))
        snapshot = context.snapshot()
        module = next(
            item
            for item in snapshot.modules.values()
            if any(
                cell.identifier.expected_type == "$add"
                for cell in item.cells.values()
            )
        )
        cell = next(
            item
            for item in module.cells.values()
            if item.identifier.expected_type == "$add"
        )
        signed_cell = replace(
            cell,
            parameters={**cell.parameters, "A_SIGNED": "1"},
        )
        signed_module = replace(
            module,
            cells={**module.cells, cell.identifier.name: signed_cell},
        )
        signed_snapshot = replace(
            snapshot,
            modules={**snapshot.modules, module.name: signed_module},
        )

        self.assertEqual(FactorioAddMapper().map(signed_snapshot), ())

    def test_add32_materializes_and_places_one_native_combinator(self) -> None:
        provider = make_factorio_provider()
        providers = {FACTORIO_PROVIDER: provider}
        _, state, material = compile_material(
            str(FIXTURE),
            mapping_providers=(FactorioAddMapper(),),
            target_providers=providers,
        )

        self.assertEqual(len(state.claims), 1)
        claim = next(iter(state.claims.values()))
        self.assertEqual(
            {binding.targets[0].port: len(binding.targets) for binding in claim.ports},
            {"A": 32, "B": 32, "Y": 32},
        )
        self.assertEqual(len(material.objects), 1)
        self.assertEqual(len(material.nets), 3)
        module_values = {
            attachment
            for net in material.nets
            for attachment in net.attachments
            if isinstance(attachment, MaterialModuleValueRef)
        }
        self.assertEqual(
            {(item.port, item.bits) for item in module_values},
            {(name, tuple(range(32))) for name in ("a", "b", "y")},
        )
        configuration = provider.decode_object_configuration(
            material.objects[0].type,
            material.objects[0].configuration,
        )
        self.assertEqual(
            configuration,
            FactorioArithmeticConfiguration("add", 32, 32, 32, False, False),
        )

        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(
            TopologicalPlacementOptions(
                column_pitch=6.0,
                row_pitch=3.0,
                routing_group_height=8.0,
                routing_gap_rows=1,
            ),
            providers=providers,
        ).place(graph).finalize(graph)

        self.assertEqual(
            len([item for item in graph.subjects if isinstance(item, ObjectSubject)]),
            1,
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in graph.subjects
                    if isinstance(item, ModuleValueSubject)
                ]
            ),
            3,
        )
        self.assertEqual(
            [item.kind for item in placed.components].count("arithmetic-combinator"),
            1,
        )
        self.assertEqual(
            [item.kind for item in placed.components].count("medium-electric-pole"),
            3,
        )
        self.assertEqual(len(placed.root_container.connections), 3)


if __name__ == "__main__":
    unittest.main()