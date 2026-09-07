from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from gateforge.mapping import (
    BoundaryBinding,
    MappingCostEstimate,
    MappingDisposition,
    MappingProposal,
    MappingProvider,
)
from gateforge.material import ImplementationPackaging
from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.configuration import (
    LBPCounterConfiguration,
    LBPObjectConfigurationCodec,
    LBPSelectorStateConfiguration,
)
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPCounterType,
    LBPNotGateType,
    LBPOrGateType,
    LBPPhaseSelectorType,
    LBPStorageSelectorType,
)
from gateforge.source import (
    BoundarySource,
    CellPortIdentifier,
    CellSnapshot,
    ConstantBit,
    ConstantBoundarySource,
    DesignSnapshot,
    SourceBit,
)
from gateforge.target import (
    ObjectPortRef,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
)


class RegisterFamily(StrEnum):
    DFF = "dff"
    ADFF = "adff"
    SDFF = "sdff"


class LBPRegisterStyle(StrEnum):
    COMPACT = "compact"
    HARDENED = "hardened"


@dataclass(frozen=True, slots=True)
class _RegisterCell:
    cell: CellSnapshot
    family: RegisterFamily
    width: int
    clock_polarity: bool
    reset_polarity: bool | None
    reset_bits: tuple[int, ...]
    clock: SourceBit
    reset: SourceBit | None
    clock_port: str
    reset_port: str | None
    enable: SourceBit | None
    enable_polarity: bool | None
    enable_port: str | None
    enable_priority: bool


@dataclass(frozen=True, slots=True)
class _BankKey:
    module: str
    family: RegisterFamily
    clock: SourceBit
    clock_polarity: bool
    reset: SourceBit | None
    reset_polarity: bool | None
    enable_priority: bool


def _parameter_bool(cell: CellSnapshot, name: str) -> bool:
    value = cell.parameter(name).as_unsigned_int()
    if value not in {0, 1}:
        raise ValueError(f"Register parameter {name} must be zero or one")
    return bool(value)


def _single_port_bit(cell: CellSnapshot, name: str) -> SourceBit:
    bits = cell.ports[name].bits
    if len(bits) != 1:
        raise ValueError(f"Register port {cell.identifier.name}.{name} must be scalar")
    return bits[0]


def _decode_register(cell: CellSnapshot) -> _RegisterCell | None:
    cell_type = cell.identifier.expected_type
    coarse_types = {
        "$dff": (RegisterFamily.DFF, False, False),
        "$dffe": (RegisterFamily.DFF, True, False),
        "$adff": (RegisterFamily.ADFF, False, False),
        "$adffe": (RegisterFamily.ADFF, True, False),
        "$sdff": (RegisterFamily.SDFF, False, False),
        "$sdffe": (RegisterFamily.SDFF, True, False),
        "$sdffce": (RegisterFamily.SDFF, True, True),
    }
    decoded = coarse_types.get(cell_type)
    if decoded is None:
        return None
    family, has_enable, enable_priority = decoded
    width = cell.parameter("WIDTH").as_unsigned_int()
    if width <= 0:
        raise ValueError("Register width must be positive")
    if len(cell.ports["D"].bits) != width or len(cell.ports["Q"].bits) != width:
        raise ValueError(f"Register {cell.identifier.name} width is inconsistent")
    clock = _single_port_bit(cell, "CLK")
    clock_polarity = _parameter_bool(cell, "CLK_POLARITY")
    enable = _single_port_bit(cell, "EN") if has_enable else None
    enable_polarity = _parameter_bool(cell, "EN_POLARITY") if has_enable else None
    if family == RegisterFamily.DFF:
        return _RegisterCell(
            cell,
            family,
            width,
            clock_polarity,
            None,
            tuple(0 for _ in range(width)),
            clock,
            None,
            "CLK",
            None,
            enable,
            enable_polarity,
            "EN" if has_enable else None,
            enable_priority,
        )
    reset_port = "ARST" if family == RegisterFamily.ADFF else "SRST"
    reset_prefix = "ARST" if family == RegisterFamily.ADFF else "SRST"
    return _RegisterCell(
        cell,
        family,
        width,
        clock_polarity,
        _parameter_bool(cell, f"{reset_prefix}_POLARITY"),
        cell.parameter(f"{reset_prefix}_VALUE").as_binary_bits(width),
        clock,
        _single_port_bit(cell, reset_port),
        "CLK",
        reset_port,
        enable,
        enable_polarity,
        "EN" if has_enable else None,
        enable_priority,
    )


