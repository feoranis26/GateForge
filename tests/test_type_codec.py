import unittest

from gateforge.type_codec import (
    TypeClassRegistry,
    TypeCodecError,
    TypePathSegment,
    format_type_path,
    parse_type_path,
)


class TypePathTests(unittest.TestCase):
    def test_round_trip_is_canonical(self) -> None:
        path = parse_type_path(
            "GATE(invert=false):VARIABLE_WIDTH(width=2):AND"
        )

        self.assertEqual(
            format_type_path(path),
            "GATE(invert=false):VARIABLE_WIDTH(width=2):AND",
        )

    def test_formatter_sorts_parameters_and_encodes_values(self) -> None:
        path = (
            TypePathSegment.create(
                "TYPE",
                {"zeta": "a:b,c=d", "alpha": "hello world"},
            ),
        )

        encoded = format_type_path(path)

        self.assertEqual(
            encoded,
            "TYPE(alpha=hello%20world,zeta=a%3Ab%2Cc%3Dd)",
        )
        self.assertEqual(parse_type_path(encoded), path)

    def test_parser_rejects_malformed_paths(self) -> None:
        invalid = (
            "",
            "TYPE()",
            "TYPE(value)",
            "TYPE(value=1,value=2)",
            "TYPE(value=%GG)",
            "TYPE(value=1",
            "TYPE:value=1",
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(TypeCodecError):
                parse_type_path(value)


class TypeClassRegistryTests(unittest.TestCase):
    def test_registration_uses_class_owned_key(self) -> None:
        registry = TypeClassRegistry[str]("test type")

        @registry.register
        class ExampleType:
            TYPE_KEY = "EXAMPLE"

            @classmethod
            def decode_type_path(cls, segment, remaining, values):
                self.assertEqual(segment.key, cls.TYPE_KEY)
                self.assertEqual(remaining, ())
                self.assertEqual(values, {"seed": 3})
                return cls.TYPE_KEY

        self.assertEqual(
            registry.decode(parse_type_path("EXAMPLE"), {"seed": 3}),
            "EXAMPLE",
        )

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = TypeClassRegistry[object]("test type")

        @registry.register
        class FirstType:
            TYPE_KEY = "SAME"

            @classmethod
            def decode_type_path(cls, segment, remaining, values):
                return cls()

        with self.assertRaises(TypeCodecError):

            @registry.register
            class SecondType:
                TYPE_KEY = "SAME"

                @classmethod
                def decode_type_path(cls, segment, remaining, values):
                    return cls()

    def test_registration_rejects_an_inherited_key(self) -> None:
        registry = TypeClassRegistry[object]("test type")

        class BaseType:
            TYPE_KEY = "BASE"

            @classmethod
            def decode_type_path(cls, segment, remaining, values):
                return cls()

        class ChildType(BaseType):
            pass

        with self.assertRaises(TypeCodecError):
            registry.register(ChildType)

    def test_unknown_key_is_rejected(self) -> None:
        registry = TypeClassRegistry[object]("test type")

        with self.assertRaises(TypeCodecError):
            registry.decode(parse_type_path("UNKNOWN"))


if __name__ == "__main__":
    unittest.main()