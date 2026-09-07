from dataclasses import replace
import unittest
from unittest.mock import patch

from gateforge.mapping import (
    Mapper,
    MappingDisposition,
    ProposalConflictGraph,
    ProposalError,
    ProposalSetEnumerator,
)
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

    def test_collection_preserves_overlapping_proposals_for_search(self) -> None:
        snapshot = _snapshot()
        proposal = LBPCombinatorialLowLevelGateMapper().map(snapshot)[0]
        alternative = replace(proposal, mapper="alternative")
        mapper = Mapper([])

        graph = ProposalConflictGraph.from_proposals(
            mapper.combine([proposal, alternative], snapshot.revision)
        )
        self.assertEqual(len(graph.proposals), 1)

        graph = ProposalConflictGraph.from_proposals([proposal, alternative])
        self.assertEqual(len(graph.proposals), 2)
        self.assertTrue(graph.conflicts_with(0, 1))
        self.assertEqual(
            {len(items) for items in ProposalSetEnumerator(graph).enumerate()},
            {0, 1},
        )

    def test_required_proposal_excludes_conflicting_speculative_proposal(self) -> None:
        snapshot = _snapshot()
        proposal = LBPCombinatorialLowLevelGateMapper().map(snapshot)[0]
        required = replace(
            proposal,
            mapper="required",
            disposition=MappingDisposition.REQUIRED,
        )
        graph = ProposalConflictGraph.from_proposals([proposal, required])

        alternatives = ProposalSetEnumerator(graph).enumerate()

        self.assertEqual(alternatives, ((required,),))

    def test_conflicting_required_proposals_are_rejected(self) -> None:
        snapshot = _snapshot()
        proposal = LBPCombinatorialLowLevelGateMapper().map(snapshot)[0]
        left = replace(
            proposal,
            mapper="required.left",
            disposition=MappingDisposition.REQUIRED,
        )
        right = replace(
            proposal,
            mapper="required.right",
            disposition=MappingDisposition.REQUIRED,
        )

        with self.assertRaisesRegex(ProposalError, "Required.*conflict"):
            ProposalSetEnumerator(
                ProposalConflictGraph.from_proposals([left, right])
            ).enumerate()

    def test_output_inversion_targets_yosys_output_inverted_cells(self) -> None:
        cell_types = LBPCombinatorialLowLevelGateMapper.tcell_types

        self.assertIn("$_NAND_", cell_types)
        self.assertIn("$_NOR_", cell_types)
        self.assertIn("$_BUF_", cell_types)
        self.assertIn("$_ANDNOT_", cell_types)
        self.assertIn("$_ORNOT_", cell_types)

    def test_ornot_maps_to_or_with_boundary_inverter(self) -> None:
        proposal = LBPCombinatorialLowLevelGateMapper().map(
            _snapshot("$_ORNOT_")
        )[0]

        self.assertEqual(
            {item.role for item in proposal.prefab.objects},
            {"invert_b", "result"},
        )
        validate_lbp_prefab(proposal.prefab, LBPTypeRegistry())


if __name__ == "__main__":
    unittest.main()