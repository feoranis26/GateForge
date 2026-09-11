from gateforge.target import (
    NetworkTypeIdentifier,
    ObjectTypeIdentifier,
    SignalTypeIdentifier,
)


FACTORIO_PROVIDER = "factorio"
FACTORIO_INT32 = SignalTypeIdentifier(FACTORIO_PROVIDER, "int32")
FACTORIO_CIRCUIT_VALUE = NetworkTypeIdentifier(
    FACTORIO_PROVIDER,
    "circuit-value",
)
FACTORIO_ARITHMETIC_COMBINATOR = ObjectTypeIdentifier(
    FACTORIO_PROVIDER,
    "arithmetic-combinator",
)
FACTORIO_LAMP = ObjectTypeIdentifier(FACTORIO_PROVIDER, "lamp")