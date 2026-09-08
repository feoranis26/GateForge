from pathlib import Path
import unittest

from gateforge.gateforge import compile_material, design_preprocess
from gateforge.graph import MaterialGraph, ObjectSubject
from gateforge.mapping import Mapper
from gateforge.placement import TopologicalPlacer
from gateforge.pipeline import default_mapping_stages
from gateforge.providers.lbp.objects import (
    LBPTypeRegistry,
    make_lbp_provider,
    validate_lbp_prefab,
)
from gateforge.providers.lbp.export import build_lbp_plan
from gateforge.providers.lbp.toolkit import encode_lbp_toolkit_plan
from gateforge.providers.lbp.configuration import (
    LBPObjectConfigurationCodec,
    LBPSelectorStateConfiguration,
)
from gateforge.providers.lbp.registers import (
    LBPCoarseRegisterBankMapper,
    LBPRegisterStyle,
    LBPScalarRegisterBankMapper,
)
from gateforge.providers.lbp.types import LBPStorageSelectorType, decode_lbp_object_type
from gateforge.providers.lbp.types import LBPCounterType
from gateforge.source import YosysParameterValue
from gateforge.source import DesignSnapshot
from gateforge.target import ObjectPortRef


FIXTURE = Path(__file__).parents[1] / "scratch" / "test.v"
DFF_SMOKE_FIXTURE = Path(__file__).parents[1] / "scratch" / "dff_smoke.v"
ENABLED_FIXTURE = Path(__file__).parent / "fixtures" / "enabled_registers.v"
VARIANTS_FIXTURE = Path(__file__).parent / "fixtures" / "register_variants.v"


