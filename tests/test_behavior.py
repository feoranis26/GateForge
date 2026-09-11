import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from gateforge.behavior import (
    BehaviorBuilder,
    BehaviorError,
    BehaviorGraph,
    BehaviorNode,
    BehaviorObservation,
    BehaviorOperation,
)
from gateforge.behavior_source import (
    YosysCombinationalBehaviorLowerer,
    bind_material_behavior,
)
from gateforge.compiler import (
    compilation_backend,
    compile_realization,
    design_preprocess,
    materialize_search_candidates,
    start_compilation_search,
)
from gateforge.source import DesignSnapshot
from gateforge.material import MaterialModuleValueRef


class BehaviorGraphTests(unittest.TestCase):
    def test_shared_additions_are_not_expanded(self) -> None:
        builder = BehaviorBuilder()
        left = builder.input("left", 32)
        right = builder.input("right", 32)
        total = builder.add(left, right, 32)
        self.assertEqual(total, builder.add(right, left, 32))
        graph = builder.build({"first": total, "second": total})
        self.assertEqual(len(graph.nodes), 3)
        self.assertEqual(
            graph.evaluate({"left": 0xffffffff, "right": 2}),
            {"first": 1, "second": 1},
        )

    def test_signed_extension_and_truncation_are_explicit(self) -> None:
        for signed, expected in ((False, 16), (True, 0)):
            builder = BehaviorBuilder()
            left = builder.input("left", 4)
            right = builder.constant(1, 8)
            total = builder.add(left, right, 8, signed=signed)
            narrowed = builder.resize(total, 3)
            graph = builder.build({"wide": total, "narrow": narrowed})
            self.assertEqual(graph.evaluate({"left": 15}), {"wide": expected, "narrow": 0})

    def test_concatenation_is_lsb_first(self) -> None:
        builder = BehaviorBuilder()
        word = builder.input("word", 8)
        swapped = builder.concat((builder.extract(word, 4, 4), builder.extract(word, 0, 4)))
        graph = builder.build({"swapped": swapped})
        self.assertEqual(graph.evaluate({"word": 0xab}), {"swapped": 0xba})

    def test_digest_ignores_independent_node_creation_order(self) -> None:
        graphs = []
        for names in (("left", "right"), ("right", "left")):
            builder = BehaviorBuilder()
            values = {name: builder.input(name, 8) for name in names}
            graphs.append(builder.build({"sum": builder.add(values["left"], values["right"], 8)}))
        self.assertEqual(graphs[0].get_digest(), graphs[1].get_digest())

    def test_invalid_graphs_and_out_of_range_inputs_fail(self) -> None:
        builder = BehaviorBuilder()
        value = builder.input("value", 8)
        with self.assertRaises(BehaviorError):
            builder.extract(value, 8)
        with self.assertRaises(BehaviorError):
            builder.constant(256, 8)
        with self.assertRaises(BehaviorError):
            BehaviorGraph((), (BehaviorObservation("missing", "unknown"),))
        with self.assertRaises(BehaviorError):
            BehaviorGraph((BehaviorNode(BehaviorOperation.ADD, 8, ("missing", "missing")),), ())
        graph = builder.build({"value": value})
        for inputs in ({}, {"value": -1}, {"value": 256}, {"value": True}):
            with self.subTest(inputs=inputs), self.assertRaises(BehaviorError):
                graph.evaluate(inputs)


