from gateforge.mapping import (
    BoundaryBinding,
    MappingCostEstimate,
    MappingDisposition,
    MappingProposal,
    MappingProvider,
)
from gateforge.material import ImplementationPackaging
from gateforge.providers.factorio.common import (
    FACTORIO_ARITHMETIC_COMBINATOR,
    FACTORIO_CIRCUIT_VALUE,
    FACTORIO_INT32,
    FACTORIO_PROVIDER,
)
from gateforge.providers.factorio.configuration import (
    FactorioArithmeticConfiguration,
    encode_factorio_arithmetic_configuration,
)
from gateforge.source import CellSnapshot, ConstantBit, DesignSnapshot
from gateforge.target import (
    ObjectPortRef,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
)


_WIDTH = 32
_PORTS = (
    ("A", "a", PortDirection.INPUT),
    ("B", "b", PortDirection.INPUT),
    ("Y", "y", PortDirection.OUTPUT),
)


class FactorioAddMapper(MappingProvider):
    provider = FACTORIO_PROVIDER
    stages = frozenset({"source"})
    mapper_id = "factorio.arithmetic.add32"
    rule_version = 1

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        proposals = []
        for module in sorted(design.modules.values(), key=lambda item: item.name):
            for cell in sorted(
                module.cells.values(),
                key=lambda item: item.identifier.name,
            ):
                proposal = self._map_cell(cell, design.revision)
                if proposal is not None:
                    proposals.append(proposal)
        return tuple(proposals)

    def _map_cell(
        self,
        cell: CellSnapshot,
        revision: int,
    ) -> MappingProposal | None:
        if cell.identifier.expected_type != "$add":
            return None
        try:
            a_width = cell.parameter("A_WIDTH").as_unsigned_int()
            b_width = cell.parameter("B_WIDTH").as_unsigned_int()
            y_width = cell.parameter("Y_WIDTH").as_unsigned_int()
            a_signed = cell.parameter("A_SIGNED").as_unsigned_int()
            b_signed = cell.parameter("B_SIGNED").as_unsigned_int()
        except ValueError:
            return None
        if (
            (a_width, b_width, y_width) != (_WIDTH, _WIDTH, _WIDTH)
            or a_signed != 0
            or b_signed != 0
        ):
            return None
        if any(name not in cell.ports for name, _, _ in _PORTS):
            return None
        if any(len(cell.ports[name].bits) != _WIDTH for name, _, _ in _PORTS):
            return None
        if any(
            isinstance(bit, ConstantBit)
            for name, _, _ in _PORTS
            for bit in cell.ports[name].bits
        ):
            return None

        configuration = FactorioArithmeticConfiguration(
            operation="add",
            a_width=a_width,
            b_width=b_width,
            y_width=y_width,
            a_signed=False,
            b_signed=False,
        )
        prefab = SemanticPrefab(
            provider=self.provider,
            objects=frozenset(
                {
                    PrefabObject(
                        "arithmetic",
                        FACTORIO_ARITHMETIC_COMBINATOR,
                        encode_factorio_arithmetic_configuration(configuration),
                    )
                }
            ),
            ports=frozenset(
                PrefabPort(name, direction, FACTORIO_INT32, _WIDTH)
                for name, _, direction in _PORTS
            ),
            nets=frozenset(
                PrefabNet(
                    target_port,
                    FACTORIO_CIRCUIT_VALUE,
                    frozenset(
                        {
                            ObjectPortRef("arithmetic", target_port),
                            *(
                                PrefabPortRef(source_port, bit)
                                for bit in range(_WIDTH)
                            ),
                        }
                    ),
                )
                for source_port, target_port, _ in _PORTS
            ),
        )
        boundary = frozenset(
            BoundaryBinding(
                cell.ports[source_port].bits[bit],
                PrefabPortRef(source_port, bit),
            )
            for source_port, _, _ in _PORTS
            for bit in range(_WIDTH)
        )
        return MappingProposal(
            revision=revision,
            provider=self.provider,
            mapper=self.mapper_id,
            rule="$add",
            rule_version=self.rule_version,
            ids=frozenset({cell.identifier}),
            prefab=prefab,
            boundary=boundary,
            disposition=MappingDisposition.REQUIRED,
            cost=MappingCostEstimate(1.0, 1.0),
            implementation_name="unsigned add32",
            packaging=ImplementationPackaging.INLINE,
        )