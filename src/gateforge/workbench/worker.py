from __future__ import annotations

from collections.abc import Mapping
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from gateforge.workbench.protocol import (
    JsonValue,
    ProtocolError,
    WorkerCommand,
    WorkerEvent,
    WorkerEventKind,
    WorkerRequest,
    ensure_correlated,
)


class WorkerClientError(RuntimeError):
    pass


class WorkerUnavailableError(WorkerClientError):
    pass


class WorkerBusyError(WorkerClientError):
    pass


class StaleWorkerEventError(WorkerClientError):
    pass


class WorkerCommandError(WorkerClientError):
    def __init__(
        self,
        request: WorkerRequest,
        *,
        code: str,
        message: str,
        exception_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.code = code
        self.exception_type = exception_type


class WorkerClient:
    def __init__(
        self,
        *,
        context: BaseContext | None = None,
        response_timeout: float = 30.0,
    ) -> None:
        if response_timeout <= 0:
            raise ValueError("response_timeout must be positive")
        self._context = context or get_context("spawn")
        self._response_timeout = response_timeout
        self._connection: Connection | None = None
        self._process: BaseProcess | None = None
        self._session_id: str | None = None
        self._pending: WorkerRequest | None = None
        self._lock = threading.RLock()

    def __enter__(self) -> "WorkerClient":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def busy(self) -> bool:
        return self._pending is not None

    def start(self) -> str:
        with self._lock:
            if self.alive:
                if self._session_id is None:
                    raise AssertionError("Live worker has no session identifier")
                return self._session_id
            self._dispose_process()
            parent_connection, child_connection = self._context.Pipe(duplex=True)
            process = self._context.Process(
                target=_worker_main,
                args=(child_connection,),
                name="gateforge-compiler-worker",
                daemon=True,
            )
            process.start()
            child_connection.close()
            self._connection = parent_connection
            self._process = process
            self._session_id = uuid4().hex
            self._pending = None
            return self._session_id

    def restart(self) -> str:
        self.stop()
        return self.start()

    def open_session(
        self,
        source: str | Path,
        *,
        target: str = "lbp",
    ) -> WorkerEvent:
        self.restart()
        return self.request(
            WorkerCommand.OPEN_SESSION,
            {"source": str(Path(source).resolve()), "target": target},
        )

    def submit(
        self,
        command: WorkerCommand,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> WorkerRequest:
        with self._lock:
            connection = self._require_connection()
            if self._pending is not None:
                raise WorkerBusyError(
                    f"Worker is still processing {self._pending.request_id}"
                )
            if self._session_id is None:
                raise AssertionError("Started worker has no session identifier")
            request = WorkerRequest(
                request_id=uuid4().hex,
                session_id=self._session_id,
                command=command,
                payload=dict(payload or {}),
            )
            try:
                connection.send(request.to_json())
            except (BrokenPipeError, EOFError, OSError) as error:
                raise WorkerUnavailableError("Compiler worker is unavailable") from error
            self._pending = request
            return request

    def receive(
        self,
        request: WorkerRequest,
        *,
        timeout: float | None = None,
    ) -> WorkerEvent:
        with self._lock:
            if request.session_id != self._session_id:
                raise StaleWorkerEventError(
                    f"Request belongs to stale session {request.session_id!r}"
                )
            if self._pending != request:
                raise StaleWorkerEventError(
                    f"Request {request.request_id!r} is not the active request"
                )
            connection = self._require_connection()
            resolved_timeout = self._response_timeout if timeout is None else timeout
            if resolved_timeout <= 0:
                raise ValueError("timeout must be positive")
        try:
            if not connection.poll(resolved_timeout):
                raise TimeoutError(
                    f"Compiler worker did not answer {request.request_id} "
                    f"within {resolved_timeout:g} seconds"
                )
            encoded = connection.recv()
        except (EOFError, OSError) as error:
            with self._lock:
                self._pending = None
            raise WorkerUnavailableError("Compiler worker exited") from error
        with self._lock:
            if request.session_id != self._session_id or self._pending != request:
                raise StaleWorkerEventError(
                    f"Response for request {request.request_id!r} is stale"
                )
            self._pending = None
        try:
            event = WorkerEvent.from_json(encoded)
            ensure_correlated(request, event)
        except ProtocolError as error:
            raise StaleWorkerEventError(str(error)) from error
        if event.kind == WorkerEventKind.ERROR:
            raise WorkerCommandError(
                request,
                code=_event_string(event.payload, "code"),
                message=_event_string(event.payload, "message"),
                exception_type=_event_optional_string(
                    event.payload,
                    "exception_type",
                ),
            )
        return event

    def request(
        self,
        command: WorkerCommand,
        payload: Mapping[str, JsonValue] | None = None,
        *,
        timeout: float | None = None,
    ) -> WorkerEvent:
        request = self.submit(command, payload)
        return self.receive(request, timeout=timeout)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            connection = self._connection
            self._connection = None
            self._process = None
            self._session_id = None
            self._pending = None
            if connection is not None:
                connection.close()
            if process is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5.0)

    def _require_connection(self) -> Connection:
        if not self.alive or self._connection is None:
            raise WorkerUnavailableError("Compiler worker is not running")
        return self._connection

    def _dispose_process(self) -> None:
        if self._connection is not None:
            self._connection.close()
        if self._process is not None:
            self._process.join(timeout=0)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=5.0)
        self._connection = None
        self._process = None
        self._session_id = None
        self._pending = None


