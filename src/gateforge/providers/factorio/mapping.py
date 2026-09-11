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
from gateforge.source import CellSnapshot, ConstantBit, ConstantBoundarySource, ConstantValue, DesignSnapshot, CellPortIdentifier
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
        if cell.identifier.expected_type == "$mux":
            return self._map_mux(cell, revision)
        operation = {"$add": "add", "$and": "and", "$or": "or", "$xor": "xor", "$not": "not", "$eq": "eq", "$ne": "ne", "$lt": "lt", "$le": "le", "$gt": "gt", "$ge": "ge", "$logic_not": "logic_not", "$logic_and": "logic_and", "$logic_or": "logic_or", "$reduce_bool": "reduce_bool"}.get(cell.identifier.expected_type)
        if operation is None:
            return None
        unary = operation in {"not", "logic_not", "reduce_bool"}
        ports = tuple(item for item in _PORTS if not unary or item[0] != "B")
        try:
            a_width = cell.parameter("A_WIDTH").as_unsigned_int()
            b_width = cell.parameter("B_WIDTH").as_unsigned_int() if not unary else a_width
            y_width = cell.parameter("Y_WIDTH").as_unsigned_int()
            a_signed = cell.parameter("A_SIGNED").as_unsigned_int()
            b_signed = cell.parameter("B_SIGNED").as_unsigned_int() if not unary else 0
        except ValueError:
            return None
        if (
            any(width not in {1, _WIDTH} for width in (a_width, b_width, y_width))
            or a_signed not in {0, 1}
            or b_signed not in {0, 1}
        ):
            return None
        if any(name not in cell.ports for name, _, _ in ports):
            return None
        widths = {"A": a_width, "B": b_width, "Y": y_width}
        if any(len(cell.ports[name].bits) != widths[name] for name, _, _ in ports):
            return None
        if any(
            isinstance(bit, ConstantBit) and (name == "Y" or bit.value not in {ConstantValue.ZERO, ConstantValue.ONE})
            for name, _, _ in ports
            for bit in cell.ports[name].bits
        ):
            return None

        configuration = FactorioArithmeticConfiguration(
            operation=operation,
            a_width=a_width,
            b_width=b_width,
            y_width=y_width,
            a_signed=bool(a_signed),
            b_signed=bool(b_signed),
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
                PrefabPort(name, direction, FACTORIO_INT32, widths[name])
                for name, _, direction in ports
            ),
            nets=frozenset(
                PrefabNet(
                    target_port,
                    FACTORIO_CIRCUIT_VALUE,
                    frozenset(
                        {
                            ObjectPortRef("arithmetic", target_port),
                            *({ObjectPortRef("arithmetic", "b")} if unary and target_port == "a" else set()),
                            *(
                                PrefabPortRef(source_port, bit)
                                for bit in range(widths[source_port])
                            ),
                        }
                    ),
                )
                for source_port, target_port, _ in ports
            ),
        )
        boundary = frozenset(
            BoundaryBinding(
                ConstantBoundarySource(cell.ports[source_port].bits[bit].value, CellPortIdentifier(cell.identifier, source_port, bit))
                if isinstance(cell.ports[source_port].bits[bit], ConstantBit) else cell.ports[source_port].bits[bit],
                PrefabPortRef(source_port, bit),
            )
            for source_port, _, _ in ports
            for bit in range(widths[source_port])
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
            disposition=MappingDisposition.REQUIRED,
            cost=MappingCostEstimate(1.0, 1.0),
            implementation_name=f"word {operation}32",
            packaging=ImplementationPackaging.INLINE,
        )

    def _map_mux(self, cell: CellSnapshot, revision: int) -> MappingProposal | None:
        if set(cell.ports) != {"A", "B", "S", "Y"}:
            return None
        width = cell.parameter("WIDTH").as_unsigned_int()
        if width not in {1, 32} or any(len(cell.ports[name].bits) != width for name in ("A", "B", "Y")) or len(cell.ports["S"].bits) != 1:
            return None
        def obj(role, operation, a_width, b_width, y_width):
            return PrefabObject(role, FACTORIO_ARITHMETIC_COMBINATOR, encode_factorio_arithmetic_configuration(FactorioArithmeticConfiguration(operation, a_width, b_width, y_width, False, False)))
        def net(name, *attachments):
            return PrefabNet(name, FACTORIO_CIRCUIT_VALUE, frozenset(attachments))
        prefab = SemanticPrefab(self.provider, frozenset({obj("inverse", "not", 1, 1, 1), obj("true", "multiply", 1, width, width), obj("false", "multiply", 1, width, width), obj("sum", "add", width, width, width)}), frozenset(PrefabPort(name, PortDirection.OUTPUT if name == "Y" else PortDirection.INPUT, FACTORIO_INT32, 1 if name == "S" else width) for name in ("A", "B", "S", "Y")), frozenset({
            net("a", ObjectPortRef("false", "b"), *(PrefabPortRef("A", bit) for bit in range(width))),
            net("b", ObjectPortRef("true", "b"), *(PrefabPortRef("B", bit) for bit in range(width))),
            net("select", PrefabPortRef("S"), ObjectPortRef("inverse", "a"), ObjectPortRef("inverse", "b"), ObjectPortRef("true", "a")),
            net("inverse", ObjectPortRef("inverse", "y"), ObjectPortRef("false", "a")),
            net("true", ObjectPortRef("true", "y"), ObjectPortRef("sum", "a")),
            net("false", ObjectPortRef("false", "y"), ObjectPortRef("sum", "b")),
            net("y", ObjectPortRef("sum", "y"), *(PrefabPortRef("Y", bit) for bit in range(width))),
        }))
        boundary = frozenset(BoundaryBinding(ConstantBoundarySource(source.value, CellPortIdentifier(cell.identifier, name, bit)) if isinstance(source, ConstantBit) else source, PrefabPortRef(name, bit)) for name in ("A", "B", "S", "Y") for bit, source in enumerate(cell.ports[name].bits))
        return MappingProposal(revision=revision, provider=self.provider, mapper=self.mapper_id, rule="$mux", rule_version=self.rule_version, ids=frozenset({cell.identifier}), prefab=prefab, boundary=boundary, disposition=MappingDisposition.REQUIRED, cost=MappingCostEstimate(4.0, 4.0), implementation_name=f"word select{width}", packaging=ImplementationPackaging.INLINE)