from pathlib import Path
import unittest

from gateforge.gateforge import compile_material
from gateforge.material import (
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialNetId,
    MaterialObjectId,
    MaterialObjectPortRef,
)
from gateforge.pipeline import default_mapping_stages
from gateforge.providers.lbp.types import (
    LBPAndGateType,
    LBPCombinatorialVariableWidthGateType,
    LBPNotGateType,
    LBPOrGateType,
    LBPXorGateType,
    decode_lbp_object_type,
)
from gateforge.target import PortDirection


FIXTURE = Path(__file__).parent / "fixtures" / "chained_and_gates.v"


def _evaluate_material(
    design: MaterialDesign,
    inputs: dict[str, bool],
    output: str,
) -> bool:
    net_values: dict[MaterialNetId, bool] = {}
    object_nets: dict[tuple[MaterialObjectId, str], MaterialNetId] = {}
    output_net: MaterialNetId | None = None

    for net in design.nets:
        for attachment in net.attachments:
            if isinstance(attachment, MaterialModulePortRef):
                if attachment.direction == PortDirection.INPUT:
                    net_values[net.identifier] = inputs[attachment.port]
                elif attachment.port == output:
                    output_net = net.identifier
            elif isinstance(attachment, MaterialConstantRef):
                if attachment.value not in {"0", "1"}:
                    raise AssertionError(
                        f"Unsupported test constant {attachment.value!r}"
                    )
                net_values[net.identifier] = attachment.value == "1"
            elif isinstance(attachment, MaterialObjectPortRef):
                object_nets[(attachment.object, attachment.port)] = net.identifier

    pending = {item.identifier: item for item in design.objects}
    while pending:
        progressed = False
        for identifier, material_object in tuple(pending.items()):
            gate = decode_lbp_object_type(material_object.type)
            if not isinstance(gate, LBPCombinatorialVariableWidthGateType):
                raise AssertionError(f"Unsupported test gate {type(gate).__name__}")
            input_values: list[bool] = []
            for index in range(gate.width):
                input_net = object_nets[(identifier, f"IN_{index}")]
                if input_net not in net_values:
                    break
                input_values.append(net_values[input_net])
            else:
                if isinstance(gate, LBPAndGateType):
                    value = all(input_values)
                elif isinstance(gate, LBPOrGateType):
                    value = any(input_values)
                elif isinstance(gate, LBPXorGateType):
                    value = sum(input_values) % 2 == 1
                elif isinstance(gate, LBPNotGateType):
                    value = not input_values[0]
                else:
                    raise AssertionError(f"Unsupported test gate {type(gate).__name__}")
                if gate.invert_output:
                    value = not value
                output_object_net = object_nets[(identifier, "OUT")]
                net_values[output_object_net] = value
                del pending[identifier]
                progressed = True
        if not progressed:
            raise AssertionError("Material graph could not be fully evaluated")

    if output_net is None or output_net not in net_values:
        raise AssertionError(f"Material output {output!r} has no resolved value")
    return net_values[output_net]


class CombinationalEquivalenceTests(unittest.TestCase):
    def test_chained_and_preserves_negated_input_with_and_without_abc(self) -> None:
        for use_abc in (False, True):
            with self.subTest(use_abc=use_abc):
                _, _, material = compile_material(
                    str(FIXTURE),
                    stages=default_mapping_stages(use_abc=use_abc),
                )
                gates = [decode_lbp_object_type(item.type) for item in material.objects]
                self.assertEqual(len(gates), 2)
                self.assertEqual(
                    sum(
                        isinstance(gate, LBPAndGateType) and gate.width == 3
                        for gate in gates
                    ),
                    1,
                )
                self.assertEqual(
                    sum(isinstance(gate, LBPNotGateType) for gate in gates),
                    1,
                )
                for input_a in (False, True):
                    for input_b in (False, True):
                        for input_c in (False, True):
                            inputs = {"a": input_a, "b": input_b, "c": input_c}
                            self.assertEqual(
                                _evaluate_material(material, inputs, "theoutput"),
                                input_a and not input_b and input_c,
                                inputs,
                            )


if __name__ == "__main__":
    unittest.main()