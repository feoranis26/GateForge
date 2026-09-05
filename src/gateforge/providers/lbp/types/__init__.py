from gateforge.providers.lbp.common import LBP_PROVIDER, LBP_TYPE_VERSION
from gateforge.providers.lbp.types.base import LBPGateType, LBP_OBJECT_TYPES
from gateforge.providers.lbp.types.combinatorial import (
    LBPAndGateType,
    LBPCombinatorialVariableWidthGateType,
    LBPNotGateType,
    LBPOrGateType,
)
from gateforge.target import ObjectTypeIdentifier, PrefabValidationError
from gateforge.type_codec import (
    TypeCodecError,
    format_type_path,
    parse_type_path,
)


def decode_lbp_object_type(identifier: ObjectTypeIdentifier) -> LBPGateType:
    if identifier.provider != LBP_PROVIDER or identifier.version != LBP_TYPE_VERSION:
        raise PrefabValidationError(f"Unsupported LBP object identifier {identifier}")

    try:
        path = parse_type_path(identifier.name)
        if format_type_path(path) != identifier.name:
            raise TypeCodecError(
                f"Non-canonical LBP object identifier {identifier.name!r}"
            )
        gate_type = LBP_OBJECT_TYPES.decode(path)
    except TypeCodecError as error:
        raise PrefabValidationError(str(error)) from error

    if gate_type.get_type() != identifier:
        raise PrefabValidationError(f"Non-canonical LBP object identifier {identifier}")
    return gate_type


__all__ = [
    "LBPAndGateType",
    "LBPCombinatorialVariableWidthGateType",
    "LBPGateType",
    "LBPNotGateType",
    "LBPOrGateType",
    "decode_lbp_object_type",
]