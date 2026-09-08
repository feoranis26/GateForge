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


if __name__ == "__main__":
    unittest.main()