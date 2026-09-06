import unittest
from unittest.mock import patch

from gateforge.providers.lbp.common import (
    LBP_LOGIC,
    LBP_PROVIDER,
    LBP_TYPE_VERSION,
    LBP_WIRE,
)
from gateforge.providers.lbp.objects import (
    LBPTypeRegistry,
    validate_lbp_prefab,
)
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPCombinatorialVariableWidthGateType,
    LBPGateType,
    LBPNotGateType,
    LBPOrGateType,
    LBPXorGateType,
    decode_lbp_object_type,
)
from gateforge.target import (
    ObjectPortRef,
    ObjectTypeIdentifier,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    PrefabValidationError,
    ProviderConfiguration,
    SemanticPrefab,
    validate_prefab,
)


class LBPObjectTypeTests(unittest.TestCase):
    def test_lbp_provider_rejects_nonempty_object_configuration(self) -> None:
        prefab = self._single_configured_prefab()

        from gateforge.providers.lbp.objects import make_lbp_provider

        with self.assertRaises(PrefabValidationError):
            make_lbp_provider().validate(prefab)

    @staticmethod
    def _single_configured_prefab() -> SemanticPrefab:
        not_type = LBPNotGateType(width=1, invert=False).get_type()
        return SemanticPrefab(
            provider=LBP_PROVIDER,
            objects=frozenset(
                {
                    PrefabObject(
                        "gate",
                        not_type,
                        ProviderConfiguration.from_canonical_data({"mode": "x"}),
                    )
                }
            ),
            ports=frozenset(
                {
                    PrefabPort("A", PortDirection.INPUT, LBP_LOGIC),
                    PrefabPort("Y", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
            nets=frozenset(
                {
                    PrefabNet(
                        "a",
                        LBP_WIRE,
                        frozenset(
                            {PrefabPortRef("A"), ObjectPortRef("gate", "IN_0")}
                        ),
                    ),
                    PrefabNet(
                        "y",
                        LBP_WIRE,
                        frozenset(
                            {ObjectPortRef("gate", "OUT"), PrefabPortRef("Y")}
                        ),
                    ),
                }
            ),
        )

    def test_hierarchy_owns_the_canonical_type_keys(self) -> None:
        self.assertEqual(LBPGateType.TYPE_KEY, "GATE")
        self.assertEqual(
            LBPCombinatorialVariableWidthGateType.TYPE_KEY,
            "VARIABLE_WIDTH",
        )
        self.assertEqual(LBPAndGateType.TYPE_KEY, "AND")
        self.assertEqual(LBPOrGateType.TYPE_KEY, "OR")
        self.assertEqual(LBPNotGateType.TYPE_KEY, "NOT")
        self.assertEqual(LBPXorGateType.TYPE_KEY, "XOR")

    def test_all_leaf_identifiers_round_trip_exactly(self) -> None:
        cases = (
            (LBPAndGateType, "AND"),
            (LBPOrGateType, "OR"),
            (LBPNotGateType, "NOT"),
            (LBPXorGateType, "XOR"),
        )
        for gate_class, key in cases:
            for invert in (False, True):
                with self.subTest(gate=key, invert=invert):
                    gate = gate_class(width=2, invert=invert)
                    inversion = "true" if invert else "false"
                    self.assertEqual(
                        gate.get_type().name,
                        f"GATE(invert={inversion}):"
                        f"VARIABLE_WIDTH(width=2):{key}",
                    )
                    restored = decode_lbp_object_type(gate.get_type())
                    self.assertIsInstance(restored, gate_class)
                    self.assertEqual(restored.width, 2)
                    self.assertEqual(restored.invert_output, invert)

    def test_decoder_rejects_noncanonical_and_unknown_paths(self) -> None:
        invalid_names = (
            "LBP:VW:AND:2:NINV",
            "OBJECT(invert=false):VARIABLE_WIDTH(width=2):AND",
            "GATE:VARIABLE_WIDTH(width=2):AND",
            "GATE(invert=0):VARIABLE_WIDTH(width=2):AND",
            "GATE(extra=x,invert=false):VARIABLE_WIDTH(width=2):AND",
            "GATE(invert=false):VARIABLE_WIDTH:AND",
            "GATE(invert=false):VARIABLE_WIDTH(width=0):AND",
            "GATE(invert=false):VARIABLE_WIDTH(width=02):AND",
            "GATE(invert=false):VARIABLE_WIDTH(width=two):AND",
            "GATE(invert=false):VARIABLE_WIDTH(width=2):AND(extra=x)",
            "GATE(invert=false):VARIABLE_WIDTH(width=2):AND:EXTRA",
            "GATE(invert=%66alse):VARIABLE_WIDTH(width=2):AND",
        )

        for name in invalid_names:
            identifier = ObjectTypeIdentifier(
                provider=LBP_PROVIDER,
                name=name,
                version=LBP_TYPE_VERSION,
            )
            with self.subTest(name=name), self.assertRaises(PrefabValidationError):
                decode_lbp_object_type(identifier)

    def test_decoder_rejects_wrong_provider_and_version(self) -> None:
        name = "GATE(invert=false):VARIABLE_WIDTH(width=2):AND"
        invalid = (
            ObjectTypeIdentifier("other", name, LBP_TYPE_VERSION),
            ObjectTypeIdentifier(LBP_PROVIDER, name, LBP_TYPE_VERSION + 1),
        )

        for identifier in invalid:
            with self.subTest(identifier=identifier), self.assertRaises(
                PrefabValidationError
            ):
                decode_lbp_object_type(identifier)

    def test_registry_generates_schema_once_per_identifier(self) -> None:
        registry = LBPTypeRegistry()
        identifier = LBPAndGateType(width=2, invert=False).get_type()
        original = LBPAndGateType.get_schema

        with patch.object(
            LBPAndGateType,
            "get_schema",
            autospec=True,
            side_effect=original,
        ) as get_schema:
            first = registry.object(identifier)
            second = registry.object(identifier)

        self.assertIs(first, second)
        self.assertEqual(get_schema.call_count, 1)

    def test_width_generates_scalar_input_ports_lazily(self) -> None:
        gate = LBPAndGateType(width=3, invert=False)

        self.assertFalse(hasattr(gate, "ports"))
        schema = gate.get_schema()
        self.assertEqual(
            {port.name for port in schema.ports},
            {"IN_0", "IN_1", "IN_2", "OUT"},
        )
        self.assertTrue(all(port.width == 1 for port in schema.ports))
        self.assertEqual(
            {
                port.name
                for port in schema.ports
                if port.direction == PortDirection.INPUT
            },
            {"IN_0", "IN_1", "IN_2"},
        )
        self.assertEqual(
            {
                port.name
                for port in schema.ports
                if port.direction == PortDirection.OUTPUT
            },
            {"OUT"},
        )

    def test_identifier_round_trips_width_and_inversion(self) -> None:
        original = LBPAndGateType(width=7, invert=True)
        restored = decode_lbp_object_type(original.get_type())

        self.assertEqual(restored.get_type(), original.get_type())
        self.assertEqual(restored.get_schema(), original.get_schema())

    def test_normal_and_inverted_types_have_distinct_identifiers(self) -> None:
        normal = LBPAndGateType(width=1, invert=False)
        inverted = LBPAndGateType(width=1, invert=True)

        self.assertNotEqual(normal.get_type(), inverted.get_type())

    def test_lbp_rejects_structurally_valid_multi_producer_net(self) -> None:
        not_type = LBPNotGateType(width=1, invert=False).get_type()
        prefab = SemanticPrefab(
            provider=LBP_PROVIDER,
            objects=frozenset(
                {
                    PrefabObject("left", not_type),
                    PrefabObject("right", not_type),
                }
            ),
            ports=frozenset(
                {
                    PrefabPort("A", PortDirection.INPUT, LBP_LOGIC),
                    PrefabPort("Y", PortDirection.OUTPUT, LBP_LOGIC),
                }
            ),
            nets=frozenset(
                {
                    PrefabNet(
                        "input",
                        LBP_WIRE,
                        frozenset(
                            {
                                PrefabPortRef("A"),
                                ObjectPortRef("left", "IN_0"),
                                ObjectPortRef("right", "IN_0"),
                            }
                        ),
                    ),
                    PrefabNet(
                        "output",
                        LBP_WIRE,
                        frozenset(
                            {
                                ObjectPortRef("left", "OUT"),
                                ObjectPortRef("right", "OUT"),
                                PrefabPortRef("Y"),
                            }
                        ),
                    ),
                }
            ),
        )
        registry = LBPTypeRegistry()

        validate_prefab(prefab, registry)
        with self.assertRaises(PrefabValidationError):
            validate_lbp_prefab(prefab, registry)


if __name__ == "__main__":
    unittest.main()