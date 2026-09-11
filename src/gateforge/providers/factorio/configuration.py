from dataclasses import dataclass

from gateforge.providers.factorio.common import (
    FACTORIO_ARITHMETIC_COMBINATOR,
    FACTORIO_LAMP,
)
from gateforge.target import (
    ObjectTypeIdentifier,
    ProviderConfiguration,
)


@dataclass(frozen=True, slots=True)
class FactorioArithmeticConfiguration:
    operation: str
    a_width: int
    b_width: int
    y_width: int
    a_signed: bool
    b_signed: bool

    def __post_init__(self) -> None:
        if self.operation != "add":
            raise ValueError(f"Unsupported Factorio arithmetic operation {self.operation!r}")
        for attribute in ("a_width", "b_width", "y_width"):
            if getattr(self, attribute) != 32:
                raise ValueError("Factorio arithmetic widths must be 32")
        if self.a_signed or self.b_signed:
            raise ValueError("Factorio addition currently requires unsigned operands")


@dataclass(frozen=True, slots=True)
class FactorioLampConfiguration:
    comparator: str = ">"
    constant: int = 0

    def __post_init__(self) -> None:
        if self.comparator != ">" or self.constant != 0:
            raise ValueError("Factorio lamps currently require a > 0 condition")


class FactorioObjectConfigurationCodec:
    def encode(
        self,
        object_type: ObjectTypeIdentifier,
        value: object,
    ) -> ProviderConfiguration:
        if object_type == FACTORIO_ARITHMETIC_COMBINATOR:
            if not isinstance(value, FactorioArithmeticConfiguration):
                raise ValueError("Expected FactorioArithmeticConfiguration")
            data: dict[str, object] = {
                "operation": value.operation,
                "a_width": value.a_width,
                "b_width": value.b_width,
                "y_width": value.y_width,
                "a_signed": value.a_signed,
                "b_signed": value.b_signed,
            }
        elif object_type == FACTORIO_LAMP:
            if not isinstance(value, FactorioLampConfiguration):
                raise ValueError("Expected FactorioLampConfiguration")
            data = {
                "comparator": value.comparator,
                "constant": value.constant,
            }
        else:
            raise ValueError(f"Unsupported Factorio object type {object_type}")
        return ProviderConfiguration.from_canonical_data(data)

    def decode(
        self,
        object_type: ObjectTypeIdentifier,
        configuration: ProviderConfiguration,
    ) -> FactorioArithmeticConfiguration | FactorioLampConfiguration:
        data = configuration.canonical_data()
        if object_type == FACTORIO_LAMP:
            if set(data) != {"comparator", "constant"}:
                raise ValueError("Factorio lamp configuration fields differ")
            comparator = data["comparator"]
            constant = data["constant"]
            if not isinstance(comparator, str):
                raise ValueError("Factorio lamp comparator must be a string")
            if isinstance(constant, bool) or not isinstance(constant, int):
                raise ValueError("Factorio lamp constant must be an integer")
            return FactorioLampConfiguration(comparator, constant)
        if object_type != FACTORIO_ARITHMETIC_COMBINATOR:
            raise ValueError(f"Unsupported Factorio object type {object_type}")
        expected = {
            "operation",
            "a_width",
            "b_width",
            "y_width",
            "a_signed",
            "b_signed",
        }
        if set(data) != expected:
            raise ValueError("Factorio arithmetic configuration fields differ")
        operation = data["operation"]
        widths = (data["a_width"], data["b_width"], data["y_width"])
        signedness = (data["a_signed"], data["b_signed"])
        if not isinstance(operation, str):
            raise ValueError("Factorio arithmetic operation must be a string")
        if any(
            not isinstance(width, int) or isinstance(width, bool)
            for width in widths
        ):
            raise ValueError("Factorio arithmetic widths must be integers")
        if any(not isinstance(signed, bool) for signed in signedness):
            raise ValueError("Factorio arithmetic signedness must be boolean")
        return FactorioArithmeticConfiguration(
            operation,
            widths[0],
            widths[1],
            widths[2],
            signedness[0],
            signedness[1],
        )


def encode_factorio_arithmetic_configuration(
    value: FactorioArithmeticConfiguration,
) -> ProviderConfiguration:
    return FactorioObjectConfigurationCodec().encode(
        FACTORIO_ARITHMETIC_COMBINATOR,
        value,
    )


def encode_factorio_lamp_configuration(
    value: FactorioLampConfiguration = FactorioLampConfiguration(),
) -> ProviderConfiguration:
    return FactorioObjectConfigurationCodec().encode(FACTORIO_LAMP, value)