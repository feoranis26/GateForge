import unittest

from gateforge.graph import (
    ConstantSubject,
    MaterialGraph,
    MaterialGraphError,
    ModulePortSubject,
    ObjectSubject,
)
from gateforge.material import (
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialNet,
    MaterialObject,
    MaterialObjectPortRef,
    OccurrenceId,
    make_material_net_id,
    make_material_object_id,
)
from gateforge.provider import ProjectedDependency, TargetProvider
from gateforge.providers.lbp.common import LBP_PROVIDER, LBP_WIRE
from gateforge.providers.lbp.objects import make_lbp_provider
from gateforge.providers.lbp.types import LBPNotGateType
from gateforge.target import (
    NetworkTypeIdentifier,
    NetworkTypeSchema,
    ObjectTypeIdentifier,
    ObjectTypeSchema,
    PortDirection,
    PortSchema,
    PrefabId,
    SignalTypeIdentifier,
    TargetTypeRegistry,
)


ADDITIVE_PROVIDER = "additive"
ADDITIVE_SIGNAL = SignalTypeIdentifier(ADDITIVE_PROVIDER, "value")
ADDITIVE_WIRE = NetworkTypeIdentifier(ADDITIVE_PROVIDER, "wire")
ADDITIVE_NODE = ObjectTypeIdentifier(ADDITIVE_PROVIDER, "node")


def _material_object(role: str, type_: ObjectTypeIdentifier, token: str) -> MaterialObject:
    occurrence = OccurrenceId(token * 64)
    prefab = PrefabId(chr(ord(token) + 1) * 64)
    return MaterialObject(
        identifier=make_material_object_id(occurrence, prefab, role),
        occurrence=occurrence,
        prefab=prefab,
        role=role,
        type=type_,
        hierarchy=f"module:test/{role}",
    )


def _material_net(type_: NetworkTypeIdentifier, *attachments) -> MaterialNet:
    frozen = frozenset(attachments)
    return MaterialNet(make_material_net_id(type_, frozen), type_, frozen)


def _lbp_inverter_design(constant_input: bool = False) -> MaterialDesign:
    gate = _material_object(
        "gate",
        LBPNotGateType(width=1, invert=False).get_type(),
        "a",
    )
    source = (
        MaterialConstantRef("1")
        if constant_input
        else MaterialModulePortRef("test", "a", 0, PortDirection.INPUT)
    )
    return MaterialDesign(
        (gate,),
        (
            _material_net(
                LBP_WIRE,
                source,
                MaterialObjectPortRef(gate.identifier, "IN_0"),
            ),
            _material_net(
                LBP_WIRE,
                MaterialObjectPortRef(gate.identifier, "OUT"),
                MaterialModulePortRef("test", "y", 0, PortDirection.OUTPUT),
            ),
        ),
    )


def _additive_provider(projector=None) -> TargetProvider:
    registry = TargetTypeRegistry()
    registry.register_network(NetworkTypeSchema(ADDITIVE_WIRE, ADDITIVE_SIGNAL))
    registry.register_object(
        ObjectTypeSchema(
            ADDITIVE_NODE,
            frozenset(
                {
                    PortSchema("IN", PortDirection.INPUT, ADDITIVE_SIGNAL),
                    PortSchema("OUT", PortDirection.OUTPUT, ADDITIVE_SIGNAL),
                }
            ),
        )
    )

    def default_projector(net, objects, registry):
        sources = [
            item
            for item in net.attachments
            if isinstance(item, MaterialObjectPortRef) and item.port == "OUT"
        ]
        targets = [
            item
            for item in net.attachments
            if isinstance(item, MaterialObjectPortRef) and item.port == "IN"
        ]
        return tuple(
            ProjectedDependency(source, target)
            for source in sources
            for target in targets
        )

    return TargetProvider(
        identifier=ADDITIVE_PROVIDER,
        registry=registry,
        validator=lambda prefab, registry: None,
        dependency_projector=projector or default_projector,
    )


