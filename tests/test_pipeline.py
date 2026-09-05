from pathlib import Path
import unittest

from gateforge.gateforge import compile_physical, compile_source
from gateforge.mapping import MappingProvider, MappingProposal
from gateforge.pipeline import default_mapping_stages
from gateforge.source import DesignSnapshot


FIXTURE = Path(__file__).parents[1] / "scratch" / "basic.v"


class RecordingProvider(MappingProvider):
    provider = "recording"

    def __init__(self) -> None:
        self.revisions: list[int] = []

    def map(self, design: DesignSnapshot) -> tuple[MappingProposal, ...]:
        self.revisions.append(design.revision)
        return ()


class MappingPipelineTests(unittest.TestCase):
    def test_provider_observes_every_default_checkpoint(self) -> None:
        provider = RecordingProvider()

        context, state = compile_source(
            str(FIXTURE),
            mapping_providers=(provider,),
            target_providers={},
        )

        self.assertEqual(len(provider.revisions), 4)
        self.assertEqual(provider.revisions, sorted(set(provider.revisions)))
        self.assertEqual(provider.revisions[-1], context.revision)
        self.assertEqual(state.revision, context.revision)

    def test_abc_optimizes_residual_logic_before_leaf_mapping(self) -> None:
        _, state, physical = compile_physical(str(FIXTURE))

        self.assertEqual(len(state.claims), 6)
        self.assertEqual(len(physical.objects), 7)

    def test_abc_stage_can_be_omitted(self) -> None:
        _, state, physical = compile_physical(
            str(FIXTURE),
            stages=default_mapping_stages(use_abc=False),
        )

        self.assertEqual(len(state.claims), 8)
        self.assertEqual(len(physical.objects), 8)


if __name__ == "__main__":
    unittest.main()