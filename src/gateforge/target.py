from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
from typing import Iterable


PREFAB_SCHEMA_VERSION = 1


class PrefabValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    canonical_json: str = "{}"

    def __post_init__(self) -> None:
        try:
            value = json.loads(
                self.canonical_json,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"Invalid JSON number {token}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("Provider configuration must be valid JSON") from error
        normalized = _normalize_provider_json(value, "provider configuration")
        if not isinstance(normalized, dict):
            raise ValueError("Provider configuration must be an object")
        object.__setattr__(
            self,
            "canonical_json",
            json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    @classmethod
    def from_canonical_data(cls, value: object) -> "ProviderConfiguration":
        normalized = _normalize_provider_json(value, "provider configuration")
        if not isinstance(normalized, dict):
            raise ValueError("Provider configuration must be an object")
        return cls(
            json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )

    @property
    def is_empty(self) -> bool:
        return self.canonical_json == "{}"

    def canonical_data(self) -> dict[str, object]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise AssertionError("Canonical provider configuration is not an object")
        return value


def _normalize_provider_json(value: object, context: str) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{context} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [
            _normalize_provider_json(item, f"{context} array") for item in value
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{context} object keys must be strings")
            normalized[key] = _normalize_provider_json(item, f"{context}.{key}")
        return normalized
    raise ValueError(f"{context} contains unsupported value {value!r}")


class PortDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


@dataclass(frozen=True, slots=True, order=True)
class ObjectTypeIdentifier:
    provider: str
    name: str
    version: int = 1


@dataclass(frozen=True, slots=True, order=True)
class SignalTypeIdentifier:
    provider: str
    name: str
    version: int = 1


@dataclass(frozen=True, slots=True, order=True)
class NetworkTypeIdentifier:
    provider: str
    name: str
    version: int = 1


@dataclass(frozen=True, slots=True)
class PortSchema:
    name: str
    direction: PortDirection
    signal: SignalTypeIdentifier
    width: int = 1


@dataclass(frozen=True, slots=True)
class ObjectTypeSchema:
    identifier: ObjectTypeIdentifier
    ports: frozenset[PortSchema]


@dataclass(frozen=True, slots=True)
class NetworkTypeSchema:
    identifier: NetworkTypeIdentifier
    signal: SignalTypeIdentifier


class TargetTypeRegistry:
    def __init__(self) -> None:
        self._objects: dict[ObjectTypeIdentifier, ObjectTypeSchema] = {}
        self._networks: dict[NetworkTypeIdentifier, NetworkTypeSchema] = {}

    def register_object(self, schema: ObjectTypeSchema) -> None:
        existing = self._objects.get(schema.identifier)
        if existing is not None and existing != schema:
            raise ValueError(f"Conflicting object schema for {schema.identifier}")
        self._objects[schema.identifier] = schema

    def register_network(self, schema: NetworkTypeSchema) -> None:
        existing = self._networks.get(schema.identifier)
        if existing is not None and existing != schema:
            raise ValueError(f"Conflicting network schema for {schema.identifier}")
        self._networks[schema.identifier] = schema

    def object(self, identifier: ObjectTypeIdentifier) -> ObjectTypeSchema:
        try:
            return self._objects[identifier]
        except KeyError as error:
            raise PrefabValidationError(
                f"Unknown object type {identifier}"
            ) from error

    def network(self, identifier: NetworkTypeIdentifier) -> NetworkTypeSchema:
        try:
            return self._networks[identifier]
        except KeyError as error:
            raise PrefabValidationError(
                f"Unknown network type {identifier}"
            ) from error


@dataclass(frozen=True, slots=True)
class PrefabObject:
    role: str
    type: ObjectTypeIdentifier
    configuration: ProviderConfiguration = ProviderConfiguration()


@dataclass(frozen=True, slots=True)
class PrefabPort:
    name: str
    direction: PortDirection
    signal: SignalTypeIdentifier
    width: int = 1


@dataclass(frozen=True, slots=True)
class ObjectPortRef:
    object_role: str
    port: str
    bit: int = 0


@dataclass(frozen=True, slots=True)
class PrefabPortRef:
    port: str
    bit: int = 0


type PrefabAttachment = ObjectPortRef | PrefabPortRef


@dataclass(frozen=True, slots=True)
class PrefabNet:
    role: str
    type: NetworkTypeIdentifier
    attachments: frozenset[PrefabAttachment]


@dataclass(frozen=True, slots=True, order=True)
class PrefabId:
    value: str


def _type_data(
    identifier: ObjectTypeIdentifier
    | SignalTypeIdentifier
    | NetworkTypeIdentifier,
) -> dict[str, str | int]:
    return {
        "provider": identifier.provider,
        "name": identifier.name,
        "version": identifier.version,
    }


def _attachment_data(attachment: PrefabAttachment) -> dict[str, str | int]:
    if isinstance(attachment, ObjectPortRef):
        return {
            "kind": "object",
            "object": attachment.object_role,
            "port": attachment.port,
            "bit": attachment.bit,
        }
    return {
        "kind": "external",
        "port": attachment.port,
        "bit": attachment.bit,
    }


def _attachment_sort_key(attachment: PrefabAttachment) -> tuple[str, str, str, int]:
    if isinstance(attachment, ObjectPortRef):
        return ("object", attachment.object_role, attachment.port, attachment.bit)
    return ("external", "", attachment.port, attachment.bit)


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    return value


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return value


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string")
    return value


def _require_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context} must be an integer")
    return value


def _decode_object_type(value: object) -> ObjectTypeIdentifier:
    data = _require_mapping(value, "object type")
    return ObjectTypeIdentifier(
        provider=_require_str(data.get("provider"), "object type provider"),
        name=_require_str(data.get("name"), "object type name"),
        version=_require_int(data.get("version"), "object type version"),
    )


def _decode_signal_type(value: object) -> SignalTypeIdentifier:
    data = _require_mapping(value, "signal type")
    return SignalTypeIdentifier(
        provider=_require_str(data.get("provider"), "signal type provider"),
        name=_require_str(data.get("name"), "signal type name"),
        version=_require_int(data.get("version"), "signal type version"),
    )


def _decode_network_type(value: object) -> NetworkTypeIdentifier:
    data = _require_mapping(value, "network type")
    return NetworkTypeIdentifier(
        provider=_require_str(data.get("provider"), "network type provider"),
        name=_require_str(data.get("name"), "network type name"),
        version=_require_int(data.get("version"), "network type version"),
    )


@dataclass(frozen=True, slots=True)
class SemanticPrefab:
    provider: str
    objects: frozenset[PrefabObject]
    ports: frozenset[PrefabPort]
    nets: frozenset[PrefabNet]

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": PREFAB_SCHEMA_VERSION,
            "provider": self.provider,
            "objects": [
                _prefab_object_data(item)
                for item in sorted(self.objects, key=lambda item: item.role)
            ],
            "ports": [
                {
                    "name": item.name,
                    "direction": item.direction.value,
                    "signal": _type_data(item.signal),
                    "width": item.width,
                }
                for item in sorted(self.ports, key=lambda item: item.name)
            ],
            "nets": [
                {
                    "role": item.role,
                    "type": _type_data(item.type),
                    "attachments": [
                        _attachment_data(attachment)
                        for attachment in sorted(
                            item.attachments, key=_attachment_sort_key
                        )
                    ],
                }
                for item in sorted(self.nets, key=lambda item: item.role)
            ],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_data(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def get_id(self) -> PrefabId:
        return PrefabId(hashlib.sha256(self.canonical_bytes()).hexdigest())

    @classmethod
    def from_canonical_data(cls, value: object) -> "SemanticPrefab":
        data = _require_mapping(value, "semantic prefab")
        version = _require_int(data.get("schema_version"), "prefab schema version")
        if version != PREFAB_SCHEMA_VERSION:
            raise ValueError(f"Unsupported prefab schema version {version}")

        objects = frozenset(
            PrefabObject(
                role=_require_str(item.get("role"), "object role"),
                type=_decode_object_type(item.get("type")),
                configuration=(
                    ProviderConfiguration.from_canonical_data(
                        item.get("configuration")
                    )
                    if "configuration" in item
                    else ProviderConfiguration()
                ),
            )
            for raw_item in _require_list(data.get("objects"), "prefab objects")
            for item in [_require_mapping(raw_item, "prefab object")]
        )
        ports = frozenset(
            PrefabPort(
                name=_require_str(item.get("name"), "prefab port name"),
                direction=PortDirection(
                    _require_str(item.get("direction"), "prefab port direction")
                ),
                signal=_decode_signal_type(item.get("signal")),
                width=_require_int(item.get("width"), "prefab port width"),
            )
            for raw_item in _require_list(data.get("ports"), "prefab ports")
            for item in [_require_mapping(raw_item, "prefab port")]
        )

        nets: set[PrefabNet] = set()
        for raw_net in _require_list(data.get("nets"), "prefab nets"):
            net = _require_mapping(raw_net, "prefab net")
            attachments: set[PrefabAttachment] = set()
            for raw_attachment in _require_list(
                net.get("attachments"), "prefab net attachments"
            ):
                attachment = _require_mapping(raw_attachment, "prefab attachment")
                kind = _require_str(attachment.get("kind"), "attachment kind")
                if kind == "object":
                    attachments.add(
                        ObjectPortRef(
                            object_role=_require_str(
                                attachment.get("object"), "attachment object"
                            ),
                            port=_require_str(
                                attachment.get("port"), "attachment port"
                            ),
                            bit=_require_int(
                                attachment.get("bit"), "attachment bit"
                            ),
                        )
                    )
                elif kind == "external":
                    attachments.add(
                        PrefabPortRef(
                            port=_require_str(
                                attachment.get("port"), "attachment port"
                            ),
                            bit=_require_int(
                                attachment.get("bit"), "attachment bit"
                            ),
                        )
                    )
                else:
                    raise ValueError(f"Unknown prefab attachment kind {kind!r}")
            nets.add(
                PrefabNet(
                    role=_require_str(net.get("role"), "prefab net role"),
                    type=_decode_network_type(net.get("type")),
                    attachments=frozenset(attachments),
                )
            )

        return cls(
            provider=_require_str(data.get("provider"), "prefab provider"),
            objects=objects,
            ports=ports,
            nets=frozenset(nets),
        )


def _prefab_object_data(item: PrefabObject) -> dict[str, object]:
    data: dict[str, object] = {
        "role": item.role,
        "type": _type_data(item.type),
    }
    if not item.configuration.is_empty:
        data["configuration"] = item.configuration.canonical_data()
    return data


def _unique_by_name(items: Iterable[object], attribute: str, kind: str) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for item in items:
        name = getattr(item, attribute)
        if not isinstance(name, str) or not name:
            raise PrefabValidationError(f"{kind} names must be non-empty strings")
        if name in indexed:
            raise PrefabValidationError(f"Duplicate {kind} name {name!r}")
        indexed[name] = item
    return indexed


def _port_by_name(schema: ObjectTypeSchema) -> dict[str, PortSchema]:
    return {
        name: port
        for name, port in _unique_by_name(schema.ports, "name", "object port").items()
        if isinstance(port, PortSchema)
    }


def attachment_direction(
    prefab: SemanticPrefab,
    registry: TargetTypeRegistry,
    attachment: PrefabAttachment,
) -> PortDirection:
    if isinstance(attachment, PrefabPortRef):
        ports = {
            port.name: port
            for port in prefab.ports
        }
        try:
            return ports[attachment.port].direction
        except KeyError as error:
            raise PrefabValidationError(
                f"Unknown external port {attachment.port!r}"
            ) from error

    objects = {item.role: item for item in prefab.objects}
    try:
        object_type = objects[attachment.object_role].type
    except KeyError as error:
        raise PrefabValidationError(
            f"Unknown object role {attachment.object_role!r}"
        ) from error
    ports = _port_by_name(registry.object(object_type))
    try:
        return ports[attachment.port].direction
    except KeyError as error:
        raise PrefabValidationError(
            f"Unknown object port {attachment.object_role}.{attachment.port}"
        ) from error


def validate_prefab(prefab: SemanticPrefab, registry: TargetTypeRegistry) -> None:
    if not prefab.provider:
        raise PrefabValidationError("A prefab must name its provider")

    objects = _unique_by_name(prefab.objects, "role", "object role")
    ports = _unique_by_name(prefab.ports, "name", "external port")
    _unique_by_name(prefab.nets, "role", "net role")

    expected_attachments: set[PrefabAttachment] = set()
    attachment_signals: dict[PrefabAttachment, SignalTypeIdentifier] = {}

    for role, raw_object in objects.items():
        if not isinstance(raw_object, PrefabObject):
            raise PrefabValidationError(f"Invalid object {raw_object!r}")
        if raw_object.type.provider != prefab.provider:
            raise PrefabValidationError(
                f"Object {role!r} belongs to provider {raw_object.type.provider!r}"
            )
        schema = registry.object(raw_object.type)
        for port in schema.ports:
            if port.width <= 0:
                raise PrefabValidationError(
                    f"Object port {role}.{port.name} has invalid width {port.width}"
                )
            for bit in range(port.width):
                reference = ObjectPortRef(role, port.name, bit)
                expected_attachments.add(reference)
                attachment_signals[reference] = port.signal

    for name, raw_port in ports.items():
        if not isinstance(raw_port, PrefabPort):
            raise PrefabValidationError(f"Invalid external port {raw_port!r}")
        if raw_port.width <= 0:
            raise PrefabValidationError(
                f"External port {name!r} has invalid width {raw_port.width}"
            )
        if raw_port.signal.provider != prefab.provider:
            raise PrefabValidationError(
                f"External port {name!r} belongs to provider "
                f"{raw_port.signal.provider!r}"
            )
        for bit in range(raw_port.width):
            reference = PrefabPortRef(name, bit)
            expected_attachments.add(reference)
            attachment_signals[reference] = raw_port.signal

    actual_attachments: set[PrefabAttachment] = set()
    for net in prefab.nets:
        if net.type.provider != prefab.provider:
            raise PrefabValidationError(
                f"Net {net.role!r} belongs to provider {net.type.provider!r}"
            )
        net_schema = registry.network(net.type)
        if len(net.attachments) < 2:
            raise PrefabValidationError(
                f"Net {net.role!r} must have at least two attachments"
            )
        for attachment in net.attachments:
            signal = attachment_signals.get(attachment)
            if signal is None:
                raise PrefabValidationError(
                    f"Net {net.role!r} references unknown endpoint {attachment}"
                )
            if signal != net_schema.signal:
                raise PrefabValidationError(
                    f"Net {net.role!r} connects {signal} to {net_schema.signal}"
                )
            if attachment in actual_attachments:
                raise PrefabValidationError(
                    f"Endpoint {attachment} is attached to more than one net"
                )
            actual_attachments.add(attachment)

    missing = expected_attachments - actual_attachments
    if missing:
        raise PrefabValidationError(
            f"Prefab has unconnected endpoints: "
            f"{sorted(missing, key=_attachment_sort_key)!r}"
        )
