import json
import unittest

from gateforge.workbench.protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    WorkerCommand,
    WorkerEvent,
    WorkerEventKind,
    WorkerRequest,
)


class WorkbenchProtocolTests(unittest.TestCase):
    def test_request_round_trip_is_canonical(self) -> None:
        request = WorkerRequest(
            request_id="request-1",
            session_id="session-1",
            command=WorkerCommand.OPEN_SESSION,
            payload={"source": "/tmp/example.v"},
        )

        encoded = request.to_json()

        self.assertEqual(WorkerRequest.from_json(encoded), request)
        self.assertEqual(
            list(json.loads(encoded)),
            sorted(json.loads(encoded)),
        )
        self.assertEqual(json.loads(encoded)["protocol_version"], PROTOCOL_VERSION)

    def test_event_round_trip_preserves_request_and_session(self) -> None:
        event = WorkerEvent(
            request_id="request-8",
            session_id="session-3",
            kind=WorkerEventKind.RESULT,
            payload={"snapshot": {"phase": "source-loaded"}},
        )

        self.assertEqual(WorkerEvent.from_json(event.to_json()), event)

    def test_factorio_export_command_round_trips(self) -> None:
        request = WorkerRequest(
            "request-factorio",
            "session-factorio",
            WorkerCommand.EXPORT_FACTORIO_BLUEPRINT,
            {
                "path": "/tmp/blueprint.json",
                "label": None,
                "add_input_combinators": False,
                "input_values": {},
                "add_output_lamps": True,
            },
        )

        self.assertEqual(WorkerRequest.from_json(request.to_json()), request)

    def test_rejects_unknown_fields_and_versions(self) -> None:
        data = WorkerRequest(
            request_id="request-1",
            session_id="session-1",
            command=WorkerCommand.SNAPSHOT,
            payload={},
        ).canonical_data()
        data["unexpected"] = True
        with self.assertRaisesRegex(ProtocolError, "unknown"):
            WorkerRequest.from_data(data)

        del data["unexpected"]
        data["protocol_version"] = PROTOCOL_VERSION + 1
        with self.assertRaisesRegex(ProtocolError, "Unsupported"):
            WorkerRequest.from_data(data)

    def test_rejects_empty_ids_and_non_json_payloads(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "request_id"):
            WorkerRequest("", "session-1", WorkerCommand.SNAPSHOT, {})
        with self.assertRaisesRegex(ProtocolError, "non-finite"):
            WorkerEvent(
                "request-1",
                "session-1",
                WorkerEventKind.ERROR,
                {"cost": float("inf")},
            )


if __name__ == "__main__":
    unittest.main()