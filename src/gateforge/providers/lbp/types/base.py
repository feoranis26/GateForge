from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from gateforge.providers.lbp.common import LBP_PROVIDER, LBP_TYPE_VERSION
from gateforge.target import (
    ObjectPlacementGeometry,
    ObjectTypeIdentifier,
    ObjectTypeSchema,
)
from gateforge.type_codec import (
    TypeClassRegistry,
    TypeCodecError,
    TypePath,
    TypePathSegment,
    format_type_path,
)


class LBPObjectType(ABC):
    def name(self) -> str:
        return format_type_path(self.type_path())

    def get_type(self) -> ObjectTypeIdentifier:
        return ObjectTypeIdentifier(
            provider=LBP_PROVIDER,
            name=self.name(),
            version=LBP_TYPE_VERSION,
        )

    @abstractmethod
    def type_path(self) -> TypePath:
        raise NotImplementedError

    @abstractmethod
    def get_schema(self) -> ObjectTypeSchema:
        raise NotImplementedError

    @abstractmethod
    def get_placement_geometry(self) -> ObjectPlacementGeometry:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> "LBPObjectType":
        raise NotImplementedError


class LBPGateType(LBPObjectType):
    TYPE_KEY = "GATE"

    def __init__(self, invert: bool):
        self.invert_output = invert

    def type_path(self) -> TypePath:
        return (
            TypePathSegment.create(
                LBPGateType.TYPE_KEY,
                {"invert": "true" if self.invert_output else "false"},
            ),
        )

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPGateType:
        if values:
            raise TypeCodecError("LBP gate root received unexpected decoded values")
        parameters = segment.require_parameters("invert")
        invert_text = parameters["invert"]
        if invert_text == "true":
            invert = True
        elif invert_text == "false":
            invert = False
        else:
            raise TypeCodecError(
                f"LBP gate invert must be 'true' or 'false', got {invert_text!r}"
            )
        return LBP_GATE_FAMILIES.decode(remaining, {"invert": invert})


LBP_OBJECT_TYPES: TypeClassRegistry[LBPObjectType] = TypeClassRegistry(
    "LBP object type"
)
LBP_GATE_FAMILIES: TypeClassRegistry[LBPGateType] = TypeClassRegistry(
    "LBP gate family"
)
LBP_OBJECT_TYPES.register(LBPGateType)