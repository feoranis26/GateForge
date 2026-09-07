from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gateforge.providers.lbp.types import (
    LBPCounterType,
    LBPGateType,
    LBPRandomizerType,
    LBPSelectorType,
    LBPTimerType,
    decode_lbp_object_type,
)
from gateforge.target import ObjectTypeIdentifier, ProviderConfiguration


class LBPTimerMode(StrEnum):
    ON_OFF = "ON_OFF"
    SPEED_SCALE = "SPEED_SCALE"
    FORWARD_BACKWARD = "FORWARD_BACKWARD"
    START_COUNT_UP = "START_COUNT_UP"
    START_COUNT_DOWN = "START_COUNT_DOWN"
    POSITIONAL = "POSITIONAL"


class LBPRandomizerMode(StrEnum):
    ADD = "ADD"
    ADD_AND_RESET = "ADD_AND_RESET"
    TOGGLE = "TOGGLE"
    ONE_AT_A_TIME = "ONE_AT_A_TIME"


class LBPRandomizerInputAction(StrEnum):
    TRIGGER = "TRIGGER"
    OVERRIDE_PATTERN = "OVERRIDE_PATTERN"


@dataclass(frozen=True, slots=True)
class LBPTimerConfiguration:
    time_ds: int = 50
    mode: LBPTimerMode = LBPTimerMode.ON_OFF

    def __post_init__(self) -> None:
        if not isinstance(self.time_ds, int) or isinstance(self.time_ds, bool):
            raise TypeError("LBP Timer time_ds must be an integer")
        if self.time_ds < 0:
            raise ValueError("LBP Timer time_ds must be nonnegative")

    @property
    def duration_frames(self) -> int:
        return 0 if self.time_ds == 0 else 3 * self.time_ds - 1


@dataclass(frozen=True, slots=True)
class LBPCounterConfiguration:
    target: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.target, int) or isinstance(self.target, bool):
            raise TypeError("LBP Counter target must be an integer")
        if self.target <= 0:
            raise ValueError("LBP Counter target must be positive")


@dataclass(frozen=True, slots=True)
class LBPRandomizerConfiguration:
    mode: LBPRandomizerMode = LBPRandomizerMode.ADD
    input_action: LBPRandomizerInputAction = LBPRandomizerInputAction.TRIGGER
    new_pick: bool = False
    on_min_ds: int = 10
    on_max_ds: int = 10
    off_min_ds: int = 0
    off_max_ds: int = 0

    def __post_init__(self) -> None:
        for name in ("on_min_ds", "on_max_ds", "off_min_ds", "off_max_ds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"LBP Randomizer {name} must be a nonnegative integer"
                )
        if self.on_min_ds > self.on_max_ds:
            raise ValueError("LBP Randomizer on_min_ds cannot exceed on_max_ds")
        if self.off_min_ds > self.off_max_ds:
            raise ValueError("LBP Randomizer off_min_ds cannot exceed off_max_ds")

    @staticmethod
    def frames(deciseconds: int) -> int:
        return 0 if deciseconds == 0 else 3 * deciseconds - 1


def _configuration_int(data: dict[str, object], name: str) -> int:
    value = data[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"LBP configuration {name} must be an integer")
    return value


class LBPObjectConfigurationCodec:
    def encode(
        self,
        object_type: ObjectTypeIdentifier,
        value: object,
    ) -> ProviderConfiguration:
        decoded_type = decode_lbp_object_type(object_type)
        if isinstance(decoded_type, LBPTimerType):
            if not isinstance(value, LBPTimerConfiguration):
                raise ValueError("LBP Timer requires LBPTimerConfiguration")
            return ProviderConfiguration.from_canonical_data(
                {"mode": value.mode.value, "time_ds": value.time_ds}
            )
        if isinstance(decoded_type, LBPCounterType):
            if not isinstance(value, LBPCounterConfiguration):
                raise ValueError("LBP Counter requires LBPCounterConfiguration")
            return ProviderConfiguration.from_canonical_data(
                {"target": value.target}
            )
        if isinstance(decoded_type, LBPRandomizerType):
            if not isinstance(value, LBPRandomizerConfiguration):
                raise ValueError(
                    "LBP Randomizer requires LBPRandomizerConfiguration"
                )
            return ProviderConfiguration.from_canonical_data(
                {
                    "input_action": value.input_action.value,
                    "mode": value.mode.value,
                    "new_pick": value.new_pick,
                    "off_max_ds": value.off_max_ds,
                    "off_min_ds": value.off_min_ds,
                    "on_max_ds": value.on_max_ds,
                    "on_min_ds": value.on_min_ds,
                }
            )
        if isinstance(decoded_type, (LBPGateType, LBPSelectorType)) and value is None:
            return ProviderConfiguration()
        raise ValueError(f"Object type {object_type.name!r} is not configurable")

    def decode(
        self,
        object_type: ObjectTypeIdentifier,
        configuration: ProviderConfiguration,
    ) -> object | None:
        decoded_type = decode_lbp_object_type(object_type)
        if isinstance(decoded_type, LBPGateType):
            if configuration.is_empty:
                return None
            raise ValueError("LBP logic gates do not accept configuration")
        if isinstance(decoded_type, LBPTimerType):
            data = configuration.canonical_data()
            if set(data) != {"mode", "time_ds"}:
                raise ValueError(
                    "LBP Timer configuration requires exactly mode and time_ds"
                )
            time_ds = data["time_ds"]
            mode = data["mode"]
            if not isinstance(time_ds, int) or isinstance(time_ds, bool):
                raise ValueError("LBP Timer time_ds must be an integer")
            if not isinstance(mode, str):
                raise ValueError("LBP Timer mode must be a string")
            return LBPTimerConfiguration(time_ds, LBPTimerMode(mode))
        if isinstance(decoded_type, LBPCounterType):
            data = configuration.canonical_data()
            if set(data) != {"target"}:
                raise ValueError(
                    "LBP Counter configuration requires exactly target"
                )
            target = data["target"]
            if not isinstance(target, int) or isinstance(target, bool):
                raise ValueError("LBP Counter target must be an integer")
            return LBPCounterConfiguration(target)
        if isinstance(decoded_type, LBPRandomizerType):
            data = configuration.canonical_data()
            expected = {
                "input_action",
                "mode",
                "new_pick",
                "off_max_ds",
                "off_min_ds",
                "on_max_ds",
                "on_min_ds",
            }
            if set(data) != expected:
                raise ValueError(
                    "LBP Randomizer configuration has missing or extra fields"
                )
            mode = data["mode"]
            input_action = data["input_action"]
            new_pick = data["new_pick"]
            if not isinstance(mode, str) or not isinstance(input_action, str):
                raise ValueError("LBP Randomizer modes must be strings")
            if not isinstance(new_pick, bool):
                raise ValueError("LBP Randomizer new_pick must be boolean")
            return LBPRandomizerConfiguration(
                mode=LBPRandomizerMode(mode),
                input_action=LBPRandomizerInputAction(input_action),
                new_pick=new_pick,
                on_min_ds=_configuration_int(data, "on_min_ds"),
                on_max_ds=_configuration_int(data, "on_max_ds"),
                off_min_ds=_configuration_int(data, "off_min_ds"),
                off_max_ds=_configuration_int(data, "off_max_ds"),
            )
        if isinstance(decoded_type, LBPSelectorType):
            if configuration.is_empty:
                return None
            raise ValueError("LBP Selector does not accept configuration")
        raise ValueError(f"Unsupported LBP object type {object_type.name!r}")