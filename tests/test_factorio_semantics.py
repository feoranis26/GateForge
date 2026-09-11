from dataclasses import replace
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gateforge.behavior import BehaviorBuilder, BehaviorNode, BehaviorOperation
from gateforge.compiler import compilation_backend, design_preprocess, materialize_search_candidates, start_compilation_search
from gateforge.material import MaterialConstantRef, MaterialDesign, MaterialDesignDigest
from gateforge.providers.factorio.realization import (
    FactorioAdd,
    FactorioCircuitDomain,
    FactorioCircuitFabric,
    FactorioDomainWire,
    FactorioInputEmission,
    FactorioObservationRead,
    FactorioSignalRead,
    propose_add_fabrics,
)
from gateforge.providers.factorio.semantics import simulate_add_fabric, simulate_finalized_add
from gateforge.providers.factorio.finalization import finalize_add_fabric, _choose_supply, _covered, _supply_candidates, _grid_power, _existing_pole_route
from gateforge.providers.factorio.blueprint import build_finalized_factorio_blueprint
from gateforge.providers.factorio.visualization import build_finalized_factorio_view
from gateforge.providers.factorio.routed import FactorioWireColor, FactorioEntity, FactorioPowerSegment, FactorioConnectorEndpoint, entities_collide
from gateforge.realization import (
    RealizationDesign,
    RealizationError,
    RealizedEndpoint,
    RealizedEntity,
    RealizedObservation,
    RealizedEntityPlacement,
)
from gateforge.target import ProviderConfiguration


RED = FactorioWireColor.RED
GREEN = FactorioWireColor.GREEN


class FactorioDomainTests(unittest.TestCase):
    def test_signed_decider_equivalence(self):
        from gateforge.providers.factorio.equivalence import validate_bitvectors
        fabric, _graph = self.fabric(second_signal="signal-B")
        endpoint = RealizedEndpoint("compare", "in")
        output = RealizedEndpoint("compare", "out")
        operation = FactorioAdd("compare", FactorioSignalRead(endpoint, "signal-A", (RED,)), FactorioSignalRead(endpoint, "signal-B", (RED,)), "signal-C", "<")
        observer = fabric.observations[0].read.endpoint
        fabric = replace(fabric, design=replace(fabric.design, entities=(*fabric.design.entities, RealizedEntity("compare", "decider-combinator", ("in", "out")))), domains=(replace(fabric.domains[0], endpoints=(*fabric.domains[0].endpoints, endpoint, output)),), wires=(*fabric.wires, FactorioDomainWire("bus", endpoint, observer), FactorioDomainWire("bus", output, observer)), additions=(operation,), observations=(replace(fabric.observations[0], read=FactorioSignalRead(observer, "signal-C", (RED,))),))
        builder = BehaviorBuilder()
        left, right = builder.input("a", 32), builder.input("b", 32)
        value = builder.intern(BehaviorNode(BehaviorOperation.SIGNED_LT, 1, (left, right)))
        validate_bitvectors(fabric, builder.build({"y": value}))
        unsigned = builder.intern(BehaviorNode(BehaviorOperation.LT, 1, (left, right)))
        with self.assertRaisesRegex(RealizationError, "counterexample"):
            validate_bitvectors(fabric, builder.build({"y": unsigned}))

    def test_bitvector_proof_rejects_sum_in_place_of_or(self):
        from gateforge.providers.factorio.equivalence import validate_bitvectors
        fabric, graph = self.fabric()
        validate_bitvectors(fabric, graph)
        builder = BehaviorBuilder()
        left, right = builder.input("a", 32), builder.input("b", 32)
        value = builder.intern(BehaviorNode(BehaviorOperation.BIT_OR, 32, (left, right)))
        with self.assertRaisesRegex(RealizationError, "counterexample"):
            validate_bitvectors(fabric, builder.build({"y": value}))
        import z3
        with patch("gateforge.providers.factorio.equivalence.z3.Solver") as solver:
            solver.return_value.check.return_value = z3.unknown
            solver.return_value.reason_unknown.return_value = "timeout"
            with self.assertRaisesRegex(RealizationError, "Could not prove.*timeout"):
                validate_bitvectors(fabric, graph)

    def fabric(self, *, second_signal="signal-A"):
        builder = BehaviorBuilder()
        left = builder.input("a", 32)
        right = builder.input("b", 32)
        total = builder.add(left, right, 32)
        graph = builder.build({"y": total})
        first = RealizedEndpoint("a", "out")
        second = RealizedEndpoint("b", "out")
        output = RealizedEndpoint("y", "circuit")
        design = RealizationDesign(
            "factorio", MaterialDesignDigest("0" * 64), graph.get_digest(),
            (
                RealizedEntity("a", "input-interface", ("out",)),
                RealizedEntity("b", "input-interface", ("out",)),
                RealizedEntity("y", "junction", ("circuit",)),
            ),
            (RealizedObservation("y", total, (output,)),),
        )
        return FactorioCircuitFabric(
            design,
            (FactorioCircuitDomain("bus", RED, (first, second, output)),),
            (FactorioDomainWire("bus", first, output), FactorioDomainWire("bus", second, output)),
            (FactorioInputEmission("a", first, "signal-A"), FactorioInputEmission("b", second, second_signal)),
            (), (FactorioObservationRead("y", FactorioSignalRead(output, "signal-A", (RED,))),),
        ), graph

    def test_same_key_broadcasts_sum_without_a_combinator_tick(self):
        fabric, _ = self.fabric()
        analysis = fabric.analyze()
        self.assertEqual(analysis.settling_ticks, 0)
        self.assertEqual([(term.input, term.delay, term.coefficient) for term in analysis.channels[0].terms], [
            ("a", 0, 1), ("b", 0, 1),
        ])
        self.assertEqual(simulate_add_fabric(fabric, {"a": 0xffffffff, "b": 2})[-1], {"y": 1})

    def test_domain_carries_multiple_keys_and_reader_selects_one(self):
        fabric, _ = self.fabric(second_signal="signal-B")
        analysis = fabric.analyze()
        self.assertEqual({channel.signal for channel in analysis.channels}, {"signal-A", "signal-B"})
        self.assertEqual([term.input for term in analysis.observations[0][1]], ["a"])
        self.assertEqual(simulate_add_fabric(fabric, {"a": 17, "b": 41})[-1], {"y": 17})

    def test_color_replication_uses_same_signal_and_double_reads_sum_twice(self):
        fabric, _ = self.fabric()
        green_domain = replace(fabric.domains[0], identifier="green", color=GREEN)
        replicated = replace(
            fabric, domains=(*fabric.domains, green_domain),
            wires=(*fabric.wires, *(replace(wire, domain="green") for wire in fabric.wires)),
        )
        self.assertEqual(replicated.analyze().observations, fabric.analyze().observations)
        doubled = replace(replicated, observations=(replace(
            replicated.observations[0], read=replace(replicated.observations[0].read, colors=(RED, GREEN)),
        ),))
        self.assertEqual([term.coefficient for term in doubled.analyze().observations[0][1]], [2, 2])
        self.assertEqual(simulate_add_fabric(doubled, {"a": 7, "b": 11})[-1], {"y": 36})

    def test_actual_wires_must_induce_exactly_declared_domains(self):
        fabric, _ = self.fabric()
        with self.assertRaisesRegex(RealizationError, "do not connect"):
            replace(fabric, wires=fabric.wires[:1])
        with self.assertRaisesRegex(RealizationError, "separate domains"):
            replace(fabric, domains=(*fabric.domains, replace(fabric.domains[0], identifier="duplicate")))
        with self.assertRaisesRegex(RealizationError, "declared domain"):
            replace(fabric, wires=(replace(fabric.wires[0], domain="missing"),))
        with self.assertRaisesRegex(RealizationError, "unknown realized endpoint"):
            replace(fabric, domains=(replace(fabric.domains[0], endpoints=(RealizedEndpoint("missing", "out"),)),))

    def test_feedback_is_rejected_even_without_source_validation(self):
        fabric, _ = self.fabric()
        input_endpoint = RealizedEndpoint("loop", "in")
        output_endpoint = RealizedEndpoint("loop", "out")
        cyclic = replace(
            fabric,
            design=replace(fabric.design, entities=(*fabric.design.entities, RealizedEntity("loop", "arithmetic-combinator", ("in", "out")))),
            additions=(FactorioAdd("loop", FactorioSignalRead(input_endpoint, "signal-A", (RED,)), FactorioSignalRead(input_endpoint, "signal-B", (RED,)), "signal-A"),),
            domains=(*fabric.domains, FactorioCircuitDomain("loop", RED, (input_endpoint, output_endpoint))),
            wires=(*fabric.wires, FactorioDomainWire("loop", input_endpoint, output_endpoint)),
        )
        with self.assertRaisesRegex(RealizationError, "feedback"):
            cyclic.analyze()

    def test_canonical_identity_includes_signal_configuration(self):
        fabric, _ = self.fabric()
        reordered = replace(fabric, wires=tuple(reversed(fabric.wires)), inputs=tuple(reversed(fabric.inputs)))
        self.assertEqual(fabric.get_digest(), reordered.get_digest())
        changed, _ = self.fabric(second_signal="signal-B")
        self.assertNotEqual(fabric.get_digest(), changed.get_digest())
        for signal in ("signal-each", "signal-everything", "signal-A:legendary"):
            with self.assertRaises(RealizationError):
                replace(fabric.inputs[0], signal=signal)

    def test_simulator_rejects_missing_extra_or_invalid_input_patterns(self):
        fabric, _ = self.fabric()
        for inputs in ({"a": 1}, {"a": 1, "b": 2, "c": 3}, {"a": -1, "b": 0}, {"a": 1 << 32, "b": 0}, {"a": True, "b": 0}):
            with self.subTest(inputs=inputs), self.assertRaises(RealizationError):
                simulate_add_fabric(fabric, inputs)
        for ticks in (-1, True, 1.5):
            with self.subTest(ticks=ticks), self.assertRaises(RealizationError):
                simulate_add_fabric(fabric, {"a": 0, "b": 0}, ticks=ticks)


