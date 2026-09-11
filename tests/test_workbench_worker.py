from multiprocessing import active_children
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

from gateforge.workbench.protocol import WorkerCommand
from gateforge.workbench.worker import (
    StaleWorkerEventError,
    WorkerClient,
    WorkerCommandError,
)


FIXTURE = Path(__file__).parent / "fixtures" / "single_not.v"
FACTORIO_FIXTURE = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"
PLACEMENT_PAYLOAD = {
    "column_pitch": 315.0,
    "row_pitch": 63.0,
    "routing_group_height": 300.0,
    "routing_gap_rows": 2,
    "physical_hierarchy": "flat",
    "hierarchy_threshold": None,
    "generated_hierarchy": "auto",
}


class WorkbenchWorkerTests(unittest.TestCase):
    def test_importing_client_does_not_import_pyosys(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import gateforge.workbench.worker; "
                    "assert 'pyosys' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_spawned_worker_steps_and_materializes_session(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        self.addCleanup(client.stop)

        opened = client.open_session(FIXTURE)
        snapshot = opened.payload["snapshot"]
        self.assertEqual(snapshot["phase"], "source-loaded")
        client.request(WorkerCommand.PREPROCESS)

        preprocessed = client.request(WorkerCommand.SNAPSHOT)
        checkpoint_digest = preprocessed.payload["snapshot"]["frontier"][0][
            "checkpoint_digest"
        ]
        schematic = client.request(
            WorkerCommand.RENDER_SCHEMATIC,
            {
                "checkpoint_digest": checkpoint_digest,
                "module": "test",
                "options": {
                    "show_widths": False,
                    "stretch_ports": False,
                },
            },
        )
        self.assertTrue(
            schematic.payload["schematic"]["svg"].lstrip().startswith("<?xml")
        )

        while True:
            event = client.request(WorkerCommand.SNAPSHOT)
            snapshot = event.payload["snapshot"]
            if snapshot["phase"] == "search-complete":
                break
            stage_event = client.request(WorkerCommand.ADVANCE_STAGE)
            self.assertIn("stage", stage_event.payload)

        candidate_id = snapshot["frontier"][0]["identifier"]
        details = client.request(
            WorkerCommand.CANDIDATE_DETAILS,
            {"identifier": candidate_id},
        )
        self.assertEqual(
            details.payload["candidate"]["candidate"]["identifier"],
            candidate_id,
        )
        materialized = client.request(WorkerCommand.MATERIALIZE)
        self.assertEqual(
            materialized.payload["snapshot"]["phase"],
            "materialized",
        )
        placed = client.request(
            WorkerCommand.PLACE,
            PLACEMENT_PAYLOAD,
        )
        self.assertEqual(placed.payload["snapshot"]["phase"], "placed")
        visualized = client.request(
            WorkerCommand.BUILD_VISUAL_DOCUMENT,
            {"provider_options": {}},
        )
        self.assertEqual(visualized.payload["snapshot"]["phase"], "realized")
        self.assertTrue(visualized.payload["document"]["views"])

        with TemporaryDirectory() as directory:
            output = Path(directory)
            material_path = output / "material.json"
            placement_path = output / "placement.json"
            export_path = output / "object.json"
            client.request(
                WorkerCommand.SAVE_ARTIFACT,
                {"kind": "material", "path": str(material_path)},
            )
            client.request(
                WorkerCommand.SAVE_ARTIFACT,
                {"kind": "placement", "path": str(placement_path)},
            )
            client.request(
                WorkerCommand.EXPORT_LBP_TOOLKIT,
                {
                    "path": str(export_path),
                    "title": None,
                    "description": None,
                    "creator": None,
                },
            )
            self.assertTrue(material_path.is_file())
            placement = json.loads(placement_path.read_text(encoding="utf-8"))
            self.assertEqual(
                placement["options"]["column_pitch"],
                PLACEMENT_PAYLOAD["column_pitch"],
            )
            self.assertEqual(
                placement["options"]["routing_gap_rows"],
                PLACEMENT_PAYLOAD["routing_gap_rows"],
            )
            self.assertTrue(export_path.is_file())

    def test_worker_reports_command_errors_without_losing_session(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        self.addCleanup(client.stop)
        client.open_session(FIXTURE)

        with self.assertRaises(WorkerCommandError) as caught:
            client.request(
                WorkerCommand.PLACE,
                PLACEMENT_PAYLOAD,
            )

        self.assertEqual(caught.exception.code, "command-failed")
        snapshot = client.request(WorkerCommand.SNAPSHOT)
        self.assertEqual(snapshot.payload["snapshot"]["phase"], "source-loaded")

    def test_restart_rejects_requests_from_the_previous_session(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        self.addCleanup(client.stop)
        client.open_session(FIXTURE)
        old_request = client.submit(WorkerCommand.SNAPSHOT)

        old_session = old_request.session_id
        new_session = client.restart()

        self.assertNotEqual(new_session, old_session)
        with self.assertRaises(StaleWorkerEventError):
            client.receive(old_request)

    def test_stop_reaps_the_spawned_process(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        client.start()
        pid = client.pid

        client.stop()

        self.assertFalse(client.alive)
        self.assertNotIn(pid, {process.pid for process in active_children()})

    def test_stop_interrupts_a_waiting_receiver(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        client.open_session(FIXTURE)
        request = client.submit(WorkerCommand.FINISH_SEARCH)
        finished = threading.Event()

        def receive() -> None:
            try:
                client.receive(request)
            except (StaleWorkerEventError, RuntimeError):
                pass
            finally:
                finished.set()

        receiver = threading.Thread(target=receive)
        receiver.start()
        client.stop()
        receiver.join(timeout=2.0)

        self.assertTrue(finished.is_set())
        self.assertFalse(client.alive)

    def test_open_session_transports_factorio_target_and_defaults(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        self.addCleanup(client.stop)

        opened = client.open_session(FACTORIO_FIXTURE, target="factorio")
        snapshot = opened.payload["snapshot"]

        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["target"], "factorio")
        self.assertEqual(snapshot["placement_defaults"]["column_pitch"], 6.0)
        client.request(WorkerCommand.RUN_TO_MATERIAL)
        placed = client.request(
            WorkerCommand.PLACE,
            {
                **PLACEMENT_PAYLOAD,
                "column_pitch": 6.0,
                "row_pitch": 3.0,
                "routing_group_height": 8.0,
                "routing_gap_rows": 1,
            },
        )
        self.assertEqual(placed.payload["snapshot"]["target"], "factorio")

    def test_worker_exports_factorio_blueprint_with_realization_options(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        self.addCleanup(client.stop)
        client.open_session(FACTORIO_FIXTURE, target="factorio")
        client.request(WorkerCommand.RUN_TO_MATERIAL)
        client.request(
            WorkerCommand.PLACE,
            {
                **PLACEMENT_PAYLOAD,
                "column_pitch": 6.0,
                "row_pitch": 3.0,
                "routing_group_height": 8.0,
                "routing_gap_rows": 1,
            },
        )

        with TemporaryDirectory() as directory:
            output = Path(directory) / "blueprint.json"
            options = {"factorio": {"input_drivers": "constant", "input_values": {"a": "0xffffffff", "b": 2}, "output_lamps": True}}
            realized = client.request(WorkerCommand.REALIZE, {"provider_options": options})
            identifier = realized.payload["snapshot"]["selected_realization"]
            client.request(WorkerCommand.SELECT_REALIZATION, {"identifier": identifier})
            visual = client.request(WorkerCommand.BUILD_VISUAL_DOCUMENT, {"provider_options": options})
            self.assertEqual(visual.payload["snapshot"]["selected_realization"], identifier)
            self.assertIn("factorio:finalized", {item["identifier"] for item in visual.payload["document"]["views"]})
            artifact_path = Path(directory) / "realization.json"
            client.request(WorkerCommand.SAVE_ARTIFACT, {"kind": "realization", "path": str(artifact_path)})
            event = client.request(
                WorkerCommand.EXPORT_FACTORIO_BLUEPRINT,
                {
                    "path": str(output),
                    "label": "Worker add32",
                    "add_input_combinators": True,
                    "input_values": {"a": "0xffffffff", "b": 2},
                    "add_output_lamps": True,
                },
            )

            self.assertEqual(event.payload["format"], "factorio-blueprint")
            self.assertEqual(event.payload["path"], str(output.resolve()))
            blueprint = json.loads(output.read_text(encoding="utf-8"))["blueprint"]
            self.assertEqual(blueprint["label"], "Worker add32")
            self.assertEqual(
                [item["name"] for item in blueprint["entities"]].count("small-lamp"),
                1,
            )
            artifact = json.loads(artifact_path.read_text())
            self.assertEqual(len(blueprint["entities"]), len(artifact["design"]["entities"]))
            with self.assertRaises(WorkerCommandError):
                client.request(WorkerCommand.EXPORT_FACTORIO_BLUEPRINT, {
                    "path": str(output), "label": None, "add_input_combinators": False,
                    "input_values": {}, "add_output_lamps": False,
                })
            snapshot = client.request(WorkerCommand.SNAPSHOT).payload["snapshot"]
            self.assertIsNone(snapshot["selected_realization"])
            self.assertEqual(json.loads(output.read_text())["blueprint"], blueprint)

    def test_worker_rejects_invalid_factorio_export_options(self) -> None:
        client = WorkerClient(response_timeout=10.0)
        self.addCleanup(client.stop)
        client.open_session(FACTORIO_FIXTURE, target="factorio")

        with self.assertRaises(WorkerCommandError) as caught:
            client.request(
                WorkerCommand.EXPORT_FACTORIO_BLUEPRINT,
                {
                    "path": "/tmp/unused.json",
                    "label": None,
                    "add_input_combinators": "yes",
                    "input_values": {},
                    "add_output_lamps": False,
                },
            )

        self.assertEqual(caught.exception.code, "invalid-payload")


if __name__ == "__main__":
    unittest.main()