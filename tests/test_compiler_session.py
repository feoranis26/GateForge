import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gateforge.session import (
    CompilationSession,
    CompilationSessionError,
    CompilationSessionPhase,
)
from gateforge.artifacts import write_json


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"
FACTORIO_FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"


class CompilationSessionTests(unittest.TestCase):
    def test_steps_through_search_and_indexes_candidate_details(self) -> None:
        session = CompilationSession(FIXTURE)

        loaded = session.snapshot()
        self.assertEqual(loaded.phase, CompilationSessionPhase.SOURCE_LOADED)
        self.assertEqual(loaded.frontier, ())
        preprocessed = session.preprocess()
        self.assertEqual(preprocessed.phase, CompilationSessionPhase.PREPROCESSED)
        self.assertEqual(len(preprocessed.frontier), 1)

        while session.phase != CompilationSessionPhase.SEARCH_COMPLETE:
            stage = session.advance_stage()
            self.assertGreaterEqual(stage.generated_candidates, 1)
            self.assertTrue(stage.post_pass_checkpoints)
            for _, digest in stage.post_pass_checkpoints:
                self.assertEqual(session.checkpoint(digest).get_digest(), digest)

        snapshot = session.snapshot()
        self.assertEqual(snapshot.stage_index, snapshot.stage_count)
        self.assertEqual(len(snapshot.stages), snapshot.stage_count)
        self.assertIsNone(snapshot.next_stage)
        self.assertTrue(snapshot.frontier)
        details = session.candidate_details(snapshot.frontier[0].identifier)
        self.assertEqual(details.candidate, snapshot.frontier[0])
        self.assertEqual(len(details.decisions), snapshot.stage_count)
        json.dumps(snapshot.canonical_data(), allow_nan=False)

    def test_runs_material_placement_and_provider_visualization_in_memory(self) -> None:
        session = CompilationSession(FIXTURE)

        material = session.run_to_material()
        self.assertEqual(session.phase, CompilationSessionPhase.MATERIALIZED)
        material_snapshot = session.snapshot()
        self.assertEqual(material_snapshot.material.digest, material.get_digest().value)
        self.assertIsNotNone(material_snapshot.winner)
        self.assertTrue(material_snapshot.terminal_scores)
        placement = session.place()
        self.assertEqual(session.phase, CompilationSessionPhase.PLACED)
        self.assertEqual(placement.material_digest, material.get_digest())
        document = session.build_visual_document()

        self.assertEqual(session.phase, CompilationSessionPhase.REALIZED)
        self.assertEqual(
            {view.identifier for view in document.views},
            {"material", "lbp:realized"},
        )
        snapshot = session.snapshot()
        self.assertIsNotNone(snapshot.placement)
        self.assertIsNotNone(snapshot.visualization)
        json.dumps(snapshot.canonical_data(), allow_nan=False)

    def test_rebuilds_provider_visualization_after_realization(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE, target="factorio")
        session.run_to_material()
        session.place()
        initial = session.build_visual_document()

        rebuilt = session.build_visual_document(
            provider_options={"factorio": {"output_lamps": True}}
        )

        self.assertEqual(session.phase, CompilationSessionPhase.REALIZED)
        self.assertNotEqual(initial.canonical_data(), rebuilt.canonical_data())

    def test_factorio_session_uses_backend_mapping_and_placement_defaults(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE, target="factorio")

        loaded = session.snapshot()
        loaded_data = loaded.canonical_data()
        self.assertEqual(session.target, "factorio")
        self.assertEqual(loaded.target, "factorio")
        self.assertEqual(loaded_data["schema_version"], 2)
        self.assertEqual(
            loaded_data["placement_defaults"],
            {
                "column_pitch": 6.0,
                "row_pitch": 3.0,
                "routing_group_height": 8.0,
                "routing_gap_rows": 1,
            },
        )

        material = session.run_to_material()
        placement = session.place()

        self.assertEqual(len(material.objects), 1)
        self.assertEqual(placement.target, "factorio")
        options = placement.provenance.options.canonical_data()
        self.assertEqual(options["column_pitch"], 6.0)
        self.assertEqual(options["row_pitch"], 3.0)
        self.assertEqual(options["routing_group_height"], 8.0)
        self.assertEqual(options["routing_gap_rows"], 1)

    def test_rejects_actions_outside_the_current_phase(self) -> None:
        session = CompilationSession(FIXTURE)

        with self.assertRaisesRegex(CompilationSessionError, "source-loaded"):
            session.advance_stage()
        with self.assertRaisesRegex(CompilationSessionError, "source-loaded"):
            session.place()
        with self.assertRaisesRegex(CompilationSessionError, "Unknown"):
            session.candidate_details("missing")

    def test_artifacts_remain_in_memory_until_explicitly_saved(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory)
            session = CompilationSession(FIXTURE)
            session.run_to_material()
            session.place()
            session.build_visual_document()

            self.assertEqual(tuple(output.iterdir()), ())
            paths = (
                session.save_material(output / "nested" / "material.json"),
                session.save_state(output / "state.json"),
                session.save_report(output / "report.json"),
                session.save_placement(output / "placement.json"),
                session.save_visual_document(output / "visualization.json"),
            )

            self.assertTrue(all(path.is_file() for path in paths))
            self.assertEqual(
                json.loads(paths[0].read_text(encoding="utf-8")),
                session.material.canonical_data(),
            )
            cli_equivalent = write_json(
                output / "cli-material.json",
                session.material.canonical_data(),
            )
            self.assertEqual(paths[0].read_bytes(), cli_equivalent.read_bytes())


if __name__ == "__main__":
    unittest.main()