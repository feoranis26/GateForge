from collections import defaultdict
from collections.abc import Hashable, Mapping
import hashlib
import json

from gateforge.design import DesignContext, rtlil_id
from gateforge.source import ConstantBit, ModulePortIdentifier, SnapshotBitRef
from gateforge.state import ClaimDefinitionId, CompilationIntermediateState
from gateforge.target import (
    NetworkTypeIdentifier,
    ObjectPortRef,
    OccurrenceId,
    PhysicalAttachment,
    PhysicalConstantRef,
    PhysicalDesign,
    PhysicalModulePortRef,
    PhysicalNet,
    PhysicalObject,
    PhysicalObjectId,
    PhysicalObjectPortRef,
    PortDirection,
    PrefabPortRef,
    TargetProvider,
)


class MaterializationError(ValueError):
    pass


class _DisjointSet:
    def __init__(self) -> None:
        self._parents: dict[Hashable, Hashable] = {}

    def add(self, item: Hashable) -> None:
        self._parents.setdefault(item, item)

    def find(self, item: Hashable) -> Hashable:
        parent = self._parents[item]
        if parent != item:
            parent = self.find(parent)
            self._parents[item] = parent
        return parent

    def union(self, first: Hashable, second: Hashable) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self._parents[second_root] = first_root


def _digest(data: object) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _occurrence_id(
    module: str,
    cell_name: str | None,
    hierarchy: str,
    claim: ClaimDefinitionId,
) -> OccurrenceId:
    return OccurrenceId(
        _digest(
            {
                "schema_version": 1,
                "module": module,
                "hierarchy": hierarchy,
                "claim": claim.value,
                **({"cell": cell_name} if cell_name is not None else {}),
            }
        )
    )


def _physical_object_id(
    occurrence: OccurrenceId,
    prefab: str,
    role: str,
) -> PhysicalObjectId:
    return PhysicalObjectId(
        _digest(
            {
                "schema_version": 1,
                "occurrence": occurrence.value,
                "prefab": prefab,
                "role": role,
            }
        )
    )


def _attachment_data(attachment: PhysicalAttachment) -> dict[str, object]:
    if isinstance(attachment, PhysicalObjectPortRef):
        return {
            "kind": "object",
            "object": attachment.object.value,
            "port": attachment.port,
            "bit": attachment.bit,
        }
    if isinstance(attachment, PhysicalModulePortRef):
        return {
            "kind": "module_port",
            "module": attachment.module,
            "port": attachment.port,
            "bit": attachment.bit,
            "direction": attachment.direction.value,
        }
    return {"kind": "constant", "value": attachment.value}


def _physical_net_id(
    network_type: NetworkTypeIdentifier,
    attachments: frozenset[PhysicalAttachment],
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "network_type": {
                "provider": network_type.provider,
                "name": network_type.name,
                "version": network_type.version,
            },
            "attachments": sorted(
                (_attachment_data(item) for item in attachments),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        }
    )