def _worker_main(connection: Connection) -> None:
    from gateforge.session import CompilationSession
    from gateforge.workbench.yosys_graph import YosysSchematicRenderer

    session: CompilationSession | None = None
    session_id: str | None = None
    schematic_renderer = YosysSchematicRenderer()
    try:
        while True:
            try:
                encoded = connection.recv()
            except EOFError:
                break
            request = WorkerRequest.from_json(encoded)
            should_exit = request.command == WorkerCommand.SHUTDOWN
            try:
                if request.command == WorkerCommand.OPEN_SESSION:
                    _require_payload(request, {"source", "target"})
                    source = _payload_string(request, "source")
                    target = _payload_string(request, "target")
                    session = CompilationSession(source, target=target)
                    session_id = request.session_id
                    schematic_renderer.clear()
                    result = {"snapshot": session.snapshot().canonical_data()}
                else:
                    if session is None or session_id is None:
                        raise _WorkerDispatchError(
                            "no-session",
                            "Open a compilation session before issuing commands",
                        )
                    if request.session_id != session_id:
                        raise _WorkerDispatchError(
                            "stale-session",
                            f"Worker owns session {session_id!r}, not "
                            f"{request.session_id!r}",
                        )
                    result = _dispatch_session_command(
                        request,
                        session,
                        schematic_renderer,
                    )
                event = WorkerEvent(
                    request.request_id,
                    request.session_id,
                    WorkerEventKind.RESULT,
                    result,
                )
            except Exception as error:
                code = getattr(error, "code", "command-failed")
                if not isinstance(code, str):
                    code = "command-failed"
                event = WorkerEvent(
                    request.request_id,
                    request.session_id,
                    WorkerEventKind.ERROR,
                    {
                        "code": code,
                        "message": str(error) or type(error).__name__,
                        "exception_type": type(error).__name__,
                    },
                )
            connection.send(event.to_json())
            if should_exit:
                break
    finally:
        connection.close()


