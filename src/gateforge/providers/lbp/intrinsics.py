from __future__ import annotations

from gateforge.intrinsics import DEFAULT_INTRINSICS, IntrinsicKind
from gateforge.mapping import (
    BoundaryBinding,
    MappingCostEstimate,
    MappingDisposition,
    MappingProposal,
    MappingProvider,
)
from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.configuration import (
    LBPCounterConfiguration,
    LBPObjectConfigurationCodec,
    LBPRandomizerConfiguration,
    LBPRandomizerInputAction,
    LBPRandomizerMode,
    LBPTimerConfiguration,
    LBPTimerMode,
)
from gateforge.providers.lbp.types import (
    LBPCounterType,
    LBPObjectType,
    LBPRandomizerType,
    LBPSelectorType,
    LBPTimerType,
)
from gateforge.source import (
    CellPortIdentifier,
    CellSnapshot,
    ConstantBit,
    ConstantBoundarySource,
    DesignSnapshot,
)
from gateforge.target import (
    ObjectPortRef,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    ProviderConfiguration,
    SemanticPrefab,
)


_TIMER_MODE_ALIASES = {
    "OFF_ON": LBPTimerMode.ON_OFF,
    "ONESHOT": LBPTimerMode.START_COUNT_UP,
    "ONE_SHOT": LBPTimerMode.START_COUNT_UP,
    "DIRECTION": LBPTimerMode.FORWARD_BACKWARD,
}
_RANDOMIZER_MODE_ALIASES = {
    "ADD_RESET": LBPRandomizerMode.ADD_AND_RESET,
    "ONE": LBPRandomizerMode.ONE_AT_A_TIME,
}
_RANDOMIZER_ACTION_ALIASES = {
    "OVERRIDE": LBPRandomizerInputAction.OVERRIDE_PATTERN,
}


def _timer_mode(value: str) -> LBPTimerMode:
    normalized = value.strip().upper()
    try:
        return _TIMER_MODE_ALIASES.get(normalized, LBPTimerMode(normalized))
    except ValueError as error:
        choices = ", ".join(mode.value for mode in LBPTimerMode)
        raise ValueError(
            f"Unsupported GF_Timer MODE {value!r}; expected one of {choices}"
        ) from error


def _boundary_bindings(
    cell: CellSnapshot,
    port_name: str,
) -> frozenset[BoundaryBinding]:
    bindings: set[BoundaryBinding] = set()
    for index, source in enumerate(cell.ports[port_name].bits):
        if isinstance(source, ConstantBit):
            source = ConstantBoundarySource(
                source.value,
                CellPortIdentifier(cell.identifier, port_name, index),
            )
        bindings.add(BoundaryBinding(source, PrefabPortRef(port_name, index)))
    return frozenset(bindings)


def _intrinsic_prefab(
    role: str,
    object_type: LBPObjectType,
    configuration: object | None,
    port_map: tuple[tuple[str, PortDirection, tuple[str, ...]], ...],
) -> SemanticPrefab:
    identifier = object_type.get_type()
    encoded = (
        ProviderConfiguration()
        if configuration is None
        else LBPObjectConfigurationCodec().encode(identifier, configuration)
    )
    return SemanticPrefab(
        provider=LBP_PROVIDER,
        objects=frozenset({PrefabObject(role, identifier, encoded)}),
        ports=frozenset(
            PrefabPort(name, direction, LBP_LOGIC, len(object_ports))
            for name, direction, object_ports in port_map
        ),
        nets=frozenset(
            PrefabNet(
                f"{name}_{index}",
                LBP_WIRE,
                frozenset(
                    {
                        PrefabPortRef(name, index),
                        ObjectPortRef(role, object_port),
                    }
                ),
            )
            for name, _, object_ports in port_map
            for index, object_port in enumerate(object_ports)
        ),
    )


def _parameter_int(cell: CellSnapshot, name: str, default: int) -> int:
    return (
        default
        if name not in cell.parameters
        else cell.parameter(name).as_unsigned_int()
    )


def _parameter_text(cell: CellSnapshot, name: str, default: str) -> str:
    return (
        default
        if name not in cell.parameters
        else cell.parameter(name).as_ascii_string()
    )


def _parameter_bool(cell: CellSnapshot, name: str, default: bool) -> bool:
    value = _parameter_int(cell, name, int(default))
    if value not in {0, 1}:
        raise ValueError(f"Intrinsic parameter {name} must be zero or one")
    return bool(value)


def _randomizer_mode(value: str) -> LBPRandomizerMode:
    normalized = value.strip().upper()
    try:
        return _RANDOMIZER_MODE_ALIASES.get(
            normalized,
            LBPRandomizerMode(normalized),
        )
    except ValueError as error:
        choices = ", ".join(mode.value for mode in LBPRandomizerMode)
        raise ValueError(
            f"Unsupported GF_Randomizer MODE {value!r}; expected one of {choices}"
        ) from error


