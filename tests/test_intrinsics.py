from collections.abc import Mapping
from pathlib import Path
import unittest

from gateforge.gateforge import compile_material, design_preprocess
from gateforge.graph import MaterialGraph
from gateforge.intrinsics import DEFAULT_INTRINSICS, IntrinsicKind
from gateforge.pipeline import default_mapping_stages
from gateforge.placement import TopologicalPlacer
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.configuration import (
    LBPTimerConfiguration,
    LBPTimerMode,
)
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.plan import LbpGadgetKind
from gateforge.providers.lbp.export import build_lbp_plan
from gateforge.providers.lbp.toolkit import encode_lbp_toolkit_plan
from gateforge.providers.lbp.types import (
    LBPRandomizerType,
    LBPSelectorType,
    LBPTimerType,
    decode_lbp_object_type,
)
from gateforge.search import MappingSearchMode, MappingSearchOptions
from gateforge.source import SnapshotError, YosysParameterValue


FIXTURE = Path(__file__).parent / "fixtures" / "timer_intrinsic.v"
DYNAMIC_FIXTURE = Path(__file__).parent / "fixtures" / "dynamic_intrinsics.v"
TIMER_MODES_FIXTURE = Path(__file__).parent / "fixtures" / "timer_modes_intrinsic.v"


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"{context} is not an object")
    return value


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError(f"{context} is not an array")
    return value


class YosysParameterValueTests(unittest.TestCase):
    def test_decodes_unsigned_signed_and_ascii_values(self) -> None:
        self.assertEqual(YosysParameterValue("00110010").as_unsigned_int(), 50)
        self.assertEqual(YosysParameterValue("11111110").as_signed_int(), -2)
        self.assertEqual(
            YosysParameterValue("START_COUNT_UP").as_ascii_string(),
            "START_COUNT_UP",
        )

    def test_rejects_unknown_integer_bits_and_nonprintable_strings(self) -> None:
        with self.assertRaises(SnapshotError):
            YosysParameterValue("10x1").as_unsigned_int()
        with self.assertRaises(SnapshotError):
            YosysParameterValue("bad\nvalue").as_ascii_string()


