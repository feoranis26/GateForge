import unittest

from gateforge.graph import MaterialGraph
from gateforge.placement import TopologicalPlacer
from gateforge.provider import TargetProvider
from gateforge.target import NetworkInterfaceMode, PortDirection
from gateforge.visualization import (
    VisualBounds,
    VisualElementDescriptor,
    VisualPoint,
    VisualPort,
    VisualRectangle,
    VisualText,
)
from gateforge.visualization.build import build_visual_document
from tests.test_graph import (
    ADDITIVE_PROVIDER,
    _additive_provider,
    _lbp_inverter_design,
    _packed_additive_design,
)
from gateforge.providers.lbp.common import LBP_PROVIDER
from gateforge.providers.lbp.objects import make_lbp_provider


class _RecordingAdapter:
    def __init__(self) -> None:
        self.roles: list[str] = []

    def describe_material_object(self, material_object, registry):
        self.roles.append(material_object.role)
        bounds = VisualBounds.centered(60.0, 30.0)
        return VisualElementDescriptor(
            material_object.role,
            bounds,
            (VisualRectangle(bounds), VisualText(VisualPoint(0.0, 0.0), "custom")),
            (
                VisualPort("IN_0[0]", "IN", PortDirection.INPUT, VisualPoint(-30.0, 0.0)),
                VisualPort("OUT[0]", "OUT", PortDirection.OUTPUT, VisualPoint(30.0, 0.0)),
            ),
        )

    def build_realized_views(self, request):
        return ()


class VisualizationBuildTests(unittest.TestCase):
    def test_registered_provider_describes_material_elements(self) -> None:
        design = _lbp_inverter_design()
        base = make_lbp_provider()
        adapter = _RecordingAdapter()
        provider = TargetProvider(
            identifier=base.identifier,
            registry=base.registry,
            validator=base.validator,
            material_validator=base.material_validator,
            object_configuration_codec=base.object_configuration_codec,
            dependency_projector=base.dependency_projector,
            object_geometry_resolver=base.object_geometry_resolver,
            object_cost_resolver=base.object_cost_resolver,
            visualization=adapter,
        )
        providers = {LBP_PROVIDER: provider}
        graph = MaterialGraph.from_design(design, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)

        document = build_visual_document(design, graph, placed, providers)
        scene = document.views[0].scenes[0]

        self.assertEqual(adapter.roles, ["gate"])
        self.assertEqual(len(scene.elements), 3)
        self.assertEqual(len(scene.nets), 2)
        self.assertEqual(scene.element(f"object:{design.objects[0].identifier.value}").descriptor.label, "gate")

    def test_provider_without_visualization_uses_generic_descriptor(self) -> None:
        provider = _additive_provider()
        self.assertEqual(provider.identifier, ADDITIVE_PROVIDER)
        self.assertIsNone(provider.visualization)

    def test_packed_module_values_render_as_single_terminals(self) -> None:
        design = _packed_additive_design()
        provider = _additive_provider(
            interface_mode=NetworkInterfaceMode.PACKED
        )
        providers = {ADDITIVE_PROVIDER: provider}
        graph = MaterialGraph.from_design(design, providers)
        placed = TopologicalPlacer(providers=providers).place(graph).finalize(graph)

        document = build_visual_document(design, graph, placed, providers)
        scene = document.views[0].scenes[0]

        terminal_labels = {
            item.descriptor.label
            for item in scene.elements
            if item.identifier.startswith("module-value:")
        }
        self.assertEqual(terminal_labels, {"a[2:0]", "y[2:0]"})


if __name__ == "__main__":
    unittest.main()