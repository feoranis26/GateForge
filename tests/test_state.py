import json
import unittest

from gateforge.providers.lbp.common import LBP_LOGIC, LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.types import LBPNotGateType
from gateforge.state import (
    ClaimDefinition,
    ClaimDefinitionId,
    ClaimPortBinding,
    CompilationIntermediateState,
)
from gateforge.target import (
    NetworkTypeIdentifier,
    ObjectPortRef,
    ObjectTypeIdentifier,
    PortDirection,
    PrefabNet,
    PrefabObject,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
    SignalTypeIdentifier,
)


def _prefab() -> SemanticPrefab:
    signal = SignalTypeIdentifier("test", "bit")
    return SemanticPrefab(
        provider="test",
        objects=frozenset(
            {PrefabObject("gate", ObjectTypeIdentifier("test", "buffer"))}
        ),
        ports=frozenset(
            {
                PrefabPort("A", PortDirection.INPUT, signal),
                PrefabPort("Y", PortDirection.OUTPUT, signal),
            }
        ),
        nets=frozenset(
            {
                PrefabNet(
                    "a",
                    NetworkTypeIdentifier("test", "wire"),
                    frozenset(
                        {PrefabPortRef("A"), ObjectPortRef("gate", "A")}
                    ),
                ),
                PrefabNet(
                    "y",
                    NetworkTypeIdentifier("test", "wire"),
                    frozenset(
                        {ObjectPortRef("gate", "Y"), PrefabPortRef("Y")}
                    ),
                ),
            }
        ),
    )


class CompilationStateTests(unittest.TestCase):
    def test_lbp_hierarchical_identifier_survives_state_round_trip(self) -> None:
        gate_type = LBPNotGateType(width=1, invert=False).get_type()
        prefab = SemanticPrefab(
            provider=LBP_PROVIDER,
            objects=frozenset({PrefabObject("gate", gate_type)}),
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
        state = CompilationIntermediateState.empty().with_acceptance(
            revision=1,
            prefabs=[prefab],
            claims=[],
        )

        restored = CompilationIntermediateState.from_canonical_data(
            json.loads(json.dumps(state.canonical_data()))
        )
        restored_prefab = next(iter(restored.prefabs.values()))
        restored_type = next(iter(restored_prefab.objects)).type

        self.assertEqual(
            restored_type.name,
            "GATE(invert=false):VARIABLE_WIDTH(width=1):NOT",
        )
        self.assertEqual(restored.canonical_data(), state.canonical_data())

    def test_acceptance_interns_prefab_and_durable_claim(self) -> None:
        prefab = _prefab()
        claim = ClaimDefinition(
            identifier=ClaimDefinitionId("claim"),
            prefab=prefab.get_id(),
            module="top",
            instance="$claim",
            blackbox="gateforge_box",
            ports=(
                ClaimPortBinding("p0", PrefabPortRef("A"), PortDirection.INPUT),
                ClaimPortBinding("p1", PrefabPortRef("Y"), PortDirection.OUTPUT),
            ),
            provider="test",
            mapper="test.mapper",
            rule="buffer",
            rule_version=1,
            accepted_revision=0,
            source_provenance=("top.source",),
        )

        state = CompilationIntermediateState.empty().with_acceptance(
            revision=1,
            prefabs=[prefab],
            claims=[claim],
        )

        self.assertIs(state.prefabs[prefab.get_id()], prefab)
        self.assertEqual(state.claims[claim.identifier], claim)
        self.assertFalse(hasattr(state.claims[claim.identifier].ports[0], "source"))

    def test_revision_cannot_move_backward(self) -> None:
        with self.assertRaises(ValueError):
            CompilationIntermediateState.empty(revision=2).with_revision(1)

    def test_state_round_trips_through_json(self) -> None:
        prefab = _prefab()
        claim = ClaimDefinition(
            identifier=ClaimDefinitionId("claim"),
            prefab=prefab.get_id(),
            module="top",
            instance="$claim",
            blackbox="gateforge_box",
            ports=(
                ClaimPortBinding("p0", PrefabPortRef("A"), PortDirection.INPUT),
                ClaimPortBinding("p1", PrefabPortRef("Y"), PortDirection.OUTPUT),
            ),
            provider="test",
            mapper="test.mapper",
            rule="buffer",
            rule_version=1,
            accepted_revision=4,
            source_provenance=("top.source",),
        )
        state = CompilationIntermediateState.empty(revision=4).with_acceptance(
            revision=5,
            prefabs=[prefab],
            claims=[claim],
        )

        restored = CompilationIntermediateState.from_canonical_data(
            json.loads(json.dumps(state.canonical_data()))
        )

        self.assertEqual(restored.canonical_data(), state.canonical_data())


if __name__ == "__main__":
    unittest.main()