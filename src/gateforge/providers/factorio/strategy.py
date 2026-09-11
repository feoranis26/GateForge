from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

from gateforge.behavior import BehaviorGraph
from gateforge.behavior_source import bind_material_behavior
from gateforge.material import MaterialDesign
from gateforge.placement import ScoreBreakdown, ScoreComponent
from gateforge.provider import TargetProvider
from gateforge.providers.factorio.blueprint import build_finalized_factorio_blueprint
from gateforge.providers.factorio.finalization import FactorioFinalizedDesign, finalize_combinational_fabric, physical_connector
from gateforge.providers.factorio.realization import propose_combinational_fabrics
from gateforge.providers.factorio.routed import connector_distance
from gateforge.providers.factorio.routing import parse_factorio_input_value
from gateforge.realization import (
    RealizationArtifact, RealizationError, RealizationInfeasibleError,
    RealizationOptions, RealizationOutcome, RealizationProblem,
)
from gateforge.target import ProviderConfiguration


@dataclass(frozen=True, slots=True)
class FactorioRealizationArtifact:
    finalized: FactorioFinalizedDesign
    behavior: BehaviorGraph

    @property
    def target(self) -> str:
        return self.finalized.target

    def canonical_data(self) -> dict[str, object]:
        return self.finalized.canonical_data()

    def validate(self, material: MaterialDesign, providers: Mapping[str, TargetProvider]) -> None:
        if set(providers) != {self.target}:
            raise RealizationError("Factorio artifact requires its target provider")
        self.finalized.validate(material, self.behavior)


class FactorioRealizationStrategy:
    def normalize_options(self, options: Mapping[str, object]) -> ProviderConfiguration:
        if set(options) - {"input_drivers", "input_values", "output_lamps", "power_layout"}:
            raise RealizationError("Unknown Factorio realization options")
        mode = options.get("input_drivers", "none")
        lamps = options.get("output_lamps", False)
        values = options.get("input_values", {})
        power_layout = options.get("power_layout", "grid")
        if not isinstance(power_layout, str) or power_layout not in {"grid", "compact"}:
            raise RealizationError("Power layout must be grid or compact")
        if not isinstance(mode, str) or mode not in {"none", "constant"} or not isinstance(lamps, bool) or not isinstance(values, Mapping):
            raise RealizationError("Invalid Factorio realization options")
        if values and mode != "constant":
            raise RealizationError("Input values require constant drivers")
        if any(not isinstance(name, str) or not name for name in values):
            raise RealizationError("Input port names must be nonempty strings")
        return ProviderConfiguration.from_canonical_data({
            "input_drivers": mode,
            "input_values": {name: parse_factorio_input_value(value) for name, value in values.items()},
            "output_lamps": lamps,
            "power_layout": power_layout,
        })

    def realize_problem(self, problem: RealizationProblem, options: RealizationOptions):
        capture = problem.behavior
        if capture is None:
            raise RealizationError("Factorio realization requires captured source behavior")
        if problem.boundary != bind_material_behavior(problem.material, capture):
            raise RealizationError("Factorio realization requires checked material behavior bindings")
        settings = self.normalize_options(options.provider_options.canonical_data()).canonical_data()
        fabrics = propose_combinational_fabrics(problem.material, capture)
        failures = []
        produced = False
        for fabric in fabrics:
            try:
                finalized = finalize_combinational_fabric(
                    fabric, problem.material, capture.graph,
                    input_drivers=settings["input_drivers"], input_values=settings["input_values"],
                    output_lamps=settings["output_lamps"],
                    column_pitch=options.placement.column_pitch,
                    row_pitch=options.placement.row_pitch,
                    power_layout=settings["power_layout"],
                )
            except RealizationInfeasibleError as error:
                failures.append(str(error))
                continue
            entities = {item.identifier: item for item in finalized.entities}
            circuit_length = 0.0
            for wire in finalized.wires:
                source = physical_connector(wire.source, entities[wire.source.entity])
                target = physical_connector(wire.target, entities[wire.target.entity])
                circuit_length += connector_distance(entities[source.entity], source.connector, entities[target.entity], target.connector)
            copper_length = sum(math.hypot(
                entities[item.source].x - entities[item.target].x,
                entities[item.source].y - entities[item.target].y,
            ) for item in finalized.power_segments)
            score = ScoreBreakdown((
                ScoreComponent("combinators", len(fabric.additions)),
                ScoreComponent("drivers", sum(item.prototype == "constant-combinator" for item in entities.values())),
                ScoreComponent("lamps", sum(item.prototype == "small-lamp" for item in entities.values())),
                ScoreComponent("poles", sum(item.prototype.endswith("electric-pole") for item in entities.values()), 0.1),
                ScoreComponent("settling_ticks", finalized.settling_ticks, 0.1),
                ScoreComponent("circuit_length", circuit_length, 0.001),
                ScoreComponent("copper_length", copper_length, 0.001),
            ))
            produced = True
            yield RealizationOutcome(FactorioRealizationArtifact(finalized, capture.graph), score)
        if not produced:
            raise RealizationInfeasibleError("; ".join(failures) or "No Factorio fabric candidates")

    def _artifact(self, artifact: RealizationArtifact) -> FactorioFinalizedDesign:
        if not isinstance(artifact, FactorioRealizationArtifact):
            raise RealizationError("Expected a finalized Factorio artifact")
        return artifact.finalized

    def views(self, artifact: RealizationArtifact) -> tuple:
        from gateforge.providers.factorio.visualization import build_finalized_factorio_view
        return (build_finalized_factorio_view(self._artifact(artifact)),)

    def placement(self, artifact: RealizationArtifact):
        return self._artifact(artifact).placement

    def export(self, artifact: RealizationArtifact, *, label: str | None = None) -> dict[str, object]:
        return build_finalized_factorio_blueprint(self._artifact(artifact), label=label).canonical_data()

    def details(self, artifact: RealizationArtifact) -> dict[str, object]:
        finalized = self._artifact(artifact)
        return {
            "entities": len(finalized.entities), "combinators": len(finalized.fabric.additions),
            "settling_ticks": finalized.settling_ticks, "external_supply": finalized.external_supply,
            "domains": len(finalized.domains), "circuit_wires": len(finalized.wires),
            "copper_wires": len(finalized.power_segments),
            "power_layout": finalized.power_layout,
        }