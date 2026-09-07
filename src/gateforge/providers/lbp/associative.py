from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gateforge.mapping import (
    BoundaryBinding,
    MappingCostEstimate,
    MappingProposal,
    MappingProvider,
)
from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPOrGateType,
    LBPXorGateType,
    LBPCombinatorialVariableWidthGateType,
    LBPNotGateType,
)
from gateforge.source import (
    BoundarySource,
    CellPortIdentifier,
    CellSnapshot,
    ConstantBit,
    ConstantBoundarySource,
    DesignSnapshot,
    ModuleDependencyGraph,
    ModuleSnapshot,
    SnapshotBitRef,
)
from gateforge.target import (
    ObjectPortRef,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
)


class AssociativeFamily(StrEnum):
    AND = "AND"
    OR = "OR"
    XOR = "XOR"


@dataclass(frozen=True, slots=True)
class _RootSpec:
    family: AssociativeFamily
    inverted: bool
    inputs: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class _SignedBoundary:
    source: BoundarySource
    negated: bool


_ROOT_SPECS = {
    "$_AND_": _RootSpec(AssociativeFamily.AND, False, (("A", False), ("B", False))),
    "$_ANDNOT_": _RootSpec(AssociativeFamily.AND, False, (("A", False), ("B", True))),
    "$_NAND_": _RootSpec(AssociativeFamily.AND, True, (("A", False), ("B", False))),
    "$_OR_": _RootSpec(AssociativeFamily.OR, False, (("A", False), ("B", False))),
    "$_ORNOT_": _RootSpec(AssociativeFamily.OR, False, (("A", False), ("B", True))),
    "$_NOR_": _RootSpec(AssociativeFamily.OR, True, (("A", False), ("B", False))),
    "$_XOR_": _RootSpec(AssociativeFamily.XOR, False, (("A", False), ("B", False))),
    "$_XNOR_": _RootSpec(AssociativeFamily.XOR, True, (("A", False), ("B", False))),
}
_POSITIVE_CELL = {
    AssociativeFamily.AND: "$_AND_",
    AssociativeFamily.OR: "$_OR_",
    AssociativeFamily.XOR: "$_XOR_",
}


def _gate_type(
    family: AssociativeFamily,
    width: int,
    inverted: bool,
) -> LBPCombinatorialVariableWidthGateType:
    if family == AssociativeFamily.AND:
        return LBPAndGateType(width=width, invert=inverted)
    if family == AssociativeFamily.OR:
        return LBPOrGateType(width=width, invert=inverted)
    return LBPXorGateType(width=width, invert=inverted)


def _port_source(
    cell: CellSnapshot,
    port_name: str,
) -> tuple[BoundarySource, CellPortIdentifier]:
    port = cell.ports[port_name]
    if len(port.bits) != 1:
        raise ValueError(
            f"Associative cell port {cell.identifier.name}.{port_name} "
            f"has width {len(port.bits)}"
        )
    endpoint = CellPortIdentifier(cell.identifier, port_name, 0)
    source = port.bits[0]
    if isinstance(source, ConstantBit):
        return ConstantBoundarySource(source.value, endpoint), endpoint
    return source, endpoint


