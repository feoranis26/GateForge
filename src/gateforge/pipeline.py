from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gateforge.claims import accept_mapping_proposals
from gateforge.design import DesignContext
from gateforge.mapping import Mapper
from gateforge.state import CompilationIntermediateState
from gateforge.target import TargetProvider


@dataclass(frozen=True, slots=True)
class MappingStage:
    name: str
    passes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Mapping stage name must not be empty")
        if any(not command for command in self.passes):
            raise ValueError(f"Mapping stage {self.name!r} contains an empty pass")


def default_mapping_stages(use_abc: bool = True) -> tuple[MappingStage, ...]:
    leaf_passes = ["techmap"]
    if use_abc:
        leaf_passes.append("abc")
    leaf_passes.append("attrmvcp -copy -attr gateforge_id")
    return (
        MappingStage(
            "source",
            ("attrmvcp -copy -attr gateforge_id",),
        ),
        MappingStage(
            "extracted-fsm",
            (
                "fsm -nomap",
                "opt -full",
                "attrmvcp -copy -attr gateforge_id",
            ),
        ),
        MappingStage(
            "post-fsm",
            (
                "fsm_map",
                "opt_clean",
                "attrmvcp -copy -attr gateforge_id",
            ),
        ),
        MappingStage("leaf", tuple(leaf_passes)),
    )


def run_mapping_stages(
    context: DesignContext,
    state: CompilationIntermediateState,
    stages: Sequence[MappingStage],
    mapper: Mapper,
    providers: Mapping[str, TargetProvider],
) -> CompilationIntermediateState:
    if state.revision != context.revision:
        raise ValueError(
            f"State revision {state.revision} does not match design revision "
            f"{context.revision}"
        )

    for stage in stages:
        for command in stage.passes:
            context.run_pass(command)
            state = state.with_revision(context.revision)

        snapshot = context.snapshot()
        proposals = mapper.map_design(snapshot)
        if proposals:
            state = accept_mapping_proposals(
                context,
                state,
                proposals,
                providers,
            )

    return state