from copy import deepcopy
import unittest

from gateforge.graph import ConstantSubject, MaterialGraph, ModulePortSubject, ObjectSubject
from gateforge.material import MaterialDesignDigest
from gateforge.placement import (
    ConstantPlacement,
    FlatPlacementProposal,
    ModulePortPlacement,
    ObjectPlacement,
    PlacedDesign,
    PlacementError,
    PlacementOptions,
    PlacementProvenance,
    ResolvedPlacements,
)
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider
from tests.test_graph import _lbp_inverter_design


def _resolved(graph: MaterialGraph) -> ResolvedPlacements:
    objects = []
    module_ports = []
    constants = []
    for index, subject in enumerate(graph.subjects):
        x = float(index - 1)
        if isinstance(subject, ObjectSubject):
            objects.append(ObjectPlacement(subject.object, x, 0.0))
        elif isinstance(subject, ModulePortSubject):
            module_ports.append(
                ModulePortPlacement(
                    subject.module,
                    subject.port,
                    subject.bit,
                    subject.direction,
                    x,
                    1.0,
                )
            )
        elif isinstance(subject, ConstantSubject):
            constants.append(ConstantPlacement(subject.net, subject.value, x, -1.0))
    return ResolvedPlacements(tuple(objects), tuple(module_ports), tuple(constants))


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
        self.proposal = FlatPlacementProposal(
            self.graph.design.get_digest(),
            self.provenance,
            _resolved(self.graph),
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

    def test_bounds_cover_objects_and_virtual_terminals(self) -> None:
        placed = self.proposal.finalize(self.graph)

        self.assertEqual(placed.bounds.min_x, -1.0)
        self.assertEqual(placed.bounds.max_x, 1.0)
        self.assertEqual(placed.bounds.min_y, -1.0)
        self.assertEqual(placed.bounds.max_y, 1.0)

    def test_decoder_normalizes_placement_array_order(self) -> None:
        graph = MaterialGraph.from_design(
            _lbp_inverter_design(),
            {LBP_PROVIDER: make_lbp_provider()},
        )
        proposal = FlatPlacementProposal(
            graph.design.get_digest(),
            self.provenance,
            _resolved(graph),
        )
        placed = proposal.finalize(graph)
        data = placed.canonical_data()
        data["module_ports"].reverse()

        restored = PlacedDesign.from_canonical_data(data, graph)

        self.assertEqual(restored, placed)

    def test_finalization_rejects_missing_subject(self) -> None:
        resolved = _resolved(self.graph)
        incomplete = FlatPlacementProposal(
            self.graph.design.get_digest(),
            self.provenance,
            ResolvedPlacements((), resolved.module_ports, resolved.constants),
        )

        self.assertFalse(incomplete.is_complete(self.graph))
        with self.assertRaises(PlacementError):
            incomplete.finalize(self.graph)

    def test_finalization_rejects_wrong_material_digest(self) -> None:
        proposal = FlatPlacementProposal(
            MaterialDesignDigest("f" * 64),
            self.provenance,
            _resolved(self.graph),
        )

        with self.assertRaises(PlacementError):
            proposal.finalize(self.graph)

    def test_decoder_rejects_unknown_keys(self) -> None:
        data = deepcopy(self.proposal.finalize(self.graph).canonical_data())
        data["unexpected"] = True

        with self.assertRaises(PlacementError):
            PlacedDesign.from_canonical_data(data, self.graph)

    def test_nonfinite_transform_is_rejected(self) -> None:
        object_id = self.graph.design.objects[0].identifier

        with self.assertRaises(PlacementError):
            ObjectPlacement(object_id, float("inf"), 0.0)

    def test_duplicate_subject_placement_is_rejected(self) -> None:
        placement = ObjectPlacement(self.graph.design.objects[0].identifier, 0.0, 0.0)

        with self.assertRaises(PlacementError):
            ResolvedPlacements((placement, placement), (), ())


if __name__ == "__main__":
    unittest.main()
