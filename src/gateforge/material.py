from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re
from typing import TYPE_CHECKING

from gateforge.target import (
    NetworkInterfaceMode,
    NetworkTypeIdentifier,
    ObjectTypeIdentifier,
    ObjectTypeSchema,
    PortDirection,
    PrefabId,
    PrefabValidationError,
    ProviderConfiguration,
)

if TYPE_CHECKING:
    from gateforge.provider import TargetProvider


MATERIAL_SCHEMA_VERSION = 3
_SHA256 = re.compile(r"[0-9a-f]{64}")


class MaterialValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True, order=True)
class OccurrenceId:
    value: str


@dataclass(frozen=True, slots=True, order=True)
class MaterialObjectId:
    value: str


@dataclass(frozen=True, slots=True, order=True)
class MaterialNetId:
    value: str


@dataclass(frozen=True, slots=True, order=True)
class MaterialDesignDigest:
    value: str


@dataclass(frozen=True, slots=True)
class MaterialObject:
    identifier: MaterialObjectId
    occurrence: OccurrenceId
    prefab: PrefabId
    role: str
    type: ObjectTypeIdentifier
    hierarchy: str
    configuration: ProviderConfiguration = ProviderConfiguration()


@dataclass(frozen=True, slots=True)
class MaterialObjectPortRef:
    object: MaterialObjectId
    port: str
    bit: int = 0


@dataclass(frozen=True, slots=True)
class MaterialModulePortRef:
    module: str
    port: str
    bit: int
    direction: PortDirection


@dataclass(frozen=True, slots=True)
class MaterialModuleValueRef:
    module: str
    port: str
    bits: tuple[int, ...]
    direction: PortDirection


@dataclass(frozen=True, slots=True)
class MaterialConstantRef:
    value: str


type MaterialAttachment = (
    MaterialObjectPortRef
    | MaterialModulePortRef
    | MaterialModuleValueRef
    | MaterialConstantRef
)


@dataclass(frozen=True, slots=True)
class MaterialNet:
    identifier: MaterialNetId
    type: NetworkTypeIdentifier
    attachments: frozenset[MaterialAttachment]


@dataclass(frozen=True, slots=True)
class MaterialModulePort:
    name: str
    bit: int
    direction: PortDirection
    net: MaterialNetId | None


@dataclass(frozen=True, slots=True)
class MaterialModuleOccurrence:
    path: str
    module: str
    implementation: str
    parent: str | None
    instance: str | None
    anchor: str | None
    ports: tuple[MaterialModulePort, ...]
    objects: tuple[MaterialObjectId, ...]
    children: tuple[str, ...]


class ImplementationPackaging(StrEnum):
    INLINE = "inline"
    AUTO = "auto"
    CONTAINER = "container"


@dataclass(frozen=True, slots=True)
class MaterialImplementationPort:
    name: str
    bit: int
    direction: PortDirection
    net: MaterialNetId


@dataclass(frozen=True, slots=True)
class MaterialImplementationOccurrence:
    path: str
    occurrence: OccurrenceId
    owner_module: str
    prefab: PrefabId
    provider: str
    mapper: str
    rule: str
    name: str
    packaging: ImplementationPackaging
    ports: tuple[MaterialImplementationPort, ...]
    objects: tuple[MaterialObjectId, ...]


