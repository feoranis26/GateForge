from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Generic, TypeVar, cast
from urllib.parse import quote, unquote_to_bytes


class TypeCodecError(ValueError):
    pass


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEGMENT = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<parameters>[^()]*)\))?"
)
_INVALID_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _validate_identifier(value: str, context: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise TypeCodecError(f"Invalid {context} {value!r}")


@dataclass(frozen=True, slots=True)
class TypePathSegment:
    key: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.key, "type key")
        normalized: list[tuple[str, str]] = []
        names: set[str] = set()
        for name, value in self.parameters:
            _validate_identifier(name, "parameter name")
            if name in names:
                raise TypeCodecError(f"Duplicate parameter {name!r}")
            if not isinstance(value, str):
                raise TypeCodecError(f"Parameter {name!r} must be a string")
            names.add(name)
            normalized.append((name, value))
        object.__setattr__(self, "parameters", tuple(sorted(normalized)))

    @classmethod
    def create(
        cls,
        key: str,
        parameters: Mapping[str, str] | None = None,
    ) -> TypePathSegment:
        return cls(key, tuple((parameters or {}).items()))

    def require_parameters(self, *names: str) -> dict[str, str]:
        expected = set(names)
        actual = {name for name, _ in self.parameters}
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise TypeCodecError(
                f"Type {self.key!r} has invalid parameters; "
                f"missing={missing!r}, extra={extra!r}"
            )
        return dict(self.parameters)


type TypePath = tuple[TypePathSegment, ...]


def _decode_parameter_value(value: str) -> str:
    if _INVALID_ESCAPE.search(value) is not None:
        raise TypeCodecError(f"Invalid percent escape in parameter value {value!r}")
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TypeCodecError(
            f"Parameter value {value!r} is not valid UTF-8"
        ) from error


def parse_type_path(value: str) -> TypePath:
    if not value:
        raise TypeCodecError("Type path must not be empty")

    segments: list[TypePathSegment] = []
    for raw_segment in value.split(":"):
        match = _SEGMENT.fullmatch(raw_segment)
        if match is None:
            raise TypeCodecError(f"Invalid type path segment {raw_segment!r}")
        raw_parameters = match.group("parameters")
        parameters: list[tuple[str, str]] = []
        if raw_parameters is not None:
            if not raw_parameters:
                raise TypeCodecError(
                    f"Type path segment {match.group('key')!r} has empty parentheses"
                )
            for raw_parameter in raw_parameters.split(","):
                if raw_parameter.count("=") != 1:
                    raise TypeCodecError(
                        f"Invalid type parameter {raw_parameter!r}"
                    )
                name, encoded_value = raw_parameter.split("=", 1)
                parameters.append((name, _decode_parameter_value(encoded_value)))
        segments.append(TypePathSegment(match.group("key"), tuple(parameters)))
    return tuple(segments)


def format_type_path(segments: Iterable[TypePathSegment]) -> str:
    path = tuple(segments)
    if not path:
        raise TypeCodecError("Type path must contain at least one segment")

    formatted: list[str] = []
    for segment in path:
        if not segment.parameters:
            formatted.append(segment.key)
            continue
        parameters = ",".join(
            f"{name}={quote(value, safe='-._~')}"
            for name, value in segment.parameters
        )
        formatted.append(f"{segment.key}({parameters})")
    return ":".join(formatted)


DecodedType = TypeVar("DecodedType")
DecoderClass = TypeVar("DecoderClass", bound=type)


class TypeClassRegistry(Generic[DecodedType]):
    def __init__(self, description: str = "type") -> None:
        self.description = description
        self._classes: dict[str, type] = {}

    def register(self, decoder_class: DecoderClass) -> DecoderClass:
        key = vars(decoder_class).get("TYPE_KEY")
        if not isinstance(key, str):
            raise TypeCodecError(
                f"Registered {self.description} class must directly define a "
                "string TYPE_KEY"
            )
        _validate_identifier(key, f"{self.description} key")
        if not callable(getattr(decoder_class, "decode_type_path", None)):
            raise TypeCodecError(
                f"Registered {self.description} class {decoder_class.__name__!r} "
                "must define decode_type_path()"
            )
        existing = self._classes.get(key)
        if existing is not None:
            raise TypeCodecError(
                f"Duplicate {self.description} key {key!r}: "
                f"{existing.__name__} and {decoder_class.__name__}"
            )
        self._classes[key] = decoder_class
        return decoder_class

    def resolve(self, key: str) -> type:
        try:
            return self._classes[key]
        except KeyError as error:
            raise TypeCodecError(
                f"Unknown {self.description} key {key!r}"
            ) from error

    def decode(
        self,
        path: TypePath,
        values: Mapping[str, object] | None = None,
    ) -> DecodedType:
        if not path:
            raise TypeCodecError(f"Missing {self.description} segment")
        decoder_class = self.resolve(path[0].key)
        return cast(
            DecodedType,
            decoder_class.decode_type_path(path[0], path[1:], dict(values or {})),
        )