def _randomizer_action(value: str) -> LBPRandomizerInputAction:
    normalized = value.strip().upper()
    try:
        return _RANDOMIZER_ACTION_ALIASES.get(
            normalized,
            LBPRandomizerInputAction(normalized),
        )
    except ValueError as error:
        choices = ", ".join(action.value for action in LBPRandomizerInputAction)
        raise ValueError(
            f"Unsupported GF_Randomizer INPUT_ACTION {value!r}; expected one of "
            f"{choices}"
        ) from error


def _require_port_width(cell: CellSnapshot, name: str, width: int) -> None:
    actual = len(cell.ports[name].bits)
    if actual != width:
        raise ValueError(
            f"Intrinsic port {cell.identifier.name}.{name} has width {actual}, "
            f"expected {width}"
        )


def _prefab_for(cell: CellSnapshot, kind: IntrinsicKind) -> SemanticPrefab:
    if kind == IntrinsicKind.TIMER:
        configuration = LBPTimerConfiguration(
            _parameter_int(cell, "TIME_DS", 50),
            _timer_mode(_parameter_text(cell, "MODE", "ON_OFF")),
        )
        return _intrinsic_prefab(
            "timer",
            LBPTimerType(),
            configuration,
            (
                ("in", PortDirection.INPUT, ("IN_0",)),
                ("reset", PortDirection.INPUT, ("IN_1",)),
                ("out", PortDirection.OUTPUT, ("OUT",)),
            ),
        )
    if kind == IntrinsicKind.COUNTER:
        configuration = LBPCounterConfiguration(
            _parameter_int(cell, "TARGET", 10)
        )
        return _intrinsic_prefab(
            "counter",
            LBPCounterType(),
            configuration,
            (
                ("in", PortDirection.INPUT, ("IN_0",)),
                ("reset", PortDirection.INPUT, ("IN_1",)),
                ("out", PortDirection.OUTPUT, ("OUT",)),
            ),
        )
    if kind == IntrinsicKind.RANDOMIZER:
        outputs = _parameter_int(cell, "OUTPUTS", 2)
        _require_port_width(cell, "out", outputs)
        configuration = LBPRandomizerConfiguration(
            mode=_randomizer_mode(_parameter_text(cell, "MODE", "ADD")),
            input_action=_randomizer_action(
                _parameter_text(cell, "INPUT_ACTION", "TRIGGER")
            ),
            new_pick=_parameter_bool(cell, "NEW_PICK", False),
            on_min_ds=_parameter_int(cell, "ON_MIN_DS", 10),
            on_max_ds=_parameter_int(cell, "ON_MAX_DS", 10),
            off_min_ds=_parameter_int(cell, "OFF_MIN_DS", 0),
            off_max_ds=_parameter_int(cell, "OFF_MAX_DS", 0),
        )
        return _intrinsic_prefab(
            "randomizer",
            LBPRandomizerType(outputs),
            configuration,
            (
                ("in", PortDirection.INPUT, ("IN_0",)),
                (
                    "out",
                    PortDirection.OUTPUT,
                    tuple(f"OUT_{index}" for index in range(outputs)),
                ),
            ),
        )
    if kind == IntrinsicKind.SELECTOR:
        width = _parameter_int(cell, "WIDTH", 2)
        _require_port_width(cell, "in", width)
        _require_port_width(cell, "out", width)
        return _intrinsic_prefab(
            "selector",
            LBPSelectorType(width),
            None,
            (
                ("cycle", PortDirection.INPUT, ("IN_0",)),
                (
                    "in",
                    PortDirection.INPUT,
                    tuple(f"IN_{index + 1}" for index in range(width)),
                ),
                (
                    "out",
                    PortDirection.OUTPUT,
                    tuple(f"OUT_{index}" for index in range(width)),
                ),
            ),
        )
    raise ValueError(f"Unsupported intrinsic kind {kind.value!r}")


class LBPIntrinsicMapper(MappingProvider):
    provider = LBP_PROVIDER
    stages = frozenset({"source"})
    rule_version = 1

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        proposals: list[MappingProposal] = []
        for module in sorted(design.modules.values(), key=lambda item: item.name):
            if "blackbox" in module.attributes:
                continue
            for cell in sorted(
                module.cells.values(), key=lambda item: item.identifier.name
            ):
                instance = DEFAULT_INTRINSICS.recognize(design, cell)
                if instance is None:
                    continue
                prefab = _prefab_for(cell, instance.definition.kind)
                mapper_id = f"lbp.intrinsic.{instance.definition.kind.value}"
                proposals.append(
                    MappingProposal(
                        revision=design.revision,
                        provider=self.provider,
                        mapper=mapper_id,
                        rule=instance.definition.module,
                        rule_version=self.rule_version,
                        ids=frozenset({cell.identifier}),
                        prefab=prefab,
                        boundary=frozenset(
                            binding
                            for port in prefab.ports
                            for binding in _boundary_bindings(cell, port.name)
                        ),
                        disposition=MappingDisposition.REQUIRED,
                        cost=MappingCostEstimate(1.0, 1.0),
                    )
                )
        return tuple(proposals)