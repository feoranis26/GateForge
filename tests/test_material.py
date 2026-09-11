from copy import deepcopy
from dataclasses import dataclass
import unittest

from gateforge.material import (
    ImplementationPackaging,
    MaterialDesign,
    MaterialImplementationOccurrence,
    MaterialImplementationPort,
    MaterialModuleOccurrence,
    MaterialModulePortRef,
    MaterialModuleValueRef,
    MaterialConstantRef,
    MaterialNet,
    MaterialNetId,
    MaterialObject,
    MaterialObjectId,
    MaterialObjectPortRef,
    MaterialValidationError,
    OccurrenceId,
    make_material_net_id,
    make_material_object_id,
    validate_material_design,
)
from gateforge.providers.lbp.common import LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.types import LBPNotGateType
from gateforge.provider import TargetProvider
from gateforge.target import (
    NetworkInterfaceMode,
    NetworkTypeIdentifier,
    NetworkTypeSchema,
    PortDirection,
    PrefabId,
    ProviderConfiguration,
    SignalTypeIdentifier,
    TargetTypeRegistry,
)


PACKED_PROVIDER = "packed-test"
PACKED_SIGNAL = SignalTypeIdentifier(PACKED_PROVIDER, "value")
PACKED_WIRE = NetworkTypeIdentifier(PACKED_PROVIDER, "wire")


def _packed_provider() -> TargetProvider:
    registry = TargetTypeRegistry()
    registry.register_network(
        NetworkTypeSchema(
            PACKED_WIRE,
            PACKED_SIGNAL,
            NetworkInterfaceMode.PACKED,
        )
    )
    return TargetProvider(
        identifier=PACKED_PROVIDER,
        registry=registry,
        validator=lambda prefab, registry: None,
    )


@dataclass(frozen=True, slots=True)
class ThresholdConfiguration:
    threshold: int


class ThresholdConfigurationCodec:
    def encode(self, object_type, value):
        if not isinstance(value, ThresholdConfiguration):
            raise ValueError("Expected ThresholdConfiguration")
        return ProviderConfiguration.from_canonical_data(
            {"threshold": value.threshold}
        )

    def decode(self, object_type, configuration):
        value = configuration.canonical_data().get("threshold")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("threshold must be an integer")
        return ThresholdConfiguration(value)


class MaterialDesignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.providers = {LBP_PROVIDER: make_lbp_provider()}
        occurrence = OccurrenceId("a" * 64)
        prefab = PrefabId("b" * 64)
        object_type = LBPNotGateType(width=1, invert=False).get_type()
        object_id = make_material_object_id(occurrence, prefab, "gate")
        material_object = MaterialObject(
            identifier=object_id,
            occurrence=occurrence,
            prefab=prefab,
            role="gate",
            type=object_type,
            hierarchy="module:test",
        )
        input_attachments = frozenset(
            {
                MaterialModulePortRef(
                    "test",
                    "a",
                    0,
                    PortDirection.INPUT,
                ),
                MaterialObjectPortRef(object_id, "IN_0"),
            }
        )
        output_attachments = frozenset(
            {
                MaterialObjectPortRef(object_id, "OUT"),
                MaterialModulePortRef(
                    "test",
                    "y",
                    0,
                    PortDirection.OUTPUT,
                ),
            }
        )
        input_net = MaterialNet(
            make_material_net_id(LBP_WIRE, input_attachments),
            LBP_WIRE,
            input_attachments,
        )
        output_net = MaterialNet(
            make_material_net_id(LBP_WIRE, output_attachments),
            LBP_WIRE,
            output_attachments,
        )
        self.design = MaterialDesign(
            objects=(material_object,),
            nets=(output_net, input_net),
        )

    def test_known_pre_rename_object_and_net_ids_are_stable(self) -> None:
        object_id = make_material_object_id(
            OccurrenceId(
                "69272891118485205f9084906f29e0c47838302f40583f988662067e0c101e80"
            ),
            PrefabId(
                "19e8b9fcb335506ba8e99d3129076e94940cdf24288a7bea188332efa90712e3"
            ),
            "gate",
        )
        self.assertEqual(
            object_id,
            MaterialObjectId(
                "183e14979182acba888fff32f45072a872c597ba5dae841fe8624432daeb4402"
            ),
        )
        attachments = frozenset(
            {
                MaterialObjectPortRef(object_id, "IN_1"),
                MaterialObjectPortRef(
                    MaterialObjectId(
                        "c465a2d9146451f713d88eabc0988f5fc3f1df5035660347ab9e2f21534b67f5"
                    ),
                    "OUT",
                ),
            }
        )
        self.assertEqual(
            make_material_net_id(LBP_WIRE, attachments),
            MaterialNetId(
                "104233a34b70dbb4c227e3921121959f0cff5a57363e13f0d5480111ef9544e9"
            ),
        )

    def test_canonical_round_trip_preserves_bytes_and_digest(self) -> None:
        restored = MaterialDesign.from_canonical_data(
            self.design.canonical_data(),
            self.providers,
        )

        self.assertEqual(restored, self.design)
        self.assertEqual(restored.canonical_bytes(), self.design.canonical_bytes())
        self.assertEqual(restored.get_digest(), self.design.get_digest())

    def test_generated_implementation_round_trips(self) -> None:
        material_object = self.design.objects[0]
        input_net = next(
            net
            for net in self.design.nets
            if any(
                isinstance(attachment, MaterialObjectPortRef)
                and attachment.port == "IN_0"
                for attachment in net.attachments
            )
        )
        output_net = next(
            net
            for net in self.design.nets
            if any(
                isinstance(attachment, MaterialObjectPortRef)
                and attachment.port == "OUT"
                for attachment in net.attachments
            )
        )
        implementation = MaterialImplementationOccurrence(
            path=f"module:test/implementation:{material_object.occurrence.value}",
            occurrence=material_object.occurrence,
            owner_module="module:test",
            prefab=material_object.prefab,
            provider=LBP_PROVIDER,
            mapper="test.mapper",
            rule="test-rule",
            name="test implementation",
            packaging=ImplementationPackaging.CONTAINER,
            ports=(
                MaterialImplementationPort(
                    "A", 0, PortDirection.INPUT, input_net.identifier
                ),
                MaterialImplementationPort(
                    "Y", 0, PortDirection.OUTPUT, output_net.identifier
                ),
            ),
            objects=(material_object.identifier,),
        )
        module = MaterialModuleOccurrence(
            path="module:test",
            module="test",
            implementation="test",
            parent=None,
            instance=None,
            anchor=None,
            ports=(),
            objects=(material_object.identifier,),
            children=(),
        )
        design = MaterialDesign(
            self.design.objects,
            self.design.nets,
            (module,),
            (implementation,),
        )

        restored = MaterialDesign.from_canonical_data(
            design.canonical_data(),
            self.providers,
        )

        self.assertEqual(restored, design)

    def test_schema_one_material_remains_readable(self) -> None:
        legacy = self.design.canonical_data()
        legacy["schema_version"] = 1

        restored = MaterialDesign.from_canonical_data(legacy, self.providers)

        self.assertEqual(restored, self.design)

    def test_packed_module_value_round_trips_in_bit_order(self) -> None:
        attachments = frozenset(
            {
                MaterialModuleValueRef(
                    "test",
                    "a",
                    (0, 1, 2),
                    PortDirection.INPUT,
                ),
                MaterialConstantRef("0"),
            }
        )
        design = MaterialDesign(
            (),
            (
                MaterialNet(
                    make_material_net_id(PACKED_WIRE, attachments),
                    PACKED_WIRE,
                    attachments,
                ),
            ),
        )

        restored = MaterialDesign.from_canonical_data(
            design.canonical_data(),
            {PACKED_PROVIDER: _packed_provider()},
        )

        self.assertEqual(restored, design)
        value = next(
            attachment
            for attachment in restored.nets[0].attachments
            if isinstance(attachment, MaterialModuleValueRef)
        )
        self.assertEqual(value.bits, (0, 1, 2))

    def test_schema_two_rejects_packed_module_value(self) -> None:
        attachments = frozenset(
            {
                MaterialModuleValueRef(
                    "test",
                    "a",
                    (0, 1),
                    PortDirection.INPUT,
                ),
                MaterialConstantRef("0"),
            }
        )
        design = MaterialDesign(
            (),
            (
                MaterialNet(
                    make_material_net_id(PACKED_WIRE, attachments),
                    PACKED_WIRE,
                    attachments,
                ),
            ),
        )
        data = design.canonical_data()
        data["schema_version"] = 2

        with self.assertRaisesRegex(MaterialValidationError, "schema version 3"):
            MaterialDesign.from_canonical_data(
                data,
                {PACKED_PROVIDER: _packed_provider()},
            )

    def test_decoder_normalizes_array_order(self) -> None:
        data = self.design.canonical_data()
        data["nets"].reverse()
        for net in data["nets"]:
            net["attachments"].reverse()

        restored = MaterialDesign.from_canonical_data(data, self.providers)

        self.assertEqual(restored.canonical_data(), self.design.canonical_data())

    def test_decoder_rejects_unknown_keys(self) -> None:
        data = self.design.canonical_data()
        data["unexpected"] = True

        with self.assertRaises(MaterialValidationError):
            MaterialDesign.from_canonical_data(data, self.providers)

    def test_decoder_rejects_content_hash_mismatch(self) -> None:
        data = deepcopy(self.design.canonical_data())
        data["objects"][0]["role"] = "changed"

        with self.assertRaises(MaterialValidationError):
            MaterialDesign.from_canonical_data(data, self.providers)

    def test_validation_rejects_missing_provider(self) -> None:
        with self.assertRaises(MaterialValidationError):
            validate_material_design(self.design, {})

    def test_empty_design_round_trips_without_a_provider(self) -> None:
        design = MaterialDesign((), ())

        restored = MaterialDesign.from_canonical_data(design.canonical_data(), {})

        self.assertEqual(restored, design)
        self.assertEqual(restored.canonical_data(), design.canonical_data())

    def test_provider_configuration_codec_round_trips_typed_values(self) -> None:
        lbp_provider = make_lbp_provider()
        provider = TargetProvider(
            identifier=LBP_PROVIDER,
            registry=lbp_provider.registry,
            validator=lbp_provider.validator,
            material_validator=lbp_provider.material_validator,
            object_configuration_codec=ThresholdConfigurationCodec(),
            dependency_projector=lbp_provider.dependency_projector,
        )
        object_type = self.design.objects[0].type

        encoded = provider.encode_object_configuration(
            object_type,
            ThresholdConfiguration(7),
        )
        decoded = provider.decode_object_configuration(object_type, encoded)

        self.assertEqual(decoded, ThresholdConfiguration(7))
        self.assertEqual(encoded.canonical_json, '{"threshold":7}')


if __name__ == "__main__":
    unittest.main()
