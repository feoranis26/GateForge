from collections.abc import Mapping

from gateforge.material import (
    MaterialConstantRef,
    MaterialModulePortRef,
    MaterialNet,
    MaterialObject,
    MaterialObjectId,
    MaterialObjectPortRef,
)
from gateforge.provider import ProjectedDependency, TargetProvider
from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.types import decode_lbp_object_type
from gateforge.target import (
    NetworkTypeSchema,
    ObjectTypeIdentifier,
    ObjectTypeSchema,
    PortDirection,
    PrefabPortRef,
    PrefabValidationError,
    SemanticPrefab,
    TargetTypeRegistry,
    attachment_direction,
    validate_prefab,
)


class LBPTypeRegistry(TargetTypeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.register_network(NetworkTypeSchema(LBP_WIRE, LBP_LOGIC))

    def object(self, identifier: ObjectTypeIdentifier) -> ObjectTypeSchema:
        try:
            return super().object(identifier)
        except PrefabValidationError:
            gate_type = decode_lbp_object_type(identifier)
            self.register_object(gate_type.get_schema())
            return super().object(identifier)


def validate_lbp_prefab(
    prefab: SemanticPrefab,
    registry: TargetTypeRegistry,
) -> None:
    validate_prefab(prefab, registry)
    for net in prefab.nets:
        producers = 0
        for attachment in net.attachments:
            direction = attachment_direction(prefab, registry, attachment)
            is_external = isinstance(attachment, PrefabPortRef)
            if (
                is_external
                and direction in {PortDirection.INPUT, PortDirection.INOUT}
            ) or (
                not is_external
                and direction in {PortDirection.OUTPUT, PortDirection.INOUT}
            ):
                producers += 1
        if producers != 1:
            raise PrefabValidationError(
                f"LBP net {net.role!r} requires exactly one producer, got {producers}"
            )


def validate_lbp_material_net(
    net: MaterialNet,
    objects: Mapping[MaterialObjectId, MaterialObject],
    registry: TargetTypeRegistry,
) -> None:
    producers = sum(
        _is_lbp_source(attachment, objects, registry)
        for attachment in net.attachments
    )
    if producers != 1:
        raise PrefabValidationError(
            f"LBP material network {net.identifier!r} requires exactly one "
            f"producer, got {producers}"
        )


def project_lbp_material_dependencies(
    net: MaterialNet,
    objects: Mapping[MaterialObjectId, MaterialObject],
    registry: TargetTypeRegistry,
) -> tuple[ProjectedDependency, ...]:
    sources = [
        attachment
        for attachment in net.attachments
        if _is_lbp_source(attachment, objects, registry)
    ]
    if len(sources) != 1:
        raise PrefabValidationError(
            f"LBP material network {net.identifier!r} requires exactly one "
            f"producer, got {len(sources)}"
        )
    source = sources[0]
    return tuple(
        ProjectedDependency(source, attachment)
        for attachment in net.attachments
        if attachment != source
    )


def _is_lbp_source(
    attachment: MaterialConstantRef
    | MaterialModulePortRef
    | MaterialObjectPortRef,
    objects: Mapping[MaterialObjectId, MaterialObject],
    registry: TargetTypeRegistry,
) -> bool:
    if isinstance(attachment, MaterialConstantRef):
        return True
    if isinstance(attachment, MaterialModulePortRef):
        if attachment.direction == PortDirection.INOUT:
            raise PrefabValidationError("LBP does not support INOUT material ports")
        return attachment.direction == PortDirection.INPUT

    try:
        material_object = objects[attachment.object]
    except KeyError as error:
        raise PrefabValidationError(
            f"Material network references unknown object {attachment.object}"
        ) from error
    schema = registry.object(material_object.type)
    port = next((item for item in schema.ports if item.name == attachment.port), None)
    if port is None or attachment.bit < 0 or attachment.bit >= port.width:
        raise PrefabValidationError(
            f"Material network references invalid object port {attachment}"
        )
    if port.direction == PortDirection.INOUT:
        raise PrefabValidationError("LBP does not support INOUT material object ports")
    return port.direction == PortDirection.OUTPUT


def make_lbp_provider() -> TargetProvider:
    return TargetProvider(
        identifier=LBP_PROVIDER,
        registry=LBPTypeRegistry(),
        validator=validate_lbp_prefab,
        material_validator=validate_lbp_material_net,
        dependency_projector=project_lbp_material_dependencies,
    )