@dataclass(frozen=True, slots=True)
class MaterialDesign:
    objects: tuple[MaterialObject, ...]
    nets: tuple[MaterialNet, ...]
    modules: tuple[MaterialModuleOccurrence, ...] = ()
    implementations: tuple[MaterialImplementationOccurrence, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(item, MaterialObject) for item in self.objects):
            raise TypeError("Material design objects must be MaterialObject values")
        if not all(isinstance(item, MaterialNet) for item in self.nets):
            raise TypeError("Material design nets must be MaterialNet values")
        objects = tuple(sorted(self.objects, key=lambda item: item.identifier.value))
        nets = tuple(sorted(self.nets, key=lambda item: item.identifier.value))
        modules = tuple(sorted(self.modules, key=lambda item: item.path))
        implementations = tuple(
            sorted(self.implementations, key=lambda item: item.path)
        )
        if len({item.identifier for item in objects}) != len(objects):
            raise MaterialValidationError("Material design has duplicate object IDs")
        if len({item.identifier for item in nets}) != len(nets):
            raise MaterialValidationError("Material design has duplicate net IDs")
        if len({item.path for item in modules}) != len(modules):
            raise MaterialValidationError("Material design has duplicate module paths")
        if len({item.path for item in implementations}) != len(implementations):
            raise MaterialValidationError(
                "Material design has duplicate implementation paths"
            )
        object.__setattr__(self, "objects", objects)
        object.__setattr__(self, "nets", nets)
        object.__setattr__(self, "modules", modules)
        object.__setattr__(self, "implementations", implementations)

    def canonical_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": MATERIAL_SCHEMA_VERSION,
            "objects": [
                _material_object_data(item)
                for item in self.objects
            ],
            "nets": [
                {
                    "id": net.identifier.value,
                    "type": _type_data(net.type),
                    "attachments": sorted(
                        (_material_attachment_data(item) for item in net.attachments),
                        key=lambda item: json.dumps(item, sort_keys=True),
                    ),
                }
                for net in self.nets
            ],
        }
        if self.modules:
            data["modules"] = [_module_data(item) for item in self.modules]
        if self.implementations:
            data["implementations"] = [
                _implementation_data(item) for item in self.implementations
            ]
        return data

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_data(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def get_digest(self) -> MaterialDesignDigest:
        return MaterialDesignDigest(hashlib.sha256(self.canonical_bytes()).hexdigest())

    @classmethod
    def from_canonical_data(
        cls,
        value: object,
        providers: Mapping[str, TargetProvider],
    ) -> "MaterialDesign":
        data = _require_mapping(value, "material design")
        _require_keys(
            data,
            {"schema_version", "objects", "nets"},
            {"modules", "implementations"},
            "material design",
        )
        version = _require_int(data.get("schema_version"), "material schema version")
        if version not in {1, 2, MATERIAL_SCHEMA_VERSION}:
            raise MaterialValidationError(
                f"Unsupported material schema version {version}"
            )

        objects: list[MaterialObject] = []
        seen_object_ids: set[MaterialObjectId] = set()
        for raw_object in _require_list(data.get("objects"), "material objects"):
            item = _require_mapping(raw_object, "material object")
            _require_keys(
                item,
                {"id", "occurrence", "prefab", "role", "type", "hierarchy"},
                {"configuration"},
                "material object",
            )
            identifier = MaterialObjectId(
                _require_digest(item.get("id"), "material object ID")
            )
            if identifier in seen_object_ids:
                raise MaterialValidationError(
                    f"Duplicate material object ID {identifier.value}"
                )
            seen_object_ids.add(identifier)
            occurrence = OccurrenceId(
                _require_digest(item.get("occurrence"), "material occurrence ID")
            )
            prefab = PrefabId(
                _require_digest(item.get("prefab"), "material prefab ID")
            )
            role = _require_nonempty_str(item.get("role"), "material object role")
            material_object = MaterialObject(
                identifier=identifier,
                occurrence=occurrence,
                prefab=prefab,
                role=role,
                type=_decode_object_type(item.get("type")),
                hierarchy=_require_nonempty_str(
                    item.get("hierarchy"), "material object hierarchy"
                ),
                configuration=(
                    ProviderConfiguration.from_canonical_data(
                        item.get("configuration")
                    )
                    if "configuration" in item
                    else ProviderConfiguration()
                ),
            )
            expected = make_material_object_id(occurrence, prefab, role)
            if identifier != expected:
                raise MaterialValidationError(
                    f"Material object {identifier.value} failed its content hash"
                )
            objects.append(material_object)

        nets: list[MaterialNet] = []
        seen_net_ids: set[MaterialNetId] = set()
        for raw_net in _require_list(data.get("nets"), "material nets"):
            item = _require_mapping(raw_net, "material net")
            _require_keys(item, {"id", "type", "attachments"}, set(), "material net")
            identifier = MaterialNetId(
                _require_digest(item.get("id"), "material net ID")
            )
            if identifier in seen_net_ids:
                raise MaterialValidationError(
                    f"Duplicate material net ID {identifier.value}"
                )
            seen_net_ids.add(identifier)
            attachments: set[MaterialAttachment] = set()
            for raw_attachment in _require_list(
                item.get("attachments"), "material net attachments"
            ):
                attachment = _decode_attachment(raw_attachment, version)
                if attachment in attachments:
                    raise MaterialValidationError(
                        f"Material net {identifier.value} has duplicate attachment "
                        f"{attachment}"
                    )
                attachments.add(attachment)
            material_net = MaterialNet(
                identifier=identifier,
                type=_decode_network_type(item.get("type")),
                attachments=frozenset(attachments),
            )
            expected = make_material_net_id(
                material_net.type,
                material_net.attachments,
            )
            if identifier != expected:
                raise MaterialValidationError(
                    f"Material net {identifier.value} failed its content hash"
                )
            nets.append(material_net)

        modules = tuple(
            _decode_module_occurrence(item)
            for item in (
                _require_list(data.get("modules"), "material modules")
                if "modules" in data
                else []
            )
        )
        implementations = tuple(
            _decode_implementation_occurrence(item)
            for item in (
                _require_list(
                    data.get("implementations"),
                    "material implementations",
                )
                if "implementations" in data
                else []
            )
        )
        if version == 1 and implementations:
            raise MaterialValidationError(
                "Material schema version 1 cannot contain implementations"
            )
        design = cls(tuple(objects), tuple(nets), modules, implementations)
        validate_material_design(design, providers)
        return design


def _material_object_data(item: MaterialObject) -> dict[str, object]:
    data: dict[str, object] = {
        "id": item.identifier.value,
        "occurrence": item.occurrence.value,
        "prefab": item.prefab.value,
        "role": item.role,
        "type": _type_data(item.type),
        "hierarchy": item.hierarchy,
    }
    if not item.configuration.is_empty:
        data["configuration"] = item.configuration.canonical_data()
    return data


def _module_data(item: MaterialModuleOccurrence) -> dict[str, object]:
    return {
        "path": item.path,
        "module": item.module,
        "implementation": item.implementation,
        "parent": item.parent,
        "instance": item.instance,
        "anchor": item.anchor,
        "ports": [
            {
                "name": port.name,
                "bit": port.bit,
                "direction": port.direction.value,
                "net": port.net.value if port.net is not None else None,
            }
            for port in item.ports
        ],
        "objects": [identifier.value for identifier in item.objects],
        "children": list(item.children),
    }


def _implementation_data(
    item: MaterialImplementationOccurrence,
) -> dict[str, object]:
    return {
        "path": item.path,
        "occurrence": item.occurrence.value,
        "owner_module": item.owner_module,
        "prefab": item.prefab.value,
        "provider": item.provider,
        "mapper": item.mapper,
        "rule": item.rule,
        "name": item.name,
        "packaging": item.packaging.value,
        "ports": [
            {
                "name": port.name,
                "bit": port.bit,
                "direction": port.direction.value,
                "net": port.net.value,
            }
            for port in item.ports
        ],
        "objects": [identifier.value for identifier in item.objects],
    }


def _decode_implementation_occurrence(
    value: object,
) -> MaterialImplementationOccurrence:
    data = _require_mapping(value, "material implementation")
    _require_keys(
        data,
        {
            "path",
            "occurrence",
            "owner_module",
            "prefab",
            "provider",
            "mapper",
            "rule",
            "name",
            "packaging",
            "ports",
            "objects",
        },
        set(),
        "material implementation",
    )
    ports: list[MaterialImplementationPort] = []
    for raw_port in _require_list(
        data.get("ports"),
        "material implementation ports",
    ):
        port = _require_mapping(raw_port, "material implementation port")
        _require_keys(
            port,
            {"name", "bit", "direction", "net"},
            set(),
            "material implementation port",
        )
        ports.append(
            MaterialImplementationPort(
                name=_require_nonempty_str(
                    port.get("name"),
                    "implementation port name",
                ),
                bit=_require_nonnegative_int(
                    port.get("bit"),
                    "implementation port bit",
                ),
                direction=PortDirection(
                    _require_nonempty_str(
                        port.get("direction"),
                        "implementation port direction",
                    )
                ),
                net=MaterialNetId(
                    _require_digest(port.get("net"), "implementation port net")
                ),
            )
        )
    return MaterialImplementationOccurrence(
        path=_require_nonempty_str(data.get("path"), "implementation path"),
        occurrence=OccurrenceId(
            _require_digest(data.get("occurrence"), "implementation occurrence")
        ),
        owner_module=_require_nonempty_str(
            data.get("owner_module"),
            "implementation owner module",
        ),
        prefab=PrefabId(
            _require_digest(data.get("prefab"), "implementation prefab")
        ),
        provider=_require_nonempty_str(
            data.get("provider"),
            "implementation provider",
        ),
        mapper=_require_nonempty_str(
            data.get("mapper"),
            "implementation mapper",
        ),
        rule=_require_nonempty_str(data.get("rule"), "implementation rule"),
        name=_require_nonempty_str(data.get("name"), "implementation name"),
        packaging=ImplementationPackaging(
            _require_nonempty_str(
                data.get("packaging"),
                "implementation packaging",
            )
        ),
        ports=tuple(ports),
        objects=tuple(
            MaterialObjectId(_require_digest(item, "implementation object ID"))
            for item in _require_list(
                data.get("objects"),
                "implementation objects",
            )
        ),
    )


def _decode_module_occurrence(value: object) -> MaterialModuleOccurrence:
    data = _require_mapping(value, "material module")
    _require_keys(
        data,
        {
            "path",
            "module",
            "implementation",
            "parent",
            "instance",
            "anchor",
            "ports",
            "objects",
            "children",
        },
        set(),
        "material module",
    )
    parent = data.get("parent")
    instance = data.get("instance")
    anchor = data.get("anchor")
    if parent is not None and not isinstance(parent, str):
        raise MaterialValidationError("Material module parent must be a string or null")
    if instance is not None and not isinstance(instance, str):
        raise MaterialValidationError("Material module instance must be a string or null")
    if anchor is not None and not isinstance(anchor, str):
        raise MaterialValidationError("Material module anchor must be a string or null")
    ports: list[MaterialModulePort] = []
    for raw_port in _require_list(data.get("ports"), "material module ports"):
        port = _require_mapping(raw_port, "material module port")
        _require_keys(
            port,
            {"name", "bit", "direction", "net"},
            set(),
            "material module port",
        )
        net = port.get("net")
        ports.append(
            MaterialModulePort(
                name=_require_nonempty_str(port.get("name"), "module port name"),
                bit=_require_nonnegative_int(port.get("bit"), "module port bit"),
                direction=PortDirection(
                    _require_nonempty_str(port.get("direction"), "module port direction")
                ),
                net=(
                    MaterialNetId(_require_digest(net, "module port net"))
                    if net is not None
                    else None
                ),
            )
        )
    return MaterialModuleOccurrence(
        path=_require_nonempty_str(data.get("path"), "material module path"),
        module=_require_nonempty_str(data.get("module"), "material module name"),
        implementation=_require_nonempty_str(
            data.get("implementation"), "material module implementation"
        ),
        parent=parent,
        instance=instance,
        anchor=anchor,
        ports=tuple(ports),
        objects=tuple(
            MaterialObjectId(_require_digest(item, "module object ID"))
            for item in _require_list(data.get("objects"), "module objects")
        ),
        children=tuple(
            _require_nonempty_str(item, "module child path")
            for item in _require_list(data.get("children"), "module children")
        ),
    )


def _material_attachment_data(
    attachment: MaterialAttachment,
) -> dict[str, object]:
    if isinstance(attachment, MaterialObjectPortRef):
        return {
            "kind": "object",
            "object": attachment.object.value,
            "port": attachment.port,
            "bit": attachment.bit,
        }
    if isinstance(attachment, MaterialModulePortRef):
        return {
            "kind": "module_port",
            "module": attachment.module,
            "port": attachment.port,
            "bit": attachment.bit,
            "direction": attachment.direction.value,
        }
    if isinstance(attachment, MaterialModuleValueRef):
        return {
            "kind": "module_value",
            "module": attachment.module,
            "port": attachment.port,
            "bits": list(attachment.bits),
            "direction": attachment.direction.value,
        }
    return {"kind": "constant", "value": attachment.value}


def _type_data(
    identifier: ObjectTypeIdentifier | NetworkTypeIdentifier,
) -> dict[str, str | int]:
    return {
        "provider": identifier.provider,
        "name": identifier.name,
        "version": identifier.version,
    }


def _digest(data: object) -> str:
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_occurrence_id(
    module: str,
    cell_name: str | None,
    hierarchy: str,
    claim: str,
) -> OccurrenceId:
    return OccurrenceId(
        _digest(
            {
                "schema_version": 1,
                "module": module,
                "hierarchy": hierarchy,
                "claim": claim,
                **({"cell": cell_name} if cell_name is not None else {}),
            }
        )
    )


def make_material_object_id(
    occurrence: OccurrenceId,
    prefab: PrefabId,
    role: str,
) -> MaterialObjectId:
    return MaterialObjectId(
        _digest(
            {
                "schema_version": 1,
                "occurrence": occurrence.value,
                "prefab": prefab.value,
                "role": role,
            }
        )
    )


def make_material_net_id(
    network_type: NetworkTypeIdentifier,
    attachments: frozenset[MaterialAttachment],
) -> MaterialNetId:
    return MaterialNetId(
        _digest(
            {
                "schema_version": 1,
                "network_type": _type_data(network_type),
                "attachments": sorted(
                    (_material_attachment_data(item) for item in attachments),
                    key=lambda item: json.dumps(item, sort_keys=True),
                ),
            }
        )
    )


def validate_material_design(
    design: MaterialDesign,
    providers: Mapping[str, TargetProvider],
) -> None:
    provider_names = {
        item.type.provider for item in design.objects
    } | {net.type.provider for net in design.nets}
    if len(provider_names) > 1:
        raise MaterialValidationError(
            f"Material design mixes providers {sorted(provider_names)!r}"
        )
    provider = None
    if provider_names:
        provider_name = next(iter(provider_names))
        try:
            provider = providers[provider_name]
        except KeyError as error:
            raise MaterialValidationError(
                f"No provider is registered for material design {provider_name!r}"
            ) from error

    objects = {item.identifier: item for item in design.objects}
    nets_by_id = {item.identifier: item for item in design.nets}
    for item in design.objects:
        expected = make_material_object_id(item.occurrence, item.prefab, item.role)
        if item.identifier != expected:
            raise MaterialValidationError(
                f"Material object {item.identifier.value} failed its content hash"
            )
        if provider is None:
            raise AssertionError("Nonempty material object set has no provider")
        try:
            provider.registry.object(item.type)
            provider.validate_object_configuration(item.type, item.configuration)
        except PrefabValidationError as error:
            raise MaterialValidationError(str(error)) from error

    if design.modules:
        modules = {item.path: item for item in design.modules}
        roots = [item for item in design.modules if item.parent is None]
        if len(roots) != 1:
            raise MaterialValidationError(
                f"Material hierarchy requires one root, found {len(roots)}"
            )
        assigned_objects: set[MaterialObjectId] = set()
        for module in design.modules:
            if not module.path or not module.module or not module.implementation:
                raise MaterialValidationError("Material module names must not be empty")
            if module.parent is not None:
                try:
                    parent = modules[module.parent]
                except KeyError as error:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} has missing parent "
                        f"{module.parent!r}"
                    ) from error
                if module.path not in parent.children:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} is absent from its parent"
                    )
            for child in module.children:
                try:
                    child_module = modules[child]
                except KeyError as error:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} has missing child {child!r}"
                    ) from error
                if child_module.parent != module.path:
                    raise MaterialValidationError(
                        f"Material module child {child!r} has inconsistent parent"
                    )
            seen_ports: set[tuple[str, int]] = set()
            for port in module.ports:
                if not port.name or port.bit < 0:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} has invalid port {port}"
                    )
                key = (port.name, port.bit)
                if key in seen_ports:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} has duplicate port {key!r}"
                    )
                seen_ports.add(key)
                if port.net is not None and port.net not in nets_by_id:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} port {key!r} refers to "
                        f"missing net {port.net.value}"
                    )
            for identifier in module.objects:
                if identifier not in objects:
                    raise MaterialValidationError(
                        f"Material module {module.path!r} refers to missing object "
                        f"{identifier.value}"
                    )
                if identifier in assigned_objects:
                    raise MaterialValidationError(
                        f"Material object {identifier.value} belongs to multiple modules"
                    )
                assigned_objects.add(identifier)
        if assigned_objects != set(objects):
            raise MaterialValidationError(
                "Material hierarchy does not assign every material object"
            )

    modules = {item.path: item for item in design.modules}
    implementation_objects: set[MaterialObjectId] = set()
    occurrences: set[OccurrenceId] = set()
    for implementation in design.implementations:
        if implementation.owner_module not in modules:
            raise MaterialValidationError(
                f"Material implementation {implementation.path!r} has missing "
                f"owner module {implementation.owner_module!r}"
            )
        if implementation.occurrence in occurrences:
            raise MaterialValidationError(
                f"Duplicate material implementation occurrence "
                f"{implementation.occurrence.value}"
            )
        occurrences.add(implementation.occurrence)
        if not implementation.objects:
            raise MaterialValidationError(
                f"Material implementation {implementation.path!r} has no objects"
            )
        seen_ports: set[tuple[str, int]] = set()
        for port in implementation.ports:
            key = (port.name, port.bit)
            if not port.name or port.bit < 0 or key in seen_ports:
                raise MaterialValidationError(
                    f"Material implementation {implementation.path!r} has invalid "
                    f"or duplicate port {key!r}"
                )
            seen_ports.add(key)
            if port.net not in nets_by_id:
                raise MaterialValidationError(
                    f"Material implementation {implementation.path!r} port "
                    f"{key!r} refers to missing net {port.net.value}"
                )
        for identifier in implementation.objects:
            try:
                material_object = objects[identifier]
            except KeyError as error:
                raise MaterialValidationError(
                    f"Material implementation {implementation.path!r} refers to "
                    f"missing object {identifier.value}"
                ) from error
            if identifier in implementation_objects:
                raise MaterialValidationError(
                    f"Material object {identifier.value} belongs to multiple "
                    "implementations"
                )
            implementation_objects.add(identifier)
            if material_object.occurrence != implementation.occurrence:
                raise MaterialValidationError(
                    f"Material implementation {implementation.path!r} mixes "
                    "object occurrences"
                )
            if material_object.prefab != implementation.prefab:
                raise MaterialValidationError(
                    f"Material implementation {implementation.path!r} mixes prefabs"
                )
            if material_object.type.provider != implementation.provider:
                raise MaterialValidationError(
                    f"Material implementation {implementation.path!r} mixes providers"
                )

    used_endpoints: set[MaterialObjectPortRef | MaterialModulePortRef] = set()
    for net in design.nets:
        expected = make_material_net_id(net.type, net.attachments)
        if net.identifier != expected:
            raise MaterialValidationError(
                f"Material net {net.identifier.value} failed its content hash"
            )
        if provider is None:
            raise AssertionError("Nonempty material net set has no provider")
        try:
            network_schema = provider.registry.network(net.type)
        except PrefabValidationError as error:
            raise MaterialValidationError(str(error)) from error
        for attachment in net.attachments:
            if isinstance(attachment, MaterialObjectPortRef):
                try:
                    material_object = objects[attachment.object]
                except KeyError as error:
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} references unknown "
                        f"object {attachment.object.value}"
                    ) from error
                port = _schema_port(material_object.type, provider.registry.object(material_object.type), attachment.port)
                if attachment.bit < 0 or attachment.bit >= port.width:
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} references invalid bit "
                        f"{attachment.bit} of {attachment.object.value}.{attachment.port}"
                    )
                if port.signal != network_schema.signal:
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} connects {port.signal} "
                        f"to {network_schema.signal}"
                    )
                _claim_endpoint(attachment, used_endpoints)
            elif isinstance(attachment, MaterialModulePortRef):
                if network_schema.interface_mode != NetworkInterfaceMode.BITWISE:
                    raise MaterialValidationError(
                        f"Packed material net {net.identifier.value} requires "
                        "module value attachments"
                    )
                if not attachment.module or not attachment.port or attachment.bit < 0:
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} has invalid module port "
                        f"{attachment}"
                    )
                _claim_endpoint(attachment, used_endpoints)
            elif isinstance(attachment, MaterialModuleValueRef):
                if network_schema.interface_mode != NetworkInterfaceMode.PACKED:
                    raise MaterialValidationError(
                        f"Bitwise material net {net.identifier.value} cannot use "
                        "a module value attachment"
                    )
                if (
                    not attachment.module
                    or not attachment.port
                    or not attachment.bits
                    or attachment.bits != tuple(range(len(attachment.bits)))
                ):
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} has invalid module "
                        f"value {attachment}"
                    )
                for bit in attachment.bits:
                    _claim_endpoint(
                        MaterialModulePortRef(
                            attachment.module,
                            attachment.port,
                            bit,
                            attachment.direction,
                        ),
                        used_endpoints,
                    )
            elif isinstance(attachment, MaterialConstantRef):
                if not attachment.value:
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} has an empty constant"
                    )
            else:
                raise MaterialValidationError(
                    f"Material net {net.identifier.value} has invalid attachment "
                    f"{attachment!r}"
                )
        try:
            provider.validate_material_net(net, objects)
        except PrefabValidationError as error:
            raise MaterialValidationError(str(error)) from error


