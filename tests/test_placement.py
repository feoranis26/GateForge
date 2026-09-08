from copy import deepcopy
from dataclasses import replace
import unittest

from gateforge.graph import MaterialGraph, ObjectSubject
from gateforge.material import MaterialDesignDigest
from gateforge.placement import (
    PhysicalEndpoint,
    PhysicalEndpointKind,
    PlacedDesign,
    PlacementError,
    PlacementOptions,
    PlacementProvenance,
    ResolvedPlacementProposal,
    TopologicalPlacer,
)
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider
from tests.test_graph import _lbp_inverter_design


class PlacementModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = MaterialGraph.from_design(
            _lbp_inverter_design(constant_input=True),
            {LBP_PROVIDER: make_lbp_provider()},
        )
        self.provenance = PlacementProvenance(
            "test",
            1,
            PlacementOptions.from_canonical_data({"row_pitch": 2, "mode": "flat"}),
        )
        placed = TopologicalPlacer(
            providers={LBP_PROVIDER: make_lbp_provider()}
        ).place(self.graph).finalize(self.graph)
        self.placed = replace(placed, provenance=self.provenance)
        self.proposal = ResolvedPlacementProposal(
            self.placed,
        )

    def test_complete_proposal_finalizes_and_round_trips(self) -> None:
        placed = self.proposal.finalize(self.graph)

        restored = PlacedDesign.from_canonical_data(
            placed.canonical_data(),
            self.graph,
        )

        self.assertTrue(self.proposal.is_complete(self.graph))
        self.assertEqual(restored, placed)
        self.assertEqual(
            restored.provenance.options.canonical_json,
            '{"mode":"flat","row_pitch":2}',
        )

    def test_bounds_are_the_root_physical_board(self) -> None:
        placed = self.proposal.finalize(self.graph)

        self.assertEqual(placed.bounds, placed.root_container.board)
        for prefab in placed.prefabs:
            self.assertLessEqual(placed.bounds.min_x, prefab.x + prefab.bounds.min_x)
            self.assertLessEqual(placed.bounds.min_y, prefab.y + prefab.bounds.min_y)
            self.assertGreaterEqual(placed.bounds.max_x, prefab.x + prefab.bounds.max_x)
            self.assertGreaterEqual(placed.bounds.max_y, prefab.y + prefab.bounds.max_y)

    def test_decoder_normalizes_container_array_order(self) -> None:
        graph = MaterialGraph.from_design(
            _lbp_inverter_design(),
            {LBP_PROVIDER: make_lbp_provider()},
        )
        placed = TopologicalPlacer(
            providers={LBP_PROVIDER: make_lbp_provider()}
        ).place(graph).finalize(graph)
        data = placed.canonical_data()
        containers = data["containers"]
        assert isinstance(containers, list)
        container = containers[0]
        assert isinstance(container, dict)
        components = container["components"]
        prefabs = container["prefabs"]
        assert isinstance(components, list)
        assert isinstance(prefabs, list)
        components.reverse()
        prefabs.reverse()

        restored = PlacedDesign.from_canonical_data(data, graph)

        self.assertEqual(restored, placed)

    def test_finalization_rejects_missing_subject(self) -> None:
        root = self.placed.root_container
        removed = next(
            item for item in root.prefabs if isinstance(item.source, ObjectSubject)
        )
        incomplete_root = replace(
            root,
            prefabs=tuple(item for item in root.prefabs if item != removed),
            components=tuple(
                item
                for item in root.components
                if item.identifier not in removed.components
            ),
            annotations=tuple(
                item
                for item in root.annotations
                if item.identifier not in removed.annotations
            ),
        )
        incomplete = ResolvedPlacementProposal(
            replace(self.placed, containers=(incomplete_root,)),
        )

        self.assertFalse(incomplete.is_complete(self.graph))
        with self.assertRaises(PlacementError):
            incomplete.finalize(self.graph)

    def test_finalization_rejects_wrong_material_digest(self) -> None:
        proposal = ResolvedPlacementProposal(
            replace(
                self.placed,
                material_digest=MaterialDesignDigest("f" * 64),
            )
        )

        with self.assertRaises(PlacementError):
            proposal.finalize(self.graph)

    def test_decoder_rejects_unknown_keys(self) -> None:
        data = deepcopy(self.proposal.finalize(self.graph).canonical_data())
        data["unexpected"] = True

        with self.assertRaises(PlacementError):
            PlacedDesign.from_canonical_data(data, self.graph)

    def test_nonfinite_transform_is_rejected(self) -> None:
        with self.assertRaises(PlacementError):
            replace(self.placed.components[0], x=float("inf"))

    def test_duplicate_subject_placement_is_rejected(self) -> None:
        root = self.placed.root_container
        prefab = next(
            item for item in root.prefabs if isinstance(item.source, ObjectSubject)
        )
        component = next(
            item for item in root.components if item.identifier in prefab.components
        )
        duplicate_component = replace(
            component,
            identifier=f"{component.identifier}:duplicate",
        )
        duplicate = replace(
            prefab,
            identifier=f"{prefab.identifier}:duplicate",
            components=(duplicate_component.identifier,),
        )
        proposal = ResolvedPlacementProposal(
            replace(
                self.placed,
                containers=(
                    replace(
                        root,
                        components=(*root.components, duplicate_component),
                        prefabs=(*root.prefabs, duplicate),
                    ),
                ),
            )
        )

        with self.assertRaisesRegex(PlacementError, "duplicate material subjects"):
            proposal.finalize(self.graph)

    def test_component_cannot_belong_to_multiple_prefabs(self) -> None:
        root = self.placed.root_container
        first, second = root.prefabs[:2]
        shared = replace(
            second,
            components=(*second.components, first.components[0]),
        )

        with self.assertRaisesRegex(PlacementError, "multiple prefabs"):
            replace(
                root,
                prefabs=tuple(
                    shared if item == second else item for item in root.prefabs
                ),
            )

    def test_component_source_must_match_owning_prefab(self) -> None:
        root = self.placed.root_container
        prefab = root.prefabs[0]
        component = next(
            item for item in root.components if item.identifier in prefab.components
        )
        other_source = next(item.source for item in root.prefabs if item != prefab)

        with self.assertRaisesRegex(PlacementError, "source differs"):
            changed = replace(component, source=other_source)
            replace(
                root,
                components=tuple(
                    changed if item == component else item for item in root.components
                ),
            )

    def test_component_must_remain_inside_its_prefab(self) -> None:
        root = self.placed.root_container
        component = root.components[0]
        moved = replace(component, x=root.board.max_x + 1000.0)

        with self.assertRaisesRegex(PlacementError, "outside prefab"):
            replace(
                root,
                components=tuple(
                    moved if item == component else item for item in root.components
                ),
            )

    def test_connection_endpoint_must_exist_in_its_container(self) -> None:
        root = self.placed.root_container
        connection = root.connections[0]
        broken = replace(
            connection,
            source=PhysicalEndpoint(
                PhysicalEndpointKind.COMPONENT,
                "missing-component",
                0,
            ),
        )
        proposal = ResolvedPlacementProposal(
            replace(
                self.placed,
                containers=(
                    replace(
                        root,
                        connections=(broken, *root.connections[1:]),
                    ),
                ),
            )
        )

        with self.assertRaisesRegex(PlacementError, "missing component"):
            proposal.finalize(self.graph)


if __name__ == "__main__":
    unittest.main()