_SCALAR_DFF = re.compile(r"\$_DFF_([NP])_$", re.ASCII)
_SCALAR_DFFE = re.compile(r"\$_DFFE_([NP])([NP])_$", re.ASCII)
_SCALAR_RESET_DFF = re.compile(r"\$_(S?DFF)_([NP])([NP])([01])_$", re.ASCII)
_SCALAR_RESET_DFFE = re.compile(
    r"\$_(DFFE|SDFFE|SDFFCE)_([NP])([NP])([01])([NP])_$",
    re.ASCII,
)


def _decode_scalar_register(cell: CellSnapshot) -> _RegisterCell | None:
    cell_type = cell.identifier.expected_type
    plain = _SCALAR_DFF.fullmatch(cell_type)
    if plain is not None:
        return _RegisterCell(
            cell=cell,
            family=RegisterFamily.DFF,
            width=1,
            clock_polarity=plain.group(1) == "P",
            reset_polarity=None,
            reset_bits=(0,),
            clock=_single_port_bit(cell, "C"),
            reset=None,
            clock_port="C",
            reset_port=None,
            enable=None,
            enable_polarity=None,
            enable_port=None,
            enable_priority=False,
        )
    enabled = _SCALAR_DFFE.fullmatch(cell_type)
    if enabled is not None:
        return _RegisterCell(
            cell=cell,
            family=RegisterFamily.DFF,
            width=1,
            clock_polarity=enabled.group(1) == "P",
            reset_polarity=None,
            reset_bits=(0,),
            clock=_single_port_bit(cell, "C"),
            reset=None,
            clock_port="C",
            reset_port=None,
            enable=_single_port_bit(cell, "E"),
            enable_polarity=enabled.group(2) == "P",
            enable_port="E",
            enable_priority=False,
        )
    reset_enabled = _SCALAR_RESET_DFFE.fullmatch(cell_type)
    if reset_enabled is not None:
        family_name = reset_enabled.group(1)
        return _RegisterCell(
            cell=cell,
            family=(
                RegisterFamily.ADFF
                if family_name == "DFFE"
                else RegisterFamily.SDFF
            ),
            width=1,
            clock_polarity=reset_enabled.group(2) == "P",
            reset_polarity=reset_enabled.group(3) == "P",
            reset_bits=(int(reset_enabled.group(4)),),
            clock=_single_port_bit(cell, "C"),
            reset=_single_port_bit(cell, "R"),
            clock_port="C",
            reset_port="R",
            enable=_single_port_bit(cell, "E"),
            enable_polarity=reset_enabled.group(5) == "P",
            enable_port="E",
            enable_priority=family_name == "SDFFCE",
        )
    reset = _SCALAR_RESET_DFF.fullmatch(cell_type)
    if reset is None:
        return None
    return _RegisterCell(
        cell=cell,
        family=(
            RegisterFamily.SDFF
            if reset.group(1) == "SDFF"
            else RegisterFamily.ADFF
        ),
        width=1,
        clock_polarity=reset.group(2) == "P",
        reset_polarity=reset.group(3) == "P",
        reset_bits=(int(reset.group(4)),),
        clock=_single_port_bit(cell, "C"),
        reset=_single_port_bit(cell, "R"),
        clock_port="C",
        reset_port="R",
        enable=None,
        enable_polarity=None,
        enable_port=None,
        enable_priority=False,
    )


def _boundary_source(
    cell: CellSnapshot,
    port_name: str,
    bit: int,
) -> BoundarySource:
    source = cell.ports[port_name].bits[bit]
    if isinstance(source, ConstantBit):
        return ConstantBoundarySource(
            source.value,
            CellPortIdentifier(cell.identifier, port_name, bit),
        )
    return source