class BehaviorCaptureTests(unittest.TestCase):
    def test_conditional_processes_do_not_accept_inferred_storage(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "storage.v"
            for process in ("always @* if (enable) y = a;", "always @(posedge enable) y <= a;"):
                with self.subTest(process=process):
                    source.write_text(f"module top(input enable, input [31:0] a, output reg [31:0] y); {process} endmodule")
                    context = design_preprocess(str(source))
                    with self.assertRaisesRegex(BehaviorError, "stateful|X/Z"):
                        YosysCombinationalBehaviorLowerer().lower(context.snapshot())

    def test_conditions_and_selection_match_yosys_sat(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "conditional.v"
            for signedness in ((False, False), (True, True), (True, False), (False, True)):
                for expression in ("a == b", "a != b", "a < b", "a <= b", "a > b", "a >= b", "a && b", "a || b", "!a", "(a < b) ? a : b", "a ? b : 8'h81"):
                    with self.subTest(signedness=signedness, expression=expression):
                        source.write_text(f"module top(input {'signed' if signedness[0] else ''} [3:0] a, input {'signed' if signedness[1] else ''} [7:0] b, output [7:0] y); assign y = {expression}; endmodule\n")
                        context = design_preprocess(str(source))
                        capture = YosysCombinationalBehaviorLowerer().lower(context.snapshot())
                        for left, right in ((0, 0), (15, 255), (8, 128), (7, 127), (15, 1), (1, 0)):
                            expected = capture.graph.evaluate({"a": left, "b": right})["port:y"]
                            context.run_pass(f"sat -verify -set a {left} -set b {right} -prove y {expected} top")

    def test_bitwise_capture_matches_yosys_width_and_sign_semantics(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "bitwise.v"
            for signed in (False, True):
                for expression in ("a & b", "a | b", "a ^ b", "~a", "((a + b) & 8'hf0) ^ 8'h81"):
                    with self.subTest(signed=signed, expression=expression):
                        source.write_text(f"module top(input {'signed' if signed else ''} [3:0] a, input {'signed' if signed else ''} [7:0] b, output [7:0] y); assign y = {expression}; endmodule\n")
                        context = design_preprocess(str(source))
                        capture = YosysCombinationalBehaviorLowerer().lower(context.snapshot())
                        for left, right in ((0, 0), (15, 255), (8, 128), (7, 127), (15, 1)):
                            expected = capture.graph.evaluate({"a": left, "b": right})["port:y"]
                            context.run_pass(f"sat -verify -set a {left} -set b {right} -prove y {expected} top")

    def test_optimized_masks_recover_word_operations(self) -> None:
        path = Path(__file__).parent / "fixtures" / "factorio" / "bitwise32.v"
        context = design_preprocess(str(path))
        original = YosysCombinationalBehaviorLowerer().lower(context.snapshot())
        context.run_pass("opt_expr")
        optimized = YosysCombinationalBehaviorLowerer().lower(context.snapshot())
        self.assertTrue(all(node.width == 32 for node in optimized.graph.nodes))
        self.assertIn(BehaviorOperation.BIT_OR, {node.operation for node in optimized.graph.nodes})
        for value in (0, 1, 0x7fffffff, 0x80000000, 0xffffffff):
            inputs = {"a": value, "b": 1, "mask": 0xff00ff00, "flags": 0xa5a5a5a5}
            self.assertEqual(optimized.graph.evaluate(inputs), original.graph.evaluate(inputs))

    def test_port_attributes_survive_source_behavior_capture(self) -> None:
        path = Path(__file__).parent / "fixtures" / "factorio" / "bound_add32.v"
        snapshot = design_preprocess(str(path)).snapshot()
        capture = YosysCombinationalBehaviorLowerer().lower(snapshot)
        ports = {port.name: dict(port.attributes) for port in capture.ports}
        self.assertEqual(ports["a"]["factorio_signal"], "signal-A")
        self.assertEqual(ports["b"]["factorio_color"], "green")
        self.assertEqual(ports["tap"]["factorio_circuit"], "outputs")

    def test_source_evaluator_matches_yosys_sat_on_width_and_sign_corners(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "addition.v"
            for left_signed, right_signed in ((False, False), (True, True), (True, False), (False, True)):
                for output_width in (3, 8):
                    source.write_text(
                        f"module top(input {'signed' if left_signed else ''} [3:0] a, "
                        f"input {'signed' if right_signed else ''} [7:0] b, "
                        f"output [{output_width - 1}:0] y); assign y = a + b; endmodule\n",
                        encoding="utf-8",
                    )
                    context = design_preprocess(str(source))
                    capture = YosysCombinationalBehaviorLowerer().lower(context.snapshot())
                    for left, right in ((0, 0), (7, 127), (8, 128), (15, 1), (15, 255)):
                        with self.subTest(signedness=(left_signed, right_signed), width=output_width, inputs=(left, right)):
                            expected = capture.graph.evaluate({"a": left, "b": right})["port:y"]
                            context.run_pass(f"sat -verify -set a {left} -set b {right} -prove y {expected} top")

    def test_generic_backend_can_request_capture_for_realization(self) -> None:
        backend = replace(compilation_backend(), behavior_lowerer=YosysCombinationalBehaviorLowerer())
        path = Path(__file__).parent / "fixtures" / "single_not.v"
        result = compile_realization(str(path), backend=backend)
        baseline = result.winner.baseline
        self.assertIsNotNone(baseline.behavior)
        self.assertIsNotNone(baseline.behavior_boundary)
        assert baseline.behavior_boundary is not None
        self.assertEqual(len(baseline.behavior_boundary.bindings), 2)
        self.assertTrue(all(item.offsets == (0,) for item in baseline.behavior_boundary.bindings))

    def test_behavior_survives_add_mapping_and_materialization(self) -> None:
        path = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"
        backend = compilation_backend("factorio")
        session = start_compilation_search(
            str(path), target="factorio", behavior_lowerer=backend.behavior_lowerer
        )
        captured = session.behavior
        self.assertIsNotNone(captured)
        assert captured is not None
        digest = captured.graph.get_digest()
        result = session.finish()
        materialized = materialize_search_candidates(result, backend.target_providers)
        self.assertIs(result.behavior, captured)
        for candidate in materialized.candidates:
            self.assertIs(candidate.behavior, captured)
            self.assertGreater(candidate.context.revision, captured.revision)
            self.assertEqual(len(candidate.material.objects), 1)
            boundary = candidate.behavior_boundary
            self.assertIsNotNone(boundary)
            assert boundary is not None
            self.assertEqual(boundary.material_digest, candidate.material.get_digest())
            self.assertEqual(boundary.behavior_digest, digest)
            self.assertEqual({item.port for item in boundary.bindings}, {"a", "b", "y"})
            self.assertTrue(all(item.offsets == tuple(range(32)) for item in boundary.bindings))
        self.assertEqual(captured.graph.get_digest(), digest)
        self.assertEqual(captured.graph.evaluate({"a": 40, "b": 2}), {"port:y": 42})

        candidate = materialized.candidates[0]
        net = candidate.material.nets[0]
        invalid_attachments = frozenset(
            replace(attachment, bits=(32,)) if isinstance(attachment, MaterialModuleValueRef)
            else attachment for attachment in net.attachments
        )
        invalid = replace(candidate.material, nets=(
            replace(net, attachments=invalid_attachments), *candidate.material.nets[1:]
        ))
        with self.assertRaisesRegex(BehaviorError, "outside its behavior value"):
            bind_material_behavior(invalid, captured)

    def test_native_lamp_is_preserved_as_an_observation(self) -> None:
        path = Path(__file__).parent / "fixtures" / "factorio" / "lamp_intrinsic.v"
        backend = compilation_backend("factorio")
        assert backend.behavior_lowerer is not None
        capture = backend.behavior_lowerer.lower(design_preprocess(str(path)).snapshot())
        observed = capture.graph.evaluate({"a": 40, "b": 2})
        self.assertTrue(any(name.startswith("cell:") for name in observed))
        self.assertTrue(all(value == 42 for value in observed.values()))

    def test_add32_snapshot_retains_word_nodes_and_source_bindings(self) -> None:
        path = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"
        snapshot = design_preprocess(str(path)).snapshot()
        capture = YosysCombinationalBehaviorLowerer().lower(snapshot)
        self.assertEqual(len(capture.graph.nodes), 3)
        self.assertEqual(capture.graph.evaluate({"a": 0xffffffff, "b": 2}), {"port:y": 1})
        self.assertEqual(len(capture.bits), 96)
        self.assertEqual(len(capture.cells), 3)
        self.assertEqual(capture.revision, snapshot.revision)

    def test_mixed_signedness_matches_yosys_extension_rule(self) -> None:
        for left_signed, right_signed, expected in ((1, 1, 0), (1, 0, 16), (0, 1, 16)):
            snapshot = self._snapshot(left_signed, right_signed)
            capture = YosysCombinationalBehaviorLowerer().lower(snapshot)
            self.assertEqual(capture.graph.evaluate({"a": 15, "b": 1}), {"port:y": expected})

    def test_source_aliases_constants_and_reordered_bits(self) -> None:
        snapshot = DesignSnapshot.from_json({"modules": {"top": {"ports": {
            "a": {"direction": "input", "bits": [2, 3, 4, 5]},
            "y": {"direction": "output", "bits": [5, 4, 3, 2, "1", "0"]},
            "constant": {"direction": "output", "bits": ["1", "0", "1"]},
        }}}}, 1)
        capture = YosysCombinationalBehaviorLowerer().lower(snapshot)
        self.assertEqual(capture.graph.evaluate({"a": 0b1100}), {"port:y": 0b010011, "port:constant": 5})

    def test_reconvergent_adds_keep_shared_value_and_distinct_observations(self) -> None:
        snapshot = self._snapshot(0, 0)
        module = snapshot.module("top")
        first = module.cells["add"]
        second = replace(
            first, identifier=replace(first.identifier, name="second"),
            parameters={**first.parameters, "A_WIDTH": "1000"},
            ports={
                **first.ports,
                "A": replace(first.ports["A"], bits=first.ports["Y"].bits),
                "Y": replace(first.ports["Y"], bits=tuple(
                    replace(bit, bit_id=bit.bit_id + 8) for bit in first.ports["Y"].bits
                )),
            },
        )
        changed = replace(module, cells={"second": second, "add": first}, ports={
            **module.ports,
            "other": replace(module.ports["y"], identifier="other", bits=second.ports["Y"].bits),
        })
        capture = YosysCombinationalBehaviorLowerer().lower(replace(snapshot, modules={"top": changed}))
        self.assertEqual(capture.graph.evaluate({"a": 3, "b": 4}), {"port:y": 7, "port:other": 11})
        self.assertEqual(sum(node.operation == BehaviorOperation.ADD for node in capture.graph.nodes), 2)

    def test_feedback_and_undriven_outputs_fail(self) -> None:
        snapshot = self._snapshot(0, 0)
        module = snapshot.module("top")
        cell = module.cells["add"]
        feedback = replace(cell, ports={**cell.ports, "B": replace(
            cell.ports["B"], bits=cell.ports["Y"].bits
        )})
        with self.assertRaisesRegex(BehaviorError, "feedback or undriven"):
            YosysCombinationalBehaviorLowerer().lower(replace(
                snapshot, modules={"top": replace(module, cells={"add": feedback})}
            ))
        with self.assertRaisesRegex(BehaviorError, "Undriven behavior output"):
            YosysCombinationalBehaviorLowerer().lower(DesignSnapshot.from_json({
                "modules": {"top": {"ports": {
                    "y": {"direction": "output", "bits": [2]},
                }}},
            }, 1))

    def test_repeated_compilation_has_stable_behavior_digest(self) -> None:
        path = Path(__file__).parent / "fixtures" / "factorio" / "add32.v"
        lowerer = YosysCombinationalBehaviorLowerer()
        captures = tuple(lowerer.lower(design_preprocess(str(path)).snapshot()) for _ in range(2))
        self.assertEqual(captures[0].graph.get_digest(), captures[1].graph.get_digest())

    def test_unknown_stateful_and_feedback_inputs_are_rejected(self) -> None:
        snapshot = self._snapshot(0, 0)
        module = snapshot.module("top")
        cell = module.cells["add"]
        stateful = replace(cell, identifier=replace(cell.identifier, expected_type="$dff"))
        with self.assertRaisesRegex(BehaviorError, "explicit semantics"):
            YosysCombinationalBehaviorLowerer().lower(replace(
                snapshot, modules={"top": replace(module, cells={"add": stateful})}
            ))
        for constant in ("x", "z"):
            data = {"modules": {"top": {"ports": {"y": {"direction": "output", "bits": [constant]}}}}}
            with self.assertRaisesRegex(BehaviorError, "X/Z"):
                YosysCombinationalBehaviorLowerer().lower(DesignSnapshot.from_json(data, 1))

    @staticmethod
    def _snapshot(left_signed, right_signed):
        return DesignSnapshot.from_json({"modules": {"top": {
            "ports": {
                "a": {"direction": "input", "bits": [2, 3, 4, 5]},
                "b": {"direction": "input", "bits": list(range(6, 14))},
                "y": {"direction": "output", "bits": list(range(14, 22))},
            },
            "cells": {"add": {
                "type": "$add",
                "parameters": {"A_WIDTH": "100", "B_WIDTH": "1000", "Y_WIDTH": "1000",
                               "A_SIGNED": str(left_signed), "B_SIGNED": str(right_signed)},
                "port_directions": {"A": "input", "B": "input", "Y": "output"},
                "connections": {"A": [2, 3, 4, 5], "B": list(range(6, 14)), "Y": list(range(14, 22))},
            }},
        }}}, 1)


if __name__ == "__main__":
    unittest.main()