def _dispatch_session_command(
    request: WorkerRequest,
    session: Any,
    schematic_renderer: Any,
) -> dict[str, JsonValue]:
    if request.command == WorkerCommand.SNAPSHOT:
        _require_payload(request, set())
        return {"snapshot": session.snapshot().canonical_data()}
    if request.command == WorkerCommand.PREPROCESS:
        _require_payload(request, set())
        return {"snapshot": session.preprocess().canonical_data()}
    if request.command == WorkerCommand.ADVANCE_STAGE:
        _require_payload(request, set())
        stage = session.advance_stage()
        return {
            "stage": stage.canonical_data(),
            "snapshot": session.snapshot().canonical_data(),
        }
    if request.command == WorkerCommand.FINISH_SEARCH:
        _require_payload(request, set())
        session.finish_search()
        return {"snapshot": session.snapshot().canonical_data()}
    if request.command == WorkerCommand.MATERIALIZE:
        _require_payload(request, set())
        session.materialize()
        return {"snapshot": session.snapshot().canonical_data()}
    if request.command == WorkerCommand.RUN_TO_MATERIAL:
        _require_payload(request, set())
        session.run_to_material()
        return {"snapshot": session.snapshot().canonical_data()}
    if request.command == WorkerCommand.PLACE:
        from gateforge.hierarchy import (
            GeneratedHierarchyMode,
            GeneratedHierarchyPolicy,
            PhysicalHierarchyMode,
            PhysicalHierarchyPolicy,
        )
        from gateforge.placement import TopologicalPlacementOptions

        _require_payload(
            request,
            {
                "column_pitch",
                "row_pitch",
                "routing_group_height",
                "routing_gap_rows",
                "physical_hierarchy",
                "hierarchy_threshold",
                "generated_hierarchy",
            },
        )
        session.place(
            options=TopologicalPlacementOptions(
                column_pitch=_payload_number(request, "column_pitch"),
                row_pitch=_payload_number(request, "row_pitch"),
                routing_group_height=_payload_number(
                    request,
                    "routing_group_height",
                ),
                routing_gap_rows=_payload_int(request, "routing_gap_rows"),
            ),
            physical_hierarchy=PhysicalHierarchyPolicy(
                PhysicalHierarchyMode(
                    _payload_string(request, "physical_hierarchy")
                ),
                _payload_optional_number(request, "hierarchy_threshold"),
            ),
            generated_hierarchy=GeneratedHierarchyPolicy(
                GeneratedHierarchyMode(
                    _payload_string(request, "generated_hierarchy")
                )
            ),
        )
        return {"snapshot": session.snapshot().canonical_data()}
    if request.command == WorkerCommand.CANDIDATE_DETAILS:
        _require_payload(request, {"identifier"})
        identifier = _payload_string(request, "identifier")
        return {"candidate": session.candidate_details(identifier).canonical_data()}
    if request.command == WorkerCommand.RENDER_SCHEMATIC:
        from gateforge.workbench.yosys_graph import SchematicRenderOptions

        _require_payload(
            request,
            {"checkpoint_digest", "module", "options"},
        )
        checkpoint_digest = _payload_string(request, "checkpoint_digest")
        module = _payload_optional_string(request, "module")
        options = _payload_object(request, "options")
        if set(options) != {"show_widths", "stretch_ports"}:
            raise _WorkerDispatchError(
                "invalid-payload",
                "render-schematic options must contain exactly "
                "'show_widths' and 'stretch_ports'",
            )
        schematic = schematic_renderer.render(
            session.checkpoint(checkpoint_digest),
            module=module,
            options=SchematicRenderOptions(
                show_widths=_object_bool(options, "show_widths"),
                stretch_ports=_object_bool(options, "stretch_ports"),
            ),
        )
        return {"schematic": schematic.canonical_data()}
    if request.command == WorkerCommand.BUILD_VISUAL_DOCUMENT:
        _require_payload(request, {"provider_options"})
        provider_options = _payload_object(request, "provider_options")
        document = session.build_visual_document(provider_options=provider_options)
        return {
            "document": document.canonical_data(),
            "snapshot": session.snapshot().canonical_data(),
        }
    if request.command == WorkerCommand.SAVE_ARTIFACT:
        _require_payload(request, {"kind", "path"})
        kind = _payload_string(request, "kind")
        path = _payload_string(request, "path")
        save = {
            "material": session.save_material,
            "state": session.save_state,
            "report": session.save_report,
            "placement": session.save_placement,
            "visualization": session.save_visual_document,
        }.get(kind)
        if save is None:
            raise _WorkerDispatchError(
                "invalid-payload",
                f"Unknown artifact kind {kind!r}",
            )
        saved_path = save(path)
        return {"kind": kind, "path": str(saved_path)}
    if request.command == WorkerCommand.EXPORT_LBP_TOOLKIT:
        from gateforge.providers.lbp.export import build_lbp_plan
        from gateforge.providers.lbp.toolkit import encode_lbp_toolkit_plan

        _require_payload(
            request,
            {
                "path",
                "title",
                "description",
                "creator",
            },
        )
        output_path = _payload_string(request, "path")
        title = _payload_optional_string(request, "title")
        description = _payload_optional_string(request, "description")
        creator = _payload_optional_string(request, "creator")
        def build(material, graph, placed, providers):
            return encode_lbp_toolkit_plan(
                build_lbp_plan(
                    material,
                    graph,
                    placed,
                    providers,
                    title=title,
                    description=description,
                    creator=creator,
                )
            )

        saved_path = session.export(output_path, build)
        return {"format": "lbp-toolkit", "path": str(saved_path)}
    if request.command == WorkerCommand.EXPORT_FACTORIO_BLUEPRINT:
        from gateforge.providers.factorio.blueprint import build_factorio_blueprint
        from gateforge.providers.factorio.routing import build_factorio_routed_design

        _require_payload(
            request,
            {
                "path",
                "label",
                "add_input_combinators",
                "input_values",
                "add_output_lamps",
            },
        )
        output_path = _payload_string(request, "path")
        label = _payload_optional_string(request, "label")
        add_input_combinators = _payload_bool(
            request, "add_input_combinators"
        )
        input_values = _payload_input_values(request, "input_values")
        add_output_lamps = _payload_bool(request, "add_output_lamps")
        if input_values and not add_input_combinators:
            raise _WorkerDispatchError(
                "invalid-payload",
                "Factorio input values require generated input combinators",
            )
        if session.target != "factorio":
            raise _WorkerDispatchError(
                "invalid-target",
                "Factorio blueprint export requires a Factorio session",
            )

        def build(material, graph, placed, providers):
            del providers
            routed = build_factorio_routed_design(
                material,
                graph,
                placed,
                input_drivers=(
                    "constant" if add_input_combinators else "none"
                ),
                input_values=input_values,
                output_lamps=add_output_lamps,
            )
            return build_factorio_blueprint(
                routed,
                label=label,
            ).canonical_data()

        saved_path = session.export(output_path, build)
        return {"format": "factorio-blueprint", "path": str(saved_path)}
    if request.command == WorkerCommand.SHUTDOWN:
        _require_payload(request, set())
        return {"stopped": True}
    raise _WorkerDispatchError(
        "unsupported-command",
        f"Worker command {request.command.value!r} is not implemented",
    )


