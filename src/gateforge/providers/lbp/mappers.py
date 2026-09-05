from gateforge.mapping import BoundaryBinding, MappingProposal, MappingProvider
from gateforge.providers.lbp.common import (
    LBP_LOGIC,
    LBP_PROVIDER,
    LBP_WIRE,
)
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPNotGateType,
    LBPOrGateType,
)
from gateforge.source import (
    CellPortIdentifier,
    CellSnapshot,
    ConstantBit,
    ConstantBoundarySource,
    DesignSnapshot,
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


_BINARY_PORT_BINDINGS = (
    ("A", "IN_0", PortDirection.INPUT),
    ("B", "IN_1", PortDirection.INPUT),
    ("Y", "OUT", PortDirection.OUTPUT),
)
_UNARY_PORT_BINDINGS = (
    ("A", "IN_0", PortDirection.INPUT),
    ("Y", "OUT", PortDirection.OUTPUT),
)


class LBPCombinatorialLowLevelGateMapper(MappingProvider):
    provider = LBP_PROVIDER
    mapper_id = "lbp.combinatorial.low_level"
    rule_version = 2
    tcell_types = frozenset(
        {
            "$_AND_",
            "$_ANDNOT_",
            "$_NAND_",
            "$_OR_",
            "$_NOR_",
            "$_NOT_",
            "$_BUF_",
        }
    )

    def _source_binding(
        self,
        cell: CellSnapshot,
        source_port: str,
        target_port: str,
    ) -> BoundaryBinding:
        port = cell.ports[source_port]
        if len(port.bits) != 1:
            raise ValueError(
                f"Low-level cell port {cell.identifier.name}.{source_port} "
                f"has width {len(port.bits)}"
            )
        source = port.bits[0]
        if isinstance(source, ConstantBit):
            source = ConstantBoundarySource(
                source.value,
                CellPortIdentifier(cell.identifier, source_port, 0),
            )
        return BoundaryBinding(source, PrefabPortRef(target_port))

    def _single_gate_prefab(
        self,
        gate_type: LBPAndGateType | LBPOrGateType | LBPNotGateType,
        port_bindings: tuple[tuple[str, str, PortDirection], ...],
    ) -> SemanticPrefab:
        return SemanticPrefab(
            provider=self.provider,
            objects=frozenset({PrefabObject("gate", gate_type.get_type())}),
            ports=frozenset(
                PrefabPort(source_port, direction, LBP_LOGIC)
                for source_port, _, direction in port_bindings
            ),
            nets=frozenset(
                PrefabNet(
                    source_port.lower(),
                    LBP_WIRE,
                    frozenset(
                        {
                            PrefabPortRef(source_port),
                            ObjectPortRef("gate", object_port),
                        }
                    ),
                )
                for source_port, object_port, _ in port_bindings
            ),
        )

    def _andnot_prefab(self) -> SemanticPrefab:
        not_gate = LBPNotGateType(width=1, invert=False)
        and_gate = LBPAndGateType(width=2, invert=False)
        return SemanticPrefab(
            provider=self.provider,
            objects=frozenset(
                {
                    PrefabObject("invert_b", not_gate.get_type()),
                    PrefabObject("result", and_gate.get_type()),
                }
            ),
            ports=frozenset(
                {
                    PrefabPort("A", PortDirection.INPUT, LBP_LOGIC),
                    PrefabPort("B", PortDirection.INPUT, LBP_LOGIC),
                    PrefabPort("Y", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
            nets=frozenset(
                {
                    PrefabNet(
                        "a",
                        LBP_WIRE,
                        frozenset(
                            {PrefabPortRef("A"), ObjectPortRef("result", "IN_0")}
                        ),
                    ),
                    PrefabNet(
                        "b",
                        LBP_WIRE,
                        frozenset(
                            {PrefabPortRef("B"), ObjectPortRef("invert_b", "IN_0")}
                        ),
                    ),
                    PrefabNet(
                        "inverted_b",
                        LBP_WIRE,
                        frozenset(
                            {
                                ObjectPortRef("invert_b", "OUT"),
                                ObjectPortRef("result", "IN_1"),
                            }
                        ),
                    ),
                    PrefabNet(
                        "y",
                        LBP_WIRE,
                        frozenset(
                            {ObjectPortRef("result", "OUT"), PrefabPortRef("Y")}
                        ),
                    ),
                }
            ),
        )

    def _prefab_for_cell(self, cell_type: str) -> SemanticPrefab | None:
        if cell_type == "$_AND_":
            return self._single_gate_prefab(
                LBPAndGateType(width=2, invert=False), _BINARY_PORT_BINDINGS
            )
        if cell_type == "$_ANDNOT_":
            return self._andnot_prefab()
        if cell_type == "$_NAND_":
            return self._single_gate_prefab(
                LBPAndGateType(width=2, invert=True), _BINARY_PORT_BINDINGS
            )
        if cell_type == "$_OR_":
            return self._single_gate_prefab(
                LBPOrGateType(width=2, invert=False), _BINARY_PORT_BINDINGS
            )
        if cell_type == "$_NOR_":
            return self._single_gate_prefab(
                LBPOrGateType(width=2, invert=True), _BINARY_PORT_BINDINGS
            )
        if cell_type == "$_NOT_":
            return self._single_gate_prefab(
                LBPNotGateType(width=1, invert=False), _UNARY_PORT_BINDINGS
            )
        if cell_type == "$_BUF_":
            return self._single_gate_prefab(
                LBPNotGateType(width=1, invert=True), _UNARY_PORT_BINDINGS
            )
        return None

    def _map_cell(self, cell: CellSnapshot, revision: int) -> MappingProposal | None:
        prefab = self._prefab_for_cell(cell.identifier.expected_type)
        if prefab is None:
            return None
        port_names = tuple(sorted(port.name for port in prefab.ports))
        if any(name not in cell.ports for name in port_names):
            return None
        boundary = frozenset(
            self._source_binding(cell, port_name, port_name)
            for port_name in port_names
        )
        return MappingProposal(
            revision=revision,
            provider=self.provider,
            mapper=self.mapper_id,
            rule=cell.identifier.expected_type,
            rule_version=self.rule_version,
            ids=frozenset({cell.identifier}),
            prefab=prefab,
            boundary=boundary,
        )

    def map(self, design: DesignSnapshot) -> list[MappingProposal]:
        proposals: list[MappingProposal] = []
        for module in sorted(design.modules.values(), key=lambda item: item.name):
            for cell in sorted(
                module.cells.values(), key=lambda item: item.identifier.name
            ):
                if cell.identifier.expected_type not in self.tcell_types:
                    continue
                proposal = self._map_cell(cell, design.revision)
                if proposal is not None:
                    proposals.append(proposal)
        return proposals