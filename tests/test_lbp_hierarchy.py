from pathlib import Path
import unittest

from gateforge.gateforge import compile_material
from gateforge.graph import MaterialGraph
from gateforge.hierarchy import PhysicalHierarchyMode, PhysicalHierarchyPolicy
from gateforge.placement import TopologicalPlacer
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.hierarchy import containerize_lbp_plan
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.plan import LbpThingKind
from gateforge.providers.lbp.realize import realize_lbp_plan
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
        cls.placed = TopologicalPlacer(providers=cls.providers).place(
            cls.graph
        ).finalize(cls.graph)
        cls.flat_plan = realize_lbp_plan(cls.material, cls.graph, cls.placed)

    def _containerize(self, policy: PhysicalHierarchyPolicy):
        return containerize_lbp_plan(
            self.flat_plan,
            self.material,
            self.graph,
            self.placed,
            policy,
            self.providers,
        )

    def test_preserve_all_retains_every_module_occurrence(self) -> None:
        plan = self._containerize(
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
        plan = self._containerize(
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
        plan = self._containerize(
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
        plan = self._containerize(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.MIN_OBJECTS, 2)
        )

        self.assertIsNone(plan.hierarchy)

    def test_min_objects_one_retains_child_modules(self) -> None:
        plan = self._containerize(
            PhysicalHierarchyPolicy(PhysicalHierarchyMode.MIN_OBJECTS, 1)
        )

        self.assertIsNotNone(plan.hierarchy)


if __name__ == "__main__":
    unittest.main()