class _PrefabBuilder:
    def __init__(self) -> None:
        self.objects: set[PrefabObject] = set()
        self.ports: set[PrefabPort] = set()
        self.nets: set[PrefabNet] = set()

    def object(self, role: str, object_type, configuration=None) -> None:
        encoded = (
            LBPObjectConfigurationCodec().encode(object_type.get_type(), configuration)
            if configuration is not None
            else None
        )
        self.objects.add(
            PrefabObject(role, object_type.get_type())
            if encoded is None
            else PrefabObject(role, object_type.get_type(), encoded)
        )

    def net(self, role: str, *attachments) -> None:
        self.nets.add(PrefabNet(role, LBP_WIRE, frozenset(attachments)))

    def build(self) -> SemanticPrefab:
        return SemanticPrefab(
            provider=LBP_PROVIDER,
            objects=frozenset(self.objects),
            ports=frozenset(self.ports),
            nets=frozenset(self.nets),
        )


def _compact_register_bank_prefab(
    family: RegisterFamily,
    width: int,
    clock_polarity: bool,
    reset_polarity: bool | None,
    reset_bits: tuple[int, ...],
    enable_indices: tuple[int | None, ...],
    enable_polarities: tuple[bool, ...],
    enable_priority: bool,
) -> SemanticPrefab:
    builder = _PrefabBuilder()
    builder.ports.update(
        {
            PrefabPort("D", PortDirection.INPUT, LBP_LOGIC, width),
            PrefabPort("Q", PortDirection.OUTPUT, LBP_LOGIC, width),
            PrefabPort("CLK", PortDirection.INPUT, LBP_LOGIC),
        }
    )
    if family != RegisterFamily.DFF:
        builder.ports.add(PrefabPort("RESET", PortDirection.INPUT, LBP_LOGIC))
    builder.ports.update(
        PrefabPort(f"EN_{index}", PortDirection.INPUT, LBP_LOGIC)
        for index in range(len(enable_polarities))
    )

    edge = "edge"
    builder.object(edge, LBPCounterType(), LBPCounterConfiguration(1))
    if clock_polarity:
        builder.net("clock", PrefabPortRef("CLK"), ObjectPortRef(edge, "IN_0"))
    else:
        builder.object("invert_clock", LBPNotGateType(1, False))
        builder.net(
            "clock",
            PrefabPortRef("CLK"),
            ObjectPortRef("invert_clock", "IN_0"),
        )
        builder.net(
            "clock_active",
            ObjectPortRef("invert_clock", "OUT"),
            ObjectPortRef(edge, "IN_0"),
        )

    pulse_sinks = [ObjectPortRef(edge, "IN_1")]
    reset_active_sinks: list[ObjectPortRef] = []
    reset_inactive_sinks: list[ObjectPortRef] = []
    enable_active_sinks = {index: [] for index in range(len(enable_polarities))}
    enable_inactive_sinks = {index: [] for index in range(len(enable_polarities))}
    for bit in range(width):
        data_source: ObjectPortRef | PrefabPortRef = PrefabPortRef("D", bit)
        q_feedback_sink: ObjectPortRef | None = None
        if family == RegisterFamily.SDFF and enable_priority:
            data_source = _condition_sync_reset(
                builder,
                bit,
                data_source,
                reset_bits[bit],
                reset_active_sinks,
                reset_inactive_sinks,
            )
        enable_index = enable_indices[bit]
        if enable_index is not None:
            data_source, q_feedback_sink = _condition_enable(
                builder,
                bit,
                data_source,
                enable_active_sinks[enable_index],
                enable_inactive_sinks[enable_index],
            )
        if family == RegisterFamily.SDFF and not enable_priority:
            data_source = _condition_sync_reset(
                builder,
                bit,
                data_source,
                reset_bits[bit],
                reset_active_sinks,
                reset_inactive_sinks,
            )

        invert_role = f"invert_d_{bit}"
        true_role = f"write_true_{bit}"
        false_role = f"write_false_{bit}"
        storage_role = f"storage_{bit}"
        gate_width = 3 if family == RegisterFamily.ADFF else 2
        builder.object(invert_role, LBPNotGateType(1, False))
        builder.object(true_role, LBPAndGateType(gate_width, False))
        builder.object(false_role, LBPAndGateType(gate_width, False))
        builder.object(
            storage_role,
            LBPStorageSelectorType(),
            LBPSelectorStateConfiguration(0 if reset_bits[bit] else 1),
        )
        builder.net(
            f"data_fanout_{bit}",
            data_source,
            ObjectPortRef(invert_role, "IN_0"),
            ObjectPortRef(true_role, "IN_1"),
        )
        builder.net(
            f"not_d_{bit}",
            ObjectPortRef(invert_role, "OUT"),
            ObjectPortRef(false_role, "IN_1"),
        )
        pulse_sinks.extend(
            {
                ObjectPortRef(true_role, "IN_0"),
                ObjectPortRef(false_role, "IN_0"),
            }
        )
        true_output: ObjectPortRef = ObjectPortRef(true_role, "OUT")
        false_output: ObjectPortRef = ObjectPortRef(false_role, "OUT")
        if family == RegisterFamily.ADFF:
            reset_inactive_sinks.extend(
                {
                    ObjectPortRef(true_role, "IN_2"),
                    ObjectPortRef(false_role, "IN_2"),
                }
            )
            force_role = f"force_reset_{bit}"
            builder.object(force_role, LBPOrGateType(2, False))
            reset_active_sinks.append(ObjectPortRef(force_role, "IN_1"))
            if reset_bits[bit]:
                builder.net(
                    f"normal_true_{bit}",
                    true_output,
                    ObjectPortRef(force_role, "IN_0"),
                )
                true_output = ObjectPortRef(force_role, "OUT")
            else:
                builder.net(
                    f"normal_false_{bit}",
                    false_output,
                    ObjectPortRef(force_role, "IN_0"),
                )
                false_output = ObjectPortRef(force_role, "OUT")
        builder.net(
            f"select_true_{bit}",
            true_output,
            ObjectPortRef(storage_role, "IN_1"),
        )
        builder.net(
            f"select_false_{bit}",
            false_output,
            ObjectPortRef(storage_role, "IN_2"),
        )
        builder.net(
            f"q_{bit}",
            ObjectPortRef(storage_role, "OUT"),
            PrefabPortRef("Q", bit),
            *(() if q_feedback_sink is None else (q_feedback_sink,)),
        )

    builder.net("edge_pulse", ObjectPortRef(edge, "OUT"), *pulse_sinks)
    _connect_reset(builder, family, reset_polarity, reset_active_sinks, reset_inactive_sinks)
    _connect_enables(
        builder,
        enable_polarities,
        enable_active_sinks,
        enable_inactive_sinks,
    )
    return builder.build()


