from collections.abc import Mapping
from dataclasses import dataclass

from gateforge.mapping import MappingProvider
from gateforge.placement import TopologicalPlacementOptions
from gateforge.provider import TargetProvider


@dataclass(frozen=True, slots=True)
class CompilationBackend:
    identifier: str
    target_providers: Mapping[str, TargetProvider]
    mapping_providers: tuple[MappingProvider, ...]
    placement_options: TopologicalPlacementOptions
    supported_placers: tuple[str, ...] = ("topological",)

    def __post_init__(self) -> None:
        if not self.identifier:
            raise ValueError("Compilation backend identifier must not be empty")
        if set(self.target_providers) != {self.identifier}:
            raise ValueError(
                "Compilation backend must provide exactly its selected target"
            )
        if any(
            provider.provider != self.identifier
            for provider in self.mapping_providers
        ):
            raise ValueError("Compilation backend contains a foreign mapper")
        if not self.supported_placers:
            raise ValueError("Compilation backend requires a placement strategy")