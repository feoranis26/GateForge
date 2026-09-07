from copy import deepcopy
import unittest

from gateforge.source import (
    CellIdentifier,
    CellPortIdentifier,
    ConstantBoundarySource,
    DesignSnapshot,
    ModuleDependencyGraph,
    ModulePortIdentifier,
    SnapshotBitRef,
    SnapshotError,
)


def _design_json() -> dict[str, object]:
    return {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": {
                    "A": {"direction": "input", "bits": [2]},
                    "Y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "first": {
                        "type": "$_NOT_",
                        "parameters": {},
                        "attributes": {"gateforge_id": "first"},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [2], "Y": [3]},
                    },
                    "second": {
                        "type": "$_AND_",
                        "parameters": {},
                        "attributes": {"gateforge_id": "second"},
                        "port_directions": {
                            "A": "input",
                            "B": "input",
                            "Y": "output",
                        },
                        "connections": {"A": [3], "B": ["1"], "Y": [4]},
                    },
                    "fanout": {
                        "type": "$_NOT_",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [3], "Y": [5]},
                    },
                },
                "netnames": {},
            }
        }
    }


class DesignSnapshotTests(unittest.TestCase):
    def test_indexes_aliases_and_fanout_by_module_local_bit(self) -> None:
        snapshot = DesignSnapshot.from_json(_design_json(), revision=7)
        module = snapshot.module("top")

        endpoints = module.endpoints[SnapshotBitRef(7, "top", 3)]

        self.assertEqual(len(endpoints), 3)

    def test_dependency_graph_distinguishes_driver_from_consumers(self) -> None:
        module = DesignSnapshot.from_json(_design_json(), revision=7).module("top")
        graph = ModuleDependencyGraph.from_module(module)
        input_bit = SnapshotBitRef(7, "top", 2)
        shared_bit = SnapshotBitRef(7, "top", 3)

        self.assertEqual(
            graph.driver(input_bit),
            ModulePortIdentifier("top", "A", 0),
        )
        self.assertEqual(
            graph.driver(shared_bit),
            CellPortIdentifier(module.cells["first"].identifier, "Y", 0),
        )
        self.assertEqual(
            graph.signal_consumers(shared_bit),
            frozenset(
                {
                    CellPortIdentifier(module.cells["second"].identifier, "A", 0),
                    CellPortIdentifier(module.cells["fanout"].identifier, "A", 0),
                }
            ),
        )

    def test_cut_collapses_fanout_and_excludes_internal_bits(self) -> None:
        snapshot = DesignSnapshot.from_json(_design_json(), revision=7)
        module = snapshot.module("top")
        first = module.cells["first"].identifier
        second = module.cells["second"].identifier
        fanout = module.cells["fanout"].identifier

        first_cut = module.derive_cut(frozenset({first}))
        combined_cut = module.derive_cut(frozenset({first, second, fanout}))

        self.assertEqual(
            {cut.source for cut in first_cut},
            {SnapshotBitRef(7, "top", 2), SnapshotBitRef(7, "top", 3)},
        )
        self.assertNotIn(
            SnapshotBitRef(7, "top", 3),
            {cut.source for cut in combined_cut},
        )
        self.assertIn(
            SnapshotBitRef(7, "top", 4),
            {cut.source for cut in combined_cut},
        )
        self.assertEqual(
            len(
                [
                    cut
                    for cut in combined_cut
                    if isinstance(cut.source, ConstantBoundarySource)
                ]
            ),
            1,
        )

    def test_rejects_duplicate_sibling_anchors(self) -> None:
        design = deepcopy(_design_json())
        cells = design["modules"]["top"]["cells"]
        cells["second"]["attributes"]["gateforge_id"] = "first"

        with self.assertRaises(SnapshotError):
            DesignSnapshot.from_json(design, revision=0)

    def test_rejects_claim_from_another_module(self) -> None:
        snapshot = DesignSnapshot.from_json(_design_json(), revision=7)
        foreign = CellIdentifier("other", "first", "$_NOT_")

        with self.assertRaises(SnapshotError):
            snapshot.module("top").derive_cut(frozenset({foreign}))


if __name__ == "__main__":
    unittest.main()