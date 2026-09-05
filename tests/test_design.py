import unittest
from unittest.mock import patch

from pyosys import libyosys as ys

from gateforge.design import DesignContext


EMPTY_DESIGN = {"modules": {}}


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


if __name__ == "__main__":
    unittest.main()