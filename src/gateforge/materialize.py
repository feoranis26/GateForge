from collections import defaultdict
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
import json

from gateforge.design import DesignContext
from gateforge.material import (
    ImplementationPackaging,
    MaterialImplementationOccurrence,
    MaterialImplementationPort,
    MaterialAttachment,
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialModuleOccurrence,
    MaterialModulePort,
    MaterialNetId,
    MaterialNet,
    MaterialObject,
    MaterialObjectId,
    MaterialObjectPortRef,
    OccurrenceId,
    make_material_net_id,
    make_material_object_id,
    make_occurrence_id,
)
from gateforge.provider import TargetProvider
from gateforge.source import ConstantBit, ModulePortIdentifier, SnapshotBitRef
from gateforge.state import ClaimDefinitionId, CompilationIntermediateState
from gateforge.target import (
    NetworkTypeIdentifier,
    ObjectPortRef,
    PortDirection,
    PrefabId,
    PrefabPortRef,
)


class MaterializationError(ValueError):
    pass


type _SignalOccurrence = tuple[str, SnapshotBitRef]


@dataclass(slots=True)
class _ModuleDraft:
    path: str
    module: str
    implementation: str
    parent: str | None
    instance: str | None
    anchor: str | None
    ports: list[tuple[str, int, PortDirection, _SignalOccurrence | None]]
    objects: list[MaterialObjectId]
    children: list[str]


@dataclass(slots=True)
class _ImplementationDraft:
    path: str
    occurrence: OccurrenceId
    owner_module: str
    prefab: PrefabId
    provider: str
    mapper: str
    rule: str
    name: str
    packaging: ImplementationPackaging
    ports: list[
        tuple[str, int, PortDirection, tuple[OccurrenceId, str]]
    ]
    objects: list[MaterialObjectId]


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


