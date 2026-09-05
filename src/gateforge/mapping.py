from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import json
from typing import Sequence

from gateforge.source import (
    BoundarySource,
    CellIdentifier,
    ConstantBoundarySource,
    DesignSnapshot,
    SnapshotBitRef,
)
from gateforge.target import PrefabPortRef, SemanticPrefab


class ProposalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoundaryBinding:
    source: BoundarySource
    target: PrefabPortRef


def _boundary_source_data(source: BoundarySource) -> dict[str, object]:
    if isinstance(source, SnapshotBitRef):
        return {
            "kind": "bit",
            "revision": source.revision,
            "module": source.module,
            "bit_id": source.bit_id,
        }
    return {
        "kind": "constant",
        "value": source.value.value,
        "consumer": {
            "module": source.consumer.cell.module,
            "cell": source.consumer.cell.name,
            "type": source.consumer.cell.expected_type,
            "port": source.consumer.name,
            "bit": source.consumer.bit,
        },
    }


@dataclass(frozen=True, slots=True)
class MappingProposal:
    revision: int
    provider: str
    mapper: str
    rule: str
    rule_version: int
    ids: frozenset[CellIdentifier]
    prefab: SemanticPrefab
    boundary: frozenset[BoundaryBinding]
    priority: int = 0
    score: int = 0

    def fingerprint(self) -> str:
        data = {
            "revision": self.revision,
            "provider": self.provider,
            "mapper": self.mapper,
            "rule": self.rule,
            "rule_version": self.rule_version,
            "ids": [
                {
                    "module": item.module,
                    "name": item.name,
                    "type": item.expected_type,
                }
                for item in sorted(self.ids)
            ],
            "prefab": self.prefab.get_id().value,
            "boundary": sorted(
                (
                    {
                        "source": _boundary_source_data(binding.source),
                        "target": {
                            "port": binding.target.port,
                            "bit": binding.target.bit,
                        },
                    }
                    for binding in self.boundary
                ),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        }
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()


class MappingProvider(ABC):
    provider: str

    @abstractmethod
    def map(self, design: DesignSnapshot) -> Sequence[MappingProposal]:
        raise NotImplementedError

class Mapper:
    def __init__(self, mappers: Sequence[MappingProvider]):
        self.mappers = tuple(mappers)

    def combine(
        self,
        proposals: Sequence[MappingProposal],
        revision: int,
    ) -> list[MappingProposal]:
        for proposal in proposals:
            if proposal.revision != revision:
                raise ProposalError(
                    f"Proposal revision {proposal.revision} does not match "
                    f"snapshot revision {revision}"
                )
            if not proposal.ids:
                raise ProposalError("A mapping proposal must claim at least one cell")
            modules = {cell.module for cell in proposal.ids}
            if len(modules) != 1:
                raise ProposalError("A mapping proposal must be module-local")
            if proposal.provider != proposal.prefab.provider:
                raise ProposalError(
                    f"Provider {proposal.provider!r} proposed a "
                    f"{proposal.prefab.provider!r} prefab"
                )

        ordered = sorted(
            proposals,
            key=lambda proposal: (
                -proposal.priority,
                -proposal.score,
                proposal.fingerprint(),
            ),
        )
        accepted: list[MappingProposal] = []
        seen_cells: set[tuple[str, str]] = set()
        for proposal in ordered:
            cells = {(cell.module, cell.name) for cell in proposal.ids}
            if seen_cells.isdisjoint(cells):
                accepted.append(proposal)
                seen_cells.update(cells)
        return accepted

    def map_design(self, design: DesignSnapshot) -> list[MappingProposal]:
        proposals = [
            proposal
            for mapper in self.mappers
            for proposal in mapper.map(design)
        ]
        return self.combine(proposals, design.revision)