def _condition_sync_reset(
    builder: _PrefabBuilder,
    bit: int,
    source: ObjectPortRef | PrefabPortRef,
    reset_value: int,
    active_sinks: list[ObjectPortRef],
    inactive_sinks: list[ObjectPortRef],
) -> ObjectPortRef:
    role = f"sync_data_{bit}"
    gate = (
        LBPOrGateType(2, False)
        if reset_value
        else LBPAndGateType(2, False)
    )
    builder.object(role, gate)
    builder.net(f"sync_data_input_{bit}", source, ObjectPortRef(role, "IN_0"))
    (active_sinks if reset_value else inactive_sinks).append(
        ObjectPortRef(role, "IN_1")
    )
    return ObjectPortRef(role, "OUT")


def _condition_enable(
    builder: _PrefabBuilder,
    bit: int,
    source: ObjectPortRef | PrefabPortRef,
    active_sinks: list[ObjectPortRef],
    inactive_sinks: list[ObjectPortRef],
) -> tuple[ObjectPortRef, ObjectPortRef]:
    data_role = f"enable_data_{bit}"
    hold_role = f"enable_hold_{bit}"
    mux_role = f"enable_mux_{bit}"
    builder.object(data_role, LBPAndGateType(2, False))
    builder.object(hold_role, LBPAndGateType(2, False))
    builder.object(mux_role, LBPOrGateType(2, False))
    builder.net(
        f"enable_data_input_{bit}",
        source,
        ObjectPortRef(data_role, "IN_0"),
    )
    active_sinks.append(ObjectPortRef(data_role, "IN_1"))
    feedback_sink = ObjectPortRef(hold_role, "IN_0")
    inactive_sinks.append(ObjectPortRef(hold_role, "IN_1"))
    builder.net(
        f"enable_data_path_{bit}",
        ObjectPortRef(data_role, "OUT"),
        ObjectPortRef(mux_role, "IN_0"),
    )
    builder.net(
        f"enable_hold_path_{bit}",
        ObjectPortRef(hold_role, "OUT"),
        ObjectPortRef(mux_role, "IN_1"),
    )
    return ObjectPortRef(mux_role, "OUT"), feedback_sink


