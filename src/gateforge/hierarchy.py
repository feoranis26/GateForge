from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from collections.abc import Mapping

from gateforge.design import DesignContext
from gateforge.material import (
    ImplementationPackaging,
    MaterialDesign,
    MaterialModuleOccurrence,
)
from gateforge.provider import TargetProvider


class HierarchyPolicyError(ValueError):
    pass


class SynthesisHierarchyMode(StrEnum):
    PRESERVE = "preserve"
    FLAT = "flat"
    MIN_CELLS = "min-cells"


@dataclass(frozen=True, slots=True)
class SynthesisHierarchyPolicy:
    mode: SynthesisHierarchyMode = SynthesisHierarchyMode.PRESERVE
    threshold: int | None = None

    def __post_init__(self) -> None:
        if self.mode == SynthesisHierarchyMode.MIN_CELLS:
            if (
                not isinstance(self.threshold, int)
                or isinstance(self.threshold, bool)
                or self.threshold <= 0
            ):
                raise HierarchyPolicyError(
                    "min-cells synthesis hierarchy requires a positive threshold"
                )
        elif self.threshold is not None:
            raise HierarchyPolicyError(
                f"Synthesis hierarchy mode {self.mode.value!r} does not use a threshold"
            )


def apply_synthesis_hierarchy(
    context: DesignContext,
    policy: SynthesisHierarchyPolicy,
) -> None:
    if policy.mode == SynthesisHierarchyMode.PRESERVE:
        return
    if policy.mode == SynthesisHierarchyMode.FLAT:
        context.run_pass("flatten -noscopeinfo")
        return
    threshold = policy.threshold
    if threshold is None:
        raise AssertionError("min-cells policy has no threshold")

    while True:
        snapshot = context.snapshot()
        top_modules = [
            module.name
            for module in snapshot.modules.values()
            if "top" in module.attributes
        ]
        if len(top_modules) != 1:
            raise HierarchyPolicyError(
                f"Expected one top module, found {top_modules!r}"
            )
        module_names = {
            name
            for name, module in snapshot.modules.items()
            if "blackbox" not in module.attributes
        }
        totals: dict[str, int] = {}

        def count_cells(module_name: str, active: frozenset[str]) -> int:
            if module_name in active:
                raise HierarchyPolicyError(
                    f"Recursive synthesis hierarchy at {module_name!r}"
                )
            cached = totals.get(module_name)
            if cached is not None:
                return cached
            module = snapshot.module(module_name)
            value = 0
            for cell in module.cells.values():
                if cell.identifier.expected_type in module_names:
                    value += count_cells(
                        cell.identifier.expected_type,
                        active | {module_name},
                    )
                else:
                    value += 1
            totals[module_name] = value
            return value

        reachable: set[str] = set()
        selections: list[str] = []

        def visit(module_name: str) -> None:
            if module_name in reachable:
                return
            reachable.add(module_name)
            module = snapshot.module(module_name)
            for cell in module.cells.values():
                child = cell.identifier.expected_type
                if child not in module_names:
                    continue
                if count_cells(child, frozenset()) < threshold:
                    selections.append(f"{module_name}/{cell.identifier.name}")
                else:
                    visit(child)

        visit(top_modules[0])
        if not selections:
            return
        if any(any(character.isspace() for character in item) for item in selections):
            raise HierarchyPolicyError(
                "Synthesis hierarchy names containing whitespace are unsupported"
            )
        before = tuple(sorted(selections))
        selection = " ".join(before)
        context.run_pass(f"flatten -noscopeinfo {selection}")
        updated = context.snapshot()
        still_present = tuple(
            item
            for item in before
            if _selection_exists(updated, item)
        )
        if still_present == before:
            raise HierarchyPolicyError(
                f"Selective flatten made no progress for {before!r}"
            )


def _selection_exists(snapshot, selection: str) -> bool:
    module_name, cell_name = selection.split("/", 1)
    module = snapshot.modules.get(module_name)
    return module is not None and cell_name in module.cells


class PhysicalHierarchyMode(StrEnum):
    FLAT = "flat"
    PRESERVE_ALL = "preserve-all"
    MIN_OBJECTS = "min-objects"
    MIN_COST = "min-cost"


