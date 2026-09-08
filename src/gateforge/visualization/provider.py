from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

from gateforge.visualization.model import VisualElementDescriptor, VisualView

if TYPE_CHECKING:
    from gateforge.graph import MaterialGraph
    from gateforge.material import MaterialDesign, MaterialObject
    from gateforge.placement import PlacedDesign
    from gateforge.provider import TargetProvider
    from gateforge.target import TargetTypeRegistry


@dataclass(frozen=True, slots=True)
class VisualizationRequest:
    material: MaterialDesign
    graph: MaterialGraph
    placement: PlacedDesign
    providers: Mapping[str, TargetProvider]
    options: Mapping[str, object]


class VisualizationAdapter(Protocol):
    def describe_material_object(
        self,
        material_object: MaterialObject,
        registry: TargetTypeRegistry,
    ) -> VisualElementDescriptor: ...

    def build_realized_views(
        self,
        request: VisualizationRequest,
    ) -> tuple[VisualView, ...]: ...