def _connect_reset(
    builder: _PrefabBuilder,
    family: RegisterFamily,
    reset_polarity: bool | None,
    active_sinks: list[ObjectPortRef],
    inactive_sinks: list[ObjectPortRef],
) -> None:
    if family == RegisterFamily.DFF:
        return
    if reset_polarity is None:
        raise AssertionError("Resettable register bank has no reset polarity")
    if reset_polarity:
        direct_sinks = active_sinks
        inverted_sinks = inactive_sinks
    else:
        direct_sinks = inactive_sinks
        inverted_sinks = active_sinks
    reset_attachments: list[ObjectPortRef | PrefabPortRef] = [
        PrefabPortRef("RESET"),
        *direct_sinks,
    ]
    if inverted_sinks:
        builder.object("invert_reset", LBPNotGateType(1, False))
        reset_attachments.append(ObjectPortRef("invert_reset", "IN_0"))
        builder.net(
            "reset_inverted",
            ObjectPortRef("invert_reset", "OUT"),
            *inverted_sinks,
        )
    builder.net("reset", *reset_attachments)


def _connect_enables(
    builder: _PrefabBuilder,
    polarities: tuple[bool, ...],
    active_sinks: dict[int, list[ObjectPortRef]],
    inactive_sinks: dict[int, list[ObjectPortRef]],
) -> None:
    for index, polarity in enumerate(polarities):
        if polarity:
            direct_sinks = active_sinks[index]
            inverted_sinks = inactive_sinks[index]
        else:
            direct_sinks = inactive_sinks[index]
            inverted_sinks = active_sinks[index]
        invert_role = f"invert_enable_{index}"
        builder.object(invert_role, LBPNotGateType(1, False))
        builder.net(
            f"enable_{index}",
            PrefabPortRef(f"EN_{index}"),
            *direct_sinks,
            ObjectPortRef(invert_role, "IN_0"),
        )
        builder.net(
            f"enable_inverted_{index}",
            ObjectPortRef(invert_role, "OUT"),
            *inverted_sinks,
        )