def _claim_endpoint(
    attachment: MaterialObjectPortRef | MaterialModulePortRef,
    used: set[MaterialObjectPortRef | MaterialModulePortRef],
) -> None:
    if attachment in used:
        raise MaterialValidationError(
            f"Material endpoint {attachment} is attached to more than one net"
        )
    used.add(attachment)


def _schema_port(
    object_type: ObjectTypeIdentifier,
    schema: ObjectTypeSchema,
    port_name: str,
):
    port = next((item for item in schema.ports if item.name == port_name), None)
    if port is None:
        raise MaterialValidationError(
            f"Material object type {object_type} has no port {port_name!r}"
        )
    return port


def _decode_attachment(value: object, schema_version: int) -> MaterialAttachment:
    data = _require_mapping(value, "material attachment")
    kind = _require_nonempty_str(data.get("kind"), "material attachment kind")
    if kind == "object":
        _require_keys(data, {"kind", "object", "port", "bit"}, set(), "object attachment")
        return MaterialObjectPortRef(
            object=MaterialObjectId(
                _require_digest(data.get("object"), "attachment object ID")
            ),
            port=_require_nonempty_str(data.get("port"), "attachment object port"),
            bit=_require_nonnegative_int(data.get("bit"), "attachment object bit"),
        )
    if kind == "module_port":
        _require_keys(
            data,
            {"kind", "module", "port", "bit", "direction"},
            set(),
            "module port attachment",
        )
        return MaterialModulePortRef(
            module=_require_nonempty_str(data.get("module"), "attachment module"),
            port=_require_nonempty_str(data.get("port"), "attachment module port"),
            bit=_require_nonnegative_int(data.get("bit"), "attachment module bit"),
            direction=PortDirection(
                _require_nonempty_str(data.get("direction"), "attachment direction")
            ),
        )
    if kind == "module_value":
        if schema_version < 3:
            raise MaterialValidationError(
                "Material module values require schema version 3"
            )
        _require_keys(
            data,
            {"kind", "module", "port", "bits", "direction"},
            set(),
            "module value attachment",
        )
        return MaterialModuleValueRef(
            module=_require_nonempty_str(data.get("module"), "attachment module"),
            port=_require_nonempty_str(data.get("port"), "attachment module port"),
            bits=tuple(
                _require_nonnegative_int(item, "attachment module value bit")
                for item in _require_list(
                    data.get("bits"),
                    "attachment module value bits",
                )
            ),
            direction=PortDirection(
                _require_nonempty_str(data.get("direction"), "attachment direction")
            ),
        )
    if kind == "constant":
        _require_keys(data, {"kind", "value"}, set(), "constant attachment")
        return MaterialConstantRef(
            _require_nonempty_str(data.get("value"), "attachment constant")
        )
    raise MaterialValidationError(f"Unknown material attachment kind {kind!r}")