def _wide_prefab(
    spec: _RootSpec,
    literals: tuple[_SignedBoundary, ...],
) -> tuple[SemanticPrefab, dict[BoundarySource, PrefabPortRef]]:
    sources = tuple(dict.fromkeys(literal.source for literal in literals))
    source_ports = {
        source: PrefabPortRef(f"IN_{index}")
        for index, source in enumerate(sources)
    }
    objects = {
        PrefabObject(
            "result",
            _gate_type(spec.family, len(literals), spec.inverted).get_type(),
        )
    }
    ports = {
        PrefabPort(reference.port, PortDirection.INPUT, LBP_LOGIC)
        for reference in source_ports.values()
    }
    ports.add(PrefabPort("Y", PortDirection.OUTPUT, LBP_LOGIC))
    nets: set[PrefabNet] = {
        PrefabNet(
            "y",
            LBP_WIRE,
            frozenset({ObjectPortRef("result", "OUT"), PrefabPortRef("Y")}),
        )
    }

    for source_index, source in enumerate(sources):
        positive_inputs = {
            ObjectPortRef("result", f"IN_{literal_index}")
            for literal_index, literal in enumerate(literals)
            if literal.source == source and not literal.negated
        }
        negative_inputs = {
            ObjectPortRef("result", f"IN_{literal_index}")
            for literal_index, literal in enumerate(literals)
            if literal.source == source and literal.negated
        }
        input_attachments = {source_ports[source], *positive_inputs}
        if negative_inputs:
            inverter_role = f"invert_{source_index}"
            objects.add(
                PrefabObject(
                    inverter_role,
                    LBPNotGateType(width=1, invert=False).get_type(),
                )
            )
            input_attachments.add(ObjectPortRef(inverter_role, "IN_0"))
            nets.add(
                PrefabNet(
                    f"negative_{source_index}",
                    LBP_WIRE,
                    frozenset(
                        {ObjectPortRef(inverter_role, "OUT"), *negative_inputs}
                    ),
                )
            )
        nets.add(
            PrefabNet(
                f"input_{source_index}",
                LBP_WIRE,
                frozenset(input_attachments),
            )
        )

    return (
        SemanticPrefab(
            provider=LBP_PROVIDER,
            objects=frozenset(objects),
            ports=frozenset(ports),
            nets=frozenset(nets),
        ),
        source_ports,
    )


class LBPAssociativeConeMapper(MappingProvider):
    provider = LBP_PROVIDER
    mapper_id = "lbp.combinatorial.associative"
    rule_version = 1

    def __init__(self, max_arity: int = 32):
        if max_arity < 3:
            raise ValueError("Associative mapper max arity must be at least three")
        self.max_arity = max_arity

    def _proposal(
        self,
        module: ModuleSnapshot,
        graph: ModuleDependencyGraph,
        root: CellSnapshot,
        spec: _RootSpec,
    ) -> MappingProposal | None:
        claimed = {root.identifier}
        literals: list[_SignedBoundary] = []

        def expand(cell: CellSnapshot, port_name: str, negated: bool) -> None:
            source, endpoint = _port_source(cell, port_name)
            if not negated and isinstance(source, SnapshotBitRef):
                driver = graph.driver(source)
                if (
                    isinstance(driver, CellPortIdentifier)
                    and driver.name == "Y"
                    and graph.signal_consumers(source) == frozenset({endpoint})
                ):
                    child = module.cells[driver.cell.name]
                    if (
                        child.identifier not in claimed
                        and child.identifier.expected_type
                        == _POSITIVE_CELL[spec.family]
                    ):
                        claimed.add(child.identifier)
                        expand(child, "A", False)
                        expand(child, "B", False)
                        return
            literals.append(_SignedBoundary(source, negated))

        for port_name, negated in spec.inputs:
            expand(root, port_name, negated)
        if len(claimed) < 2 or len(literals) > self.max_arity:
            return None

        output_source, _ = _port_source(root, "Y")
        if not isinstance(output_source, SnapshotBitRef):
            return None
        prefab, source_ports = _wide_prefab(spec, tuple(literals))
        cut_sources = {cut.source for cut in module.derive_cut(frozenset(claimed))}
        expected_sources = {*source_ports, output_source}
        if cut_sources != expected_sources:
            return None
        boundary = {
            BoundaryBinding(source, target)
            for source, target in source_ports.items()
        }
        boundary.add(BoundaryBinding(output_source, PrefabPortRef("Y")))
        object_cost = float(len(prefab.objects))
        return MappingProposal(
            revision=module.revision,
            provider=self.provider,
            mapper=self.mapper_id,
            rule=f"wide_{spec.family.value.lower()}",
            rule_version=self.rule_version,
            ids=frozenset(claimed),
            prefab=prefab,
            boundary=frozenset(boundary),
            priority=1,
            score=len(claimed),
            cost=MappingCostEstimate(object_cost, object_cost),
        )

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        proposals: list[MappingProposal] = []
        for module in sorted(design.modules.values(), key=lambda item: item.name):
            if "blackbox" in module.attributes:
                continue
            graph = ModuleDependencyGraph.from_module(module)
            for cell in sorted(
                module.cells.values(), key=lambda item: item.identifier.name
            ):
                spec = _ROOT_SPECS.get(cell.identifier.expected_type)
                if spec is None:
                    continue
                proposal = self._proposal(module, graph, cell, spec)
                if proposal is not None:
                    proposals.append(proposal)
        return tuple(proposals)