from dataclasses import replace
import unittest

from gateforge.target import (
    NetworkInterfaceMode,
    NetworkTypeIdentifier,
    NetworkTypeSchema,
    ObjectPortRef,
    ObjectTypeIdentifier,
    ObjectTypeSchema,
    PortDirection,
    PortSchema,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    PrefabValidationError,
    ProviderConfiguration,
    SemanticPrefab,
    SignalTypeIdentifier,
    TargetTypeRegistry,
    validate_prefab,
)


PROVIDER = "test"
BIT = SignalTypeIdentifier(PROVIDER, "bit")
WIRE = NetworkTypeIdentifier(PROVIDER, "wire")
NOT = ObjectTypeIdentifier(PROVIDER, "not")
AND = ObjectTypeIdentifier(PROVIDER, "and")


def _registry() -> TargetTypeRegistry:
    registry = TargetTypeRegistry()
    registry.register_network(NetworkTypeSchema(WIRE, BIT))
    registry.register_object(
        ObjectTypeSchema(
            NOT,
            frozenset(
                {
                    PortSchema("A", PortDirection.INPUT, BIT),
                    PortSchema("Y", PortDirection.OUTPUT, BIT),
                }
            ),
        )
    )
    registry.register_object(
        ObjectTypeSchema(
            AND,
            frozenset(
                {
                    PortSchema("A", PortDirection.INPUT, BIT),
                    PortSchema("B", PortDirection.INPUT, BIT),
                    PortSchema("Y", PortDirection.OUTPUT, BIT),
                }
            ),
        )
    )
    return registry


def _andnot_prefab(reverse: bool = False) -> SemanticPrefab:
    objects = [PrefabObject("invert_b", NOT), PrefabObject("result", AND)]
    ports = [
        PrefabPort("A", PortDirection.INPUT, BIT),
        PrefabPort("B", PortDirection.INPUT, BIT),
        PrefabPort("Y", PortDirection.OUTPUT, BIT),
    ]
    nets = [
        PrefabNet(
            "a",
            WIRE,
            frozenset({PrefabPortRef("A"), ObjectPortRef("result", "A")}),
        ),
        PrefabNet(
            "b",
            WIRE,
            frozenset({PrefabPortRef("B"), ObjectPortRef("invert_b", "A")}),
        ),
        PrefabNet(
            "inverted_b",
            WIRE,
            frozenset(
                {
                    ObjectPortRef("invert_b", "Y"),
                    ObjectPortRef("result", "B"),
                }
            ),
        ),
        PrefabNet(
            "y",
            WIRE,
            frozenset({ObjectPortRef("result", "Y"), PrefabPortRef("Y")}),
        ),
    ]
    if reverse:
        objects.reverse()
        ports.reverse()
        nets.reverse()
    return SemanticPrefab(
        provider=PROVIDER,
        objects=frozenset(objects),
        ports=frozenset(ports),
        nets=frozenset(nets),
    )


class SemanticPrefabTests(unittest.TestCase):
    def test_provider_configuration_is_canonical_and_affects_prefab_id(self) -> None:
        configuration = ProviderConfiguration.from_canonical_data(
            {"right": [2, 3], "left": 1}
        )
        configured = replace(
            _andnot_prefab(),
            objects=frozenset(
                replace(item, configuration=configuration)
                if item.role == "result"
                else item
                for item in _andnot_prefab().objects
            ),
        )

        self.assertEqual(
            configuration.canonical_json,
            '{"left":1,"right":[2,3]}',
        )
        self.assertNotEqual(configured.get_id(), _andnot_prefab().get_id())
        self.assertEqual(
            SemanticPrefab.from_canonical_data(configured.canonical_data()),
            configured,
        )

    def test_empty_provider_configuration_does_not_change_canonical_data(self) -> None:
        prefab = _andnot_prefab()

        self.assertTrue(
            all("configuration" not in item for item in prefab.canonical_data()["objects"])
        )

    def test_prefab_id_is_independent_of_construction_order(self) -> None:
        first = _andnot_prefab()
        second = _andnot_prefab(reverse=True)

        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.get_id(), second.get_id())

    def test_prefab_id_covers_internal_connectivity(self) -> None:
        original = _andnot_prefab()
        changed_net = next(net for net in original.nets if net.role == "inverted_b")
        changed = replace(
            original,
            nets=(original.nets - {changed_net})
            | {
                replace(
                    changed_net,
                    attachments=frozenset(
                        {
                            ObjectPortRef("invert_b", "Y"),
                            ObjectPortRef("result", "A"),
                        }
                    ),
                )
            },
        )

        self.assertNotEqual(original.get_id(), changed.get_id())

    def test_validation_rejects_unconnected_object_port(self) -> None:
        prefab = _andnot_prefab()
        input_net = next(net for net in prefab.nets if net.role == "a")
        invalid = replace(
            prefab,
            nets=(prefab.nets - {input_net})
            | {
                replace(
                    input_net,
                    attachments=frozenset({PrefabPortRef("A")}),
                )
            },
        )

        with self.assertRaises(PrefabValidationError):
            validate_prefab(invalid, _registry())

    def test_validates_multi_object_prefab(self) -> None:
        validate_prefab(_andnot_prefab(), _registry())

    def test_packed_net_requires_complete_port_values(self) -> None:
        packed_wire = NetworkTypeIdentifier(PROVIDER, "packed")
        registry = _registry()
        registry.register_network(
            NetworkTypeSchema(
                packed_wire,
                BIT,
                NetworkInterfaceMode.PACKED,
            )
        )
        prefab = SemanticPrefab(
            provider=PROVIDER,
            objects=frozenset({PrefabObject("invert", NOT)}),
            ports=frozenset(
                {
                    PrefabPort("A", PortDirection.INPUT, BIT, width=3),
                    PrefabPort("Y", PortDirection.OUTPUT, BIT),
                }
            ),
            nets=frozenset(
                {
                    PrefabNet(
                        "value",
                        packed_wire,
                        frozenset(
                            {
                                PrefabPortRef("A", 0),
                                PrefabPortRef("A", 1),
                                PrefabPortRef("A", 2),
                                ObjectPortRef("invert", "A"),
                            }
                        ),
                    ),
                    PrefabNet(
                        "output",
                        WIRE,
                        frozenset(
                            {
                                ObjectPortRef("invert", "Y"),
                                PrefabPortRef("Y"),
                            }
                        ),
                    ),
                }
            ),
        )
        value_net = next(net for net in prefab.nets if net.role == "value")
        valid = prefab

        validate_prefab(valid, registry)

        incomplete = replace(
            valid,
            nets=(valid.nets - {value_net})
            | {
                replace(
                    value_net,
                    attachments=value_net.attachments
                    - {PrefabPortRef("A", 2)},
                )
            },
        )
        with self.assertRaisesRegex(PrefabValidationError, "complete port A"):
            validate_prefab(incomplete, registry)


if __name__ == "__main__":
    unittest.main()