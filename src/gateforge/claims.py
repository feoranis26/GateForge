from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json

from pyosys import libyosys as ys

from gateforge.design import DesignContext, rtlil_id
from gateforge.mapping import BoundaryBinding, MappingProposal
from gateforge.source import (
    ConstantBoundarySource,
    DesignSnapshot,
    ModuleSnapshot,
    SourceCut,
)
from gateforge.state import (
    ClaimDefinition,
    ClaimDefinitionId,
    ClaimPortBinding,
    CompilationIntermediateState,
)
from gateforge.target import (
    PortDirection,
    PrefabId,
    PrefabPort,
    PrefabPortRef,
    SemanticPrefab,
    TargetProvider,
)


class ClaimApplicationError(ValueError):
    pass


@dataclass(slots=True)
class _PreparedPort:
    formal: str
    target: PrefabPortRef
    direction: PortDirection
    signal: ys.SigSpec


@dataclass(slots=True)
class _PreparedClaim:
    proposal: MappingProposal
    identifier: ClaimDefinitionId
    prefab_id: PrefabId
    module_name: str
    module: ys.Module
    cells: tuple[ys.Cell, ...]
    ports: tuple[_PreparedPort, ...]
    source_provenance: tuple[str, ...]


def _prefab_ports(prefab: SemanticPrefab) -> dict[str, PrefabPort]:
    return {port.name: port for port in prefab.ports}


def _cut_direction(module: ModuleSnapshot, cut: SourceCut) -> PortDirection:
    if isinstance(cut.source, ConstantBoundarySource):
        return PortDirection.INPUT

    has_input = False
    has_output = False
    for endpoint in cut.inside:
        port = module.cells[endpoint.cell.name].ports[endpoint.name]
        has_input |= port.direction in {PortDirection.INPUT, PortDirection.INOUT}
        has_output |= port.direction in {PortDirection.OUTPUT, PortDirection.INOUT}

    if has_input and has_output:
        return PortDirection.INOUT
    if has_output:
        return PortDirection.OUTPUT
    if has_input:
        return PortDirection.INPUT
    raise ClaimApplicationError(f"Cannot derive direction for source cut {cut}")


