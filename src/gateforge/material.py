from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import TYPE_CHECKING

from gateforge.target import (
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


MATERIAL_SCHEMA_VERSION = 1
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
class MaterialConstantRef:
    value: str


type MaterialAttachment = (
    MaterialObjectPortRef | MaterialModulePortRef | MaterialConstantRef
)


@dataclass(frozen=True, slots=True)
class MaterialNet:
    identifier: MaterialNetId
    type: NetworkTypeIdentifier
    attachments: frozenset[MaterialAttachment]


@dataclass(frozen=True, slots=True)
class MaterialDesign:
    objects: tuple[MaterialObject, ...]
    nets: tuple[MaterialNet, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(item, MaterialObject) for item in self.objects):
            raise TypeError("Material design objects must be MaterialObject values")
        if not all(isinstance(item, MaterialNet) for item in self.nets):
            raise TypeError("Material design nets must be MaterialNet values")
        objects = tuple(sorted(self.objects, key=lambda item: item.identifier.value))
        nets = tuple(sorted(self.nets, key=lambda item: item.identifier.value))
        if len({item.identifier for item in objects}) != len(objects):
            raise MaterialValidationError("Material design has duplicate object IDs")
        if len({item.identifier for item in nets}) != len(nets):
            raise MaterialValidationError("Material design has duplicate net IDs")
        object.__setattr__(self, "objects", objects)
        object.__setattr__(self, "nets", nets)

    def canonical_data(self) -> dict[str, object]:
        return {
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
        _require_keys(data, {"schema_version", "objects", "nets"}, set(), "material design")
        version = _require_int(data.get("schema_version"), "material schema version")
        if version != MATERIAL_SCHEMA_VERSION:
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
                attachment = _decode_attachment(raw_attachment)
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

        design = cls(tuple(objects), tuple(nets))
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
                if not attachment.module or not attachment.port or attachment.bit < 0:
                    raise MaterialValidationError(
                        f"Material net {net.identifier.value} has invalid module port "
                        f"{attachment}"
                    )
                _claim_endpoint(attachment, used_endpoints)
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


def _decode_attachment(value: object) -> MaterialAttachment:
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