class MaterialGraphTests(unittest.TestCase):
    def test_lbp_projection_builds_terminal_object_chain(self) -> None:
        design = _lbp_inverter_design()

        graph = MaterialGraph.from_design(
            design,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        gate = ObjectSubject(design.objects[0].identifier)
        source = ModulePortSubject("test", "a", 0, PortDirection.INPUT)
        sink = ModulePortSubject("test", "y", 0, PortDirection.OUTPUT)
        self.assertEqual(graph.predecessors[gate], frozenset({source}))
        self.assertEqual(graph.successors[gate], frozenset({sink}))
        self.assertEqual(len(graph.dependencies), 2)

    def test_constant_subject_is_scoped_to_its_net(self) -> None:
        design = _lbp_inverter_design(constant_input=True)

        graph = MaterialGraph.from_design(
            design,
            {LBP_PROVIDER: make_lbp_provider()},
        )

        input_net = next(
            net
            for net in design.nets
            if any(isinstance(item, MaterialConstantRef) for item in net.attachments)
        )
        constant = ConstantSubject(input_net.identifier, "1")
        self.assertIn(constant, graph.subjects)
        self.assertEqual(graph.incident_nets[constant], frozenset({input_net.identifier}))

    def test_multiple_sources_are_provider_projected_not_core_rejected(self) -> None:
        left = _material_object("left", ADDITIVE_NODE, "c")
        right = _material_object("right", ADDITIVE_NODE, "e")
        sink = _material_object("sink", ADDITIVE_NODE, "g")
        net = _material_net(
            ADDITIVE_WIRE,
            MaterialObjectPortRef(left.identifier, "OUT"),
            MaterialObjectPortRef(right.identifier, "OUT"),
            MaterialObjectPortRef(sink.identifier, "IN"),
        )
        design = MaterialDesign((sink, right, left), (net,))

        graph = MaterialGraph.from_design(
            design,
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        sink_subject = ObjectSubject(sink.identifier)
        self.assertEqual(
            graph.predecessors[sink_subject],
            frozenset(
                {ObjectSubject(left.identifier), ObjectSubject(right.identifier)}
            ),
        )
        self.assertEqual(len(graph.dependencies), 2)

    def test_duplicate_projected_dependencies_are_normalized(self) -> None:
        source = _material_object("source", ADDITIVE_NODE, "i")
        sink = _material_object("sink", ADDITIVE_NODE, "k")
        source_ref = MaterialObjectPortRef(source.identifier, "OUT")
        sink_ref = MaterialObjectPortRef(sink.identifier, "IN")
        net = _material_net(ADDITIVE_WIRE, source_ref, sink_ref)

        def duplicate_projector(net, objects, registry):
            dependency = ProjectedDependency(source_ref, sink_ref)
            return (dependency, dependency)

        graph = MaterialGraph.from_design(
            MaterialDesign((source, sink), (net,)),
            {ADDITIVE_PROVIDER: _additive_provider(duplicate_projector)},
        )

        self.assertEqual(len(graph.dependencies), 1)

    def test_projector_cannot_reference_an_attachment_outside_the_net(self) -> None:
        source = _material_object("source", ADDITIVE_NODE, "m")
        sink = _material_object("sink", ADDITIVE_NODE, "o")
        source_ref = MaterialObjectPortRef(source.identifier, "OUT")
        sink_ref = MaterialObjectPortRef(sink.identifier, "IN")
        net = _material_net(ADDITIVE_WIRE, source_ref, sink_ref)

        def invalid_projector(net, objects, registry):
            return (ProjectedDependency(source_ref, MaterialConstantRef("outside")),)

        with self.assertRaises(MaterialGraphError):
            MaterialGraph.from_design(
                MaterialDesign((source, sink), (net,)),
                {ADDITIVE_PROVIDER: _additive_provider(invalid_projector)},
            )

    def test_nonempty_graph_requires_a_provider_dependency_projector(self) -> None:
        source = _material_object("source", ADDITIVE_NODE, "u")
        sink = _material_object("sink", ADDITIVE_NODE, "w")
        net = _material_net(
            ADDITIVE_WIRE,
            MaterialObjectPortRef(source.identifier, "OUT"),
            MaterialObjectPortRef(sink.identifier, "IN"),
        )
        provider = _additive_provider()
        provider = TargetProvider(
            identifier=provider.identifier,
            registry=provider.registry,
            validator=provider.validator,
        )

        with self.assertRaises(MaterialGraphError):
            MaterialGraph.from_design(
                MaterialDesign((source, sink), (net,)),
                {ADDITIVE_PROVIDER: provider},
            )

    def test_cycles_are_preserved_for_placer_policy(self) -> None:
        left = _material_object("left", ADDITIVE_NODE, "q")
        right = _material_object("right", ADDITIVE_NODE, "s")
        forward = _material_net(
            ADDITIVE_WIRE,
            MaterialObjectPortRef(left.identifier, "OUT"),
            MaterialObjectPortRef(right.identifier, "IN"),
        )
        backward = _material_net(
            ADDITIVE_WIRE,
            MaterialObjectPortRef(right.identifier, "OUT"),
            MaterialObjectPortRef(left.identifier, "IN"),
        )

        graph = MaterialGraph.from_design(
            MaterialDesign((left, right), (forward, backward)),
            {ADDITIVE_PROVIDER: _additive_provider()},
        )

        left_subject = ObjectSubject(left.identifier)
        right_subject = ObjectSubject(right.identifier)
        self.assertIn(right_subject, graph.successors[left_subject])
        self.assertIn(left_subject, graph.successors[right_subject])

    def test_empty_graph_is_valid(self) -> None:
        graph = MaterialGraph.from_design(MaterialDesign((), ()), {})

        self.assertEqual(graph.subjects, ())
        self.assertEqual(graph.dependencies, ())


if __name__ == "__main__":
    unittest.main()
