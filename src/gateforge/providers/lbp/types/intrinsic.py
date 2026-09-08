from __future__ import annotations

from collections.abc import Mapping
import re

from gateforge.providers.lbp.common import LBP_LOGIC
from gateforge.providers.lbp.types.base import LBPObjectType, LBP_OBJECT_TYPES
from gateforge.target import (
    ObjectPlacementGeometry,
    ObjectTypeSchema,
    PortDirection,
    PortSchema,
)
from gateforge.type_codec import TypeCodecError, TypePath, TypePathSegment


_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*")


def _decode_width(
    segment: TypePathSegment,
    remaining: TypePath,
    values: Mapping[str, object],
    label: str,
) -> int:
    if values:
        raise TypeCodecError(f"LBP {label} received unexpected decoded values")
    if remaining:
        raise TypeCodecError(f"LBP {label} must be the final type path segment")
    parameters = segment.require_parameters("width")
    width = parameters["width"]
    if _POSITIVE_DECIMAL.fullmatch(width) is None:
        raise TypeCodecError(
            f"LBP {label} width must be a canonical positive decimal"
        )
    return int(width)


@LBP_OBJECT_TYPES.register
class LBPTimerType(LBPObjectType):
    TYPE_KEY = "TIMER"

    def type_path(self) -> TypePath:
        return (TypePathSegment(self.TYPE_KEY),)

    def get_schema(self) -> ObjectTypeSchema:
        return ObjectTypeSchema(
            identifier=self.get_type(),
            ports=frozenset(
                {
                    PortSchema("IN_0", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("IN_1", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("OUT", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
        )

    def get_placement_geometry(self) -> ObjectPlacementGeometry:
        return ObjectPlacementGeometry(width=105.0, height=52.5)

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPObjectType:
        if values:
            raise TypeCodecError("LBP Timer received unexpected decoded values")
        segment.require_parameters()
        if remaining:
            raise TypeCodecError("LBP Timer must be the final type path segment")
        return cls()


@LBP_OBJECT_TYPES.register
class LBPCounterType(LBPObjectType):
    TYPE_KEY = "COUNTER"

    def type_path(self) -> TypePath:
        return (TypePathSegment(self.TYPE_KEY),)

    def get_schema(self) -> ObjectTypeSchema:
        return ObjectTypeSchema(
            identifier=self.get_type(),
            ports=frozenset(
                {
                    PortSchema("IN_0", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("IN_1", PortDirection.INPUT, LBP_LOGIC),
                    PortSchema("OUT", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
        )

    def get_placement_geometry(self) -> ObjectPlacementGeometry:
        return ObjectPlacementGeometry(width=105.0, height=52.5)

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPObjectType:
        if values:
            raise TypeCodecError("LBP Counter received unexpected decoded values")
        segment.require_parameters()
        if remaining:
            raise TypeCodecError("LBP Counter must be the final type path segment")
        return cls()


@LBP_OBJECT_TYPES.register
class LBPRandomizerType(LBPObjectType):
    TYPE_KEY = "RANDOMIZER"

    def __init__(self, outputs: int):
        if outputs <= 0:
            raise ValueError("LBP Randomizer outputs must be positive")
        self.outputs = outputs

    def type_path(self) -> TypePath:
        return (TypePathSegment.create(self.TYPE_KEY, {"width": str(self.outputs)}),)

    def get_schema(self) -> ObjectTypeSchema:
        ports = {PortSchema("IN_0", PortDirection.INPUT, LBP_LOGIC)}
        ports.update(
            PortSchema(f"OUT_{index}", PortDirection.OUTPUT, LBP_LOGIC)
            for index in range(self.outputs)
        )
        return ObjectTypeSchema(self.get_type(), frozenset(ports))

    def get_placement_geometry(self) -> ObjectPlacementGeometry:
        return ObjectPlacementGeometry(width=52.5, height=52.5)

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPObjectType:
        return cls(_decode_width(segment, remaining, values, "Randomizer"))


@LBP_OBJECT_TYPES.register
class LBPSelectorType(LBPObjectType):
    TYPE_KEY = "SELECTOR"

    def __init__(self, width: int):
        if width <= 0:
            raise ValueError("LBP Selector width must be positive")
        self.width = width

    def type_path(self) -> TypePath:
        return (TypePathSegment.create(self.TYPE_KEY, {"width": str(self.width)}),)

    def get_schema(self) -> ObjectTypeSchema:
        ports = {PortSchema("IN_0", PortDirection.INPUT, LBP_LOGIC)}
        ports.update(
            PortSchema(f"IN_{index + 1}", PortDirection.INPUT, LBP_LOGIC)
            for index in range(self.width)
        )
        ports.update(
            PortSchema(f"OUT_{index}", PortDirection.OUTPUT, LBP_LOGIC)
            for index in range(self.width)
        )
        return ObjectTypeSchema(self.get_type(), frozenset(ports))

    def get_placement_geometry(self) -> ObjectPlacementGeometry:
        return ObjectPlacementGeometry(
            width=52.5,
            height=26.25 * max(self.width, 2),
        )

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPObjectType:
        return cls(_decode_width(segment, remaining, values, "Selector"))