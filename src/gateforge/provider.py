from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from typing import Callable, Protocol

from gateforge.material import (
    MaterialAttachment,
    MaterialNet,
    MaterialObject,
    MaterialObjectId,
)
from gateforge.target import (
    ObjectTypeIdentifier,
    ObjectPlacementGeometry,
    PrefabValidationError,
    ProviderConfiguration,
    SemanticPrefab,
    TargetTypeRegistry,
)


class ObjectConfigurationCodec(Protocol):
    def encode(
        self,
        object_type: ObjectTypeIdentifier,
        value: object,
    ) -> ProviderConfiguration: ...

    def decode(
        self,
        object_type: ObjectTypeIdentifier,
        configuration: ProviderConfiguration,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ProjectedDependency:
    source: MaterialAttachment
    target: MaterialAttachment


@dataclass(frozen=True, slots=True)
class TargetProvider:
    identifier: str
    registry: TargetTypeRegistry
    validator: Callable[[SemanticPrefab, TargetTypeRegistry], None]
    material_validator: Callable[
        [MaterialNet, Mapping[MaterialObjectId, MaterialObject], TargetTypeRegistry],
        None,
    ] | None = None
    object_configuration_codec: ObjectConfigurationCodec | None = None
    dependency_projector: Callable[
        [MaterialNet, Mapping[MaterialObjectId, MaterialObject], TargetTypeRegistry],
        Iterable[ProjectedDependency],
    ] | None = None
    object_geometry_resolver: Callable[
        [MaterialObject, TargetTypeRegistry],
        ObjectPlacementGeometry,
    ] | None = None
    object_cost_resolver: Callable[
        [MaterialObject, TargetTypeRegistry],
        float,
    ] | None = None

    def validate(self, prefab: SemanticPrefab) -> None:
        if prefab.provider != self.identifier:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} cannot validate "
                f"{prefab.provider!r} prefab"
            )
        for item in prefab.objects:
            self.validate_object_configuration(item.type, item.configuration)
        self.validator(prefab, self.registry)

    def encode_object_configuration(
        self,
        object_type: ObjectTypeIdentifier,
        value: object,
    ) -> ProviderConfiguration:
        self._require_object_type_provider(object_type)
        if self.object_configuration_codec is None:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} does not support object configuration"
            )
        configuration = self.object_configuration_codec.encode(object_type, value)
        if not isinstance(configuration, ProviderConfiguration):
            raise PrefabValidationError(
                f"Provider {self.identifier!r} returned invalid object configuration"
            )
        self.decode_object_configuration(object_type, configuration)
        return configuration

    def decode_object_configuration(
        self,
        object_type: ObjectTypeIdentifier,
        configuration: ProviderConfiguration,
    ) -> object | None:
        self._require_object_type_provider(object_type)
        if configuration.is_empty and self.object_configuration_codec is None:
            return None
        if self.object_configuration_codec is None:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} does not support object configuration"
            )
        try:
            return self.object_configuration_codec.decode(object_type, configuration)
        except ValueError as error:
            raise PrefabValidationError(
                f"Invalid configuration for {object_type}: {error}"
            ) from error

    def validate_object_configuration(
        self,
        object_type: ObjectTypeIdentifier,
        configuration: ProviderConfiguration,
    ) -> None:
        self.decode_object_configuration(object_type, configuration)

    def _require_object_type_provider(
        self,
        object_type: ObjectTypeIdentifier,
    ) -> None:
        if object_type.provider != self.identifier:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} cannot configure object type "
                f"from {object_type.provider!r}"
            )

    def validate_material_net(
        self,
        net: MaterialNet,
        objects: Mapping[MaterialObjectId, MaterialObject],
    ) -> None:
        if net.type.provider != self.identifier:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} cannot validate material network "
                f"from {net.type.provider!r}"
            )
        if self.material_validator is not None:
            self.material_validator(net, objects, self.registry)

    def project_material_dependencies(
        self,
        net: MaterialNet,
        objects: Mapping[MaterialObjectId, MaterialObject],
    ) -> tuple[ProjectedDependency, ...]:
        if net.type.provider != self.identifier:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} cannot project material network "
                f"from {net.type.provider!r}"
            )
        if self.dependency_projector is None:
            raise PrefabValidationError(
                f"Provider {self.identifier!r} does not define dependency projection"
            )
        return tuple(self.dependency_projector(net, objects, self.registry))

    def object_placement_geometry(
        self,
        material_object: MaterialObject,
    ) -> ObjectPlacementGeometry | None:
        self._require_object_type_provider(material_object.type)
        if self.object_geometry_resolver is None:
            return None
        return self.object_geometry_resolver(material_object, self.registry)

    def material_object_cost(self, material_object: MaterialObject) -> float:
        self._require_object_type_provider(material_object.type)
        if self.object_cost_resolver is not None:
            value = float(self.object_cost_resolver(material_object, self.registry))
        else:
            geometry = self.object_placement_geometry(material_object)
            value = 1.0 if geometry is None else geometry.width * geometry.height
        if not math.isfinite(value) or value < 0:
            raise ValueError("Material object cost must be finite and nonnegative")
        return value
