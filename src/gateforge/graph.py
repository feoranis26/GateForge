from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gateforge.material import (
    MaterialAttachment,
    MaterialConstantRef,
    MaterialDesign,
    MaterialModulePortRef,
    MaterialNetId,
    MaterialObjectId,
    MaterialObjectPortRef,
    validate_material_design,
)
from gateforge.provider import TargetProvider
from gateforge.target import PortDirection, PrefabValidationError


class MaterialGraphError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ObjectSubject:
    object: MaterialObjectId


@dataclass(frozen=True, slots=True)
class ModulePortSubject:
    module: str
    port: str
    bit: int
    direction: PortDirection


@dataclass(frozen=True, slots=True)
class ConstantSubject:
    net: MaterialNetId
    value: str


type MaterialSubject = ObjectSubject | ModulePortSubject | ConstantSubject


@dataclass(frozen=True, slots=True)
class MaterialDependency:
    net: MaterialNetId
    source: MaterialAttachment
    target: MaterialAttachment


@dataclass(frozen=True, slots=True)
class MaterialGraph:
    design: MaterialDesign
    subjects: tuple[MaterialSubject, ...]
    dependencies: tuple[MaterialDependency, ...]
    predecessors: Mapping[MaterialSubject, frozenset[MaterialSubject]]
    successors: Mapping[MaterialSubject, frozenset[MaterialSubject]]
    incident_nets: Mapping[MaterialSubject, frozenset[MaterialNetId]]

    @classmethod
    def from_design(
        cls,
        design: MaterialDesign,
        providers: Mapping[str, TargetProvider],
    ) -> "MaterialGraph":
        validate_material_design(design, providers)
        objects = {item.identifier: item for item in design.objects}
        subjects: set[MaterialSubject] = {
            ObjectSubject(item.identifier) for item in design.objects
        }
        incident: dict[MaterialSubject, set[MaterialNetId]] = {
            subject: set() for subject in subjects
        }
        dependencies: set[MaterialDependency] = set()

        for net in design.nets:
            for attachment in net.attachments:
                subject = _attachment_subject(net.identifier, attachment)
                subjects.add(subject)
                incident.setdefault(subject, set()).add(net.identifier)
            try:
                provider = providers[net.type.provider]
            except KeyError as error:
                raise MaterialGraphError(
                    f"No provider is registered for material network {net.type}"
                ) from error
            try:
                projected = provider.project_material_dependencies(net, objects)
            except (PrefabValidationError, ValueError) as error:
                raise MaterialGraphError(str(error)) from error
            for item in projected:
                if item.source not in net.attachments:
                    raise MaterialGraphError(
                        f"Provider {provider.identifier!r} projected source "
                        f"{item.source} outside material net {net.identifier.value}"
                    )
                if item.target not in net.attachments:
                    raise MaterialGraphError(
                        f"Provider {provider.identifier!r} projected target "
                        f"{item.target} outside material net {net.identifier.value}"
                    )
                dependencies.add(
                    MaterialDependency(net.identifier, item.source, item.target)
                )

        ordered_subjects = tuple(sorted(subjects, key=material_subject_key))
        predecessors: dict[MaterialSubject, set[MaterialSubject]] = {
            subject: set() for subject in ordered_subjects
        }
        successors: dict[MaterialSubject, set[MaterialSubject]] = {
            subject: set() for subject in ordered_subjects
        }
        for dependency in dependencies:
            source = _attachment_subject(dependency.net, dependency.source)
            target = _attachment_subject(dependency.net, dependency.target)
            successors[source].add(target)
            predecessors[target].add(source)

        ordered_dependencies = tuple(
            sorted(
                dependencies,
                key=lambda item: (
                    item.net.value,
                    material_subject_key(
                        _attachment_subject(item.net, item.source)
                    ),
                    material_subject_key(
                        _attachment_subject(item.net, item.target)
                    ),
                    _attachment_key(item.source),
                    _attachment_key(item.target),
                ),
            )
        )
        return cls(
            design=design,
            subjects=ordered_subjects,
            dependencies=ordered_dependencies,
            predecessors=MappingProxyType(
                {
                    subject: frozenset(predecessors[subject])
                    for subject in ordered_subjects
                }
            ),
            successors=MappingProxyType(
                {
                    subject: frozenset(successors[subject])
                    for subject in ordered_subjects
                }
            ),
            incident_nets=MappingProxyType(
                {
                    subject: frozenset(incident.get(subject, set()))
                    for subject in ordered_subjects
                }
            ),
        )


def material_subject_key(subject: MaterialSubject) -> tuple[object, ...]:
    if isinstance(subject, ConstantSubject):
        return (0, subject.net.value, subject.value)
    if isinstance(subject, ModulePortSubject):
        return (
            1,
            subject.module,
            subject.port,
            subject.bit,
            subject.direction.value,
        )
    return (2, subject.object.value)


def _attachment_subject(
    net: MaterialNetId,
    attachment: MaterialAttachment,
) -> MaterialSubject:
    if isinstance(attachment, MaterialObjectPortRef):
        return ObjectSubject(attachment.object)
    if isinstance(attachment, MaterialModulePortRef):
        return ModulePortSubject(
            attachment.module,
            attachment.port,
            attachment.bit,
            attachment.direction,
        )
    if isinstance(attachment, MaterialConstantRef):
        return ConstantSubject(net, attachment.value)
    raise MaterialGraphError(f"Unknown material attachment {attachment!r}")


def _attachment_key(attachment: MaterialAttachment) -> tuple[object, ...]:
    if isinstance(attachment, MaterialObjectPortRef):
        return (0, attachment.object.value, attachment.port, attachment.bit)
    if isinstance(attachment, MaterialModulePortRef):
        return (
            1,
            attachment.module,
            attachment.port,
            attachment.bit,
            attachment.direction.value,
        )
    return (2, attachment.value)