class FactorioAddProposalTests(unittest.TestCase):
    def test_boolean_interface_preserves_width_without_computation(self):
        baseline = self.compile("module top(input enable, output ready); assign ready=enable; endmodule")
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        self.assertEqual(fabric.inputs[0].width, 1)
        self.assertEqual(fabric.analyze().settling_ticks, 0)
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph)
        for value in (0, 1):
            self.assertEqual(simulate_finalized_add(finalized, {"enable": value})[-1], {"port:ready": value})

    def test_conditional_operators_match_serialized_simulation(self):
        expressions = ("a == b", "a != b", "a < b", "a <= b", "a > b", "a >= b", "a && b", "a || b", "!a", "(a < b) ? a : b", "a ? b : 32'h80000000", "(a == b) ? 32'hffffffff : a", "32'd1 < a", "(a < b) ? ((a != 0) ? a : 32'd7) : b")
        random = Random(71)
        for signed in (False, True):
            for expression in expressions:
                with self.subTest(signed=signed, expression=expression):
                    baseline = self.compile(f"module top(input {'signed' if signed else ''} [31:0] a,b, output [31:0] y); assign y={expression}; endmodule")
                    assert baseline.behavior is not None
                    fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, power_layout="compact", column_pitch=3, row_pitch=2)
                    inputs_list = [(0, 0), (1, 2), (2, 1), (0xffffffff, 1), (0x7fffffff, 0x80000000), (0x80000000, 0x7fffffff)]
                    inputs_list += [(random.getrandbits(32), random.getrandbits(32)) for _ in range(4)]
                    for left, right in inputs_list:
                        inputs = {"a": left, "b": right}
                        expected = baseline.behavior.graph.evaluate(inputs)
                        self.assertTrue(all(value == expected for value in simulate_add_fabric(fabric, inputs)[finalized.settling_ticks:]))
                        self.assertTrue(all(value == expected for value in simulate_finalized_add(finalized, inputs)[finalized.settling_ticks:]))

    def test_conditional_process_compiles_through_normal_pipeline(self):
        baseline = self.compile((Path(__file__).parent / "fixtures" / "factorio" / "conditional32.v").read_text())
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, column_pitch=3, row_pitch=2)
        blueprint = build_finalized_factorio_blueprint(finalized).canonical_data()["blueprint"]
        combinators = [item for item in blueprint["entities"] if item["name"] in {"arithmetic-combinator", "decider-combinator"}]
        self.assertTrue(all(item["direction"] == 4 for item in combinators))
        for enable in (0, 1):
            for left, right in ((0, 0), (1, 2), (2, 1), (0xffffffff, 0x7fffffff), (0x7fffffff, 0x80000000)):
                inputs = {"a": left, "b": right, "enable": enable}
                expected = baseline.behavior.graph.evaluate(inputs)
                self.assertEqual(simulate_add_fabric(fabric, inputs)[-1], expected)
                self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], expected)
        with self.assertRaisesRegex(RealizationError, "width"):
            simulate_finalized_add(finalized, {"a": 0, "b": 0, "enable": 2})
        with self.assertRaisesRegex(RealizationError, "width"):
            finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", input_values={"enable": 2})
        driven = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", input_values={"a": 0xffffffff, "b": 1, "enable": 1}, output_lamps=True, power_layout="compact")
        self.assertEqual(simulate_finalized_add(driven)[-1], {"port:y": 0, "port:ready": 1})

    def test_decider_configuration_tampering_is_rejected(self):
        baseline = self.compile((Path(__file__).parent / "fixtures" / "factorio" / "conditional32.v").read_text())
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph)
        decider = next(item for item in finalized.design.entities if item.kind == "decider-combinator")
        for mutation in ("comparator", "constant", "copy_count_from_input"):
            configuration = decider.configuration.canonical_data()
            settings = configuration["decider_conditions"]
            if mutation == "comparator":
                settings["conditions"][0]["comparator"] = "<" if settings["conditions"][0]["comparator"] != "<" else ">"
            else:
                settings["outputs"][0][mutation] = 2 if mutation == "constant" else True
            design = replace(finalized.design, entities=tuple(replace(item, configuration=ProviderConfiguration.from_canonical_data(configuration)) if item == decider else item for item in finalized.design.entities))
            with self.subTest(mutation=mutation), self.assertRaisesRegex(RealizationError, "configuration differs"):
                replace(finalized, design=design, placement=replace(finalized.placement, realization_digest=design.get_digest()))

    def test_conditional_fabric_proves_unsigned_selection(self):
        from gateforge.behavior_source import YosysCombinationalBehaviorLowerer
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conditional.v"
            path.write_text("module top(input [31:0] a,b, output [31:0] y); assign y=(a < b) ? a : b; endmodule")
            capture = YosysCombinationalBehaviorLowerer().lower(design_preprocess(str(path)).snapshot())
        fabric = propose_add_fabrics(baseline.material, capture)[0]
        self.assertEqual(sum(item.prototype == "decider-combinator" for item in fabric.additions), 2)
        self.assertEqual(sum(item.operation == "XOR" for item in fabric.additions), 2)
        self.assertGreater(fabric.validate(baseline.material, capture.graph).settling_ticks, 0)
        finalized = finalize_add_fabric(fabric, baseline.material, capture.graph, column_pitch=3, row_pitch=2)
        for left, right in ((0, 0), (1, 2), (0xffffffff, 1), (0x80000000, 0x7fffffff)):
            inputs = {"a": left, "b": right}
            expected = capture.graph.evaluate(inputs)
            self.assertEqual(simulate_add_fabric(fabric, inputs)[-1], expected)
            self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], expected)

    def test_bound_bitwise_fixture_uses_inline_constants(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bitwise32.v").read_text()
        baseline = self.compile(source)
        assert baseline.behavior is not None
        fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
        self.assertEqual(len(fabrics), 1)
        fabric = fabrics[0]
        self.assertEqual(len(fabric.additions), 5)
        self.assertEqual(len({item.endpoint for item in fabric.inputs}), 1)
        self.assertEqual(len({item.read.endpoint for item in fabric.observations}), 1)
        self.assertEqual(fabric.analyze().settling_ticks, 4)
        for operation in fabric.additions:
            if operation.operation == "AND":
                changed = replace(operation, operation="OR")
            elif operation.operation == "XOR" and hasattr(operation.second, "value"):
                changed = replace(operation, second=replace(operation.second, value=0))
            else:
                continue
            invalid = replace(fabric, additions=tuple(changed if item == operation else item for item in fabric.additions))
            with self.assertRaisesRegex(RealizationError, "counterexample"):
                invalid.validate(baseline.material, baseline.behavior.graph)
        self.check_simulation(fabrics, baseline)
        for layout in ("grid", "compact"):
            for drivers in ("none", "constant"):
                with self.subTest(layout=layout, drivers=drivers):
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, power_layout=layout, input_drivers=drivers, column_pitch=3, row_pitch=2)
                    entities = build_finalized_factorio_blueprint(finalized).canonical_data()["blueprint"]["entities"]
                    self.assertEqual(sum(item["name"] == "constant-combinator" for item in entities), int(drivers == "constant"))
                    conditions = [item["control_behavior"]["arithmetic_conditions"] for item in entities if item["name"] == "arithmetic-combinator"]
                    constants = [item[name] for item in conditions for name in ("first_constant", "second_constant") if name in item]
                    self.assertEqual(sorted(constants), [-2147483648, -1])
                    for value in (0, 1, 0x7fffffff, 0x80000000, 0xffffffff):
                        inputs = {"a": value, "b": 1, "mask": 0xff00ff00, "flags": 0xa5a5a5a5}
                        self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], baseline.behavior.graph.evaluate(inputs))

    def test_literal_operands_and_constant_outputs(self):
        for expression in ("a + 1", "a + 32'd7", "32'h80000000 | a", "a & 32'hff00ff00", "a ^ 32'hffffffff", "~a", "32'hffffffff"):
            with self.subTest(expression=expression):
                baseline = self.compile(f"module top(input [31:0] a, output [31:0] y); assign y = {expression}; endmodule")
                assert baseline.behavior is not None
                if expression == "a + 32'd7":
                    constants = [item.value for net in baseline.material.nets for item in net.attachments if isinstance(item, MaterialConstantRef)]
                    self.assertEqual(constants, [format(7, "032b")])
                    restored = MaterialDesign.from_canonical_data(baseline.material.canonical_data(), compilation_backend("factorio").target_providers)
                    self.assertEqual(restored.get_digest(), baseline.material.get_digest())
                fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
                self.assertEqual(len(fabrics[0].additions), 1)
                self.check_simulation(fabrics, baseline)
                finalized = finalize_add_fabric(fabrics[0], baseline.material, baseline.behavior.graph)
                for value in (0, 1, 0x80000000, 0xffffffff):
                    self.assertEqual(simulate_finalized_add(finalized, {"a": value})[-1], baseline.behavior.graph.evaluate({"a": value}))

    def test_mixed_combinational_proposal_keeps_word_operations(self):
        baseline = self.compile("module top(input [31:0] a,b,mask,flags, output [31:0] y); assign y = ((a+b) & mask) ^ flags; endmodule")
        assert baseline.behavior is not None
        fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
        self.assertEqual(len(fabrics), 1)
        self.assertEqual(sorted(item.operation for item in fabrics[0].additions), ["+", "AND", "XOR"])
        self.assertEqual(fabrics[0].analyze().settling_ticks, 3)
        self.check_simulation(fabrics, baseline)
        for layout in ("grid", "compact"):
            finalized = finalize_add_fabric(fabrics[0], baseline.material, baseline.behavior.graph, power_layout=layout)
            conditions = [entity["control_behavior"]["arithmetic_conditions"] for entity in build_finalized_factorio_blueprint(finalized).canonical_data()["blueprint"]["entities"] if entity["name"] == "arithmetic-combinator"]
            self.assertEqual(sorted(item["operation"] for item in conditions), ["+", "AND", "XOR"])
            for inputs in ({"a": 41, "b": 1, "mask": 255, "flags": 0x80000000}, {"a": 0xffffffff, "b": 2, "mask": 0xffffffff, "flags": 0xffffffff}):
                self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], baseline.behavior.graph.evaluate(inputs))

    def test_bound_bus_layout_needs_only_adder_tap_renamer_and_two_poles(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v").read_text()
        baseline = self.compile(source)
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        self.assertEqual(len(fabric.additions), 2)
        self.assertEqual(len({item.endpoint for item in fabric.inputs}), 1)
        self.assertEqual(len({item.read.endpoint for item in fabric.observations}), 1)
        for layout in ("grid", "compact"):
            finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, column_pitch=2, row_pitch=1, power_layout=layout)
            self.assertEqual(len(finalized.entities), 4)
            self.assertEqual(sum(item.prototype.endswith("electric-pole") for item in finalized.entities), 2)
            self.assertEqual(finalized.settling_ticks, 1)
            self.assertEqual(simulate_finalized_add(finalized, {"a": 41, "b": 1})[-1], {"port:y": 42, "port:tap": 41})

    def test_bound_adder_without_tap_needs_one_combinator_and_two_poles(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v").read_text()
        source = source.replace('output [31:0] y,', 'output [31:0] y').replace('    (* factorio_signal = "signal-D", factorio_circuit = "outputs", factorio_color = "red" *)\n    output [31:0] tap\n', '').replace('    assign tap = a;\n', '')
        baseline = self.compile(source)
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        self.assertEqual(len(fabric.additions), 1)
        for layout in ("grid", "compact"):
            for pitch in (2, 3):
                with self.subTest(layout=layout, pitch=pitch):
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, column_pitch=pitch, row_pitch=1, power_layout=layout)
                    self.assertEqual(len(finalized.entities), 3)
                    self.assertEqual(sum(item.prototype.endswith("electric-pole") for item in finalized.entities), 2)
                    self.assertEqual(finalized.settling_ticks, 1)
                    self.assertEqual(simulate_finalized_add(finalized, {"a": 41, "b": 1})[-1], {"port:y": 42})

    def test_signal_route_reuses_an_available_supply_pole(self):
        entities = {
            "source": FactorioEntity("source", "constant-combinator", 0.5, 0.5),
            "supply": FactorioEntity("supply", "medium-electric-pole", 8.5, 0.5),
            "target": FactorioEntity("target", "arithmetic-combinator", 16, 0.5),
        }
        source, supply, target = (FactorioConnectorEndpoint(name, 1) for name in ("source", "supply", "target"))
        self.assertEqual(_existing_pole_route(source, target, entities, {"supply"}), ((source, supply), (supply, target)))
        self.assertIsNone(_existing_pole_route(source, target, entities, set()))

    def test_hdl_interface_bindings_group_inputs_and_outputs(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v").read_text()
        baseline = self.compile(source)
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        self.assertEqual({item.name: item.signal for item in fabric.inputs}, {"a": "signal-A", "b": "signal-B"})
        self.assertEqual({item.name: item.read.signal for item in fabric.observations}, {"port:y": "signal-C", "port:tap": "signal-D"})
        shared = [domain for domain in fabric.domains if domain.identifier.startswith("interface:")]
        self.assertEqual(len(shared), 2)
        self.assertEqual({domain.color for domain in shared}, {RED, GREEN})
        self.assertTrue(all(len(domain.endpoints) == 3 for domain in shared))
        for layout in ("grid", "compact"):
            for drivers in ("none", "constant"):
                with self.subTest(layout=layout, drivers=drivers):
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers=drivers, input_values={"a": 41, "b": 1} if drivers == "constant" else None, output_lamps=True, power_layout=layout)
                    for inputs in ({"a": 41, "b": 1}, {"a": 0xffffffff, "b": 2}, {"a": 0x80000000, "b": 0x80000000}):
                        self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], baseline.behavior.graph.evaluate(inputs))
                    self.assertEqual(finalized.settling_ticks, 1)
                    if drivers == "constant":
                        self.assertEqual(simulate_finalized_add(finalized)[-1], {"port:y": 42, "port:tap": 41})
                    blueprint = build_finalized_factorio_blueprint(finalized).canonical_data()["blueprint"]
                    self.assertEqual(sum(entity["name"] == "constant-combinator" for entity in blueprint["entities"]), int(drivers == "constant"))
                    self.assertEqual(sum(entity["name"] == "small-lamp" for entity in blueprint["entities"]), 2)
                    filters = [item["name"] for entity in blueprint["entities"] if entity["name"] == "constant-combinator" for item in entity["control_behavior"]["sections"]["sections"][0]["filters"]]
                    self.assertEqual(set(filters), {"signal-A", "signal-B"} if drivers == "constant" else set())

    def test_direct_bindings_preserve_fanout_and_distinct_bus_semantics(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v").read_text()
        cases = (
            (source.replace("assign y = a + b;", "assign y = a + a;"), 2),
            (source.replace("assign tap = a;", "assign tap = y + a;"), 2),
            (source.replace("assign tap = a;", "assign tap = y;"), 2),
            (source.replace('factorio_signal = "signal-B", factorio_circuit = "inputs"', 'factorio_signal = "signal-B", factorio_circuit = "other"'), 3),
            (source.replace('factorio_signal = "signal-B", factorio_circuit = "inputs", factorio_color = "green"', 'factorio_signal = "signal-A", factorio_circuit = "other", factorio_color = "green"'), 3),
            (source.replace('    (* factorio_signal = "signal-B", factorio_circuit = "inputs", factorio_color = "green" *)\n', ''), 2),
        )
        for index, (variant, expected_additions) in enumerate(cases):
            with self.subTest(case=index):
                baseline = self.compile(variant)
                assert baseline.behavior is not None
                for fabric in propose_add_fabrics(baseline.material, baseline.behavior):
                    self.assertEqual(len(fabric.additions), expected_additions)
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", power_layout="compact")
                    for inputs in ({"a": 41, "b": 1}, {"a": 0xffffffff, "b": 2}):
                        expected = baseline.behavior.graph.evaluate(inputs)
                        self.assertEqual(simulate_add_fabric(fabric, inputs)[-1], expected)
                        self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], expected)

    def test_bound_wired_sum_candidates_preserve_bus_semantics(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v").read_text()
        source = source.replace("input [31:0] b,", "input [31:0] b,\n    input [31:0] c,d,e,").replace("assign y = a + b;", "assign y = ((a + b) + (c + d)) + e;")
        baseline = self.compile(source)
        assert baseline.behavior is not None
        fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
        self.assertEqual({len(item.additions) for item in fabrics}, {4, 5})
        for fabric in fabrics:
            with self.subTest(additions=len(fabric.additions)):
                provenance = {entity for origin in fabric.design.provenance if origin.values for entity in origin.entities}
                self.assertTrue({item.endpoint.entity for item in fabric.inputs} <= provenance)
                finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", power_layout="compact")
                for inputs in ({"a": 41, "b": 1, "c": 2, "d": 3, "e": 4}, {"a": 0xffffffff, "b": 2, "c": 0x80000000, "d": 0x80000000, "e": 1}):
                    expected = baseline.behavior.graph.evaluate(inputs)
                    self.assertEqual(simulate_add_fabric(fabric, inputs)[-1], expected)
                    self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], expected)

    def test_invalid_hdl_interface_bindings_are_rejected(self):
        source = (Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v").read_text()
        for invalid, message in (
            (source.replace('"signal-B"', '"signal-A"'), "Duplicate signal"),
            (source.replace('"signal-B"', '"iron-plate"'), "virtual signal"),
            (source.replace('"green"', '"blue"'), "red or green"),
            (source.replace(', factorio_color = "green"', ''), "requires exactly"),
        ):
            with self.subTest(message=message):
                baseline = self.compile(invalid)
                with self.assertRaisesRegex(RealizationError, message):
                    propose_add_fabrics(baseline.material, baseline.behavior)

    def test_serialized_copper_connects_only_poles_with_and_without_input_drivers(self):
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        for layout in ("grid", "compact"):
            for drivers in ("none", "constant"):
                with self.subTest(layout=layout, drivers=drivers):
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, power_layout=layout, input_drivers=drivers, column_pitch=6, row_pitch=3)
                    blueprint = build_finalized_factorio_blueprint(finalized).canonical_data()["blueprint"]
                    by_number = {item["entity_number"]: item for item in blueprint["entities"]}
                    self.assertEqual(sum(item["name"] == "constant-combinator" for item in by_number.values()), 2 if drivers == "constant" else 0)
                    copper = [wire for wire in blueprint["wires"] if 5 in (wire[1], wire[3])]
                    self.assertEqual(len(copper), len(finalized.power_segments))
                    self.assertTrue(copper)
                    for source, source_connector, target, target_connector in copper:
                        self.assertEqual((source_connector, target_connector), (5, 5))
                        self.assertTrue(by_number[source]["name"].endswith("electric-pole"))
                        self.assertTrue(by_number[target]["name"].endswith("electric-pole"))
                    with self.assertRaisesRegex(RealizationError, "Copper wires may connect poles only"):
                        replace(finalized, power_segments=(*finalized.power_segments, FactorioPowerSegment(fabric.additions[0].entity, finalized.external_supply)))
                    scene = build_finalized_factorio_view(finalized).scenes[0]
                    for entity in finalized.entities:
                        element = scene.element(f"factorio-entity:{entity.identifier}")
                        properties = {item.name: item.value for item in element.descriptor.properties}
                        self.assertEqual(properties["Prototype"], entity.prototype)
                        self.assertEqual(float(properties["Tile X"]), entity.x)
                        self.assertTrue(properties["Role"])

    def test_supply_reuses_circuit_poles_and_shares_coverage(self):
        loads = {
            "first": FactorioEntity("first", "arithmetic-combinator", 4, 0.5),
            "second": FactorioEntity("second", "small-lamp", 7.5, 0.5),
        }
        supply = _choose_supply(loads, _supply_candidates(loads))
        self.assertEqual(len(supply), 1)
        self.assertTrue(all(_covered(item, supply[0]) for item in loads.values()))
        self.assertFalse(any(entities_collide(item, supply[0]) for item in loads.values()))
        loads["relay"] = replace(supply[0], identifier="relay")
        self.assertEqual(_choose_supply(loads, _supply_candidates(loads)), ())

    def test_grid_routes_around_occupied_sites_without_duplicate_poles(self):
        entities = {
            "left": FactorioEntity("left", "medium-electric-pole", 0.5, 0.5),
            "right": FactorioEntity("right", "medium-electric-pole", 16.5, 0.5),
            "obstacle": FactorioEntity("obstacle", "constant-combinator", 8.5, 0.5),
        }
        power = _grid_power(entities, (0.5, 0.5))
        poles = [item for item in entities.values() if item.prototype.endswith("electric-pole")]
        self.assertEqual(len(power), len(poles) - 1)
        self.assertTrue(any(item.y != 0.5 for item in poles))
        for pole in poles:
            self.assertEqual((pole.x - 0.5) % 4, 0)
            self.assertEqual((pole.y - 0.5) % 4, 0)
            self.assertFalse(any(entities_collide(pole, other) for other in entities.values() if other != pole))
        for segment in power:
            first, second = entities[segment.source], entities[segment.target]
            self.assertTrue(first.x == second.x or first.y == second.y)

    def test_both_power_layouts_preserve_inventory_and_semantics(self):
        baseline = self.compile("module top(input [31:0] a,b,c,d,e, output [31:0] y); assign y=((a+b)+(c+d))+e; endmodule")
        assert baseline.behavior is not None
        for fabric in propose_add_fabrics(baseline.material, baseline.behavior):
            for layout in ("grid", "compact"):
                with self.subTest(layout=layout, adders=len(fabric.additions)):
                    finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, power_layout=layout, input_drivers="constant", output_lamps=True, column_pitch=6, row_pitch=3)
                    finalized.validate_physical()
                    self.assertEqual(finalized.canonical_data()["power_layout"], layout)
                    entities = {item.identifier: item for item in finalized.entities}
                    self.assertEqual(len({(item.x, item.y) for item in entities.values()}), len(entities))
                    if layout == "grid":
                        generated = [item for item in entities.values() if item.identifier.startswith("power:")]
                        self.assertEqual(len({(item.x % 4, item.y % 4) for item in generated}), 1)
                        for segment in finalized.power_segments:
                            if segment.source.startswith("power:") and segment.target.startswith("power:"):
                                first, second = entities[segment.source], entities[segment.target]
                                self.assertTrue(first.x == second.x or first.y == second.y)
                    inputs = dict(a=0xffffffff, b=3, c=5, d=7, e=11)
                    self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], baseline.behavior.graph.evaluate(inputs))

    def test_wired_sum_reassociation_has_a_universal_yosys_sat_check(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "miter.v"
            path.write_text("""
                module top(input [31:0] a,b,c,d,e, output equivalent);
                wire [31:0] left_sum=a+b, right_sum=c+d;
                wire [31:0] source_total=(left_sum+right_sum)+e;
                wire [31:0] broadcast_total=(((a+b)+c)+d)+e;
                assign equivalent=(source_total==broadcast_total);
                endmodule
            """, encoding="utf-8")
            context = design_preprocess(str(path))
            context.run_pass("sat -verify -prove equivalent 1 top")

    def compile(self, source):
        backend = compilation_backend("factorio")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "circuit.v"
            path.write_text(source, encoding="utf-8")
            result = start_compilation_search(
                str(path), target="factorio", behavior_lowerer=backend.behavior_lowerer
            ).finish()
            return materialize_search_candidates(result, backend.target_providers).candidates[0]

    def check_simulation(self, fabrics, baseline):
        assert baseline.behavior is not None
        graph = baseline.behavior.graph
        names = [item.name for item in fabrics[0].inputs]
        random = Random(71)
        values = [0, 1, 0x7fffffff, 0x80000000, 0xffffffff]
        inputs = [{name: value for name in names} for value in values]
        inputs.extend({name: random.getrandbits(32) for name in names} for _ in range(30))
        for fabric in fabrics:
            bound = fabric.validate(baseline.material, graph).settling_ticks
            for stimulus in inputs:
                history = simulate_add_fabric(fabric, stimulus)
                for output in history[bound:]:
                    self.assertEqual(output, graph.evaluate(stimulus))

    def test_add32_direct_fabric_and_independent_input_channels(self):
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        assert baseline.behavior is not None
        fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
        self.assertEqual(len(fabrics), 1)
        self.assertEqual(len(fabrics[0].additions), 1)
        self.assertEqual(fabrics[0].analyze().settling_ticks, 1)
        finalized = finalize_add_fabric(fabrics[0], baseline.material, baseline.behavior.graph)
        finalized.validate(baseline.material, baseline.behavior.graph)
        self.assertEqual(finalized.settling_ticks, 1)
        self.assertIn(finalized.external_supply, {item.identifier for item in finalized.entities})
        blueprint = build_finalized_factorio_blueprint(finalized)
        self.assertEqual(len(blueprint.entities), len(finalized.entities))
        self.assertEqual(len(blueprint.wires), len(finalized.wires) + len(finalized.power_segments))
        for entity, serialized in zip(finalized.entities, blueprint.entities):
            self.assertEqual((entity.x, entity.y), (serialized.x, serialized.y))
            if not entity.configuration.is_empty:
                self.assertEqual(serialized.canonical_data()["control_behavior"], entity.configuration.canonical_data())
        self.assertEqual(simulate_finalized_add(finalized, {"a": 0xffffffff, "b": 2})[-1], {"port:y": 1})
        view = build_finalized_factorio_view(finalized)
        scene = view.scenes[0]
        self.assertEqual(len(scene.elements), len(finalized.entities) + len(finalized.wires) + len(finalized.power_segments))
        for entity in finalized.entities:
            element = scene.element(f"factorio-entity:{entity.identifier}")
            self.assertEqual((element.transform.x, element.transform.y), (entity.x * 32, entity.y * 32))
        self.assertEqual({net.identifier for net in scene.nets}, {domain.identifier for domain in finalized.domains if len(domain.endpoints) > 1})
        with self.assertRaisesRegex(RealizationError, "require simulation stimuli"):
            simulate_finalized_add(finalized)
        input_endpoints = {item.endpoint for item in fabrics[0].inputs}
        self.assertTrue(all(len(input_endpoints & set(domain.endpoints)) <= 1 for domain in fabrics[0].domains))
        for limit in (-1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(RealizationError):
                propose_add_fabrics(baseline.material, baseline.behavior, max_rewrites=limit)
        self.check_simulation(fabrics, baseline)

    def test_wired_sum_removes_an_adder_and_one_critical_tick(self):
        baseline = self.compile("""
            module top(input [31:0] a,b,c,d,e, output [31:0] y);
            wire [31:0] left_sum=a+b, right_sum=c+d;
            wire [31:0] total=left_sum+right_sum;
            assign y=total+e;
            endmodule
        """)
        assert baseline.behavior is not None
        fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
        self.assertEqual([len(item.additions) for item in fabrics], [4, 3])
        self.assertEqual([item.analyze().settling_ticks for item in fabrics], [3, 2])
        self.assertEqual(len(baseline.material.objects), 4)
        self.assertNotEqual(fabrics[0].design.get_digest(), fabrics[1].design.get_digest())
        removed = {item.entity for item in fabrics[0].additions} - {item.entity for item in fabrics[1].additions}
        self.assertEqual(len(removed), 1)
        self.assertFalse(removed & {item.identifier for item in fabrics[1].design.entities})
        self.assertTrue(any(origin.rule == "factorio/wired-sum/v1" for origin in fabrics[1].design.provenance))
        self.assertTrue(any(len(channel.terms) == 4 for channel in fabrics[1].analyze().channels))
        self.assertEqual(len(propose_add_fabrics(baseline.material, baseline.behavior, max_rewrites=0)), 1)
        for fabric in fabrics:
            finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", input_values={"a": "0xffffffff"}, output_lamps=True)
            finalized.validate(baseline.material, baseline.behavior.graph)
            self.assertEqual(sum(item.prototype == "arithmetic-combinator" for item in finalized.entities), len(fabric.additions))
            self.assertTrue(finalized.power_segments)
            self.assertEqual(sum(item.name == "arithmetic-combinator" for item in build_finalized_factorio_blueprint(finalized).entities), len(fabric.additions))
            stimulus = {item.name: 0 for item in fabric.inputs}
            stimulus["a"] = 0xffffffff
            self.assertEqual(simulate_finalized_add(finalized)[-1], baseline.behavior.graph.evaluate(stimulus))
            random = Random(29)
            for _ in range(20):
                stimulus = {item.name: random.getrandbits(32) for item in fabric.inputs}
                expected = baseline.behavior.graph.evaluate(stimulus)
                self.assertTrue(all(output == expected for output in simulate_finalized_add(finalized, stimulus)[finalized.settling_ticks:]))
        self.check_simulation(fabrics, baseline)

    def test_long_shared_domains_route_through_inventory_owned_relays(self):
        baseline = self.compile("""
            module top(input [31:0] a,b,c,d, output [31:0] y,z,tap);
            wire [31:0] subtotal=a+b;
            assign tap=subtotal; assign y=subtotal+c; assign z=subtotal+d;
            endmodule
        """)
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        positions = tuple(RealizedEntityPlacement(
            entity.identifier, index * 45 + (0 if entity.kind == "arithmetic-combinator" else 0.5), 0.5
        ) for index, entity in enumerate(fabric.design.entities))
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, positions=positions)
        self.assertTrue(any(len(item.endpoints) >= 3 for item in fabric.domains))
        self.assertGreater(len(finalized.design.entities), len(fabric.design.entities))
        for domain in finalized.domains:
            self.assertEqual(sum(wire.domain == domain.identifier for wire in finalized.wires), len(domain.endpoints) - 1)
        inputs = {item.name: index + 1 for index, item in enumerate(fabric.inputs)}
        self.assertEqual(simulate_finalized_add(finalized, inputs)[-1], baseline.behavior.graph.evaluate(inputs))

    def test_physical_tampering_is_rejected_before_export(self):
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", output_lamps=True)
        for change, message in (
            ({"placement": replace(finalized.placement, realization_digest="0" * 64)}, "inventory digest"),
            ({"wires": finalized.wires[1:]}, "connect"),
            ({"power_segments": ()}, "disconnected"),
            ({"external_supply": "missing"}, "supply"),
            ({"settling_ticks": finalized.settling_ticks + 1}, "settling bound"),
            ({"domains": ()}, "domain identities"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RealizationError, message):
                replace(finalized, **change)
        moved = tuple(replace(item, x=item.x + 100) if item.entity == fabric.inputs[0].endpoint.entity else item for item in finalized.placement.entities)
        with self.assertRaisesRegex(RealizationError, "exceeds reach"):
            replace(finalized, placement=replace(finalized.placement, entities=moved))
        addition_id = fabric.additions[0].entity
        design = replace(finalized.design, entities=tuple(
            replace(item, configuration=ProviderConfiguration()) if item.identifier == addition_id else item
            for item in finalized.design.entities
        ))
        with self.assertRaisesRegex(RealizationError, "arithmetic configuration"):
            replace(finalized, design=design, placement=replace(finalized.placement, realization_digest=design.get_digest()))

    def test_driver_values_change_final_artifact_not_source_or_topology(self):
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        first = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant")
        second = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, input_drivers="constant", input_values={"a": 41, "b": 1})
        self.assertEqual(first.fabric.get_digest(), second.fabric.get_digest())
        self.assertEqual(first.wires, second.wires)
        self.assertNotEqual(first.get_digest(), second.get_digest())
        self.assertEqual(simulate_finalized_add(second)[-1], {"port:y": 42})
        for options in ({"input_values": {"a": 1}}, {"input_drivers": "constant", "input_values": {"missing": 1}}, {"output_lamps": "yes"}):
            with self.subTest(options=options), self.assertRaises(RealizationError):
                finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, **options)

    def test_power_coverage_collision_and_grid_alignment_are_enforced(self):
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        coordinates = {
            fabric.inputs[0].endpoint.entity: (0.5, -1.5),
            fabric.inputs[1].endpoint.entity: (0.5, 2.5),
            fabric.additions[0].entity: (4.0, 0.5),
            fabric.observations[0].read.endpoint.entity: (7.5, 0.5),
        }
        positions = tuple(RealizedEntityPlacement(identifier, *position) for identifier, position in coordinates.items())
        options = {"input_drivers": "constant", "output_lamps": True}
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, positions=positions, **options)
        self.assertEqual(sum(item.prototype.endswith("electric-pole") for item in finalized.entities), 1)
        moved = tuple(replace(item, x=item.x + 100) if item.entity == finalized.external_supply else item for item in finalized.placement.entities)
        with self.assertRaisesRegex(RealizationError, "supply coverage"):
            replace(finalized, placement=replace(finalized.placement, entities=moved))
        for invalid, message in (
            (positions[:-1], "exactly its inventory"),
            ((replace(positions[0], angle=45), *positions[1:]), "quarter turns"),
            ((replace(positions[0], x=0.1), *positions[1:]), "tile-aligned"),
            ((replace(positions[0], x=positions[1].x, y=positions[1].y), *positions[1:]), "overlap"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RealizationError, message):
                finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, positions=invalid, **options)
        rotated = tuple(replace(item, x=4.5, y=0, angle=90) if item.entity == fabric.additions[0].entity else item for item in positions)
        rotated_final = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph, positions=rotated, **options)
        arithmetic = next(item for item in build_finalized_factorio_blueprint(rotated_final).entities if item.name == "arithmetic-combinator")
        self.assertEqual(arithmetic.direction, 8)
        self.assertEqual(simulate_finalized_add(rotated_final, {"a": 2, "b": 3})[-1], {"port:y": 5})

    def test_native_lamp_survives_without_optional_output_lamps(self):
        backend = compilation_backend("factorio")
        path = Path(__file__).parent / "fixtures" / "factorio" / "lamp_intrinsic.v"
        result = start_compilation_search(str(path), target="factorio", behavior_lowerer=backend.behavior_lowerer).finish()
        baseline = materialize_search_candidates(result, backend.target_providers).candidates[0]
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        finalized = finalize_add_fabric(fabric, baseline.material, baseline.behavior.graph)
        self.assertEqual(sum(item.prototype == "small-lamp" for item in finalized.entities), 1)
        self.assertEqual(sum(item.name == "small-lamp" for item in build_finalized_factorio_blueprint(finalized).entities), 1)

    def test_observed_or_shared_upstream_values_block_destructive_merge(self):
        for output, expression in (("left_sum", "a+b"), ("other", "left_sum+e")):
            with self.subTest(output=output):
                baseline = self.compile(f"""
                    module top(input [31:0] a,b,c,d,e, output [31:0] y, {output});
                    {'' if output == 'left_sum' else 'wire [31:0] left_sum=a+b;'}
                    wire [31:0] right_sum=c+d;
                    assign {output}={expression};
                    assign y=left_sum+right_sum;
                    endmodule
                """)
                assert baseline.behavior is not None
                fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
                self.assertEqual(len(fabrics), 1)
                self.check_simulation(fabrics, baseline)

    def test_repeated_operand_is_not_mistaken_for_two_broadcasts(self):
        baseline = self.compile("""
            module top(input [31:0] a,b, output [31:0] y);
            wire [31:0] subtotal=a+b;
            assign y=subtotal+subtotal;
            endmodule
        """)
        assert baseline.behavior is not None
        fabrics = propose_add_fabrics(baseline.material, baseline.behavior)
        self.assertEqual(len(fabrics), 1)
        self.check_simulation(fabrics, baseline)

    def test_source_validation_rejects_accidental_double_read(self):
        baseline = self.compile("module top(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule")
        assert baseline.behavior is not None
        fabric = propose_add_fabrics(baseline.material, baseline.behavior)[0]
        addition = fabric.additions[0]
        invalid = replace(fabric, additions=(replace(addition, first=replace(addition.first, colors=(RED, GREEN))),))
        with self.assertRaisesRegex(RealizationError, "differs from source"):
            invalid.validate(baseline.material, baseline.behavior.graph)
        self.assertNotEqual(simulate_add_fabric(invalid, {"a": 3, "b": 5})[-1], {"port:y": 8})


if __name__ == "__main__":
    unittest.main()