from collections.abc import Mapping

from gateforge.material import (
    MaterialAttachment,
    MaterialConstantRef,
    MaterialModuleValueRef,
    MaterialNet,
    MaterialObject,
    MaterialObjectId,
    MaterialObjectPortRef,
)
from gateforge.provider import ProjectedDependency, TargetProvider
from gateforge.providers.factorio.common import (
    FACTORIO_ARITHMETIC_COMBINATOR,
    FACTORIO_CIRCUIT_VALUE,
    FACTORIO_INT32,
    FACTORIO_LAMP,
    FACTORIO_PROVIDER,
)
from gateforge.providers.factorio.configuration import (
    FactorioObjectConfigurationCodec,
)
from gateforge.target import (
    NetworkInterfaceMode,
    NetworkTypeSchema,
    ObjectPlacementGeometry,
    ObjectTypeSchema,
    PortDirection,
    PortSchema,
    PrefabPortRef,
    PrefabValidationError,
    SemanticPrefab,
    TargetTypeRegistry,
    attachment_direction,
    validate_prefab,
)


class FactorioTypeRegistry(TargetTypeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.register_network(
            NetworkTypeSchema(
                FACTORIO_CIRCUIT_VALUE,
                FACTORIO_INT32,
                NetworkInterfaceMode.PACKED,
            )
        )
        self.register_object(
            ObjectTypeSchema(
                FACTORIO_ARITHMETIC_COMBINATOR,
                frozenset(
                    {
                        PortSchema("a", PortDirection.INPUT, FACTORIO_INT32),
                        PortSchema("b", PortDirection.INPUT, FACTORIO_INT32),
                        PortSchema("y", PortDirection.OUTPUT, FACTORIO_INT32),
                    }
                ),
            )
        )
        self.register_object(
            ObjectTypeSchema(
                FACTORIO_LAMP,
                frozenset(
                    {PortSchema("in", PortDirection.INPUT, FACTORIO_INT32)}
                ),
            )
        )


def validate_factorio_prefab(
    prefab: SemanticPrefab,
    registry: TargetTypeRegistry,
) -> None:
    validate_prefab(prefab, registry)
    for net in prefab.nets:
        producers = {
            _prefab_endpoint_key(attachment)
            for attachment in net.attachments
            if _is_prefab_source(prefab, registry, attachment)
        }
        if len(producers) != 1:
            raise PrefabValidationError(
                f"Factorio net {net.role!r} requires exactly one producer, "
                f"got {len(producers)}"
            )


def validate_factorio_material_net(
    net: MaterialNet,
    objects: Mapping[MaterialObjectId, MaterialObject],
    registry: TargetTypeRegistry,
) -> None:
    producers = sum(
        _is_material_source(attachment, objects, registry)
        for attachment in net.attachments
    )
    if producers != 1:
        raise PrefabValidationError(
            f"Factorio material network {net.identifier.value!r} requires exactly "
            f"one producer, got {producers}"
        )


def project_factorio_material_dependencies(
    net: MaterialNet,
    objects: Mapping[MaterialObjectId, MaterialObject],
    registry: TargetTypeRegistry,
) -> tuple[ProjectedDependency, ...]:
    sources = tuple(
        attachment
        for attachment in net.attachments
        if _is_material_source(attachment, objects, registry)
    )
    if len(sources) != 1:
        raise PrefabValidationError(
            f"Factorio material network {net.identifier.value!r} requires exactly "
            f"one producer, got {len(sources)}"
        )
    source = sources[0]
    return tuple(
        ProjectedDependency(source, attachment)
        for attachment in net.attachments
        if attachment != source
    )


def _is_prefab_source(
    prefab: SemanticPrefab,
    registry: TargetTypeRegistry,
    attachment,
) -> bool:
    direction = attachment_direction(prefab, registry, attachment)
    if direction == PortDirection.INOUT:
        raise PrefabValidationError("Factorio does not support INOUT prefab ports")
    if isinstance(attachment, PrefabPortRef):
        return direction == PortDirection.INPUT
    return direction == PortDirection.OUTPUT


def _prefab_endpoint_key(attachment) -> tuple[str, ...]:
    if isinstance(attachment, PrefabPortRef):
        return ("external", attachment.port)
    return ("object", attachment.object_role, attachment.port)


def _is_material_source(
    attachment: MaterialAttachment,
    objects: Mapping[MaterialObjectId, MaterialObject],
    registry: TargetTypeRegistry,
) -> bool:
    if isinstance(attachment, MaterialConstantRef):
        return True
    if isinstance(attachment, MaterialModuleValueRef):
        if attachment.direction == PortDirection.INOUT:
            raise PrefabValidationError("Factorio does not support INOUT module values")
        return attachment.direction == PortDirection.INPUT
    if not isinstance(attachment, MaterialObjectPortRef):
        raise PrefabValidationError(
            f"Factorio network has unsupported attachment {attachment!r}"
        )
    try:
        material_object = objects[attachment.object]
    except KeyError as error:
        raise PrefabValidationError(
            f"Factorio network references unknown object {attachment.object.value}"
        ) from error
    schema = registry.object(material_object.type)
    port = next((item for item in schema.ports if item.name == attachment.port), None)
    if port is None or attachment.bit < 0 or attachment.bit >= port.width:
        raise PrefabValidationError(
            f"Factorio network references invalid object port {attachment}"
        )
    if port.direction == PortDirection.INOUT:
        raise PrefabValidationError("Factorio does not support INOUT object ports")
    return port.direction == PortDirection.OUTPUT


def make_factorio_provider() -> TargetProvider:
    from gateforge.providers.factorio.physical import (
        elaborate_factorio_physical_design,
    )
    from gateforge.providers.factorio.visualization import (
        FactorioVisualizationAdapter,
    )

    return TargetProvider(
        identifier=FACTORIO_PROVIDER,
        registry=FactorioTypeRegistry(),
        validator=validate_factorio_prefab,
        material_validator=validate_factorio_material_net,
        object_configuration_codec=FactorioObjectConfigurationCodec(),
        dependency_projector=project_factorio_material_dependencies,
        object_geometry_resolver=lambda material_object, registry: (
            ObjectPlacementGeometry(1.0, 1.0)
            if material_object.type == FACTORIO_LAMP
            else ObjectPlacementGeometry(2.0, 1.0)
        ),
        physical_elaborator=elaborate_factorio_physical_design,
        visualization=FactorioVisualizationAdapter(),
    )