def _claim_definition_id(
    snapshot: DesignSnapshot,
    proposal: MappingProposal,
) -> tuple[ClaimDefinitionId, tuple[str, ...]]:
    module_name = next(iter({identifier.module for identifier in proposal.ids}))
    module = snapshot.module(module_name)
    sources: list[dict[str, str]] = []
    provenance: list[str] = []
    for identifier in sorted(proposal.ids):
        cell = module.cells[identifier.name]
        if cell.anchor:
            sources.append({"anchor": cell.anchor, "type": identifier.expected_type})
            provenance.append(f"{module_name}.[{cell.anchor}]")
        elif source_location := cell.attributes.get("src"):
            siblings = sorted(
                item.identifier.name
                for item in module.cells.values()
                if item.identifier.expected_type == identifier.expected_type
                and item.attributes.get("src") == source_location
            )
            ordinal = siblings.index(identifier.name)
            sources.append(
                {
                    "source": source_location,
                    "ordinal": str(ordinal),
                    "type": identifier.expected_type,
                }
            )
            provenance.append(f"{module_name}.@{source_location}[{ordinal}]")
        else:
            sources.append({"name": identifier.name, "type": identifier.expected_type})
            provenance.append(f"{module_name}.{identifier.name}")

    data = {
        "schema_version": 1,
        "module": module_name,
        "sources": sources,
        "provider": proposal.provider,
        "mapper": proposal.mapper,
        "rule": proposal.rule,
        "rule_version": proposal.rule_version,
        "prefab": proposal.prefab.get_id().value,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return (
        ClaimDefinitionId(hashlib.sha256(encoded).hexdigest()),
        tuple(provenance),
    )


def _validate_boundary(
    snapshot: DesignSnapshot,
    proposal: MappingProposal,
) -> tuple[ModuleSnapshot, dict[object, SourceCut]]:
    module_name = next(iter({identifier.module for identifier in proposal.ids}))
    module = snapshot.module(module_name)
    cuts = module.derive_cut(proposal.ids)
    cuts_by_source: dict[object, SourceCut] = {cut.source: cut for cut in cuts}

    actual_sources = {binding.source for binding in proposal.boundary}
    expected_sources = set(cuts_by_source)
    if actual_sources != expected_sources:
        missing = expected_sources - actual_sources
        extra = actual_sources - expected_sources
        raise ClaimApplicationError(
            f"Proposal boundary does not match the source-region cut; "
            f"missing={missing!r}, extra={extra!r}"
        )

    ports = _prefab_ports(proposal.prefab)
    expected_targets = {
        PrefabPortRef(port.name, bit)
        for port in proposal.prefab.ports
        for bit in range(port.width)
    }
    targets = [binding.target for binding in proposal.boundary]
    if len(targets) != len(set(targets)):
        raise ClaimApplicationError("A prefab boundary port is bound more than once")
    if set(targets) != expected_targets:
        missing = expected_targets - set(targets)
        extra = set(targets) - expected_targets
        raise ClaimApplicationError(
            f"Proposal does not bind the complete prefab interface; "
            f"missing={missing!r}, extra={extra!r}"
        )

    for binding in proposal.boundary:
        try:
            target_port = ports[binding.target.port]
        except KeyError as error:
            raise ClaimApplicationError(
                f"Unknown prefab port {binding.target.port!r}"
            ) from error
        if binding.target.bit < 0 or binding.target.bit >= target_port.width:
            raise ClaimApplicationError(
                f"Bit {binding.target.bit} is outside prefab port "
                f"{binding.target.port}[{target_port.width - 1}:0]"
            )
        source_direction = _cut_direction(module, cuts_by_source[binding.source])
        if source_direction != target_port.direction:
            raise ClaimApplicationError(
                f"Source {binding.source} is {source_direction.value}, but prefab "
                f"port {binding.target.port} is {target_port.direction.value}"
            )

    return module, cuts_by_source


def _prepare_claim(
    context: DesignContext,
    snapshot: DesignSnapshot,
    proposal: MappingProposal,
    providers: Mapping[str, TargetProvider],
) -> _PreparedClaim:
    if proposal.revision != snapshot.revision:
        raise ClaimApplicationError(
            f"Proposal revision {proposal.revision} does not match snapshot "
            f"revision {snapshot.revision}"
        )
    try:
        provider = providers[proposal.provider]
    except KeyError as error:
        raise ClaimApplicationError(
            f"No target provider is registered as {proposal.provider!r}"
        ) from error
    try:
        provider.validate(proposal.prefab)
    except ValueError as error:
        raise ClaimApplicationError(str(error)) from error

    module_snapshot, cuts_by_source = _validate_boundary(snapshot, proposal)
    module = context.module(module_snapshot.name)
    cells: list[ys.Cell] = []
    for identifier in sorted(proposal.ids):
        cell = module.cell(rtlil_id(identifier.name))
        if cell is None:
            raise ClaimApplicationError(
                f"Cell {identifier.module}.{identifier.name} no longer exists"
            )
        if cell.type.unescape() != identifier.expected_type:
            raise ClaimApplicationError(
                f"Cell {identifier.module}.{identifier.name} changed type from "
                f"{identifier.expected_type!r} to {cell.type.unescape()!r}"
            )
        cells.append(cell)

    bindings_by_target: dict[PrefabPortRef, BoundaryBinding] = {
        binding.target: binding for binding in proposal.boundary
    }
    prefab_ports = _prefab_ports(proposal.prefab)
    prepared_ports: list[_PreparedPort] = []
    for index, target in enumerate(sorted(bindings_by_target, key=lambda item: (item.port, item.bit))):
        binding = bindings_by_target[target]
        cut = cuts_by_source[binding.source]
        prepared_ports.append(
            _PreparedPort(
                formal=f"p{index}",
                target=target,
                direction=prefab_ports[target.port].direction,
                signal=context.resolve_cut_source(snapshot, cut),
            )
        )

    identifier, provenance = _claim_definition_id(snapshot, proposal)
    return _PreparedClaim(
        proposal=proposal,
        identifier=identifier,
        prefab_id=proposal.prefab.get_id(),
        module_name=module_snapshot.name,
        module=module,
        cells=tuple(cells),
        ports=tuple(prepared_ports),
        source_provenance=provenance,
    )


def _blackbox_name(prefab_id: PrefabId) -> str:
    return f"gateforge_box_{prefab_id.value}"


def _get_or_create_blackbox(
    context: DesignContext,
    prepared: _PreparedClaim,
) -> ys.Module:
    name = _blackbox_name(prepared.prefab_id)
    module_id = rtlil_id(name)
    existing = context.design.module(module_id)
    generated_attribute = rtlil_id("gateforge_generated_box")
    prefab_attribute = rtlil_id("gateforge_prefab")
    if existing is not None:
        if (
            not existing.get_bool_attribute(generated_attribute)
            or existing.get_string_attribute(prefab_attribute)
            != prepared.prefab_id.value
        ):
            raise ClaimApplicationError(f"Generated module name collision: {name}")
        return existing

    blackbox = context.design.addModule(module_id)
    for index, port in enumerate(prepared.ports):
        wire = blackbox.addWire(rtlil_id(port.formal), 1)
        wire.port_id = index + 1
        wire.port_input = port.direction in {PortDirection.INPUT, PortDirection.INOUT}
        wire.port_output = port.direction in {PortDirection.OUTPUT, PortDirection.INOUT}
    blackbox.fixup_ports()
    blackbox.makeblackbox()
    blackbox.set_bool_attribute(generated_attribute)
    blackbox.set_string_attribute(prefab_attribute, prepared.prefab_id.value)
    return blackbox


def accept_mapping_proposals(
    context: DesignContext,
    state: CompilationIntermediateState,
    proposals: Sequence[MappingProposal],
    providers: Mapping[str, TargetProvider],
) -> CompilationIntermediateState:
    if not proposals:
        return state
    if state.revision != context.revision:
        raise ClaimApplicationError(
            f"State revision {state.revision} does not match live design "
            f"revision {context.revision}"
        )

    snapshot = context.snapshot()
    prepared_claims = tuple(
        _prepare_claim(context, snapshot, proposal, providers)
        for proposal in proposals
    )
    identifiers = [prepared.identifier for prepared in prepared_claims]
    if len(identifiers) != len(set(identifiers)):
        raise ClaimApplicationError("Accepted proposals produce duplicate claim IDs")

    claims: list[ClaimDefinition] = []
    replacements: list[tuple[_PreparedClaim, ys.Cell]] = []
    for prepared in prepared_claims:
        blackbox = _get_or_create_blackbox(context, prepared)
        instance_name = f"$gateforge$claim${prepared.identifier.value}"
        if prepared.module.cell(rtlil_id(instance_name)) is not None:
            raise ClaimApplicationError(
                f"Claim instance {prepared.module_name}.{instance_name} already exists"
            )
        replacement = prepared.module.addCell(rtlil_id(instance_name), blackbox.name)
        replacement.set_bool_attribute(rtlil_id("keep"))
        replacement.set_string_attribute(
            rtlil_id("gateforge_claim_definition"), prepared.identifier.value
        )
        replacement.set_string_attribute(
            rtlil_id("gateforge_prefab"), prepared.prefab_id.value
        )
        replacement.set_string_attribute(
            rtlil_id("gateforge_mapper"), prepared.proposal.mapper
        )
        for port in prepared.ports:
            replacement.setPort(rtlil_id(port.formal), port.signal)
        replacements.append((prepared, replacement))

        claims.append(
            ClaimDefinition(
                identifier=prepared.identifier,
                prefab=prepared.prefab_id,
                module=prepared.module_name,
                instance=instance_name,
                blackbox=blackbox.name.unescape(),
                ports=tuple(
                    ClaimPortBinding(
                        formal=port.formal,
                        target=port.target,
                        direction=port.direction,
                    )
                    for port in prepared.ports
                ),
                provider=prepared.proposal.provider,
                mapper=prepared.proposal.mapper,
                rule=prepared.proposal.rule,
                rule_version=prepared.proposal.rule_version,
                accepted_revision=snapshot.revision,
                source_provenance=prepared.source_provenance,
            )
        )

    for prepared, _ in replacements:
        for cell in prepared.cells:
            prepared.module.remove(cell)

    context.design.check()
    context.mark_mutated()
    return state.with_acceptance(
        revision=context.revision,
        prefabs=(prepared.proposal.prefab for prepared in prepared_claims),
        claims=claims,
    )