class IntrinsicRegistryTests(unittest.TestCase):
    def test_timer_metadata_and_parameters_survive_mapping_passes(self) -> None:
        context = design_preprocess(str(FIXTURE))

        for stage in default_mapping_stages():
            for command in stage.passes:
                context.run_pass(command)
            snapshot = context.snapshot()
            cell = snapshot.module("top").cells["timer"]
            instance = DEFAULT_INTRINSICS.recognize(snapshot, cell)

            if instance is None:
                self.fail("GF_Timer was not recognized as an intrinsic")
            self.assertEqual(instance.definition.kind, IntrinsicKind.TIMER)
            self.assertEqual(cell.parameter("TIME_DS").as_unsigned_int(), 50)
            self.assertEqual(
                cell.parameter("MODE").as_ascii_string(),
                "START_COUNT_UP",
            )

    def test_timer_materializes_as_required_native_object(self) -> None:
        for mode in (MappingSearchMode.GREEDY, MappingSearchMode.BEAM):
            with self.subTest(mode=mode):
                _, state, material = compile_material(
                    str(FIXTURE),
                    mapping_search=MappingSearchOptions(mode=mode),
                )

                self.assertEqual(len(state.claims), 1)
                claim = next(iter(state.claims.values()))
                self.assertEqual(claim.mapper, "lbp.intrinsic.timer")
                self.assertEqual(len(material.objects), 1)
                material_object = material.objects[0]
                self.assertIsInstance(
                    decode_lbp_object_type(material_object.type),
                    LBPTimerType,
                )
                configuration = make_lbp_provider().decode_object_configuration(
                    material_object.type,
                    material_object.configuration,
                )
                self.assertEqual(
                    configuration,
                    LBPTimerConfiguration(50, LBPTimerMode.START_COUNT_UP),
                )

    def test_timer_realizes_to_captured_toolkit_fields_and_ports(self) -> None:
        _, _, material = compile_material(str(FIXTURE))
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        plan = build_lbp_plan(material, graph, placed, providers)
        timer = next(
            gadget for gadget in plan.gadgets if gadget.kind == LbpGadgetKind.TIMER
        )

        self.assertEqual(timer.input_count, 2)
        self.assertEqual(timer.output_count, 1)
        self.assertEqual(
            {
                connection.target.port
                for connection in plan.connections
                if connection.target.gadget == timer.identifier
            },
            {0, 1},
        )
        self.assertEqual(
            {
                connection.source.port
                for connection in plan.connections
                if connection.source.gadget == timer.identifier
            },
            {0},
        )

        encoded = encode_lbp_toolkit_plan(plan)
        things: dict[int, dict[str, object]] = {}

        def collect(value: object) -> None:
            if isinstance(value, dict):
                uid = value.get("UID")
                if isinstance(uid, int):
                    things[uid] = value
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(encoded)
        timer_thing = next(
            thing for thing in things.values() if thing.get("planGUID") == 73790
        )
        switch = _mapping(timer_thing["PSwitch"], "Timer PSwitch")
        self.assertEqual(switch["type"], "TIMER")
        self.assertEqual(switch["activationHoldTime"], 149)
        self.assertEqual(switch["behavior"], "ONE_SHOT")
        self.assertEqual(switch["bulletsRequired"], 120)

    def test_all_timer_modes_preserve_the_captured_raw_enum_values(self) -> None:
        _, _, material = compile_material(str(TIMER_MODES_FIXTURE))
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        encoded = encode_lbp_toolkit_plan(
            build_lbp_plan(material, graph, placed, providers)
        )
        things: dict[int, dict[str, object]] = {}

        def collect(value: object) -> None:
            if isinstance(value, dict):
                uid = value.get("UID")
                if isinstance(uid, int):
                    things[uid] = value
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(encoded)
        behaviors = {
            _mapping(thing["PSwitch"], "Timer PSwitch")["behavior"]
            for thing in things.values()
            if thing.get("planGUID") == 73790
        }

        self.assertEqual(
            behaviors,
            {"OFF_ON", "SPEED_SCALE", "DIRECTION", "ONE_SHOT", 4, 5},
        )

    def test_dynamic_intrinsics_materialize_with_vector_shapes(self) -> None:
        _, state, material = compile_material(str(DYNAMIC_FIXTURE))

        self.assertEqual(len(state.claims), 3)
        self.assertEqual(
            {claim.mapper for claim in state.claims.values()},
            {
                "lbp.intrinsic.counter",
                "lbp.intrinsic.randomizer",
                "lbp.intrinsic.selector",
            },
        )
        decoded = [decode_lbp_object_type(item.type) for item in material.objects]
        self.assertEqual(
            {type(item).__name__ for item in decoded},
            {"LBPCounterType", "LBPRandomizerType", "LBPSelectorType"},
        )
        randomizer = next(item for item in decoded if isinstance(item, LBPRandomizerType))
        selector = next(item for item in decoded if isinstance(item, LBPSelectorType))
        self.assertEqual(randomizer.outputs, 3)
        self.assertEqual(selector.width, 3)

    def test_dynamic_intrinsics_realize_to_captured_toolkit_fields(self) -> None:
        _, _, material = compile_material(str(DYNAMIC_FIXTURE))
        providers = {LBP_PROVIDER: make_lbp_provider()}
        graph = MaterialGraph.from_design(material, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)
        plan = build_lbp_plan(material, graph, placed, providers)

        by_kind = {gadget.kind: gadget for gadget in plan.gadgets}
        self.assertEqual(by_kind[LbpGadgetKind.COUNTER].input_count, 2)
        self.assertEqual(by_kind[LbpGadgetKind.RANDOMIZER].output_count, 3)
        self.assertEqual(by_kind[LbpGadgetKind.SELECTOR].input_count, 4)
        self.assertEqual(by_kind[LbpGadgetKind.SELECTOR].output_count, 3)
        selector = by_kind[LbpGadgetKind.SELECTOR]
        self.assertEqual(
            {
                connection.target.port
                for connection in plan.connections
                if connection.target.gadget == selector.identifier
            },
            {0, 1, 2, 3},
        )

        encoded = encode_lbp_toolkit_plan(plan)
        things: dict[int, dict[str, object]] = {}

        def collect(value: object) -> None:
            if isinstance(value, dict):
                uid = value.get("UID")
                if isinstance(uid, int):
                    things[uid] = value
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(encoded)
        by_guid = {thing.get("planGUID"): thing for thing in things.values()}
        counter_switch = _mapping(by_guid[73789]["PSwitch"], "Counter PSwitch")
        randomizer = by_guid[73799]
        randomizer_switch = _mapping(randomizer["PSwitch"], "Randomizer PSwitch")
        selector_switch = _mapping(by_guid[100663]["PSwitch"], "Selector PSwitch")

        self.assertEqual(counter_switch["bulletsRequired"], 20)
        self.assertEqual(
            len(_list(randomizer_switch["outputs"], "Randomizer outputs")),
            3,
        )
        self.assertEqual(randomizer_switch["bulletRefreshTime"], randomizer["UID"])
        self.assertEqual(randomizer_switch["randomBehavior"], 1)
        self.assertEqual(randomizer_switch["randomPattern"], 2)
        self.assertTrue(randomizer_switch["randomNonRepeating"])
        self.assertEqual(randomizer_switch["randomOnTimeMin"], 29)
        self.assertEqual(randomizer_switch["randomOnTimeMax"], 59)
        self.assertEqual(randomizer_switch["randomOffTimeMin"], 0)
        self.assertEqual(randomizer_switch["randomOffTimeMax"], 0)
        self.assertEqual(selector_switch["bulletsRequired"], 3)
        self.assertEqual(
            len(_list(selector_switch["outputs"], "Selector outputs")),
            3,
        )


if __name__ == "__main__":
    unittest.main()