def materialize(
    context: DesignContext,
    state: CompilationIntermediateState,
    providers: Mapping[str, TargetProvider],
) -> PhysicalDesign:
    if state.revision != context.revision:
        raise MaterializationError(
            f"State revision {state.revision} does not match design revision "
            f"{context.revision}"
        )
    snapshot = context.snapshot()
    disjoint = _DisjointSet()
    objects: dict[PhysicalObjectId, PhysicalObject] = {}
    local_types: dict[tuple[OccurrenceId, str], NetworkTypeIdentifier] = {}
    local_attachments: dict[
        tuple[OccurrenceId, str], set[PhysicalAttachment]
    ] = defaultdict(set)
    external_to_local: dict[
        tuple[OccurrenceId, PrefabPortRef], tuple[OccurrenceId, str]
    ] = {}
    signal_to_local: dict[
        SnapshotBitRef, list[tuple[OccurrenceId, str]]
    ] = defaultdict(list)
    local_constants: dict[
        tuple[OccurrenceId, str], set[PhysicalConstantRef]
    ] = defaultdict(set)

    for module in snapshot.modules.values():
        if "blackbox" in module.attributes:
            continue
        for cell in module.cells.values():
            claim_value = cell.attributes.get("gateforge_claim_definition")
            prefab_value = cell.attributes.get("gateforge_prefab")
            if claim_value is None or prefab_value is None:
                if cell.identifier.expected_type != "$scopeinfo":
                    raise MaterializationError(
                        f"Unmapped cell {module.name}.{cell.identifier.name} "
                        f"of type {cell.identifier.expected_type} remains after flatten"
                    )
                continue

            claim_id = ClaimDefinitionId(claim_value)
            try:
                claim = state.claims[claim_id]
            except KeyError as error:
                raise MaterializationError(
                    f"Claim occurrence {module.name}.{cell.identifier.name} refers "
                    f"to unknown definition {claim_value}"
                ) from error
            if claim.prefab.value != prefab_value:
                raise MaterializationError(
                    f"Claim occurrence {module.name}.{cell.identifier.name} has "
                    f"prefab {prefab_value}, expected {claim.prefab.value}"
                )
            try:
                prefab = state.prefabs[claim.prefab]
            except KeyError as error:
                raise MaterializationError(
                    f"Claim {claim_value} refers to missing prefab {prefab_value}"
                ) from error

            stamped_path = cell.attributes.get("gateforge_occurrence_path")
            if stamped_path is not None:
                try:
                    path = json.loads(stamped_path)
                except json.JSONDecodeError as error:
                    raise MaterializationError(
                        f"Invalid occurrence path on {module.name}."
                        f"{cell.identifier.name}"
                    ) from error
                if not isinstance(path, list) or not all(
                    isinstance(item, str) for item in path
                ):
                    raise MaterializationError(
                        f"Invalid occurrence path on {module.name}."
                        f"{cell.identifier.name}"
                    )
                hierarchy = "/".join(path)
                identity_cell_name = None
            else:
                hierarchy = cell.attributes.get("hdlname", cell.identifier.name)
                identity_cell_name = cell.identifier.name
            occurrence = _occurrence_id(
                module.name,
                identity_cell_name,
                hierarchy,
                claim_id,
            )
            role_to_object: dict[str, PhysicalObjectId] = {}
            for prefab_object in prefab.objects:
                identifier = _physical_object_id(
                    occurrence,
                    claim.prefab.value,
                    prefab_object.role,
                )
                role_to_object[prefab_object.role] = identifier
                objects[identifier] = PhysicalObject(
                    identifier=identifier,
                    occurrence=occurrence,
                    prefab=claim.prefab,
                    role=prefab_object.role,
                    type=prefab_object.type,
                    hierarchy=hierarchy,
                )

            for prefab_net in prefab.nets:
                local_key = (occurrence, prefab_net.role)
                disjoint.add(local_key)
                local_types[local_key] = prefab_net.type
                for attachment in prefab_net.attachments:
                    if isinstance(attachment, ObjectPortRef):
                        local_attachments[local_key].add(
                            PhysicalObjectPortRef(
                                object=role_to_object[attachment.object_role],
                                port=attachment.port,
                                bit=attachment.bit,
                            )
                        )
                    elif isinstance(attachment, PrefabPortRef):
                        external_key = (occurrence, attachment)
                        if external_key in external_to_local:
                            raise MaterializationError(
                                f"Prefab port {attachment} is attached to multiple nets"
                            )
                        external_to_local[external_key] = local_key

            for port_binding in claim.ports:
                try:
                    cell_port = cell.ports[port_binding.formal]
                except KeyError as error:
                    raise MaterializationError(
                        f"Claim occurrence {cell.identifier.name} has no formal port "
                        f"{port_binding.formal}"
                    ) from error
                if len(cell_port.bits) != 1:
                    raise MaterializationError(
                        f"Claim formal {cell.identifier.name}.{port_binding.formal} "
                        f"has width {len(cell_port.bits)}"
                    )
                local_key = external_to_local[(occurrence, port_binding.target)]
                source = cell_port.bits[0]
                if isinstance(source, SnapshotBitRef):
                    signal_to_local[source].append(local_key)
                elif isinstance(source, ConstantBit):
                    local_constants[local_key].add(
                        PhysicalConstantRef(source.value.value)
                    )

    for local_keys in signal_to_local.values():
        first = local_keys[0]
        for other in local_keys[1:]:
            disjoint.union(first, other)

    grouped_keys: dict[Hashable, list[tuple[OccurrenceId, str]]] = defaultdict(list)
    for local_key in local_types:
        grouped_keys[disjoint.find(local_key)].append(local_key)

    signal_roots = {
        signal: disjoint.find(keys[0])
        for signal, keys in signal_to_local.items()
    }
    root_module_ports: dict[Hashable, set[PhysicalModulePortRef]] = defaultdict(set)
    for signal, root in signal_roots.items():
        module = snapshot.module(signal.module)
        for endpoint in module.endpoints.get(signal, frozenset()):
            if isinstance(endpoint, ModulePortIdentifier):
                port = module.ports[endpoint.name]
                root_module_ports[root].add(
                    PhysicalModulePortRef(
                        module=endpoint.module,
                        port=endpoint.name,
                        bit=endpoint.bit,
                        direction=port.direction,
                    )
                )

    physical_nets: list[PhysicalNet] = []
    for root, local_keys in grouped_keys.items():
        network_types = {local_types[key] for key in local_keys}
        if len(network_types) != 1:
            raise MaterializationError(
                f"Physical network joins incompatible types {network_types!r}"
            )
        network_type = next(iter(network_types))
        attachments: set[PhysicalAttachment] = set(root_module_ports[root])
        for local_key in local_keys:
            attachments.update(local_attachments[local_key])
            attachments.update(local_constants[local_key])
        frozen_attachments = frozenset(attachments)
        physical_nets.append(
            PhysicalNet(
                identifier=_physical_net_id(network_type, frozen_attachments),
                type=network_type,
                attachments=frozen_attachments,
            )
        )

    physical = PhysicalDesign(
        objects=tuple(sorted(objects.values(), key=lambda item: item.identifier.value)),
        nets=tuple(sorted(physical_nets, key=lambda item: item.identifier)),
    )
    objects_by_id = {item.identifier: item for item in physical.objects}
    for net in physical.nets:
        try:
            provider = providers[net.type.provider]
        except KeyError as error:
            raise MaterializationError(
                f"No provider is registered for physical network {net.type}"
            ) from error
        try:
            provider.validate_physical_net(net, objects_by_id)
        except ValueError as error:
            raise MaterializationError(str(error)) from error
    return physical


