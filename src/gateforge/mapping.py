from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from typing import Sequence

from gateforge.source import (
    BoundarySource,
    CellIdentifier,
    DesignSnapshot,
    SnapshotBitRef,
)
from gateforge.target import PrefabPortRef, SemanticPrefab


class ProposalError(ValueError):
    pass


class MappingDisposition(StrEnum):
    REQUIRED = "required"
    SPECULATIVE = "speculative"


@dataclass(frozen=True, slots=True, order=True)
class MappingCostEstimate:
    lower_bound: float = 0.0
    expected: float = 0.0

    def __post_init__(self) -> None:
        for name in ("lower_bound", "expected"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"Mapping cost {name} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized < 0:
                raise ValueError(
                    f"Mapping cost {name} must be finite and nonnegative"
                )
            object.__setattr__(self, name, normalized)
        if self.lower_bound > self.expected:
            raise ValueError("Mapping cost lower bound cannot exceed expected cost")


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
    disposition: MappingDisposition = MappingDisposition.SPECULATIVE
    cost: MappingCostEstimate = field(default_factory=MappingCostEstimate)

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


@dataclass(frozen=True, slots=True)
class ProposalConflictGraph:
    proposals: tuple[MappingProposal, ...]
    conflicts: tuple[frozenset[int], ...]

    @classmethod
    def from_proposals(
        cls,
        proposals: Sequence[MappingProposal],
    ) -> "ProposalConflictGraph":
        ordered = tuple(sorted(proposals, key=MappingProposal.fingerprint))
        cells = tuple(
            {(identifier.module, identifier.name) for identifier in proposal.ids}
            for proposal in ordered
        )
        conflicts: list[set[int]] = [set() for _ in ordered]
        for left_index, left_cells in enumerate(cells):
            for right_index in range(left_index + 1, len(cells)):
                if left_cells.isdisjoint(cells[right_index]):
                    continue
                conflicts[left_index].add(right_index)
                conflicts[right_index].add(left_index)
        return cls(ordered, tuple(frozenset(items) for items in conflicts))

    def conflicts_with(self, left: int, right: int) -> bool:
        return right in self.conflicts[left]


class ProposalSetEnumerator:
    def __init__(self, graph: ProposalConflictGraph):
        self.graph = graph

    def enumerate(
        self,
        limit: int | None = None,
        required_cells: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[tuple[MappingProposal, ...], ...]:
        if limit is not None and limit <= 0:
            raise ValueError("Proposal set limit must be positive")

        required = tuple(
            index
            for index, proposal in enumerate(self.graph.proposals)
            if proposal.disposition == MappingDisposition.REQUIRED
        )
        for offset, left in enumerate(required):
            for right in required[offset + 1 :]:
                if self.graph.conflicts_with(left, right):
                    raise ProposalError("Required mapping proposals conflict")

        blocked = {
            conflict
            for index in required
            for conflict in self.graph.conflicts[index]
        }
        optional = tuple(
            index
            for index, proposal in enumerate(self.graph.proposals)
            if proposal.disposition == MappingDisposition.SPECULATIVE
            and index not in blocked
        )
        proposal_cells = tuple(
            frozenset(
                (identifier.module, identifier.name)
                for identifier in proposal.ids
            )
            for proposal in self.graph.proposals
        )
        suffix_cells: list[frozenset[tuple[str, str]]] = [frozenset()] * (
            len(optional) + 1
        )
        for position in range(len(optional) - 1, -1, -1):
            suffix_cells[position] = (
                suffix_cells[position + 1] | proposal_cells[optional[position]]
            )
        results: list[tuple[MappingProposal, ...]] = []

        required_coverage = frozenset(
            cell for index in required for cell in proposal_cells[index]
        )

        def visit(
            position: int,
            selected: tuple[int, ...],
            coverage: frozenset[tuple[str, str]],
        ) -> None:
            if limit is not None and len(results) >= limit:
                return
            if not required_cells.issubset(coverage | suffix_cells[position]):
                return
            if position == len(optional):
                if not required_cells.issubset(coverage):
                    return
                indices = tuple(sorted((*required, *selected)))
                results.append(tuple(self.graph.proposals[index] for index in indices))
                return

            candidate = optional[position]
            if all(
                not self.graph.conflicts_with(candidate, existing)
                for existing in (*required, *selected)
            ):
                visit(
                    position + 1,
                    (*selected, candidate),
                    coverage | proposal_cells[candidate],
                )
            visit(position + 1, selected, coverage)

        visit(0, (), required_coverage)
        return tuple(results)

class Mapper:
    def __init__(self, mappers: Sequence[MappingProvider]):
        self.mappers = tuple(mappers)

    def combine(
        self,
        proposals: Sequence[MappingProposal],
        revision: int,
    ) -> list[MappingProposal]:
        self._validate_proposals(proposals, revision)
        ProposalSetEnumerator(
            ProposalConflictGraph.from_proposals(proposals)
        ).enumerate(limit=1)

        ordered = sorted(
            proposals,
            key=lambda proposal: (
                proposal.disposition != MappingDisposition.REQUIRED,
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

    def _validate_proposals(
        self,
        proposals: Sequence[MappingProposal],
        revision: int,
    ) -> None:
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

    def collect_proposals(
        self,
        design: DesignSnapshot,
    ) -> tuple[MappingProposal, ...]:
        proposals = tuple(
            proposal
            for mapper in self.mappers
            for proposal in mapper.map(design)
        )
        self._validate_proposals(proposals, design.revision)
        return tuple(sorted(proposals, key=MappingProposal.fingerprint))

    def conflict_graph(self, design: DesignSnapshot) -> ProposalConflictGraph:
        return ProposalConflictGraph.from_proposals(self.collect_proposals(design))

    def map_design(self, design: DesignSnapshot) -> list[MappingProposal]:
        return self.combine(self.collect_proposals(design), design.revision)
