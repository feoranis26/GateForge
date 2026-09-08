import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PySide6 workbench extra is not installed") from error

from gateforge.workbench.qt_models import (
    CANDIDATE_ID_ROLE,
    ClaimsTableModel,
    CompilationCandidateTreeModel,
    ProposalTableModel,
)


class QtModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_candidate_model_includes_every_branch_status(self) -> None:
        model = CompilationCandidateTreeModel()

        model.set_snapshot(_snapshot())

        stage = model.item(0)
        self.assertEqual(stage.text(), "source")
        self.assertEqual(stage.rowCount(), 3)
        self.assertEqual(
            {stage.child(row, 1).text() for row in range(stage.rowCount())},
            {"frontier", "pruned", "deduplicated"},
        )
        candidate = stage.child(0)
        self.assertEqual(candidate.data(CANDIDATE_ID_ROLE), "candidate-a")
        self.assertEqual(
            model.candidate_id(candidate.index().siblingAtColumn(3)),
            "candidate-a",
        )

    def test_proposals_can_be_filtered_by_source_candidate(self) -> None:
        model = ProposalTableModel()

        model.set_snapshot(_snapshot(), candidate_id="parent-a")

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.item(0, 0).text(), "mapper.a")
        self.assertEqual(model.item(0, 5).text(), "1")

    def test_claims_model_displays_selected_candidate_details(self) -> None:
        model = ClaimsTableModel()

        model.set_candidate_details(
            {
                "claims": [
                    {
                        "module": "top",
                        "instance": "gate",
                        "mapper": "mapper.a",
                        "provider": "provider.a",
                        "prefab": "a" * 64,
                        "rule": "rule.a",
                        "implementation_name": "NOT",
                        "packaging": "inline",
                    }
                ]
            }
        )

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.item(0, 0).text(), "top.gate")
        self.assertEqual(model.item(0, 3).text(), "a" * 12)
        self.assertEqual(model.item(0, 4).text(), "NOT")


def _snapshot() -> dict[str, object]:
    candidate = {
        "identifier": "candidate-a",
        "checkpoint_digest": "a" * 64,
        "stage_index": 1,
        "revision": 2,
        "lower_bound": 1.0,
        "expected_cost": 2.0,
        "claim_count": 1,
        "decision_count": 1,
    }
    return {
        "stages": [
            {
                "name": "source",
                "transitions": [
                    {
                        "source_candidate": "parent-a",
                        "candidate": candidate,
                        "status": status,
                    }
                    for status in ("frontier", "pruned", "deduplicated")
                ],
                "proposals": [
                    {
                        "source_candidate": "parent-a",
                        "fingerprint": "proposal-a",
                        "mapper": "mapper.a",
                        "provider": "provider.a",
                        "rule": "rule.a",
                        "disposition": "exclusive",
                        "cost": {"expected": 2.0},
                        "cells": [["top", "gate", "$not"]],
                    },
                    {
                        "source_candidate": "parent-b",
                        "fingerprint": "proposal-b",
                        "mapper": "mapper.b",
                        "provider": "provider.b",
                        "rule": "rule.b",
                        "disposition": "exclusive",
                        "cost": {"expected": 3.0},
                        "cells": [],
                    },
                ],
            }
        ],
        "frontier": [candidate],
    }


if __name__ == "__main__":
    unittest.main()