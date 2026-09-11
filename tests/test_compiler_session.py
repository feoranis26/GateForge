import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gateforge.session import (
    CompilationSession,
    CompilationSessionError,
    CompilationSessionPhase,
)
from gateforge.artifacts import write_json


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"
FACTORIO_FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"


class CompilationSessionTests(unittest.TestCase):
    def test_factorio_place_owns_final_layout_without_baseline_pass(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE, target="factorio")
        session.run_to_material()
        with patch("gateforge.session.TopologicalPlacer", side_effect=AssertionError("Discarded baseline placement")):
            placement = session.place()
            self.assertIsNotNone(session.selected_realization)
            selected = session.selected_realization
            document = session.build_visual_document()
            self.assertEqual({view.identifier for view in document.views}, {"factorio:finalized"})
            self.assertIs(session.selected_realization, selected)
            self.assertIs(session.placement, placement)
            with TemporaryDirectory() as directory:
                saved = session.save_placement(Path(directory) / "placement.json")
                self.assertEqual(json.loads(saved.read_text()), placement.canonical_data())

    def test_factorio_placed_entities_do_not_overlap(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE, target="factorio")
        session.run_to_material()
        session.place()
        session.realize()
        scenes = session.build_visual_document().views[0].scenes
        terminals = [item for scene in scenes for item in scene.elements if item.collision_enabled]
        self.assertGreaterEqual(len(terminals), 4)
        for index, first in enumerate(terminals):
            for second in terminals[index + 1:]:
                self.assertTrue(
                    abs(first.transform.x - second.transform.x) >= (first.descriptor.bounds.width + second.descriptor.bounds.width) / 2
                    or abs(first.transform.y - second.transform.y) >= (first.descriptor.bounds.height + second.descriptor.bounds.height) / 2
                )

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
        session.realize()
        initial = session.build_visual_document()

        session.realize(provider_options={"factorio": {"output_lamps": True}})
        rebuilt = session.build_visual_document(
            provider_options={"factorio": {"output_lamps": True}}
        )

        self.assertEqual(session.phase, CompilationSessionPhase.REALIZED)
        self.assertNotEqual(initial.canonical_data(), rebuilt.canonical_data())

    def test_session_owns_realized_artifact_and_blocks_stale_exports(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE, target="factorio")
        session.run_to_material()
        with self.assertRaises(CompilationSessionError):
            session.build_visual_document()
        options = {"factorio": {"input_drivers": "constant", "input_values": {"a": 41, "b": 1}}}
        session.place(provider_options=options)
        result = session.realization_result
        assert result is not None
        self.assertIs(session.selected_realization, result.winner)
        self.assertIsNone(session.visual_document)
        snapshot = session.snapshot().canonical_data()
        self.assertEqual(snapshot["selected_realization"], snapshot["realization_winner"])
        document = session.build_visual_document(provider_options=options)
        self.assertEqual({view.identifier for view in document.views}, {"factorio:finalized"})
        self.assertIs(session.selected_realization, result.winner)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "blueprint.json"
            session.export_realization(output, provider_options=options)
            expected = session.backend.realization_presenter.export(result.winner.outcome.artifact)
            self.assertEqual(json.loads(output.read_text()), expected)
            session.save_realization(Path(directory) / "finalized.json")
            report = session.save_report(Path(directory) / "report.json")
            self.assertEqual(json.loads(report.read_text())["winner"], snapshot["realization_winner"])
            with self.assertRaisesRegex(CompilationSessionError, "options changed"):
                session.export_realization(output, provider_options={})
            self.assertIsNone(session.selected_realization)
            self.assertIsNone(session.visual_document)
            self.assertEqual(json.loads(output.read_text()), expected)

    def test_selecting_nonwinner_changes_export_without_changing_search_winner(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE.parent / "chained_add32.v", target="factorio")
        session.run_to_material()
        session.place()
        result = session.realize()
        self.assertEqual(len(result.candidates), 2)
        snapshot = session.snapshot()
        self.assertEqual({item["details"]["combinators"] for item in snapshot.realizations}, {3, 4})
        winner = snapshot.realization_winner
        alternative = next(item for item in snapshot.realizations if item["identifier"] != winner)
        session.select_realization(alternative["identifier"])
        self.assertEqual(session.snapshot().realization_winner, winner)
        self.assertEqual(session.snapshot().selected_realization, alternative["identifier"])
        self.assertIsNone(session.visual_document)
        with TemporaryDirectory() as directory:
            output = session.export_realization(Path(directory) / "blueprint.json")
            entities = json.loads(output.read_text())["blueprint"]["entities"]
            self.assertEqual(sum(item["name"] == "arithmetic-combinator" for item in entities), alternative["details"]["combinators"])
            report = json.loads(session.save_report(Path(directory) / "report.json").read_text())
            self.assertEqual(report["winner"], winner)
            self.assertEqual(report["selected_realization"], alternative["identifier"])
        with self.assertRaisesRegex(CompilationSessionError, "Unknown realization"):
            session.select_realization("missing")
        self.assertEqual(session.snapshot().selected_realization, alternative["identifier"])
        session.place()
        self.assertIsNotNone(session.selected_realization)
        self.assertIsNone(session.visual_document)

    def test_invalid_realization_options_clear_previous_selection(self) -> None:
        session = CompilationSession(FACTORIO_FIXTURE, target="factorio")
        session.run_to_material()
        session.place()
        session.realize()
        with self.assertRaisesRegex(ValueError, "Invalid Factorio"):
            session.build_visual_document(provider_options={"factorio": {"input_drivers": []}})
        self.assertIsNone(session.selected_realization)
        self.assertIsNone(session.visual_document)
        self.assertEqual(session.phase, CompilationSessionPhase.PLACED)

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
        self.assertIs(placement, session.backend.realization_presenter.placement(session.selected_realization.outcome.artifact))
        self.assertEqual(dict(session.snapshot().placement.details)["entities"], len(placement.entities))

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