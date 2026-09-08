from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from typing import TypeAlias


PROTOCOL_VERSION = 1

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class ProtocolError(ValueError):
    pass


class WorkerCommand(StrEnum):
    OPEN_SESSION = "open-session"
    SNAPSHOT = "snapshot"
    PREPROCESS = "preprocess"
    ADVANCE_STAGE = "advance-stage"
    FINISH_SEARCH = "finish-search"
    MATERIALIZE = "materialize"
    RUN_TO_MATERIAL = "run-to-material"
    PLACE = "place"
    CANDIDATE_DETAILS = "candidate-details"
    RENDER_SCHEMATIC = "render-schematic"
    BUILD_VISUAL_DOCUMENT = "build-visual-document"
    SAVE_ARTIFACT = "save-artifact"
    EXPORT_LBP_TOOLKIT = "export-lbp-toolkit"
    SHUTDOWN = "shutdown"


class WorkerEventKind(StrEnum):
    RESULT = "result"
    ERROR = "error"


def ensure_correlated(request: WorkerRequest, event: WorkerEvent) -> None:
    if event.request_id != request.request_id:
        raise ProtocolError(
            f"Event request_id {event.request_id!r} does not match "
            f"request {request.request_id!r}"
        )
    if event.session_id != request.session_id:
        raise ProtocolError(
            f"Event session_id {event.session_id!r} does not match "
            f"session {request.session_id!r}"
        )


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    request_id: str
    session_id: str
    command: WorkerCommand
    payload: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        object.__setattr__(self, "payload", _copy_json_object(self.payload, "payload"))

    def canonical_data(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "request",
            "request_id": self.request_id,
            "session_id": self.session_id,
            "command": self.command.value,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return _dump_json(self.canonical_data())

    @classmethod
    def from_json(cls, encoded: str) -> "WorkerRequest":
        return cls.from_data(_load_json_object(encoded))

    @classmethod
    def from_data(cls, data: dict[str, JsonValue]) -> "WorkerRequest":
        _require_exact_keys(
            data,
            {
                "protocol_version",
                "message_type",
                "request_id",
                "session_id",
                "command",
                "payload",
            },
        )
        _require_protocol_header(data, "request")
        request_id = _require_string(data["request_id"], "request_id")
        session_id = _require_string(data["session_id"], "session_id")
        command_name = _require_string(data["command"], "command")
        try:
            command = WorkerCommand(command_name)
        except ValueError as error:
            raise ProtocolError(f"Unknown worker command {command_name!r}") from error
        payload = _require_object(data["payload"], "payload")
        return cls(request_id, session_id, command, payload)


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    request_id: str
    session_id: str
    kind: WorkerEventKind
    payload: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        object.__setattr__(self, "payload", _copy_json_object(self.payload, "payload"))

    def canonical_data(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "event",
            "request_id": self.request_id,
            "session_id": self.session_id,
            "event": self.kind.value,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return _dump_json(self.canonical_data())

    @classmethod
    def from_json(cls, encoded: str) -> "WorkerEvent":
        return cls.from_data(_load_json_object(encoded))

    @classmethod
    def from_data(cls, data: dict[str, JsonValue]) -> "WorkerEvent":
        _require_exact_keys(
            data,
            {
                "protocol_version",
                "message_type",
                "request_id",
                "session_id",
                "event",
                "payload",
            },
        )
        _require_protocol_header(data, "event")
        request_id = _require_string(data["request_id"], "request_id")
        session_id = _require_string(data["session_id"], "session_id")
        event_name = _require_string(data["event"], "event")
        try:
            kind = WorkerEventKind(event_name)
        except ValueError as error:
            raise ProtocolError(f"Unknown worker event {event_name!r}") from error
        payload = _require_object(data["payload"], "payload")
        return cls(request_id, session_id, kind, payload)


def _dump_json(value: JsonValue) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ProtocolError(f"Value is not strict JSON: {error}") from error


def _load_json_object(encoded: str) -> dict[str, JsonValue]:
    if not isinstance(encoded, str):
        raise ProtocolError("Protocol message must be a JSON string")
    try:
        value = json.loads(encoded, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProtocolError(f"Invalid protocol JSON: {error}") from error
    return _require_object(value, "message")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite number {value!r} is not valid protocol JSON")


def _copy_json_object(
    value: object,
    name: str,
) -> dict[str, JsonValue]:
    validated = _require_object(value, name)
    return _require_object(_load_json_object(_dump_json(validated)), name)


def _require_protocol_header(
    data: dict[str, JsonValue],
    message_type: str,
) -> None:
    version = data["protocol_version"]
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"Unsupported protocol version {version!r}; expected {PROTOCOL_VERSION}"
        )
    actual_type = _require_string(data["message_type"], "message_type")
    if actual_type != message_type:
        raise ProtocolError(
            f"Expected message_type {message_type!r}, got {actual_type!r}"
        )


def _require_exact_keys(
    data: dict[str, JsonValue],
    expected: set[str],
) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ProtocolError(
            f"Invalid protocol fields; missing={missing}, unknown={unknown}"
        )


def _require_identifier(value: object, name: str) -> None:
    text = _require_string(value, name)
    if not text.strip():
        raise ProtocolError(f"{name} must not be empty")


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")
    return value


def _require_object(value: object, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise ProtocolError(f"{name} must be a JSON object with string keys")
    _validate_json_value(value, name)
    return value


def _validate_json_value(value: object, path: str) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ProtocolError(f"{path} contains a non-JSON value {type(value).__name__}")