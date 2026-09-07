from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from typing import Protocol

from gateforge.claims import accept_mapping_proposals
from gateforge.design import DesignCheckpoint, DesignContext
from gateforge.mapping import (
    Mapper,
    ProposalConflictGraph,
    ProposalSetEnumerator,
)
from gateforge.provider import TargetProvider
from gateforge.source import CellIdentifier, DesignSnapshot
from gateforge.state import CompilationIntermediateState


class MappingSearchError(RuntimeError):
    pass


class MappingSearchMode(StrEnum):
    GREEDY = "greedy"
    BEAM = "beam"
    EXHAUSTIVE = "exhaustive"


class MappingSearchStage(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def passes(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class MappingSearchOptions:
    mode: MappingSearchMode = MappingSearchMode.BEAM
    beam_width: int = 16
    stage_alternative_limit: int = 16

    def __post_init__(self) -> None:
        for name in ("beam_width", "stage_alternative_limit"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Mapping search {name} must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class CompilationCandidateId:
    value: str


@dataclass(frozen=True, slots=True)
class MappingDecision:
    stage: str
    accepted: tuple[str, ...]
    deferred: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompilationCandidate:
    checkpoint: DesignCheckpoint
    state: CompilationIntermediateState
    stage_index: int = 0
    decisions: tuple[MappingDecision, ...] = ()
    lower_bound: float = 0.0
    expected_cost: float = 0.0
    identifier: CompilationCandidateId = field(init=False)

    def __post_init__(self) -> None:
        if self.checkpoint.revision != self.state.revision:
            raise ValueError(
                "Candidate checkpoint and compilation state revisions differ"
            )
        if self.stage_index < 0:
            raise ValueError("Candidate stage index must be nonnegative")
        for name in ("lower_bound", "expected_cost"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Candidate {name} must be finite and nonnegative")
        if self.lower_bound > self.expected_cost:
            raise ValueError("Candidate lower bound cannot exceed expected cost")
        payload = {
            "stage_index": self.stage_index,
            "checkpoint": self.checkpoint.get_digest(),
            "state": self.state.get_digest(),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(
            self,
            "identifier",
            CompilationCandidateId(hashlib.sha256(encoded).hexdigest()),
        )

    def restore_context(self) -> DesignContext:
        return self.checkpoint.restore()


@dataclass(frozen=True, slots=True)
class MappingSearchStageReport:
    stage: str
    input_candidates: int
    generated_candidates: int
    deduplicated_candidates: int
    pruned_candidates: int
    output_candidates: int

    def canonical_data(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "input_candidates": self.input_candidates,
            "generated_candidates": self.generated_candidates,
            "deduplicated_candidates": self.deduplicated_candidates,
            "pruned_candidates": self.pruned_candidates,
            "output_candidates": self.output_candidates,
        }


@dataclass(frozen=True, slots=True)
class TerminalCandidateScore:
    candidate: CompilationCandidateId
    material_digest: str
    provider_object_cost: float
    object_count: int
    decisions: tuple[MappingDecision, ...]

    def canonical_data(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.value,
            "material_digest": self.material_digest,
            "score": {
                "provider_object_cost": self.provider_object_cost,
                "object_count": self.object_count,
            },
            "decisions": [
                {
                    "stage": decision.stage,
                    "accepted": list(decision.accepted),
                    "deferred": list(decision.deferred),
                }
                for decision in self.decisions
            ],
        }


@dataclass(frozen=True, slots=True)
class MappingSearchReport:
    stages: tuple[MappingSearchStageReport, ...]
    terminal_scores: tuple[TerminalCandidateScore, ...] = ()
    winner: CompilationCandidateId | None = None

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "stages": [stage.canonical_data() for stage in self.stages],
            "terminal_candidates": [
                score.canonical_data() for score in self.terminal_scores
            ],
            "winner": None if self.winner is None else self.winner.value,
        }


@dataclass(frozen=True, slots=True)
class MappingSearchResult:
    candidates: tuple[CompilationCandidate, ...]
    report: MappingSearchReport


def residual_cells(snapshot: DesignSnapshot) -> frozenset[CellIdentifier]:
    module_names = set(snapshot.modules)
    return frozenset(
        cell.identifier
        for module in snapshot.modules.values()
        if "blackbox" not in module.attributes
        for cell in module.cells.values()
        if "gateforge_prefab" not in cell.attributes
        and cell.identifier.expected_type not in module_names
        and cell.identifier.expected_type != "$scopeinfo"
    )


class CompilationSearch:
    def __init__(
        self,
        mapper: Mapper,
        providers: Mapping[str, TargetProvider],
        options: MappingSearchOptions = MappingSearchOptions(),
    ) -> None:
        self.mapper = mapper
        self.providers = providers
        self.options = options

    def run(
        self,
        context: DesignContext,
        state: CompilationIntermediateState,
        stages: Sequence[MappingSearchStage],
    ) -> MappingSearchResult:
        if context.revision != state.revision:
            raise ValueError(
                f"State revision {state.revision} does not match design revision "
                f"{context.revision}"
            )
        frontier = (
            CompilationCandidate(
                checkpoint=context.checkpoint(),
                state=state,
            ),
        )
        reports: list[MappingSearchStageReport] = []
        for stage_index, stage in enumerate(stages):
            generated = tuple(
                candidate
                for source in frontier
                for candidate in self._expand_candidate(
                    source,
                    stage,
                    stage_index == len(stages) - 1,
                )
            )
            deduplicated = self._deduplicate(generated)
            ordered = tuple(sorted(deduplicated, key=self._candidate_order))
            if self.options.mode == MappingSearchMode.BEAM:
                next_frontier = ordered[: self.options.beam_width]
            elif self.options.mode == MappingSearchMode.GREEDY:
                next_frontier = ordered[:1]
            else:
                next_frontier = ordered
            reports.append(
                MappingSearchStageReport(
                    stage=stage.name,
                    input_candidates=len(frontier),
                    generated_candidates=len(generated),
                    deduplicated_candidates=len(generated) - len(deduplicated),
                    pruned_candidates=len(ordered) - len(next_frontier),
                    output_candidates=len(next_frontier),
                )
            )
            if not next_frontier:
                raise MappingSearchError(
                    f"Mapping search produced no candidates at stage {stage.name!r}"
                )
            frontier = next_frontier
        return MappingSearchResult(frontier, MappingSearchReport(tuple(reports)))

    def _expand_candidate(
        self,
        candidate: CompilationCandidate,
        stage: MappingSearchStage,
        terminal: bool,
    ) -> tuple[CompilationCandidate, ...]:
        context = candidate.restore_context()
        state = candidate.state
        for command in stage.passes:
            context.run_pass(command)
            state = state.with_revision(context.revision)

        snapshot = context.snapshot()
        proposals = self.mapper.collect_proposals(snapshot, stage.name)
        if self.options.mode == MappingSearchMode.GREEDY:
            alternatives = (tuple(self.mapper.combine(proposals, snapshot.revision)),)
        else:
            required = frozenset(
                (identifier.module, identifier.name)
                for identifier in residual_cells(snapshot)
            ) if terminal else frozenset()
            alternatives = ProposalSetEnumerator(
                ProposalConflictGraph.from_proposals(proposals)
            ).enumerate(
                limit=self.options.stage_alternative_limit,
                required_cells=required,
            )

        if terminal and self.options.mode != MappingSearchMode.GREEDY:
            required = residual_cells(snapshot)
            alternatives = tuple(
                selected
                for selected in alternatives
                if required.issubset(
                    identifier
                    for proposal in selected
                    for identifier in proposal.ids
                )
            )

        post_pass_checkpoint = context.checkpoint()
        proposal_fingerprints = {
            proposal.fingerprint(): proposal for proposal in proposals
        }
        expanded: list[CompilationCandidate] = []
        for selected in alternatives:
            branch = post_pass_checkpoint.restore()
            branch_state = state
            if selected:
                branch_state = accept_mapping_proposals(
                    branch,
                    branch_state,
                    selected,
                    self.providers,
                )
            accepted = tuple(sorted(proposal.fingerprint() for proposal in selected))
            deferred = tuple(
                sorted(set(proposal_fingerprints) - set(accepted))
            )
            expanded.append(
                CompilationCandidate(
                    checkpoint=branch.checkpoint(),
                    state=branch_state,
                    stage_index=candidate.stage_index + 1,
                    decisions=(
                        *candidate.decisions,
                        MappingDecision(stage.name, accepted, deferred),
                    ),
                    lower_bound=candidate.lower_bound
                    + sum(proposal.cost.lower_bound for proposal in selected),
                    expected_cost=candidate.expected_cost
                    + sum(proposal.cost.expected for proposal in selected),
                )
            )
        return tuple(expanded)

    @staticmethod
    def _candidate_order(
        candidate: CompilationCandidate,
    ) -> tuple[float, float, str]:
        return (
            candidate.expected_cost,
            candidate.lower_bound,
            candidate.identifier.value,
        )

    @classmethod
    def _deduplicate(
        cls,
        candidates: Sequence[CompilationCandidate],
    ) -> tuple[CompilationCandidate, ...]:
        unique: dict[CompilationCandidateId, CompilationCandidate] = {}
        for candidate in candidates:
            existing = unique.get(candidate.identifier)
            if existing is None or cls._candidate_order(candidate) < cls._candidate_order(
                existing
            ):
                unique[candidate.identifier] = candidate
        return tuple(unique.values())