from gateforge.intrinsics import DEFAULT_INTRINSICS, IntrinsicKind
from gateforge.mapping import (
    BoundaryBinding,
    MappingCostEstimate,
    MappingDisposition,
    MappingProposal,
    MappingProvider,
)
from gateforge.material import ImplementationPackaging
from gateforge.providers.factorio.common import (
    FACTORIO_CIRCUIT_VALUE,
    FACTORIO_INT32,
    FACTORIO_LAMP,
    FACTORIO_PROVIDER,
)
from gateforge.providers.factorio.configuration import (
    encode_factorio_lamp_configuration,
)
from gateforge.source import ConstantBit, DesignSnapshot
from gateforge.target import (
    ObjectPortRef,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
)


_LAMP_WIDTH = 32


class FactorioLampIntrinsicMapper(MappingProvider):
    provider = FACTORIO_PROVIDER
    stages = frozenset({"source"})
    mapper_id = "factorio.intrinsic.lamp"
    rule_version = 1

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        proposals: list[MappingProposal] = []
        for module in sorted(design.modules.values(), key=lambda item: item.name):
            if "blackbox" in module.attributes:
                continue
            for cell in sorted(
                module.cells.values(), key=lambda item: item.identifier.name
            ):
                instance = DEFAULT_INTRINSICS.recognize(design, cell)
                if instance is None or instance.definition.kind != IntrinsicKind.LAMP:
                    continue
                port = cell.ports["in"]
                if len(port.bits) != _LAMP_WIDTH or any(
                    isinstance(bit, ConstantBit) for bit in port.bits
                ):
                    continue
                prefab = SemanticPrefab(
                    provider=self.provider,
                    objects=frozenset(
                        {
                            PrefabObject(
                                "lamp",
                                FACTORIO_LAMP,
                                encode_factorio_lamp_configuration(),
                            )
                        }
                    ),
                    ports=frozenset(
                        {
                            PrefabPort(
                                "in",
                                PortDirection.INPUT,
                                FACTORIO_INT32,
                                _LAMP_WIDTH,
                            )
                        }
                    ),
                    nets=frozenset(
                        {
                            PrefabNet(
                                "in",
                                FACTORIO_CIRCUIT_VALUE,
                                frozenset(
                                    {
                                        ObjectPortRef("lamp", "in"),
                                        *(
                                            PrefabPortRef("in", bit)
                                            for bit in range(_LAMP_WIDTH)
                                        ),
                                    }
                                ),
                            )
                        }
                    ),
                )
                proposals.append(
                    MappingProposal(
                        revision=design.revision,
                        provider=self.provider,
                        mapper=self.mapper_id,
                        rule=instance.definition.module,
                        rule_version=self.rule_version,
                        ids=frozenset({cell.identifier}),
                        prefab=prefab,
                        boundary=frozenset(
                            BoundaryBinding(bit, PrefabPortRef("in", index))
                            for index, bit in enumerate(port.bits)
                        ),
                        disposition=MappingDisposition.REQUIRED,
                        cost=MappingCostEstimate(1.0, 1.0),
                        implementation_name="lamp",
                        packaging=ImplementationPackaging.INLINE,
                    )
                )
        return tuple(proposals)