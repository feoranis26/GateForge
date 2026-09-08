from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from gateforge.providers.lbp.common import LBP_LOGIC
from gateforge.providers.lbp.types.base import LBPObjectType, LBP_OBJECT_TYPES
from gateforge.target import (
    ObjectPlacementGeometry,
    ObjectTypeSchema,
    PortDirection,
    PortSchema,
)
from gateforge.type_codec import TypeCodecError, TypePath, TypePathSegment


class _LBPStateSelectorType(LBPObjectType):
    TYPE_KEY: ClassVar[str]

    def get_placement_geometry(self) -> ObjectPlacementGeometry:
        return ObjectPlacementGeometry(width=52.5, height=52.5)

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPObjectType:
        if values:
            raise TypeCodecError(
                f"LBP {cls.TYPE_KEY} received unexpected decoded values"
            )
        segment.require_parameters()
        if remaining:
            raise TypeCodecError(
                f"LBP {cls.TYPE_KEY} must be the final type path segment"
            )
        return cls()


@LBP_OBJECT_TYPES.register
class LBPPhaseSelectorType(_LBPStateSelectorType):
    TYPE_KEY = "PHASE_SELECTOR"

    def type_path(self) -> TypePath:
        return (TypePathSegment(self.TYPE_KEY),)

    def get_schema(self) -> ObjectTypeSchema:
        return ObjectTypeSchema(
            self.get_type(),
            frozenset(
                {
                    PortSchema("IN_0", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("OUT_0", PortDirection.OUTPUT, LBP_LOGIC),
                    PortSchema("OUT_1", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
        )


@LBP_OBJECT_TYPES.register
class LBPStorageSelectorType(_LBPStateSelectorType):
    TYPE_KEY = "STORAGE_SELECTOR"

    def type_path(self) -> TypePath:
        return (TypePathSegment(self.TYPE_KEY),)

    def get_schema(self) -> ObjectTypeSchema:
        return ObjectTypeSchema(
            self.get_type(),
            frozenset(
                {
                    PortSchema("IN_1", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("IN_2", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("OUT", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
        )