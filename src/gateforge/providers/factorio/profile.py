from dataclasses import dataclass


FACTORIO_PROFILE_VERSION = "2.x"


@dataclass(frozen=True, slots=True)
class FactorioConnectorProfile:
    identifier: int
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class FactorioEntityProfile:
    name: str
    width: float
    height: float
    circuit_wire_reach: float
    connectors: tuple[FactorioConnectorProfile, ...]
    copper_wire_reach: float | None = None


ARITHMETIC_COMBINATOR = FactorioEntityProfile(
    "arithmetic-combinator",
    2.0,
    1.0,
    9.0,
    (
        FactorioConnectorProfile(1, -0.5, 0.0),
        FactorioConnectorProfile(2, 0.5, 0.0),
    ),
)
CONSTANT_COMBINATOR = FactorioEntityProfile(
    "constant-combinator",
    1.0,
    1.0,
    9.0,
    (FactorioConnectorProfile(1, 0.0, 0.0),),
)
SMALL_LAMP = FactorioEntityProfile(
    "small-lamp",
    1.0,
    1.0,
    9.0,
    (FactorioConnectorProfile(1, 0.0, 0.0),),
)
MEDIUM_ELECTRIC_POLE = FactorioEntityProfile(
    "medium-electric-pole",
    1.0,
    1.0,
    9.0,
    (FactorioConnectorProfile(1, 0.0, 0.0),),
    9.0,
)
BIG_ELECTRIC_POLE = FactorioEntityProfile(
    "big-electric-pole",
    2.0,
    2.0,
    32.0,
    (FactorioConnectorProfile(1, 0.0, 0.0),),
    32.0,
)


_PROFILES = {
    item.name: item
    for item in (
        ARITHMETIC_COMBINATOR,
        CONSTANT_COMBINATOR,
        SMALL_LAMP,
        MEDIUM_ELECTRIC_POLE,
        BIG_ELECTRIC_POLE,
    )
}


def factorio_entity_profile(name: str) -> FactorioEntityProfile:
    try:
        return _PROFILES[name]
    except KeyError as error:
        raise ValueError(f"Unknown Factorio entity prototype {name!r}") from error