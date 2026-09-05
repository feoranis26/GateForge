from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from gateforge.target import PortDirection, PrefabId, PrefabPortRef, SemanticPrefab


STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True, order=True)
class ClaimDefinitionId:
    value: str


@dataclass(frozen=True, slots=True)
class ClaimPortBinding:
    formal: str
    target: PrefabPortRef
    direction: PortDirection


@dataclass(frozen=True, slots=True)
class ClaimDefinition:
    identifier: ClaimDefinitionId
    prefab: PrefabId
    module: str
    instance: str
    blackbox: str
    ports: tuple[ClaimPortBinding, ...]
    provider: str
    mapper: str
    rule: str
    rule_version: int
    accepted_revision: int
    source_provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompilationIntermediateState:
    revision: int
    prefabs: Mapping[PrefabId, SemanticPrefab]
    claims: Mapping[ClaimDefinitionId, ClaimDefinition]

    @classmethod
    def empty(cls, revision: int = 0) -> "CompilationIntermediateState":
        return cls(
            revision=revision,
            prefabs=MappingProxyType({}),
            claims=MappingProxyType({}),
        )

    def with_revision(self, revision: int) -> "CompilationIntermediateState":
        if revision < self.revision:
            raise ValueError(
                f"Cannot move state revision backward from {self.revision} to {revision}"
            )
        return CompilationIntermediateState(
            revision=revision,
            prefabs=self.prefabs,
            claims=self.claims,
        )

    def with_acceptance(
        self,
        revision: int,
        prefabs: Iterable[SemanticPrefab],
        claims: Iterable[ClaimDefinition],
    ) -> "CompilationIntermediateState":
        if revision <= self.revision:
            raise ValueError(
                f"Acceptance revision {revision} must follow {self.revision}"
            )

        updated_prefabs = dict(self.prefabs)
        for prefab in prefabs:
            identifier = prefab.get_id()
            existing = updated_prefabs.get(identifier)
            if existing is not None and existing.canonical_bytes() != prefab.canonical_bytes():
                raise ValueError(f"Prefab hash collision for {identifier.value}")
            updated_prefabs[identifier] = prefab

        updated_claims = dict(self.claims)
        for claim in claims:
            existing = updated_claims.get(claim.identifier)
            if existing is not None and existing != claim:
                raise ValueError(
                    f"Conflicting claim definition {claim.identifier.value}"
                )
            updated_claims[claim.identifier] = claim

        return CompilationIntermediateState(
            revision=revision,
            prefabs=MappingProxyType(updated_prefabs),
            claims=MappingProxyType(updated_claims),
        )

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "revision": self.revision,
            "prefabs": [
                {
                    "id": identifier.value,
                    "prefab": prefab.canonical_data(),
                }
                for identifier, prefab in sorted(
                    self.prefabs.items(), key=lambda item: item[0].value
                )
            ],
            "claims": [
                {
                    "id": claim.identifier.value,
                    "prefab": claim.prefab.value,
                    "module": claim.module,
                    "instance": claim.instance,
                    "blackbox": claim.blackbox,
                    "ports": [
                        {
                            "formal": port.formal,
                            "target": {
                                "port": port.target.port,
                                "bit": port.target.bit,
                            },
                            "direction": port.direction.value,
                        }
                        for port in claim.ports
                    ],
                    "provider": claim.provider,
                    "mapper": claim.mapper,
                    "rule": claim.rule,
                    "rule_version": claim.rule_version,
                    "accepted_revision": claim.accepted_revision,
                    "source_provenance": list(claim.source_provenance),
                }
                for claim in sorted(
                    self.claims.values(), key=lambda item: item.identifier.value
                )
            ],
        }

    @classmethod
    def from_canonical_data(
        cls,
        value: Mapping[str, Any],
    ) -> "CompilationIntermediateState":
        version = value.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported state schema version {version!r}")
        revision = value.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("State revision must be an integer")

        prefabs: dict[PrefabId, SemanticPrefab] = {}
        raw_prefabs = value.get("prefabs")
        if not isinstance(raw_prefabs, list):
            raise ValueError("State prefabs must be a list")
        for raw_entry in raw_prefabs:
            if not isinstance(raw_entry, Mapping):
                raise ValueError("State prefab entry must be an object")
            raw_id = raw_entry.get("id")
            if not isinstance(raw_id, str):
                raise ValueError("State prefab ID must be a string")
            prefab = SemanticPrefab.from_canonical_data(raw_entry.get("prefab"))
            identifier = PrefabId(raw_id)
            if prefab.get_id() != identifier:
                raise ValueError(f"State prefab {raw_id} failed its content hash")
            prefabs[identifier] = prefab

        claims: dict[ClaimDefinitionId, ClaimDefinition] = {}
        raw_claims = value.get("claims")
        if not isinstance(raw_claims, list):
            raise ValueError("State claims must be a list")
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, Mapping):
                raise ValueError("State claim entry must be an object")
            identifier = ClaimDefinitionId(_required_string(raw_claim, "id"))
            prefab_id = PrefabId(_required_string(raw_claim, "prefab"))
            if prefab_id not in prefabs:
                raise ValueError(
                    f"State claim {identifier.value} refers to missing prefab "
                    f"{prefab_id.value}"
                )
            raw_ports = raw_claim.get("ports")
            if not isinstance(raw_ports, list):
                raise ValueError("State claim ports must be a list")
            ports: list[ClaimPortBinding] = []
            for raw_port in raw_ports:
                if not isinstance(raw_port, Mapping):
                    raise ValueError("State claim port must be an object")
                target = raw_port.get("target")
                if not isinstance(target, Mapping):
                    raise ValueError("State claim target must be an object")
                bit = target.get("bit")
                if not isinstance(bit, int) or isinstance(bit, bool):
                    raise ValueError("State claim target bit must be an integer")
                ports.append(
                    ClaimPortBinding(
                        formal=_required_string(raw_port, "formal"),
                        target=PrefabPortRef(
                            _required_string(target, "port"),
                            bit,
                        ),
                        direction=PortDirection(
                            _required_string(raw_port, "direction")
                        ),
                    )
                )
            accepted_revision = raw_claim.get("accepted_revision")
            if not isinstance(accepted_revision, int) or isinstance(
                accepted_revision, bool
            ):
                raise ValueError("Accepted revision must be an integer")
            rule_version = raw_claim.get("rule_version")
            if not isinstance(rule_version, int) or isinstance(rule_version, bool):
                raise ValueError("Rule version must be an integer")
            source_provenance = raw_claim.get("source_provenance")
            if not isinstance(source_provenance, list) or not all(
                isinstance(item, str) for item in source_provenance
            ):
                raise ValueError("Source provenance must be a list of strings")
            claims[identifier] = ClaimDefinition(
                identifier=identifier,
                prefab=prefab_id,
                module=_required_string(raw_claim, "module"),
                instance=_required_string(raw_claim, "instance"),
                blackbox=_required_string(raw_claim, "blackbox"),
                ports=tuple(ports),
                provider=_required_string(raw_claim, "provider"),
                mapper=_required_string(raw_claim, "mapper"),
                rule=_required_string(raw_claim, "rule"),
                rule_version=rule_version,
                accepted_revision=accepted_revision,
                source_provenance=tuple(source_provenance),
            )

        return cls(
            revision=revision,
            prefabs=MappingProxyType(prefabs),
            claims=MappingProxyType(claims),
        )


def _required_string(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value