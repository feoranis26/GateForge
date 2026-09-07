import unittest

from gateforge.providers.lbp.associative import LBPAssociativeConeMapper
from gateforge.providers.lbp.objects import LBPTypeRegistry, validate_lbp_prefab
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPNotGateType,
    LBPOrGateType,
    LBPXorGateType,
    decode_lbp_object_type,
)
from gateforge.source import DesignSnapshot, SnapshotBitRef


def _binary_tree(
    root_type: str,
    child_type: str,
    *,
    fanout: bool = False,
) -> DesignSnapshot:
    cells: dict[str, object] = {
        "left": {
            "type": child_type,
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "B": "input", "Y": "output"},
            "connections": {"A": [2], "B": [3], "Y": [5]},
        },
        "root": {
            "type": root_type,
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "B": "input", "Y": "output"},
            "connections": {"A": [5], "B": [4], "Y": [6]},
        },
    }
    ports: dict[str, object] = {
        "a": {"direction": "input", "bits": [2]},
        "c": {"direction": "input", "bits": [3]},
        "b": {"direction": "input", "bits": [4]},
        "y": {"direction": "output", "bits": [6]},
    }
    if fanout:
        cells["fanout"] = {
            "type": "$_NOT_",
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [5], "Y": [7]},
        }
        ports["z"] = {"direction": "output", "bits": [7]}
    return DesignSnapshot.from_json(
        {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": ports,
                    "cells": cells,
                    "netnames": {},
                }
            }
        },
        revision=4,
    )


def _andnot_tree(*, fanout: bool = False) -> DesignSnapshot:
    return _binary_tree("$_ANDNOT_", "$_AND_", fanout=fanout)


class AssociativeMappingTests(unittest.TestCase):
    def test_andnot_tree_maps_to_and3_with_boundary_inverter(self) -> None:
        proposal = LBPAssociativeConeMapper().map(_andnot_tree())[0]

        self.assertEqual(
            {identifier.name for identifier in proposal.ids},
            {"left", "root"},
        )
        decoded = {
            item.role: decode_lbp_object_type(item.type)
            for item in proposal.prefab.objects
        }
        self.assertIsInstance(decoded["result"], LBPAndGateType)
        self.assertEqual(decoded["result"].width, 3)
        self.assertEqual(
            sum(isinstance(item, LBPNotGateType) for item in decoded.values()),
            1,
        )
        self.assertEqual(
            {binding.source for binding in proposal.boundary},
            {
                SnapshotBitRef(4, "top", 2),
                SnapshotBitRef(4, "top", 3),
                SnapshotBitRef(4, "top", 4),
                SnapshotBitRef(4, "top", 6),
            },
        )
        validate_lbp_prefab(proposal.prefab, LBPTypeRegistry())

    def test_shared_child_is_not_absorbed(self) -> None:
        self.assertEqual(LBPAssociativeConeMapper().map(_andnot_tree(fanout=True)), ())

    def test_ornot_tree_preserves_negative_b_literal(self) -> None:
        proposal = LBPAssociativeConeMapper().map(
            _binary_tree("$_ORNOT_", "$_OR_")
        )[0]
        decoded = {
            item.role: decode_lbp_object_type(item.type)
            for item in proposal.prefab.objects
        }

        self.assertIsInstance(decoded["result"], LBPOrGateType)
        self.assertEqual(decoded["result"].width, 3)
        self.assertEqual(
            sum(isinstance(item, LBPNotGateType) for item in decoded.values()),
            1,
        )

    def test_xnor_root_inverts_wide_xor_output(self) -> None:
        proposal = LBPAssociativeConeMapper().map(
            _binary_tree("$_XNOR_", "$_XOR_")
        )[0]
        result = next(
            decode_lbp_object_type(item.type)
            for item in proposal.prefab.objects
            if item.role == "result"
        )

        self.assertIsInstance(result, LBPXorGateType)
        self.assertEqual(result.width, 3)
        self.assertTrue(result.invert_output)


if __name__ == "__main__":
    unittest.main()