def _hardened_register_bank_prefab(
    family: RegisterFamily,
    width: int,
    clock_polarity: bool,
    reset_polarity: bool | None,
    reset_bits: tuple[int, ...],
    enable_indices: tuple[int | None, ...],
    enable_polarities: tuple[bool, ...],
    enable_priority: bool,
) -> SemanticPrefab:
    builder = _PrefabBuilder()
    builder.ports.update(
        {
            PrefabPort("D", PortDirection.INPUT, LBP_LOGIC, width),
            PrefabPort("Q", PortDirection.OUTPUT, LBP_LOGIC, width),
            PrefabPort("CLK", PortDirection.INPUT, LBP_LOGIC),
        }
    )
    if family != RegisterFamily.DFF:
        builder.ports.add(PrefabPort("RESET", PortDirection.INPUT, LBP_LOGIC))
    builder.ports.update(
        PrefabPort(f"EN_{index}", PortDirection.INPUT, LBP_LOGIC)
        for index in range(len(enable_polarities))
    )

    phase = "phase"
    builder.object(
        phase,
        LBPPhaseSelectorType(),
        LBPSelectorStateConfiguration(0),
    )
    if clock_polarity:
        builder.net("clock", PrefabPortRef("CLK"), ObjectPortRef(phase, "IN_0"))
    else:
        builder.object("invert_clock", LBPNotGateType(1, False))
        builder.net(
            "clock",
            PrefabPortRef("CLK"),
            ObjectPortRef("invert_clock", "IN_0"),
        )
        builder.net(
            "clock_active",
            ObjectPortRef("invert_clock", "OUT"),
            ObjectPortRef(phase, "IN_0"),
        )

    phase_sinks: dict[int, list[ObjectPortRef]] = {0: [], 1: []}
    reset_active_sinks: list[ObjectPortRef] = []
    reset_inactive_sinks: list[ObjectPortRef] = []
    enable_active_sinks = {index: [] for index in range(len(enable_polarities))}
    enable_inactive_sinks = {index: [] for index in range(len(enable_polarities))}
    for bit in range(width):
        data_source: ObjectPortRef | PrefabPortRef = PrefabPortRef("D", bit)
        q_feedback_sink: ObjectPortRef | None = None
        if family == RegisterFamily.SDFF and enable_priority:
            data_source = _condition_sync_reset(
                builder,
                bit,
                data_source,
                reset_bits[bit],
                reset_active_sinks,
                reset_inactive_sinks,
            )
        enable_index = enable_indices[bit]
        if enable_index is not None:
            data_source, q_feedback_sink = _condition_enable(
                builder,
                bit,
                data_source,
                enable_active_sinks[enable_index],
                enable_inactive_sinks[enable_index],
            )
        if family == RegisterFamily.SDFF and not enable_priority:
            data_source = _condition_sync_reset(
                builder,
                bit,
                data_source,
                reset_bits[bit],
                reset_active_sinks,
                reset_inactive_sinks,
            )

        data_true_sinks: list[ObjectPortRef] = []
        for bank in range(2):
            invert_role = f"invert_d_{bit}_{bank}"
            true_role = f"write_true_{bit}_{bank}"
            false_role = f"write_false_{bit}_{bank}"
            storage_role = f"storage_{bit}_{bank}"
            true_width = 3 if family == RegisterFamily.ADFF else 2
            builder.object(invert_role, LBPNotGateType(1, False))
            builder.object(true_role, LBPAndGateType(true_width, False))
            builder.object(false_role, LBPAndGateType(true_width, False))
            builder.object(
                storage_role,
                LBPStorageSelectorType(),
                LBPSelectorStateConfiguration(0 if reset_bits[bit] else 1),
            )
            data_true_sinks.extend(
                {
                    ObjectPortRef(invert_role, "IN_0"),
                    ObjectPortRef(true_role, "IN_1"),
                }
            )
            builder.net(
                f"not_d_{bit}_{bank}",
                ObjectPortRef(invert_role, "OUT"),
                ObjectPortRef(false_role, "IN_1"),
            )
            phase_sinks[bank].extend(
                {
                    ObjectPortRef(true_role, "IN_0"),
                    ObjectPortRef(false_role, "IN_0"),
                }
            )
            true_output: ObjectPortRef = ObjectPortRef(true_role, "OUT")
            false_output: ObjectPortRef = ObjectPortRef(false_role, "OUT")
            if family == RegisterFamily.ADFF:
                reset_inactive_sinks.extend(
                    {
                        ObjectPortRef(true_role, "IN_2"),
                        ObjectPortRef(false_role, "IN_2"),
                    }
                )
                force_role = f"force_reset_{bit}_{bank}"
                builder.object(force_role, LBPOrGateType(2, False))
                reset_active_sinks.append(ObjectPortRef(force_role, "IN_1"))
                if reset_bits[bit]:
                    builder.net(
                        f"normal_true_{bit}_{bank}",
                        true_output,
                        ObjectPortRef(force_role, "IN_0"),
                    )
                    true_output = ObjectPortRef(force_role, "OUT")
                else:
                    builder.net(
                        f"normal_false_{bit}_{bank}",
                        false_output,
                        ObjectPortRef(force_role, "IN_0"),
                    )
                    false_output = ObjectPortRef(force_role, "OUT")
            builder.net(
                f"select_true_{bit}_{bank}",
                true_output,
                ObjectPortRef(storage_role, "IN_1"),
            )
            builder.net(
                f"select_false_{bit}_{bank}",
                false_output,
                ObjectPortRef(storage_role, "IN_2"),
            )

        builder.net(f"data_fanout_{bit}", data_source, *data_true_sinks)
        read_roles = (f"read_{bit}_0", f"read_{bit}_1")
        for bank, read_role in enumerate(read_roles):
            builder.object(read_role, LBPAndGateType(2, False))
            builder.net(
                f"stored_{bit}_{bank}",
                ObjectPortRef(f"storage_{bit}_{bank}", "OUT"),
                ObjectPortRef(read_role, "IN_0"),
            )
            phase_sinks[1 - bank].append(ObjectPortRef(read_role, "IN_1"))
        output_role = f"output_{bit}"
        builder.object(output_role, LBPOrGateType(2, False))
        builder.net(
            f"read_0_{bit}",
            ObjectPortRef(read_roles[0], "OUT"),
            ObjectPortRef(output_role, "IN_0"),
        )
        builder.net(
            f"read_1_{bit}",
            ObjectPortRef(read_roles[1], "OUT"),
            ObjectPortRef(output_role, "IN_1"),
        )
        builder.net(
            f"q_{bit}",
            ObjectPortRef(output_role, "OUT"),
            PrefabPortRef("Q", bit),
            *(() if q_feedback_sink is None else (q_feedback_sink,)),
        )

    for phase_index, sinks in phase_sinks.items():
        builder.net(
            f"phase_{phase_index}",
            ObjectPortRef(phase, f"OUT_{phase_index}"),
            *sinks,
        )
    _connect_reset(builder, family, reset_polarity, reset_active_sinks, reset_inactive_sinks)
    _connect_enables(
        builder,
        enable_polarities,
        enable_active_sinks,
        enable_inactive_sinks,
    )
    return builder.build()


