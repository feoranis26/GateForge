from __future__ import annotations

from collections.abc import Mapping

from gateforge.graph import MaterialGraph
from gateforge.material import MaterialDesign
from gateforge.placement import PlacedDesign
from gateforge.provider import TargetProvider
from gateforge.providers.lbp.plan import LbpPlanDesign
from gateforge.providers.lbp.realize import serialize_lbp_placed_design


def build_lbp_plan(
    material: MaterialDesign,
    graph: MaterialGraph,
    placed: PlacedDesign,
    providers: Mapping[str, TargetProvider],
    *,
    title: str | None = None,
    description: str | None = None,
    creator: str | None = None,
) -> LbpPlanDesign:
    del providers
    return serialize_lbp_placed_design(
        material,
        graph,
        placed,
        title=title,
        description=description,
        creator=creator,
    )