def _decode_object_type(value: object) -> ObjectTypeIdentifier:
    data = _require_mapping(value, "object type")
    _require_keys(data, {"provider", "name", "version"}, set(), "object type")
    return ObjectTypeIdentifier(
        provider=_require_nonempty_str(data.get("provider"), "object type provider"),
        name=_require_nonempty_str(data.get("name"), "object type name"),
        version=_require_int(data.get("version"), "object type version"),
    )


def _decode_network_type(value: object) -> NetworkTypeIdentifier:
    data = _require_mapping(value, "network type")
    _require_keys(data, {"provider", "name", "version"}, set(), "network type")
    return NetworkTypeIdentifier(
        provider=_require_nonempty_str(data.get("provider"), "network type provider"),
        name=_require_nonempty_str(data.get("name"), "network type name"),
        version=_require_int(data.get("version"), "network type version"),
    )


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MaterialValidationError(f"{context} must be an object")
    return value


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise MaterialValidationError(f"{context} must be a list")
    return value


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise MaterialValidationError(f"{context} must be a string")
    return value


def _require_nonempty_str(value: object, context: str) -> str:
    result = _require_str(value, context)
    if not result:
        raise MaterialValidationError(f"{context} must not be empty")
    return result


def _require_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MaterialValidationError(f"{context} must be an integer")
    return value


def _require_nonnegative_int(value: object, context: str) -> int:
    result = _require_int(value, context)
    if result < 0:
        raise MaterialValidationError(f"{context} must be nonnegative")
    return result


def _require_digest(value: object, context: str) -> str:
    result = _require_str(value, context)
    if _SHA256.fullmatch(result) is None:
        raise MaterialValidationError(
            f"{context} must be a lowercase SHA-256 hexadecimal digest"
        )
    return result


def _require_keys(
    value: Mapping[str, object],
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional
    if missing:
        raise MaterialValidationError(
            f"{context} is missing keys {sorted(missing)!r}"
        )
    if unknown:
        raise MaterialValidationError(
            f"{context} has unknown keys {sorted(unknown)!r}"
        )