def materialize(
    context: DesignContext,
    state: CompilationIntermediateState,
    providers: Mapping[str, TargetProvider],
) -> MaterialDesign:
    if state.revision != context.revision:
        raise MaterializationError(
            f"State revision {state.revision} does not match design revision "
            f"{context.revision}"
        )
    snapshot = context.snapshot()
    disjoint = _DisjointSet()
    objects: dict[MaterialObjectId, MaterialObject] = {}
    local_types: dict[tuple[OccurrenceId, str], NetworkTypeIdentifier] = {}
    local_attachments: dict[
        tuple[OccurrenceId, str], set[MaterialAttachment]
    ] = defaultdict(set)
    external_to_local: dict[
        tuple[OccurrenceId, PrefabPortRef], tuple[OccurrenceId, str]
    ] = {}
    signal_to_local: dict[
        SnapshotBitRef, list[tuple[OccurrenceId, str]]
    ] = defaultdict(list)
    local_constants: dict[
        tuple[OccurrenceId, str], set[MaterialConstantRef]
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
            occurrence = make_occurrence_id(
                module.name,
                identity_cell_name,
                hierarchy,
                claim_id.value,
            )
            role_to_object: dict[str, MaterialObjectId] = {}
            for prefab_object in prefab.objects:
                identifier = make_material_object_id(
                    occurrence,
                    claim.prefab,
                    prefab_object.role,
                )
                role_to_object[prefab_object.role] = identifier
                objects[identifier] = MaterialObject(
                    identifier=identifier,
                    occurrence=occurrence,
                    prefab=claim.prefab,
                    role=prefab_object.role,
                    type=prefab_object.type,
                    hierarchy=hierarchy,
                    configuration=prefab_object.configuration,
                )

            for prefab_net in prefab.nets:
                local_key = (occurrence, prefab_net.role)
                disjoint.add(local_key)
                local_types[local_key] = prefab_net.type
                for attachment in prefab_net.attachments:
                    if isinstance(attachment, ObjectPortRef):
                        local_attachments[local_key].add(
                            MaterialObjectPortRef(
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
                        MaterialConstantRef(source.value.value)
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
    root_module_ports: dict[Hashable, set[MaterialModulePortRef]] = defaultdict(set)
    for signal, root in signal_roots.items():
        module = snapshot.module(signal.module)
        for endpoint in module.endpoints.get(signal, frozenset()):
            if isinstance(endpoint, ModulePortIdentifier):
                port = module.ports[endpoint.name]
                root_module_ports[root].add(
                    MaterialModulePortRef(
                        module=endpoint.module,
                        port=endpoint.name,
                        bit=endpoint.bit,
                        direction=port.direction,
                    )
                )

    material_nets: list[MaterialNet] = []
    for root, local_keys in grouped_keys.items():
        network_types = {local_types[key] for key in local_keys}
        if len(network_types) != 1:
            raise MaterializationError(
                f"Material network joins incompatible types {network_types!r}"
            )
        network_type = next(iter(network_types))
        attachments: set[MaterialAttachment] = set(root_module_ports[root])
        for local_key in local_keys:
            attachments.update(local_attachments[local_key])
            attachments.update(local_constants[local_key])
        frozen_attachments = frozenset(attachments)
        material_nets.append(
            MaterialNet(
                identifier=make_material_net_id(network_type, frozen_attachments),
                type=network_type,
                attachments=frozen_attachments,
            )
        )

    material = MaterialDesign(
        objects=tuple(sorted(objects.values(), key=lambda item: item.identifier.value)),
        nets=tuple(sorted(material_nets, key=lambda item: item.identifier.value)),
    )
    objects_by_id = {item.identifier: item for item in material.objects}
    for net in material.nets:
        try:
            provider = providers[net.type.provider]
        except KeyError as error:
            raise MaterializationError(
                f"No provider is registered for material network {net.type}"
            ) from error
        try:
            provider.validate_material_net(net, objects_by_id)
        except ValueError as error:
            raise MaterializationError(str(error)) from error
    return material


def materialize_hierarchy(
    context: DesignContext,
    state: CompilationIntermediateState,
    providers: Mapping[str, TargetProvider],
    top_module: str,
) -> MaterialDesign:
    if state.revision != context.revision:
        raise MaterializationError(
            f"State revision {state.revision} does not match design revision "
            f"{context.revision}"
        )
    snapshot = context.snapshot()
    signal_sets = _DisjointSet()
    local_sets = _DisjointSet()
    objects: dict[MaterialObjectId, MaterialObject] = {}
    local_types: dict[tuple[OccurrenceId, str], NetworkTypeIdentifier] = {}
    local_attachments: dict[
        tuple[OccurrenceId, str], set[MaterialAttachment]
    ] = defaultdict(set)
    signal_to_local: dict[
        _SignalOccurrence, list[tuple[OccurrenceId, str]]
    ] = defaultdict(list)
    local_constants: dict[
        tuple[OccurrenceId, str], set[MaterialConstantRef]
    ] = defaultdict(set)
    signal_constants: dict[_SignalOccurrence, set[MaterialConstantRef]] = defaultdict(set)
    drafts: list[_ModuleDraft] = []
    implementation_drafts: list[_ImplementationDraft] = []

    def signal(path: str, bit: SnapshotBitRef) -> _SignalOccurrence:
        result = (path, bit)
        signal_sets.add(result)
        return result

    def visit_module(
        implementation: str,
        path_parts: tuple[str, ...],
        parent: str | None,
        instance: str | None,
        anchor: str | None,
        active_modules: frozenset[str],
    ) -> None:
        if implementation in active_modules:
            raise MaterializationError(
                f"Recursive module hierarchy encountered at {implementation!r}"
            )
        module = snapshot.module(implementation)
        path = "/".join(path_parts)
        draft = _ModuleDraft(
            path=path,
            module=module.attributes.get("hdlname", module.name),
            implementation=module.name,
            parent=parent,
            instance=instance,
            anchor=anchor,
            ports=[],
            objects=[],
            children=[],
        )
        drafts.append(draft)
        for port in module.ports.values():
            for bit_index, bit in enumerate(port.bits):
                reference = signal(path, bit) if isinstance(bit, SnapshotBitRef) else None
                draft.ports.append(
                    (port.identifier, bit_index, port.direction, reference)
                )

        next_active = active_modules | {implementation}
        for cell in module.cells.values():
            claim_value = cell.attributes.get("gateforge_claim_definition")
            prefab_value = cell.attributes.get("gateforge_prefab")
            if claim_value is not None and prefab_value is not None:
                claim_id = ClaimDefinitionId(claim_value)
                try:
                    claim = state.claims[claim_id]
                    prefab = state.prefabs[claim.prefab]
                except KeyError as error:
                    raise MaterializationError(
                        f"Claim occurrence {module.name}.{cell.identifier.name} "
                        "refers to missing state"
                    ) from error
                if claim.prefab.value != prefab_value:
                    raise MaterializationError(
                        f"Claim occurrence {module.name}.{cell.identifier.name} has "
                        f"prefab {prefab_value}, expected {claim.prefab.value}"
                    )
                occurrence = make_occurrence_id(
                    top_module,
                    None,
                    path,
                    claim_id.value,
                )
                role_to_object: dict[str, MaterialObjectId] = {}
                for prefab_object in prefab.objects:
                    identifier = make_material_object_id(
                        occurrence,
                        claim.prefab,
                        prefab_object.role,
                    )
                    role_to_object[prefab_object.role] = identifier
                    draft.objects.append(identifier)
                    objects[identifier] = MaterialObject(
                        identifier=identifier,
                        occurrence=occurrence,
                        prefab=claim.prefab,
                        role=prefab_object.role,
                        type=prefab_object.type,
                        hierarchy=path,
                        configuration=prefab_object.configuration,
                    )

                external_to_local: dict[
                    PrefabPortRef, tuple[OccurrenceId, str]
                ] = {}
                for prefab_net in prefab.nets:
                    local_key = (occurrence, prefab_net.role)
                    local_sets.add(local_key)
                    local_types[local_key] = prefab_net.type
                    for attachment in prefab_net.attachments:
                        if isinstance(attachment, ObjectPortRef):
                            local_attachments[local_key].add(
                                MaterialObjectPortRef(
                                    role_to_object[attachment.object_role],
                                    attachment.port,
                                    attachment.bit,
                                )
                            )
                        elif isinstance(attachment, PrefabPortRef):
                            if attachment in external_to_local:
                                raise MaterializationError(
                                    f"Prefab port {attachment} is attached to multiple nets"
                                )
                            external_to_local[attachment] = local_key
                for binding in claim.ports:
                    try:
                        cell_port = cell.ports[binding.formal]
                        local_key = external_to_local[binding.target]
                    except KeyError as error:
                        raise MaterializationError(
                            f"Invalid claim port binding on {cell.identifier.name}"
                        ) from error
                    if len(cell_port.bits) != 1:
                        raise MaterializationError(
                            f"Claim formal {cell.identifier.name}.{binding.formal} "
                            f"has width {len(cell_port.bits)}"
                        )
                    source = cell_port.bits[0]
                    if isinstance(source, SnapshotBitRef):
                        signal_to_local[signal(path, source)].append(local_key)
                    else:
                        local_constants[local_key].add(
                            MaterialConstantRef(source.value.value)
                        )
                implementation_drafts.append(
                    _ImplementationDraft(
                        path=f"{path}/implementation:{occurrence.value}",
                        occurrence=occurrence,
                        owner_module=path,
                        prefab=claim.prefab,
                        provider=claim.provider,
                        mapper=claim.mapper,
                        rule=claim.rule,
                        name=claim.implementation_name or claim.rule,
                        packaging=claim.packaging,
                        ports=[
                            (
                                binding.target.port,
                                binding.target.bit,
                                binding.direction,
                                external_to_local[binding.target],
                            )
                            for binding in claim.ports
                        ],
                        objects=list(role_to_object.values()),
                    )
                )
                continue

            child_module = snapshot.modules.get(cell.identifier.expected_type)
            if child_module is None or "blackbox" in child_module.attributes:
                if cell.identifier.expected_type != "$scopeinfo":
                    raise MaterializationError(
                        f"Unmapped cell {module.name}.{cell.identifier.name} "
                        f"of type {cell.identifier.expected_type} remains"
                    )
                continue
            token = (
                f"anchor:{cell.anchor}"
                if cell.anchor is not None
                else f"name:{cell.identifier.name}"
            )
            child_parts = path_parts + (token,)
            child_path = "/".join(child_parts)
            draft.children.append(child_path)
            visit_module(
                child_module.name,
                child_parts,
                path,
                cell.identifier.name,
                cell.anchor,
                next_active,
            )
            for port_name, child_port in child_module.ports.items():
                try:
                    parent_port = cell.ports[port_name]
                except KeyError as error:
                    raise MaterializationError(
                        f"Module instance {module.name}.{cell.identifier.name} "
                        f"has no connection for port {port_name}"
                    ) from error
                if len(parent_port.bits) != len(child_port.bits):
                    raise MaterializationError(
                        f"Module instance {module.name}.{cell.identifier.name}.{port_name} "
                        "has incompatible port width"
                    )
                for parent_bit, child_bit in zip(parent_port.bits, child_port.bits):
                    parent_signal = (
                        signal(path, parent_bit)
                        if isinstance(parent_bit, SnapshotBitRef)
                        else None
                    )
                    child_signal = (
                        signal(child_path, child_bit)
                        if isinstance(child_bit, SnapshotBitRef)
                        else None
                    )
                    if parent_signal is not None and child_signal is not None:
                        signal_sets.union(parent_signal, child_signal)
                    elif parent_signal is not None and isinstance(child_bit, ConstantBit):
                        signal_constants[parent_signal].add(
                            MaterialConstantRef(child_bit.value.value)
                        )
                    elif child_signal is not None and isinstance(parent_bit, ConstantBit):
                        signal_constants[child_signal].add(
                            MaterialConstantRef(parent_bit.value.value)
                        )

    root_path = f"module:{top_module}"
    visit_module(top_module, (root_path,), None, None, None, frozenset())

    root_to_local_keys: dict[Hashable, list[tuple[OccurrenceId, str]]] = defaultdict(list)
    for signal_reference, local_keys in signal_to_local.items():
        root_to_local_keys[signal_sets.find(signal_reference)].extend(local_keys)
    for local_keys in root_to_local_keys.values():
        first = local_keys[0]
        for other in local_keys[1:]:
            local_sets.union(first, other)

    constants_by_signal_root: dict[Hashable, set[MaterialConstantRef]] = defaultdict(set)
    for signal_reference, constants in signal_constants.items():
        constants_by_signal_root[signal_sets.find(signal_reference)].update(constants)
    for root, local_keys in root_to_local_keys.items():
        for local_key in local_keys:
            local_constants[local_key].update(constants_by_signal_root[root])

    root_ports: dict[Hashable, set[MaterialModulePortRef]] = defaultdict(set)
    root_draft = drafts[0]
    for name, bit, direction, signal_reference in root_draft.ports:
        if signal_reference is None:
            continue
        signal_root = signal_sets.find(signal_reference)
        local_keys = root_to_local_keys.get(signal_root)
        if local_keys:
            root_ports[local_sets.find(local_keys[0])].add(
                MaterialModulePortRef(top_module, name, bit, direction)
            )

    grouped_keys: dict[Hashable, list[tuple[OccurrenceId, str]]] = defaultdict(list)
    for local_key in local_types:
        grouped_keys[local_sets.find(local_key)].append(local_key)
    material_nets: list[MaterialNet] = []
    local_root_to_net: dict[Hashable, MaterialNetId] = {}
    for local_root, local_keys in grouped_keys.items():
        network_types = {local_types[key] for key in local_keys}
        if len(network_types) != 1:
            raise MaterializationError(
                f"Material network joins incompatible types {network_types!r}"
            )
        network_type = next(iter(network_types))
        attachments: set[MaterialAttachment] = set(root_ports[local_root])
        for local_key in local_keys:
            attachments.update(local_attachments[local_key])
            attachments.update(local_constants[local_key])
        frozen_attachments = frozenset(attachments)
        identifier = make_material_net_id(network_type, frozen_attachments)
        local_root_to_net[local_root] = identifier
        material_nets.append(MaterialNet(identifier, network_type, frozen_attachments))

    signal_root_to_net: dict[Hashable, MaterialNetId] = {}
    for signal_root, local_keys in root_to_local_keys.items():
        if local_keys:
            signal_root_to_net[signal_root] = local_root_to_net[
                local_sets.find(local_keys[0])
            ]
    modules = tuple(
        MaterialModuleOccurrence(
            path=draft.path,
            module=draft.module,
            implementation=draft.implementation,
            parent=draft.parent,
            instance=draft.instance,
            anchor=draft.anchor,
            ports=tuple(
                MaterialModulePort(
                    name,
                    bit,
                    direction,
                    (
                        signal_root_to_net.get(signal_sets.find(signal_reference))
                        if signal_reference is not None
                        else None
                    ),
                )
                for name, bit, direction, signal_reference in draft.ports
            ),
            objects=tuple(sorted(draft.objects, key=lambda item: item.value)),
            children=tuple(sorted(draft.children)),
        )
        for draft in drafts
    )
    implementations = tuple(
        MaterialImplementationOccurrence(
            path=draft.path,
            occurrence=draft.occurrence,
            owner_module=draft.owner_module,
            prefab=draft.prefab,
            provider=draft.provider,
            mapper=draft.mapper,
            rule=draft.rule,
            name=draft.name,
            packaging=draft.packaging,
            ports=tuple(
                MaterialImplementationPort(
                    name,
                    bit,
                    direction,
                    local_root_to_net[local_sets.find(local_key)],
                )
                for name, bit, direction, local_key in sorted(
                    draft.ports,
                    key=lambda item: (item[0], item[1]),
                )
            ),
            objects=tuple(sorted(draft.objects, key=lambda item: item.value)),
        )
        for draft in implementation_drafts
    )
    material = MaterialDesign(
        objects=tuple(objects.values()),
        nets=tuple(material_nets),
        modules=modules,
        implementations=implementations,
    )
    objects_by_id = {item.identifier: item for item in material.objects}
    for net in material.nets:
        try:
            providers[net.type.provider].validate_material_net(net, objects_by_id)
        except KeyError as error:
            raise MaterializationError(
                f"No provider is registered for material network {net.type}"
            ) from error
        except ValueError as error:
            raise MaterializationError(str(error)) from error
    return material


def flatten_and_materialize(
    context: DesignContext,
    state: CompilationIntermediateState,
    providers: Mapping[str, TargetProvider],
) -> tuple[CompilationIntermediateState, MaterialDesign]:
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

    top_module = top_modules[0]
    material = materialize_hierarchy(context, state, providers, top_module)
    context.run_pass("flatten -noscopeinfo")
    state = state.with_revision(context.revision)
    return state, material