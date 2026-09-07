from pathlib import Path
import unittest
from unittest.mock import patch

from pyosys import libyosys as ys

from gateforge.design import DesignContext, export_json


EMPTY_DESIGN = {"modules": {}}
FIXTURE = Path(__file__).parent / "fixtures" / "chained_and_gates.v"


class DesignContextTests(unittest.TestCase):
    def test_snapshot_is_exported_once_per_revision(self) -> None:
        context = DesignContext(ys.Design())

        with patch("gateforge.design.export_json", return_value=EMPTY_DESIGN) as export:
            first = context.snapshot()
            self.assertIs(context.snapshot(), first)
            self.assertEqual(export.call_count, 1)

            context.mark_mutated()
            second = context.snapshot()
            self.assertIsNot(second, first)
            self.assertIs(context.snapshot(), second)
            self.assertEqual(export.call_count, 2)

    def test_checkpoint_round_trips_design_revision_and_scratchpad(self) -> None:
        context = DesignContext(ys.Design())
        context.run_pass(f"read_verilog {FIXTURE}")
        context.run_pass("hierarchy -check -auto-top")
        context.design.scratchpad_set_string("gateforge.test", "preserved")

        checkpoint = context.checkpoint()
        restored = checkpoint.restore()

        self.assertEqual(restored.revision, context.revision)
        self.assertEqual(export_json(restored.design), export_json(context.design))
        self.assertEqual(
            restored.design.scratchpad_get_string("gateforge.test"),
            "preserved",
        )
        self.assertTrue(restored.design.full_selection())
        self.assertEqual(restored.checkpoint(), checkpoint)
        self.assertEqual(restored.checkpoint().get_digest(), checkpoint.get_digest())

    def test_fork_mutates_independently(self) -> None:
        context = DesignContext(ys.Design())
        context.run_pass(f"read_verilog {FIXTURE}")
        original_revision = context.revision
        original_json = export_json(context.design)

        fork = context.fork()
        fork.run_pass("techmap")

        self.assertEqual(context.revision, original_revision)
        self.assertEqual(export_json(context.design), original_json)
        self.assertEqual(fork.revision, original_revision + 1)
        self.assertNotEqual(export_json(fork.design), original_json)


if __name__ == "__main__":
    unittest.main()