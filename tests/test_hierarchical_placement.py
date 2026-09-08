from copy import deepcopy
from collections import Counter
from dataclasses import replace
from pathlib import Path
import unittest

from gateforge.gateforge import compile_material
from gateforge.graph import ConstantSubject, MaterialGraph, ModulePortSubject, ObjectSubject
from gateforge.hierarchy import GeneratedHierarchyPolicy, PhysicalHierarchyPolicy
from gateforge.placement import (
    ContainerKind,
    PlacedDesign,
    PlacementError,
    TopologicalPlacer,
)
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.physical import elaborate_lbp_physical_design
from gateforge.providers.lbp.export import build_lbp_plan
from gateforge.target import PortDirection
from gateforge.placement.hierarchy import (
    BoundaryPortSubject,
    ChildContainerSubject,
    build_placement_hierarchy,
)
from tests.test_graph import _lbp_inverter_design


def _placed_design(graph: MaterialGraph) -> PlacedDesign:
    providers = {LBP_PROVIDER: make_lbp_provider()}
    return TopologicalPlacer(providers=providers).place(graph).finalize(graph)


class HierarchicalPlacementModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = MaterialGraph.from_design(
            _lbp_inverter_design(constant_input=True),
            {LBP_PROVIDER: make_lbp_provider()},
        )

    def test_hierarchy_native_schema_v1_round_trips(self) -> None:
        placed = _placed_design(self.graph)

        restored = PlacedDesign.from_canonical_data(
            placed.canonical_data(),
            self.graph,
        )

        self.assertEqual(restored, placed)
        self.assertEqual(restored.bounds, placed.root_container.board)
        self.assertEqual(restored.canonical_data()["schema_version"], 1)

    def test_container_order_is_canonical(self) -> None:
        placed = _placed_design(self.graph)
        data = deepcopy(placed.canonical_data())
        data["containers"].reverse()

        restored = PlacedDesign.from_canonical_data(data, self.graph)

        self.assertEqual(restored, placed)

    def test_obsolete_flat_schema_requires_regeneration(self) -> None:
        data = {
            "schema_version": 1,
            "material_digest": self.graph.design.get_digest().value,
            "placer": "topological",
            "placer_version": 1,
            "options": {},
            "objects": [],
            "module_ports": [],
            "constants": [],
        }

        with self.assertRaisesRegex(PlacementError, "regenerate"):
            PlacedDesign.from_canonical_data(data, self.graph)