class RegisterMappingTests(unittest.TestCase):
    def test_binary_parameter_bits_are_decoded_lsb_first(self) -> None:
        value = YosysParameterValue("001")

        self.assertEqual(value.as_binary_bits(3), (1, 0, 0))
        self.assertEqual(value.as_binary_bits(3, lsb_first=False), (0, 0, 1))

    def test_shared_async_registers_form_one_width_nine_bank(self) -> None:
        context = design_preprocess(str(FIXTURE))
        stages = default_mapping_stages()
        for stage in stages[:3]:
            for command in stage.passes:
                context.run_pass(command)
        proposals = Mapper([LBPCoarseRegisterBankMapper()]).collect_proposals(
            context.snapshot(),
            "post-fsm",
        )

        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(len(proposal.ids), 2)
        self.assertEqual(proposal.implementation_name, "register-bank[9]")
        self.assertEqual(len(proposal.prefab.ports), 4)
        self.assertEqual(
            {port.name: port.width for port in proposal.prefab.ports},
            {"D": 9, "Q": 9, "CLK": 1, "RESET": 1},
        )
        self.assertEqual(len(proposal.prefab.objects), 47)
        counters = [
            item
            for item in proposal.prefab.objects
            if isinstance(decode_lbp_object_type(item.type), LBPCounterType)
        ]
        self.assertEqual([item.role for item in counters], ["edge"])
        edge_pulse = next(net for net in proposal.prefab.nets if net.role == "edge_pulse")
        self.assertIn(ObjectPortRef("edge", "OUT"), edge_pulse.attachments)
        self.assertIn(ObjectPortRef("edge", "IN_1"), edge_pulse.attachments)
        selections: dict[int, int] = {}
        codec = LBPObjectConfigurationCodec()
        for item in proposal.prefab.objects:
            if not isinstance(decode_lbp_object_type(item.type), LBPStorageSelectorType):
                continue
            _, bit = item.role.split("_")
            configuration = codec.decode(item.type, item.configuration)
            self.assertIsInstance(configuration, LBPSelectorStateConfiguration)
            assert isinstance(configuration, LBPSelectorStateConfiguration)
            selections[int(bit)] = configuration.selection
        self.assertEqual(
            selections,
            {
                bit: (0 if bit == 0 else 1)
                for bit in range(9)
            },
        )
        validate_lbp_prefab(proposal.prefab, LBPTypeRegistry())

    def test_stateful_design_materializes_one_generated_bank(self) -> None:
        _, _, material = compile_material(str(FIXTURE))

        banks = [
            implementation
            for implementation in material.implementations
            if implementation.mapper == "lbp.register_bank.coarse"
        ]
        self.assertEqual(len(banks), 1)
        bank = banks[0]
        self.assertEqual(bank.name, "register-bank[9]")
        self.assertEqual(len(bank.objects), 47)
        self.assertEqual(
            {port.name for port in bank.ports},
            {"D", "Q", "CLK", "RESET"},
        )

    def test_scalar_leaf_fallback_regroups_the_same_nine_bits(self) -> None:
        context = design_preprocess(str(FIXTURE))
        for stage in default_mapping_stages():
            for command in stage.passes:
                context.run_pass(command)
        proposals = Mapper([LBPScalarRegisterBankMapper()]).collect_proposals(
            context.snapshot(),
            "leaf",
        )

        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(proposals[0].ids), 9)
        self.assertEqual(proposals[0].implementation_name, "register-bank[9]")

    def test_hardened_style_remains_available_as_a_larger_fallback(self) -> None:
        context = design_preprocess(str(DFF_SMOKE_FIXTURE))
        for stage in default_mapping_stages()[:3]:
            for command in stage.passes:
                context.run_pass(command)

        compact = Mapper([LBPCoarseRegisterBankMapper()]).collect_proposals(
            context.snapshot(),
            "post-fsm",
        )[0]
        hardened = Mapper(
            [LBPCoarseRegisterBankMapper(LBPRegisterStyle.HARDENED)]
        ).collect_proposals(
            context.snapshot(),
            "post-fsm",
        )[0]

        self.assertEqual(len(compact.prefab.objects), 7)
        self.assertEqual(len(hardened.prefab.objects), 15)
        self.assertLess(compact.cost.expected, hardened.cost.expected)

    def test_compile_can_use_only_the_scalar_register_fallback(self) -> None:
        from gateforge.providers.lbp.associative import LBPAssociativeConeMapper
        from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper

        _, state, material = compile_material(
            str(FIXTURE),
            mapping_providers=(
                LBPScalarRegisterBankMapper(),
                LBPAssociativeConeMapper(),
                LBPCombinatorialLowLevelGateMapper(),
            ),
        )

        banks = [
            implementation
            for implementation in material.implementations
            if implementation.mapper == "lbp.register_bank.scalar"
        ]
        self.assertEqual(len(banks), 1)
        self.assertEqual(banks[0].name, "register-bank[9]")
        self.assertEqual(
            len(
                [
                    claim
                    for claim in state.claims.values()
                    if claim.mapper == "lbp.register_bank.scalar"
                ]
            ),
            1,
        )

    def test_flat_source_export_contains_one_generated_register_microchip(self) -> None:
        _, _, material = compile_material(str(FIXTURE))
        providers = {"lbp": make_lbp_provider()}
        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        plan = build_lbp_plan(material, graph, placed, providers)

        hierarchy = plan.hierarchy
        self.assertIsNotNone(hierarchy)
        assert hierarchy is not None
        self.assertEqual(len(hierarchy.containers), 2)
        root = next(item for item in hierarchy.containers if item.parent is None)
        bank = next(
            item for item in hierarchy.containers if item.name == "register-bank[9]"
        )
        implementation = next(
            item
            for item in material.implementations
            if item.name == "register-bank[9]"
        )
        self.assertEqual(len(bank.gadgets), len(implementation.objects))
        for board_size in (root.board_size, bank.board_size):
            self.assertGreater(board_size.x, 0.0)
            self.assertGreater(board_size.y, 0.0)
            self.assertEqual(board_size.x % 52.5, 0.0)
            self.assertEqual(board_size.y % 52.5, 0.0)
        placements = {
            item.gadget: (item.x, item.y) for item in plan.placements
        }
        bank_positions = {placements[identifier] for identifier in bank.gadgets}
        self.assertEqual(len(bank_positions), len(bank.gadgets))

        encoded = encode_lbp_toolkit_plan(plan)
        chips: list[dict[str, object]] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                switch = value.get("PSwitch")
                if isinstance(switch, dict) and switch.get("type") == "MICROCHIP":
                    chips.append(value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(encoded)
        self.assertEqual(len(chips), 2)

    def test_one_bit_generated_register_bank_exports(self) -> None:
        _, _, material = compile_material(str(DFF_SMOKE_FIXTURE))
        providers = {"lbp": make_lbp_provider()}
        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        plan = build_lbp_plan(material, graph, placed, providers)

        encoded = encode_lbp_toolkit_plan(plan)
        self.assertEqual(encoded["type"], "PLAN")
        hierarchy = plan.hierarchy
        self.assertIsNotNone(hierarchy)
        assert hierarchy is not None
        self.assertEqual(
            {container.name for container in hierarchy.containers},
            {"dff_smoke", "register-bank[1]"},
        )

    def test_coarse_mapper_supports_dff_adff_and_sdff_variants(self) -> None:
        context = design_preprocess(str(VARIANTS_FIXTURE))
        for stage in default_mapping_stages()[:3]:
            for command in stage.passes:
                context.run_pass(command)
        proposals = Mapper([LBPCoarseRegisterBankMapper()]).collect_proposals(
            context.snapshot(),
            "post-fsm",
        )

        self.assertEqual(
            {proposal.rule for proposal in proposals},
            {"$dff-bank", "$adff-bank", "$sdff-bank"},
        )
        self.assertTrue(all(len(proposal.ids) == 1 for proposal in proposals))
        self.assertTrue(all(proposal.implementation_name == "register-bank[1]" for proposal in proposals))
        for proposal in proposals:
            validate_lbp_prefab(proposal.prefab, LBPTypeRegistry())

        _, _, material = compile_material(str(VARIANTS_FIXTURE))
        banks = [
            item
            for item in material.implementations
            if item.mapper == "lbp.register_bank.coarse"
        ]
        self.assertEqual(len(banks), 3)

    def test_enabled_bits_share_clock_bank_with_independent_enable_groups(self) -> None:
        context = design_preprocess(str(ENABLED_FIXTURE))
        for stage in default_mapping_stages()[:3]:
            for command in stage.passes:
                context.run_pass(command)
        proposals = Mapper([LBPCoarseRegisterBankMapper()]).collect_proposals(
            context.snapshot(),
            "post-fsm",
        )

        self.assertEqual(len(proposals), 2)
        plain_bank = next(item for item in proposals if item.rule == "$dff-bank")
        async_bank = next(item for item in proposals if item.rule == "$adff-bank")
        self.assertEqual(plain_bank.implementation_name, "register-bank[3]")
        self.assertEqual(
            {port.name: port.width for port in plain_bank.prefab.ports},
            {"D": 3, "Q": 3, "CLK": 1, "EN_0": 1, "EN_1": 1},
        )
        self.assertEqual(async_bank.implementation_name, "register-bank[1]")
        self.assertEqual(
            {port.name: port.width for port in async_bank.prefab.ports},
            {"D": 1, "Q": 1, "CLK": 1, "RESET": 1, "EN_0": 1},
        )
        validate_lbp_prefab(plain_bank.prefab, LBPTypeRegistry())
        validate_lbp_prefab(async_bank.prefab, LBPTypeRegistry())

        _, _, material = compile_material(str(ENABLED_FIXTURE))
        providers = {"lbp": make_lbp_provider()}
        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        self.assertEqual(
            sum(isinstance(item, ObjectSubject) for item in placed.subjects),
            len(material.objects),
        )
        plan = build_lbp_plan(material, graph, placed, providers)
        encoded = encode_lbp_toolkit_plan(plan)
        self.assertEqual(encoded["type"], "PLAN")

    def test_enabled_scalar_fallback_covers_lowered_cells(self) -> None:
        from gateforge.providers.lbp.associative import LBPAssociativeConeMapper
        from gateforge.providers.lbp.mappers import LBPCombinatorialLowLevelGateMapper

        _, state, material = compile_material(
            str(ENABLED_FIXTURE),
            mapping_providers=(
                LBPScalarRegisterBankMapper(),
                LBPAssociativeConeMapper(),
                LBPCombinatorialLowLevelGateMapper(),
            ),
        )

        scalar_banks = [
            claim
            for claim in state.claims.values()
            if claim.mapper == "lbp.register_bank.scalar"
        ]
        self.assertEqual(len(scalar_banks), 2)
        self.assertTrue(material.objects)

    def test_sdffe_and_sdffce_priorities_form_separate_banks(self) -> None:
        def cell(cell_type: str, d: int, q: int) -> dict[str, object]:
            return {
                "type": cell_type,
                "parameters": {
                    "CLK_POLARITY": "1",
                    "EN_POLARITY": "1",
                    "SRST_POLARITY": "1",
                    "SRST_VALUE": "0",
                    "WIDTH": "00000000000000000000000000000001",
                },
                "attributes": {},
                "port_directions": {
                    "CLK": "input",
                    "D": "input",
                    "EN": "input",
                    "Q": "output",
                    "SRST": "input",
                },
                "connections": {
                    "CLK": [2],
                    "D": [d],
                    "EN": [3],
                    "Q": [q],
                    "SRST": [4],
                },
            }

        snapshot = DesignSnapshot.from_json(
            {
                "modules": {
                    "top": {
                        "attributes": {"top": "1"},
                        "ports": {
                            "clk": {"direction": "input", "bits": [2]},
                            "enable": {"direction": "input", "bits": [3]},
                            "reset": {"direction": "input", "bits": [4]},
                            "d0": {"direction": "input", "bits": [5]},
                            "d1": {"direction": "input", "bits": [6]},
                            "q0": {"direction": "output", "bits": [7]},
                            "q1": {"direction": "output", "bits": [8]},
                        },
                        "cells": {
                            "reset_priority": cell("$sdffe", 5, 7),
                            "enable_priority": cell("$sdffce", 6, 8),
                        },
                        "netnames": {},
                    }
                }
            },
            revision=3,
        )

        proposals = LBPCoarseRegisterBankMapper().map(snapshot)

        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(len(proposal.ids) == 1 for proposal in proposals))
        self.assertTrue(
            all(
                {port.name for port in proposal.prefab.ports}
                == {"D", "Q", "CLK", "RESET", "EN_0"}
                for proposal in proposals
            )
        )
        self.assertNotEqual(
            proposals[0].prefab.get_id(),
            proposals[1].prefab.get_id(),
        )


if __name__ == "__main__":
    unittest.main()