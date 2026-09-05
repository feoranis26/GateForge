from collections.abc import Mapping

from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.types import decode_lbp_object_type
from gateforge.target import (
    NetworkTypeSchema,
    ObjectTypeIdentifier,
    ObjectTypeSchema,
    PhysicalConstantRef,
    PhysicalModulePortRef,
    PhysicalNet,
    PhysicalObject,
    PhysicalObjectId,
    PhysicalObjectPortRef,
    PortDirection,
    PrefabPortRef,
    PrefabValidationError,
    SemanticPrefab,
    TargetProvider,
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


def validate_lbp_physical_net(
    net: PhysicalNet,
    objects: Mapping[PhysicalObjectId, PhysicalObject],
    registry: TargetTypeRegistry,
) -> None:
    producers = 0
    for attachment in net.attachments:
        if isinstance(attachment, PhysicalConstantRef):
            producers += 1
        elif isinstance(attachment, PhysicalModulePortRef):
            if attachment.direction in {PortDirection.INPUT, PortDirection.INOUT}:
                producers += 1
        elif isinstance(attachment, PhysicalObjectPortRef):
            try:
                physical_object = objects[attachment.object]
            except KeyError as error:
                raise PrefabValidationError(
                    f"Physical network references unknown object {attachment.object}"
                ) from error
            schema = registry.object(physical_object.type)
            port = next(
                (item for item in schema.ports if item.name == attachment.port),
                None,
            )
            if port is None or attachment.bit < 0 or attachment.bit >= port.width:
                raise PrefabValidationError(
                    f"Physical network references invalid object port {attachment}"
                )
            if port.direction in {PortDirection.OUTPUT, PortDirection.INOUT}:
                producers += 1
    if producers != 1:
        raise PrefabValidationError(
            f"LBP physical network {net.identifier!r} requires exactly one "
            f"producer, got {producers}"
        )


def make_lbp_provider() -> TargetProvider:
    return TargetProvider(
        identifier=LBP_PROVIDER,
        registry=LBPTypeRegistry(),
        validator=validate_lbp_prefab,
        physical_validator=validate_lbp_physical_net,
    )