class _WorkerDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_payload(request: WorkerRequest, keys: set[str]) -> None:
    actual = set(request.payload)
    if actual != keys:
        raise _WorkerDispatchError(
            "invalid-payload",
            f"Invalid {request.command.value} payload; "
            f"missing={sorted(keys - actual)}, unknown={sorted(actual - keys)}",
        )


def _payload_string(request: WorkerRequest, name: str) -> str:
    value = request.payload[name]
    if not isinstance(value, str) or not value:
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be a non-empty string",
        )
    return value


def _payload_optional_string(request: WorkerRequest, name: str) -> str | None:
    value = request.payload[name]
    if value is not None and (not isinstance(value, str) or not value):
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be null or "
            "a non-empty string",
        )
    return value


def _payload_object(
    request: WorkerRequest,
    name: str,
) -> dict[str, JsonValue]:
    value = request.payload[name]
    if not isinstance(value, dict):
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be an object",
        )
    return value


def _payload_optional_number(request: WorkerRequest, name: str) -> float | None:
    value = request.payload[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be null or numeric",
        )
    return float(value)


def _payload_number(request: WorkerRequest, name: str) -> float:
    value = _payload_optional_number(request, name)
    if value is None:
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be numeric",
        )
    return value


def _payload_int(request: WorkerRequest, name: str) -> int:
    value = request.payload[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be an integer",
        )
    return value


def _payload_bool(request: WorkerRequest, name: str) -> bool:
    value = request.payload[name]
    if type(value) is not bool:
        raise _WorkerDispatchError(
            "invalid-payload",
            f"{request.command.value} payload field {name!r} must be a boolean",
        )
    return value


def _payload_input_values(
    request: WorkerRequest,
    name: str,
) -> dict[str, str | int]:
    values = _payload_object(request, name)
    result: dict[str, str | int] = {}
    for port, value in values.items():
        if not port:
            raise _WorkerDispatchError(
                "invalid-payload",
                "Factorio input-value port names must not be empty",
            )
        if isinstance(value, str) and value:
            result[port] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            result[port] = value
        else:
            raise _WorkerDispatchError(
                "invalid-payload",
                f"Factorio input value for {port!r} must be an integer or "
                "non-empty string",
            )
    return result


def _object_bool(value: dict[str, JsonValue], name: str) -> bool:
    item = value[name]
    if type(item) is not bool:
        raise _WorkerDispatchError(
            "invalid-payload",
            f"render-schematic option {name!r} must be a boolean",
        )
    return item


def _event_string(payload: dict[str, JsonValue], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise StaleWorkerEventError(f"Worker error field {name!r} is invalid")
    return value


def _event_optional_string(
    payload: dict[str, JsonValue],
    name: str,
) -> str | None:
    value = payload.get(name)
    if value is not None and not isinstance(value, str):
        raise StaleWorkerEventError(f"Worker error field {name!r} is invalid")
    return value