class GeneratedHierarchyMode(StrEnum):
    INLINE = "inline"
    AUTO = "auto"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class GeneratedHierarchyPolicy:
    mode: GeneratedHierarchyMode = GeneratedHierarchyMode.AUTO
    auto_min_objects: int = 3

    def __post_init__(self) -> None:
        if (
            not isinstance(self.auto_min_objects, int)
            or isinstance(self.auto_min_objects, bool)
            or self.auto_min_objects <= 0
        ):
            raise HierarchyPolicyError(
                "Generated hierarchy auto_min_objects must be positive"
            )


def retained_implementation_paths(
    design: MaterialDesign,
    policy: GeneratedHierarchyPolicy,
) -> frozenset[str]:
    if policy.mode == GeneratedHierarchyMode.INLINE:
        return frozenset()
    retained: set[str] = set()
    for implementation in design.implementations:
        if implementation.packaging == ImplementationPackaging.INLINE:
            continue
        if policy.mode == GeneratedHierarchyMode.ALL:
            if len(implementation.objects) > 1:
                retained.add(implementation.path)
            continue
        if implementation.packaging == ImplementationPackaging.CONTAINER or (
            implementation.packaging == ImplementationPackaging.AUTO
            and len(implementation.objects) >= policy.auto_min_objects
        ):
            retained.add(implementation.path)
    return frozenset(retained)


@dataclass(frozen=True, slots=True)
class PhysicalHierarchyPolicy:
    mode: PhysicalHierarchyMode = PhysicalHierarchyMode.FLAT
    threshold: float | None = None

    def __post_init__(self) -> None:
        if self.mode in {
            PhysicalHierarchyMode.MIN_OBJECTS,
            PhysicalHierarchyMode.MIN_COST,
        }:
            if (
                isinstance(self.threshold, bool)
                or not isinstance(self.threshold, (int, float))
                or not math.isfinite(float(self.threshold))
                or self.threshold <= 0
            ):
                raise HierarchyPolicyError(
                    f"{self.mode.value} physical hierarchy requires a positive threshold"
                )
            object.__setattr__(self, "threshold", float(self.threshold))
        elif self.threshold is not None:
            raise HierarchyPolicyError(
                f"Physical hierarchy mode {self.mode.value!r} does not use a threshold"
            )


def retained_module_paths(
    design: MaterialDesign,
    policy: PhysicalHierarchyPolicy,
    providers: Mapping[str, TargetProvider],
) -> frozenset[str]:
    if not design.modules:
        return frozenset()
    modules = {item.path: item for item in design.modules}
    roots = [item for item in design.modules if item.parent is None]
    if len(roots) != 1:
        raise HierarchyPolicyError(
            f"Material hierarchy requires one root, found {len(roots)}"
        )
    root = roots[0]
    if policy.mode == PhysicalHierarchyMode.FLAT:
        return frozenset({root.path})
    if policy.mode == PhysicalHierarchyMode.PRESERVE_ALL:
        return frozenset(modules)

    objects = {item.identifier: item for item in design.objects}
    direct_values: dict[str, float] = {}
    for module in design.modules:
        if policy.mode == PhysicalHierarchyMode.MIN_OBJECTS:
            direct_values[module.path] = float(len(module.objects))
        else:
            total = 0.0
            for identifier in module.objects:
                material_object = objects[identifier]
                try:
                    provider = providers[material_object.type.provider]
                except KeyError as error:
                    raise HierarchyPolicyError(
                        f"Missing provider {material_object.type.provider!r}"
                    ) from error
                total += provider.material_object_cost(material_object)
            direct_values[module.path] = total

    totals: dict[str, float] = {}

    def total(path: str) -> float:
        cached = totals.get(path)
        if cached is not None:
            return cached
        module = modules[path]
        value = direct_values[path] + sum(total(child) for child in module.children)
        totals[path] = value
        return value

    threshold = policy.threshold
    if threshold is None:
        raise AssertionError("Threshold policy has no threshold")
    retained = {root.path}
    retained.update(
        module.path
        for module in design.modules
        if module.path != root.path and total(module.path) >= threshold
    )
    return frozenset(retained)


def nearest_retained_parent(
    occurrence: MaterialModuleOccurrence,
    modules: Mapping[str, MaterialModuleOccurrence],
    retained: frozenset[str],
) -> str | None:
    parent = occurrence.parent
    while parent is not None and parent not in retained:
        parent = modules[parent].parent
    return parent