def flatten_and_materialize(
    context: DesignContext,
    state: CompilationIntermediateState,
    providers: Mapping[str, TargetProvider],
) -> tuple[CompilationIntermediateState, PhysicalDesign]:
    context.run_pass("uniquify")
    state = state.with_revision(context.revision)
    snapshot = context.snapshot()
    top_modules = [
        module.name for module in snapshot.modules.values() if "top" in module.attributes
    ]
    if len(top_modules) != 1:
        raise MaterializationError(
            f"Expected exactly one top module, found {top_modules!r}"
        )

    stamped = 0

    def stamp_module(
        module_name: str,
        path: tuple[str, ...],
        active_modules: frozenset[str],
    ) -> None:
        nonlocal stamped
        if module_name in active_modules:
            raise MaterializationError(
                f"Recursive module hierarchy encountered at {module_name!r}"
            )
        module = snapshot.module(module_name)
        live_module = context.module(module_name)
        next_active = active_modules | {module_name}
        for cell in module.cells.values():
            if "gateforge_claim_definition" in cell.attributes:
                live_cell = live_module.cell(rtlil_id(cell.identifier.name))
                if live_cell is None:
                    raise MaterializationError(
                        f"Claim cell {module_name}.{cell.identifier.name} disappeared"
                    )
                live_cell.set_string_attribute(
                    rtlil_id("gateforge_occurrence_path"),
                    json.dumps(path, separators=(",", ":")),
                )
                stamped += 1
                continue
            child_module = snapshot.modules.get(cell.identifier.expected_type)
            if child_module is None or "blackbox" in child_module.attributes:
                continue
            token = (
                f"anchor:{cell.anchor}"
                if cell.anchor is not None
                else f"name:{cell.identifier.name}"
            )
            stamp_module(
                child_module.name,
                path + (token,),
                next_active,
            )

    top_module = top_modules[0]
    stamp_module(top_module, (f"module:{top_module}",), frozenset())
    if stamped:
        context.mark_mutated()
        state = state.with_revision(context.revision)
    context.run_pass("flatten -noscopeinfo")
    state = state.with_revision(context.revision)
    return state, materialize(context, state, providers)