class PlacementHierarchyTopologyTests(unittest.TestCase):
    def test_generated_register_bank_is_one_root_child(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        _, _, material = compile_material(
            str(Path(__file__).parents[1] / "scratch" / "test.v"),
            target_providers=providers,
        )
        graph = MaterialGraph.from_design(material, providers)

        topology = build_placement_hierarchy(
            graph,
            providers,
            PhysicalHierarchyPolicy(),
            GeneratedHierarchyPolicy(),
        )

        root = topology.container(topology.root)
        bank = next(item for item in topology.containers if item.parent == root.path)
        implementation = next(
            item for item in material.implementations if item.name == "register-bank[9]"
        )
        self.assertEqual(bank.name, "register-bank[9]")
        self.assertEqual(
            {
                subject.object
                for subject in bank.subjects
                if isinstance(subject, ObjectSubject)
            },
            set(implementation.objects),
        )
        self.assertIn(ChildContainerSubject(bank.path), root.subjects)
        self.assertTrue(
            any(isinstance(item, BoundaryPortSubject) for item in bank.subjects)
        )

    def test_topological_placer_emits_generated_register_container(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        _, _, material = compile_material(
            str(Path(__file__).parents[1] / "scratch" / "test.v"),
            target_providers=providers,
        )
        graph = MaterialGraph.from_design(material, providers)

        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)

        root = placed.root_container
        bank = next(item for item in placed.containers if item.parent == root.path)
        implementation = next(
            item for item in material.implementations if item.name == "register-bank[9]"
        )
        self.assertEqual(root.children, (bank.path,))
        self.assertEqual(
            {
                item.source.object
                for item in bank.prefabs
                if isinstance(item.source, ObjectSubject)
            },
            set(implementation.objects),
        )
        self.assertFalse(
            {
                item.source.object
                for item in root.prefabs
                if isinstance(item.source, ObjectSubject)
            }
            & set(implementation.objects)
        )
        self.assertGreater(bank.board.width, 0.0)
        self.assertGreater(bank.board.height, 0.0)
        self.assertGreater(bank.facade.width, 0.0)
        self.assertGreater(bank.facade.height, 0.0)
        root_columns = Counter(
            (
                item.x
                for item in root.prefabs
                if isinstance(item.source, ObjectSubject)
            )
        )
        root_columns[bank.x] += 1
        self.assertGreaterEqual(len(root_columns), 6)
        self.assertLess(max(root_columns.values()), sum(root_columns.values()) / 2)


class LbpPhysicalElaborationTests(unittest.TestCase):
    def test_physical_inventory_covers_every_topology_subject(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(_lbp_inverter_design(), providers)
        physical = elaborate_lbp_physical_design(graph, providers)
        root = physical.container(physical.root)
        removed = root.prefabs[0]
        incomplete = replace(
            root,
            prefabs=tuple(item for item in root.prefabs if item != removed),
            components=tuple(
                item
                for item in root.components
                if item.identifier
                not in {member.identifier for member in removed.components}
            ),
            annotations=tuple(
                item
                for item in root.annotations
                if item.identifier
                not in {member.identifier for member in removed.annotations}
            ),
        )

        with self.assertRaisesRegex(PlacementError, "subject coverage"):
            replace(physical, containers=(incomplete,))

    def test_io_notes_are_rigid_prefab_members_before_placement(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(_lbp_inverter_design(), providers)

        physical = elaborate_lbp_physical_design(graph, providers)

        root = physical.container(physical.root)
        self.assertEqual(len(root.components), 3)
        self.assertEqual(len(root.annotations), 2)
        self.assertEqual(len(root.prefabs), 3)
        input_prefab = next(
            item
            for item in root.prefabs
            if isinstance(item.subject, ModulePortSubject)
            and item.subject.direction == PortDirection.INPUT
        )
        self.assertEqual(len(input_prefab.components), 1)
        self.assertEqual(len(input_prefab.annotations), 1)
        self.assertEqual(input_prefab.annotations[0].x, -105.0)
        self.assertLessEqual(input_prefab.bounds.min_x, -105.0)
        self.assertEqual(len(root.connections), len(graph.dependencies))

    def test_constant_battery_exists_before_placement(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(
            _lbp_inverter_design(constant_input=True),
            providers,
        )

        physical = elaborate_lbp_physical_design(graph, providers)

        root = physical.container(physical.root)
        battery = next(
            item
            for item in root.components
            if isinstance(item.source, ConstantSubject)
        )
        self.assertEqual(battery.kind, "ALWAYS_ON")
        self.assertTrue(battery.payload.canonical_data()["manual_activation"])

    def test_placement_resolves_io_note_members_and_lbp_board_bounds(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(_lbp_inverter_design(), providers)

        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)

        root = placed.root_container
        self.assertEqual({item.text for item in root.annotations}, {"a", "y"})
        input_port = next(
            item
            for item in root.prefabs
            if isinstance(item.source, ModulePortSubject)
            and item.source.direction == PortDirection.INPUT
        )
        input_note = next(item for item in root.annotations if item.text == "a")
        self.assertEqual(input_note.x, input_port.x - 105.0)
        self.assertEqual(input_note.y, input_port.y)
        self.assertEqual(root.board.min_x, -472.5)
        self.assertEqual(root.board.max_x, 472.5)
        self.assertEqual(root.board.min_y, -262.5)
        self.assertEqual(root.board.max_y, 262.5)

    def test_lbp_export_is_pure_serialization_of_physical_placement(self) -> None:
        providers = {LBP_PROVIDER: make_lbp_provider()}
        design = _lbp_inverter_design()
        graph = MaterialGraph.from_design(design, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        before = deepcopy(placed.canonical_data())

        plan = build_lbp_plan(design, graph, placed, providers)

        self.assertEqual(placed.canonical_data(), before)
        self.assertEqual(
            {item.identifier.value for item in plan.gadgets},
            {item.identifier for item in placed.components},
        )
        self.assertEqual(
            {item.identifier.value for item in plan.notes},
            {item.identifier for item in placed.annotations},
        )


if __name__ == "__main__":
    unittest.main()