def _register_bank_prefab(
    family: RegisterFamily,
    width: int,
    clock_polarity: bool,
    reset_polarity: bool | None,
    reset_bits: tuple[int, ...],
    enable_indices: tuple[int | None, ...],
    enable_polarities: tuple[bool, ...],
    enable_priority: bool,
    style: LBPRegisterStyle,
) -> SemanticPrefab:
    builder = (
        _compact_register_bank_prefab
        if style == LBPRegisterStyle.COMPACT
        else _hardened_register_bank_prefab
    )
    return builder(
        family,
        width,
        clock_polarity,
        reset_polarity,
        reset_bits,
        enable_indices,
        enable_polarities,
        enable_priority,
    )


def _map_registers(
    design: DesignSnapshot,
    mapper_id: str,
    rule_version: int,
    decoder,
    style: LBPRegisterStyle,
) -> tuple[MappingProposal, ...]:
    proposals: list[MappingProposal] = []
    for module in sorted(design.modules.values(), key=lambda item: item.name):
        if "blackbox" in module.attributes:
            continue
        groups: dict[_BankKey, list[_RegisterCell]] = {}
        for cell in module.cells.values():
            register = decoder(cell)
            if register is None:
                continue
            key = _BankKey(
                module.name,
                register.family,
                register.clock,
                register.clock_polarity,
                register.reset,
                register.reset_polarity,
                register.enable_priority,
            )
            groups.setdefault(key, []).append(register)
        for key, registers in sorted(
            groups.items(),
            key=lambda item: (
                item[0].family.value,
                repr(item[0].clock),
                repr(item[0].reset),
                item[0].enable_priority,
            ),
        ):
                ordered = tuple(
                    sorted(registers, key=lambda item: item.cell.identifier.name)
                )
                width = sum(item.width for item in ordered)
                reset_bits = tuple(
                    bit for item in ordered for bit in item.reset_bits
                )
                enable_keys = tuple(
                    sorted(
                        {
                            (item.enable, item.enable_polarity)
                            for item in ordered
                            if item.enable is not None
                            and item.enable_polarity is not None
                        },
                        key=lambda item: (repr(item[0]), item[1]),
                    )
                )
                enable_key_to_index = {
                    enable_key: index
                    for index, enable_key in enumerate(enable_keys)
                }
                enable_indices = tuple(
                    (
                        None
                        if item.enable is None or item.enable_polarity is None
                        else enable_key_to_index[
                            (item.enable, item.enable_polarity)
                        ]
                    )
                    for item in ordered
                    for _ in range(item.width)
                )
                enable_polarities = tuple(
                    polarity for _, polarity in enable_keys
                )
                prefab = _register_bank_prefab(
                    key.family,
                    width,
                    key.clock_polarity,
                    key.reset_polarity,
                    reset_bits,
                    enable_indices,
                    enable_polarities,
                    key.enable_priority,
                    style,
                )
                boundary: set[BoundaryBinding] = set()
                bit_offset = 0
                for register in ordered:
                    for bit in range(register.width):
                        boundary.add(
                            BoundaryBinding(
                                _boundary_source(register.cell, "D", bit),
                                PrefabPortRef("D", bit_offset + bit),
                            )
                        )
                        boundary.add(
                            BoundaryBinding(
                                _boundary_source(register.cell, "Q", bit),
                                PrefabPortRef("Q", bit_offset + bit),
                            )
                        )
                    bit_offset += register.width
                first = ordered[0].cell
                boundary.add(
                    BoundaryBinding(
                        _boundary_source(first, ordered[0].clock_port, 0),
                        PrefabPortRef("CLK"),
                    )
                )
                if key.family != RegisterFamily.DFF:
                    reset_port = ordered[0].reset_port
                    if reset_port is None:
                        raise AssertionError("Resettable bank has no reset port")
                    boundary.add(
                        BoundaryBinding(
                            _boundary_source(first, reset_port, 0),
                            PrefabPortRef("RESET"),
                        )
                    )
                for enable_index, enable_key in enumerate(enable_keys):
                    enable_source, _ = enable_key
                    register = next(
                        item for item in ordered if item.enable == enable_source
                    )
                    enable_port = register.enable_port
                    if enable_port is None:
                        raise AssertionError("Enabled register has no enable port")
                    boundary.add(
                        BoundaryBinding(
                            _boundary_source(register.cell, enable_port, 0),
                            PrefabPortRef(f"EN_{enable_index}"),
                        )
                    )
                object_cost = float(len(prefab.objects))
                proposals.append(
                    MappingProposal(
                        revision=design.revision,
                        provider=LBP_PROVIDER,
                        mapper=mapper_id,
                        rule=f"${key.family.value}-bank",
                        rule_version=rule_version,
                        ids=frozenset(item.cell.identifier for item in ordered),
                        prefab=prefab,
                        boundary=frozenset(boundary),
                        disposition=MappingDisposition.SPECULATIVE,
                        cost=MappingCostEstimate(object_cost, object_cost),
                        implementation_name=f"register-bank[{width}]",
                        packaging=ImplementationPackaging.CONTAINER,
                    )
                )
    return tuple(proposals)


class LBPCoarseRegisterBankMapper(MappingProvider):
    provider = LBP_PROVIDER
    stages = frozenset({"post-fsm"})
    mapper_id = "lbp.register_bank.coarse"
    rule_version = 2

    def __init__(
        self,
        style: LBPRegisterStyle = LBPRegisterStyle.COMPACT,
    ) -> None:
        self.style = LBPRegisterStyle(style)

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        return _map_registers(
            design,
            self.mapper_id,
            self.rule_version,
            _decode_register,
            self.style,
        )


class LBPScalarRegisterBankMapper(MappingProvider):
    provider = LBP_PROVIDER
    stages = frozenset({"leaf"})
    mapper_id = "lbp.register_bank.scalar"
    rule_version = 2

    def __init__(
        self,
        style: LBPRegisterStyle = LBPRegisterStyle.COMPACT,
    ) -> None:
        self.style = LBPRegisterStyle(style)

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        return _map_registers(
            design,
            self.mapper_id,
            self.rule_version,
            _decode_scalar_register,
            self.style,
        )