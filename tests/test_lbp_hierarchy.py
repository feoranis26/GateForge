from dataclasses import replace
from pathlib import Path
import unittest

from gateforge.gateforge import compile_material
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import (
    GeneratedHierarchyMode,
    GeneratedHierarchyPolicy,
    PhysicalHierarchyMode,
    PhysicalHierarchyPolicy,
)
from gateforge.material import ImplementationPackaging
from gateforge.placement import TopologicalPlacer
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.export import build_lbp_plan
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.plan import LbpThingKind
from gateforge.providers.lbp.toolkit import encode_lbp_toolkit_plan


FIXTURE = Path(__file__).parents[1] / "scratch" / "basic_nested.v"


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


class LbpHierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.providers = {LBP_PROVIDER: make_lbp_provider()}
        _, _, cls.material = compile_material(
            str(FIXTURE),
            target_providers=cls.providers,
        )
        cls.graph = MaterialGraph.from_design(cls.material, cls.providers)
    def _build(self, policy: PhysicalHierarchyPolicy):
        placed = TopologicalPlacer(
            providers=self.providers,
            physical_hierarchy=policy,
        ).place(self.graph).finalize(self.graph)
        return build_lbp_plan(
            self.material,
            self.graph,
            placed,
            self.providers,
        )

    def test_preserve_all_retains_every_module_occurrence(self) -> None:
        plan = self._build(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.PRESERVE_ALL)
        )
        hierarchy = plan.hierarchy
        self.assertIsNotNone(hierarchy)
        assert hierarchy is not None
        containers = {item.path: item for item in hierarchy.containers}

        self.assertEqual(len(containers), 3)
        root = containers["module:test_with_inverters"]
        left = containers["module:test_with_inverters/name:inv1"]
        right = containers["module:test_with_inverters/name:inv2"]
        self.assertEqual(root.children, (left.path, right.path))
        for child in (left, right):
            self.assertEqual(len(child.input_nets), 1)
            self.assertEqual(len(child.output_nets), 1)
            self.assertEqual(len(child.gadgets), 1)

    def test_nested_routes_use_one_based_chip_inputs_and_zero_based_board_ports(self) -> None:
        plan = self._build(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.PRESERVE_ALL)
        )
        hierarchy = plan.hierarchy
        assert hierarchy is not None
        child = "module:test_with_inverters/name:inv1"
        connections = hierarchy.connections

        self.assertTrue(
            any(
                item.target.kind == LbpThingKind.MICROCHIP
                and item.target.identifier == child
                and item.target.port == 1
                for item in connections
            )
        )
        self.assertTrue(
            any(
                item.source.kind == LbpThingKind.BOARD
                and item.source.identifier == child
                and item.source.port == 0
                and item.target.kind == LbpThingKind.GADGET
                for item in connections
            )
        )
        self.assertTrue(
            any(
                item.source.kind == LbpThingKind.GADGET
                and item.target.kind == LbpThingKind.BOARD
                and item.target.identifier == child
                and item.target.port == 0
                for item in connections
            )
        )
        self.assertTrue(
            any(
                item.source.kind == LbpThingKind.MICROCHIP
                and item.source.identifier == child
                and item.source.port == 0
                for item in connections
            )
        )

    def test_preserve_all_encodes_nested_microchip_ownership_and_ports(self) -> None:
        plan = self._build(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.PRESERVE_ALL)
        )
        encoded = encode_lbp_toolkit_plan(plan)
        things = _full_things(encoded)
        chips = [
            (uid, thing)
            for uid, thing in things.items()
            if thing.get("PSwitch", {}).get("type") == "MICROCHIP"
        ]

        self.assertEqual(len(chips), 3)
        root_uid, root = next((uid, item) for uid, item in chips if item["parent"] is None)
        root_board_value = root["PMicrochip"]["circuitBoardThing"]
        root_board_uid = (
            root_board_value
            if isinstance(root_board_value, int)
            else root_board_value["UID"]
        )
        children = [(uid, item) for uid, item in chips if uid != root_uid]
        for uid, child in children:
            parent = child["parent"]
            self.assertEqual(
                parent if isinstance(parent, int) else parent["UID"],
                root_board_uid,
            )
            board_value = child["PMicrochip"]["circuitBoardThing"]
            board_uid = board_value if isinstance(board_value, int) else board_value["UID"]
            board = things[board_uid]
            self.assertEqual(len(board["PSwitch"]["outputs"]), 1)
            self.assertEqual(len(child["PSwitch"]["outputs"]), 1)
            inner_components = child["PMicrochip"]["components"]
            self.assertEqual(len(inner_components), 1)
            inner_value = inner_components[0]["thing"]
            inner_uid = inner_value if isinstance(inner_value, int) else inner_value["UID"]
            inner_parent = things[inner_uid]["parent"]
            self.assertEqual(
                inner_parent if isinstance(inner_parent, int) else inner_parent["UID"],
                board_uid,
            )

    def test_min_objects_collapses_small_child_modules(self) -> None:
        plan = self._build(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.MIN_OBJECTS, 2)
        )

        self.assertIsNone(plan.hierarchy)

    def test_min_objects_one_retains_child_modules(self) -> None:
        plan = self._build(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.MIN_OBJECTS, 1)
        )

        self.assertIsNotNone(plan.hierarchy)

    def test_generated_container_is_nested_under_flat_source_root(self) -> None:
        implementation = replace(
            self.material.implementations[0],
            packaging=ImplementationPackaging.CONTAINER,
        )
        material = replace(
            self.material,
            implementations=(implementation, *self.material.implementations[1:]),
        )
        graph = MaterialGraph.from_design(material, self.providers)
        placed = TopologicalPlacer(
            providers=self.providers,
            physical_hierarchy=PhysicalHierarchyPolicy(
                PhysicalHierarchyMode.FLAT
            ),
            generated_hierarchy=GeneratedHierarchyPolicy(
                GeneratedHierarchyMode.AUTO
            ),
        ).place(graph).finalize(graph)
        plan = build_lbp_plan(material, graph, placed, self.providers)

        hierarchy = plan.hierarchy
        self.assertIsNotNone(hierarchy)
        assert hierarchy is not None
        containers = {item.path: item for item in hierarchy.containers}
        root = containers["module:test_with_inverters"]
        generated = containers[implementation.path]
        self.assertEqual(generated.parent, root.path)
        self.assertEqual(generated.name, implementation.name)
        self.assertEqual(set(generated.gadgets), {
            next(
                gadget.identifier
                for gadget in plan.gadgets
                if gadget.identifier.value.endswith(identifier.value)
            )
            for identifier in implementation.objects
        })

    def test_placement_selected_hierarchy_exports_without_reflow(self) -> None:
        placed = TopologicalPlacer(
            providers=self.providers,
            physical_hierarchy=PhysicalHierarchyPolicy(
                PhysicalHierarchyMode.PRESERVE_ALL
            ),
        ).place(self.graph).finalize(self.graph)
        before = placed.canonical_data()

        plan = build_lbp_plan(self.material, self.graph, placed, self.providers)

        self.assertEqual(placed.canonical_data(), before)
        self.assertIsNotNone(plan.hierarchy)
        assert plan.hierarchy is not None
        self.assertEqual(
            {item.path for item in plan.hierarchy.containers},
            {item.path for item in placed.containers},
        )
        for exported in plan.hierarchy.containers:
            source = placed.container(exported.path)
            self.assertEqual((exported.x, exported.y), (source.x, source.y))
            self.assertEqual(
                (exported.board_size.x, exported.board_size.y),
                (source.board.max_x, source.board.max_y),
            )


if __name__ == "__main__":
    unittest.main()
