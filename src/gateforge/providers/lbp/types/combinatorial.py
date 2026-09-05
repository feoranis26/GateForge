from __future__ import annotations

from collections.abc import Mapping
import re

from gateforge.providers.lbp.common import LBP_LOGIC
from gateforge.providers.lbp.types.base import (
    LBPGateType,
    LBP_GATE_FAMILIES,
)
from gateforge.target import ObjectTypeSchema, PortDirection, PortSchema
from gateforge.type_codec import (
    TypeClassRegistry,
    TypeCodecError,
    TypePath,
    TypePathSegment,
)


LBP_COMBINATORIAL_TYPES: TypeClassRegistry[LBPGateType] = TypeClassRegistry(
    "LBP variable-width gate type"
)
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*")


@LBP_GATE_FAMILIES.register
class LBPCombinatorialVariableWidthGateType(LBPGateType):
    TYPE_KEY = "VARIABLE_WIDTH"

    def __init__(self, width: int, invert: bool):
        if width <= 0:
            raise ValueError("LBP gate width must be positive")
        self.width = width
        super().__init__(invert)

    def type_path(self) -> TypePath:
        leaf_key = vars(type(self)).get("TYPE_KEY")
        if not isinstance(leaf_key, str):
            raise TypeCodecError(
                f"Concrete LBP gate {type(self).__name__} must define TYPE_KEY"
            )
        return (
            *super().type_path(),
            TypePathSegment.create(
                LBPCombinatorialVariableWidthGateType.TYPE_KEY,
                {"width": str(self.width)},
            ),
            TypePathSegment(leaf_key),
        )

    def get_schema(self) -> ObjectTypeSchema:
        ports = {
            PortSchema(f"IN_{index}", PortDirection.INPUT, LBP_LOGIC)
            for index in range(self.width)
        }
        ports.add(PortSchema("OUT", PortDirection.OUTPUT, LBP_LOGIC))
        return ObjectTypeSchema(
            identifier=self.get_type(),
            ports=frozenset(ports),
        )

    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPGateType:
        if set(values) != {"invert"} or not isinstance(values["invert"], bool):
            raise TypeCodecError("Variable-width gate requires decoded invert value")
        parameters = segment.require_parameters("width")
        width_text = parameters["width"]
        if _POSITIVE_DECIMAL.fullmatch(width_text) is None:
            raise TypeCodecError(
                f"LBP gate width must be a canonical positive decimal, "
                f"got {width_text!r}"
            )
        return LBP_COMBINATORIAL_TYPES.decode(
            remaining,
            {"invert": values["invert"], "width": int(width_text)},
        )


class _LBPVariableWidthLeaf(LBPCombinatorialVariableWidthGateType):
    @classmethod
    def decode_type_path(
        cls,
        segment: TypePathSegment,
        remaining: TypePath,
        values: Mapping[str, object],
    ) -> LBPGateType:
        segment.require_parameters()
        if remaining:
            raise TypeCodecError(
                f"LBP gate type {segment.key!r} must be the final path segment"
            )
        if set(values) != {"invert", "width"}:
            raise TypeCodecError(
                f"LBP gate type {segment.key!r} is missing decoded parameters"
            )
        invert = values["invert"]
        width = values["width"]
        if not isinstance(invert, bool) or not isinstance(width, int):
            raise TypeCodecError(
                f"LBP gate type {segment.key!r} has invalid decoded parameters"
            )
        return cls(width=width, invert=invert)


@LBP_COMBINATORIAL_TYPES.register
class LBPAndGateType(_LBPVariableWidthLeaf):
    TYPE_KEY = "AND"


@LBP_COMBINATORIAL_TYPES.register
class LBPOrGateType(_LBPVariableWidthLeaf):
    TYPE_KEY = "OR"


@LBP_COMBINATORIAL_TYPES.register
class LBPNotGateType(_LBPVariableWidthLeaf):
    TYPE_KEY = "NOT"