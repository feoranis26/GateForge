from dataclasses import replace
import unittest
from unittest.mock import patch

from gateforge.mapping import Mapper, ProposalError
from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper
from gateforge.providers.lbp.objects import (
    LBPTypeRegistry,
    validate_lbp_prefab,
)
from gateforge.providers.lbp.types import LBPAndGateType
from gateforge.source import DesignSnapshot, SnapshotBitRef
from gateforge.target import ObjectPortRef


def _snapshot(cell_type: str = "$_ANDNOT_") -> DesignSnapshot:
    design = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": {
                    "A": {"direction": "input", "bits": [2]},
                    "B": {"direction": "input", "bits": [3]},
                    "Y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "andnot": {
                        "type": cell_type,
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "A": "input",
                            "B": "input",
                            "Y": "output",
                        },
                        "connections": {"A": [2], "B": [3], "Y": [4]},
                    }
                },
                "netnames": {},
            }
        }
    }
    return DesignSnapshot.from_json(design, revision=3)


class MappingTests(unittest.TestCase):
    def test_proposal_generation_does_not_resolve_object_schema(self) -> None:
        mapper = LBPCombinatorialLowLevelGateMapper()

        with patch.object(
            LBPAndGateType,
            "get_schema",
            side_effect=AssertionError("schema was generated eagerly"),
        ):
            proposals = mapper.map(_snapshot("$_AND_"))

        self.assertEqual(len(proposals), 1)

    def test_andnot_maps_to_deterministic_two_object_prefab(self) -> None:
        mapper = LBPCombinatorialLowLevelGateMapper()

        first = mapper.map(_snapshot())[0]
        second = mapper.map(_snapshot())[0]

        self.assertEqual(first.prefab.get_id(), second.prefab.get_id())
        self.assertEqual(
            {item.role for item in first.prefab.objects},
            {"invert_b", "result"},
        )
        self.assertEqual(
            {binding.source for binding in first.boundary},
            {
                SnapshotBitRef(3, "top", 2),
                SnapshotBitRef(3, "top", 3),
                SnapshotBitRef(3, "top", 4),
            },
        )
        self.assertEqual(
            {
                (attachment.object_role, attachment.port)
                for net in first.prefab.nets
                for attachment in net.attachments
                if isinstance(attachment, ObjectPortRef)
            },
            {
                ("invert_b", "IN_0"),
                ("invert_b", "OUT"),
                ("result", "IN_0"),
                ("result", "IN_1"),
                ("result", "OUT"),
            },
        )
        validate_lbp_prefab(first.prefab, LBPTypeRegistry())

    def test_arbitration_rejects_stale_proposals(self) -> None:
        snapshot = _snapshot()
        proposal = LBPCombinatorialLowLevelGateMapper().map(snapshot)[0]

        with self.assertRaises(ProposalError):
            Mapper([]).combine([replace(proposal, revision=2)], snapshot.revision)

    def test_output_inversion_targets_yosys_output_inverted_cells(self) -> None:
        cell_types = LBPCombinatorialLowLevelGateMapper.tcell_types

        self.assertIn("$_NAND_", cell_types)
        self.assertIn("$_NOR_", cell_types)
        self.assertIn("$_BUF_", cell_types)
        self.assertIn("$_ANDNOT_", cell_types)


if __name__ == "__main__":
    unittest.main()