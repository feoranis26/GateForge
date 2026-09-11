from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
import re

from gateforge.behavior import BehaviorGraph, BehaviorOperation
from gateforge.behavior_source import BehaviorCapture, bind_material_behavior
from gateforge.material import MaterialDesign
from gateforge.providers.factorio.routed import FactorioWireColor
from gateforge.realization import (
    RealizationDesign,
    RealizationError,
    RealizationProvenance,
    RealizedEndpoint,
    RealizedEntity,
    RealizedObservation,
)
from gateforge.target import ProviderConfiguration
from gateforge.target import PortDirection


MODULUS = 1 << 32
COMPARATORS = frozenset({"=", "!=", "<", "<=", ">", ">="})


def _signal(name: str) -> None:
    if re.fullmatch(r"signal-[A-Z0-9]", name) is None:
        raise RealizationError("Expected a normal-quality alphanumeric virtual signal")


def _unique(items: tuple, label: str) -> None:
    if not isinstance(items, tuple) or len(set(items)) != len(items):
        raise RealizationError(f"{label} must be a unique immutable tuple")


@dataclass(frozen=True, slots=True)
class FactorioCircuitDomain:
    identifier: str
    color: FactorioWireColor
    endpoints: tuple[RealizedEndpoint, ...]

    def __post_init__(self) -> None:
        if not self.identifier or not isinstance(self.color, FactorioWireColor):
            raise RealizationError("Domain requires an identifier and wire color")
        _unique(self.endpoints, "Domain endpoints")
        if not self.endpoints:
            raise RealizationError("Domain must contain at least one endpoint")

    def canonical_data(self) -> dict[str, object]:
        return {
            "identifier": self.identifier, "color": self.color.value,
            "endpoints": [item.canonical_data() for item in sorted(self.endpoints)],
        }


@dataclass(frozen=True, slots=True)
class FactorioDomainWire:
    domain: str
    source: RealizedEndpoint
    target: RealizedEndpoint

    def __post_init__(self) -> None:
        if not self.domain or self.source == self.target:
            raise RealizationError("Domain wire requires a domain and distinct endpoints")
        if self.target < self.source:
            source = self.source
            object.__setattr__(self, "source", self.target)
            object.__setattr__(self, "target", source)

    def canonical_data(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "source": self.source.canonical_data(), "target": self.target.canonical_data(),
        }


@dataclass(frozen=True, slots=True)
class FactorioSignalRead:
    endpoint: RealizedEndpoint
    signal: str
    colors: tuple[FactorioWireColor, ...] = (FactorioWireColor.RED, FactorioWireColor.GREEN)

    def __post_init__(self) -> None:
        _signal(self.signal)
        _unique(self.colors, "Reader colors")
        if not self.colors or any(not isinstance(color, FactorioWireColor) for color in self.colors):
            raise RealizationError("Reader must select at least one valid wire color")

    def canonical_data(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint.canonical_data(), "signal": self.signal,
            "colors": sorted(color.value for color in self.colors),
        }


@dataclass(frozen=True, slots=True)
class FactorioInputEmission:
    name: str
    endpoint: RealizedEndpoint
    signal: str
    width: int = 32

    def __post_init__(self) -> None:
        if not self.name:
            raise RealizationError("Input emission requires an external input name")
        _signal(self.signal)
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width not in {1, 32}:
            raise RealizationError("Factorio inputs must be Boolean or 32-bit words")

    def canonical_data(self) -> dict[str, object]:
        return {"name": self.name, "endpoint": self.endpoint.canonical_data(), "signal": self.signal, "width": self.width}


@dataclass(frozen=True, slots=True)
class FactorioConstantRead:
    endpoint: RealizedEndpoint
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or not -(1 << 31) <= self.value < (1 << 31):
            raise RealizationError("Arithmetic constant must fit signed int32")

    def canonical_data(self) -> dict[str, object]:
        return {"endpoint": self.endpoint.canonical_data(), "constant": self.value}


@dataclass(frozen=True, slots=True)
class FactorioArithmetic:
    entity: str
    first: FactorioSignalRead | FactorioConstantRead
    second: FactorioSignalRead | FactorioConstantRead
    output_signal: str
    operation: str = "+"

    def __post_init__(self) -> None:
        if self.operation not in {"+", "*", "AND", "OR", "XOR", *COMPARATORS}:
            raise RealizationError("Unsupported arithmetic operation")
        _signal(self.output_signal)
        if any(read.endpoint != RealizedEndpoint(self.entity, "in") for read in (self.first, self.second)):
            raise RealizationError("Arithmetic operands must read their owning input connector")

    @property
    def prototype(self) -> str:
        return "decider-combinator" if self.operation in COMPARATORS else "arithmetic-combinator"

    @property
    def output(self) -> RealizedEndpoint:
        return RealizedEndpoint(self.entity, "out")

    def canonical_data(self) -> dict[str, object]:
        return {
            "entity": self.entity, "operation": self.operation, "latency_ticks": 1,
            "first": self.first.canonical_data(), "second": self.second.canonical_data(),
            "output_signal": self.output_signal,
        }


FactorioAdd = FactorioArithmetic


@dataclass(frozen=True, slots=True)
class FactorioObservationRead:
    name: str
    read: FactorioSignalRead

    def canonical_data(self) -> dict[str, object]:
        return {"name": self.name, "read": self.read.canonical_data()}


@dataclass(frozen=True, slots=True, order=True)
class FactorioTimedTerm:
    input: str
    delay: int
    coefficient: int


@dataclass(frozen=True, slots=True)
class FactorioChannelEquation:
    domain: str
    signal: str
    terms: tuple[FactorioTimedTerm, ...] | None


@dataclass(frozen=True, slots=True)
class FactorioFabricAnalysis:
    settling_ticks: int
    channels: tuple[FactorioChannelEquation, ...]
    observations: tuple[tuple[str, tuple[FactorioTimedTerm, ...] | None], ...]


@dataclass(frozen=True, slots=True)
class FactorioCircuitFabric:
    design: RealizationDesign
    domains: tuple[FactorioCircuitDomain, ...]
    wires: tuple[FactorioDomainWire, ...]
    inputs: tuple[FactorioInputEmission, ...]
    additions: tuple[FactorioAdd, ...]
    observations: tuple[FactorioObservationRead, ...]

    def __post_init__(self) -> None:
        if self.design.target != "factorio":
            raise RealizationError("Factorio fabric requires a Factorio inventory")
        for items, label in (
            (self.domains, "Domains"), (self.wires, "Wires"), (self.inputs, "Inputs"),
            (self.additions, "Additions"), (self.observations, "Observations"),
        ):
            _unique(items, label)
        entities = {item.identifier: item for item in self.design.entities}
        endpoints = {RealizedEndpoint(item.identifier, port) for item in self.design.entities for port in item.ports}
        for key, records in (
            ("identifier", self.domains), ("name", self.inputs),
            ("entity", self.additions), ("name", self.observations),
        ):
            if len({getattr(item, key) for item in records}) != len(records):
                raise RealizationError(f"Duplicate fabric {key}")
        membership: dict[tuple[RealizedEndpoint, FactorioWireColor], str] = {}
        domain_by_id = {item.identifier: item for item in self.domains}
        adjacency: dict[str, dict[RealizedEndpoint, set[RealizedEndpoint]]] = {}
        for domain in self.domains:
            if not set(domain.endpoints) <= endpoints:
                raise RealizationError("Domain references an unknown realized endpoint")
            adjacency[domain.identifier] = {endpoint: set() for endpoint in domain.endpoints}
            for endpoint in domain.endpoints:
                key = (endpoint, domain.color)
                if key in membership:
                    raise RealizationError("Same connector/color cannot belong to separate domains")
                membership[key] = domain.identifier
        for wire in self.wires:
            domain = domain_by_id.get(wire.domain)
            if domain is None or not {wire.source, wire.target} <= set(domain.endpoints):
                raise RealizationError("Wire endpoints must belong to their declared domain")
            adjacency[wire.domain][wire.source].add(wire.target)
            adjacency[wire.domain][wire.target].add(wire.source)
        for domain in self.domains:
            reached: set[RealizedEndpoint] = set()
            pending = [domain.endpoints[0]]
            while pending:
                endpoint = pending.pop()
                if endpoint not in reached:
                    reached.add(endpoint)
                    pending.extend(adjacency[domain.identifier][endpoint] - reached)
            if reached != set(domain.endpoints):
                raise RealizationError("Actual wires do not connect the declared domain")
        configured: set[str] = set()
        input_channels = set()
        for item in self.inputs:
            entity = entities.get(item.endpoint.entity)
            if entity is None or entity.kind != "input-interface" or entity.ports != ("out",):
                raise RealizationError("Input emission requires an input-interface entity")
            if item.endpoint.port != "out" or (item.endpoint, item.signal) in input_channels:
                raise RealizationError("Input interfaces must have distinct signal keys")
            input_channels.add((item.endpoint, item.signal))
            configured.add(item.endpoint.entity)
        for addition in self.additions:
            entity = entities.get(addition.entity)
            if entity is None or entity.kind != addition.prototype or set(entity.ports) != {"in", "out"}:
                raise RealizationError("Addition requires an arithmetic entity with input/output connectors")
            configured.add(addition.entity)
        expected = {item.name: item.endpoints for item in self.design.observations}
        if {item.name: (item.read.endpoint,) for item in self.observations} != expected:
            raise RealizationError("Fabric observation endpoints differ from inventory")
        reads = [item.read for item in self.observations]
        reads.extend(read for item in self.additions for read in (item.first, item.second))
        if any(read.endpoint not in endpoints for read in reads):
            raise RealizationError("Reader references an unknown endpoint")
        for entity in self.design.entities:
            if entity.kind in {"input-interface", "arithmetic-combinator", "decider-combinator"}:
                if entity.identifier not in configured:
                    raise RealizationError("Active entity lacks configuration")
            elif entity.kind != "junction" or entity.ports != ("circuit",):
                raise RealizationError("Unsupported unplaced fabric entity kind")

    def canonical_data(self) -> dict[str, object]:
        return {
            "schema_version": 1, "kind": "factorio-circuit-fabric",
            "timing": "settled", "design": self.design.canonical_data(),
            "domains": [item.canonical_data() for item in sorted(self.domains, key=lambda item: item.identifier)],
            "wires": [item.canonical_data() for item in sorted(self.wires, key=lambda item: (item.domain, item.source, item.target))],
            "inputs": [item.canonical_data() for item in sorted(self.inputs, key=lambda item: item.name)],
            "additions": [item.canonical_data() for item in sorted(self.additions, key=lambda item: item.entity)],
            "observations": [item.canonical_data() for item in sorted(self.observations, key=lambda item: item.name)],
        }

    def get_digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.canonical_data(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).hexdigest()

    def emitters(self) -> frozenset[tuple[RealizedEndpoint, str]]:
        return frozenset((item.endpoint, item.signal) for item in self.inputs) | frozenset((item.output, item.output_signal) for item in self.additions)

    def drivers(self, read: FactorioSignalRead | FactorioConstantRead) -> tuple[RealizedEndpoint, ...]:
        if isinstance(read, FactorioConstantRead):
            return ()
        emitters = self.emitters()
        return tuple(
            endpoint for domain in self.domains
            if domain.color in read.colors and read.endpoint in domain.endpoints
            for endpoint in domain.endpoints if (endpoint, read.signal) in emitters
        )

    def analyze(self) -> FactorioFabricAnalysis:
        if any(item.operation != "+" or any(isinstance(read, FactorioConstantRead) for read in (item.first, item.second)) for item in self.additions):
            from gateforge.providers.factorio.equivalence import analyze_bitvectors
            return analyze_bitvectors(self)
        expressions: dict[tuple[RealizedEndpoint, str], Counter[tuple[str, int]]] = {
            (item.endpoint, item.signal): Counter({(item.name, 0): 1}) for item in self.inputs
        }
        depths = {channel: 0 for channel in expressions}
        pending = list(self.additions)
        while pending:
            following: list[FactorioAdd] = []
            for addition in pending:
                drivers = tuple((endpoint, read.signal) for read in (addition.first, addition.second) for endpoint in self.drivers(read))
                if any(endpoint not in expressions for endpoint in drivers):
                    following.append(addition)
                    continue
                expression: Counter[tuple[str, int]] = Counter()
                for endpoint in drivers:
                    for (name, delay), coefficient in expressions[endpoint].items():
                        expression[name, delay + 1] += coefficient
                expressions[addition.output, addition.output_signal] = expression
                depths[addition.output, addition.output_signal] = 1 + max((depths[endpoint] for endpoint in drivers), default=0)
            if len(following) == len(pending):
                raise RealizationError("Factorio fabric has combinational feedback")
            pending = following

        def terms(drivers: tuple[tuple[RealizedEndpoint, str], ...]) -> tuple[FactorioTimedTerm, ...]:
            combined: Counter[tuple[str, int]] = Counter()
            for endpoint in drivers:
                combined.update(expressions[endpoint])
            return tuple(
                FactorioTimedTerm(name, delay, coefficient % MODULUS)
                for (name, delay), coefficient in sorted(combined.items()) if coefficient % MODULUS
            )

        emitters = self.emitters()
        channels = tuple(
            FactorioChannelEquation(domain.identifier, signal, terms(tuple(
                (endpoint, signal) for endpoint in domain.endpoints if (endpoint, signal) in emitters
            )))
            for domain in sorted(self.domains, key=lambda item: item.identifier)
            for signal in sorted({signal for endpoint, signal in emitters if endpoint in domain.endpoints})
        )
        return FactorioFabricAnalysis(
            max((depths[endpoint, item.read.signal] for item in self.observations for endpoint in self.drivers(item.read)), default=0),
            channels,
            tuple((item.name, terms(tuple((endpoint, item.read.signal) for endpoint in self.drivers(item.read)))) for item in sorted(self.observations, key=lambda item: item.name)),
        )

    def validate(self, material: MaterialDesign, behavior: BehaviorGraph) -> FactorioFabricAnalysis:
        self.design.validate(material, behavior)
        if any(node.width != 32 or node.operation not in {BehaviorOperation.INPUT, BehaviorOperation.ADD} for node in behavior.nodes) or any(item.operation != "+" or any(isinstance(read, FactorioConstantRead) for read in (item.first, item.second)) for item in self.additions):
            from gateforge.providers.factorio.equivalence import validate_bitvectors
            validate_bitvectors(self, behavior)
            return self.analyze()
        expected: dict[str, Counter[str]] = {}
        names: set[str] = set()
        for node in behavior.nodes:
            if node.width != 32:
                raise RealizationError("Factorio add fabric currently requires 32-bit values")
            if node.operation == BehaviorOperation.INPUT:
                names.add(node.name)
                expected[node.identifier] = Counter({node.name: 1})
            elif node.operation == BehaviorOperation.ADD:
                expected[node.identifier] = expected[node.operands[0]] + expected[node.operands[1]]
            else:
                raise RealizationError("Factorio add fabric only supports input/add behavior")
        if {item.name for item in self.inputs} != names:
            raise RealizationError("Fabric external input identities differ from source")
        analysis = self.analyze()
        observations = dict(analysis.observations)
        for observation in behavior.observations:
            actual: Counter[str] = Counter()
            for term in observations[observation.name]:
                actual[term.input] += term.coefficient
            actual_reduced = {name: coefficient % MODULUS for name, coefficient in actual.items() if coefficient % MODULUS}
            expected_reduced = {name: coefficient % MODULUS for name, coefficient in expected[observation.value].items() if coefficient % MODULUS}
            if actual_reduced != expected_reduced:
                raise RealizationError(f"Fabric observation {observation.name!r} differs from source behavior")
        return analysis


@dataclass(frozen=True, slots=True)
class FactorioInterfaceBinding:
    port: str
    direction: PortDirection
    signal: str
    circuit: str
    color: FactorioWireColor


def _interface_bindings(capture: BehaviorCapture) -> tuple[FactorioInterfaceBinding, ...]:
    bindings = []
    occupied = set()
    for port in capture.ports:
        attributes = {name: value for name, value in port.attributes if name.startswith("factorio_")}
        if not attributes:
            continue
        if set(attributes) != {"factorio_signal", "factorio_circuit", "factorio_color"}:
            raise RealizationError(f"Port {port.name!r} requires exactly factorio_signal, factorio_circuit and factorio_color attributes")
        signal = attributes["factorio_signal"]
        _signal(signal)
        circuit = attributes["factorio_circuit"]
        if not circuit.strip():
            raise RealizationError(f"Port {port.name!r} has an empty circuit name")
        try:
            color = FactorioWireColor(attributes["factorio_color"])
        except ValueError as error:
            raise RealizationError(f"Port {port.name!r} requires red or green circuit color") from error
        key = (port.direction, circuit, color, signal)
        if key in occupied:
            raise RealizationError(f"Duplicate signal {signal} on {port.direction.value} circuit {circuit!r}/{color.value}")
        occupied.add(key)
        bindings.append(FactorioInterfaceBinding(port.name, port.direction, signal, circuit, color))
    return tuple(bindings)


def _bind_interfaces(fabric: FactorioCircuitFabric, bindings: tuple[FactorioInterfaceBinding, ...]) -> FactorioCircuitFabric:
    if not bindings:
        return fabric
    by_port = {(item.direction, item.port): item for item in bindings}
    entities = {item.identifier: item for item in fabric.design.entities}
    groups: dict[str, list[FactorioInterfaceBinding]] = {}

    def bus(binding):
        return "interface:" + json.dumps([binding.direction.value, binding.circuit, binding.color.value], separators=(",", ":"))

    for binding in bindings:
        groups.setdefault(bus(binding), []).append(binding)
    endpoints = {}
    for identifier, members in groups.items():
        direction = members[0].direction
        endpoint = RealizedEndpoint(identifier, "out" if direction == PortDirection.INPUT else "circuit")
        endpoints[identifier] = endpoint
        configuration = ProviderConfiguration.from_canonical_data({
            "interface_direction": direction.value, "interface_circuit": members[0].circuit,
            "interface_color": members[0].color.value,
            "interface_bindings": {item.port: item.signal for item in members},
        })
        entities[identifier] = RealizedEntity(identifier, "input-interface" if direction == PortDirection.INPUT else "junction", (endpoint.port,), configuration)

    sources = {endpoint for endpoint, _signal_name in fabric.emitters()}
    parents = {endpoint: endpoint for endpoint in sources}

    def root(endpoint):
        while parents[endpoint] != endpoint:
            endpoint = parents[endpoint]
        return endpoint

    reads = [read for addition in fabric.additions for read in (addition.first, addition.second)] + [item.read for item in fabric.observations]
    for read in reads:
        drivers = fabric.drivers(read)
        for driver in drivers[1:]:
            parents[root(driver)] = root(drivers[0])
    signals = {}
    mapped = {endpoint: endpoint for endpoint in sources}
    replaced_entities = {}
    input_buses = {}
    inputs = []
    for emission in fabric.inputs:
        binding = by_port.get((PortDirection.INPUT, emission.name))
        signal = emission.signal if binding is None else binding.signal
        signals[root(emission.endpoint)] = signal
        if binding is not None:
            mapped[emission.endpoint] = endpoints[bus(binding)]
            replaced_entities[emission.endpoint.entity] = mapped[emission.endpoint].entity
            input_buses[emission.endpoint] = binding
            del entities[emission.endpoint.entity]
        inputs.append(replace(emission, endpoint=mapped[emission.endpoint], signal=signal))

    direct = set()
    output_connections = {}
    for observation in fabric.observations:
        binding = by_port.get((PortDirection.OUTPUT, observation.name.removeprefix("port:")))
        drivers = fabric.drivers(observation.read)
        if binding is None or len(drivers) != 1 or drivers[0].entity not in {item.entity for item in fabric.additions}:
            continue
        driver = drivers[0]
        if sum(root(item) == root(driver) for item in sources) != 1:
            continue
        if signals.get(root(driver), binding.signal) != binding.signal or output_connections.get((driver, binding.color), bus(binding)) != bus(binding):
            continue
        signals[root(driver)] = binding.signal
        output_connections[driver, binding.color] = bus(binding)
        direct.add(observation.name)
    reserved = {item.signal for item in bindings} | set(signals.values())
    available = iter(f"signal-{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if f"signal-{letter}" not in reserved)
    for endpoint in sorted(sources):
        if root(endpoint) not in signals:
            signal = next(available, None)
            if signal is None:
                raise RealizationError("Explicit interface routing exhausted available virtual signal keys")
            signals[root(endpoint)] = signal

    networks: dict[tuple[str, FactorioWireColor], set[RealizedEndpoint]] = {}
    additions = []

    def opposite(color):
        return FactorioWireColor.GREEN if color == FactorioWireColor.RED else FactorioWireColor.RED

    def source_read(original, endpoint, preferred=None):
        drivers = fabric.drivers(original)
        if not drivers:
            return replace(original, endpoint=endpoint)
        binding = input_buses.get(drivers[0])
        color = binding.color if binding is not None else preferred or original.colors[0]
        name = bus(binding) if binding is not None else f"value:{root(drivers[0]).entity}"
        networks.setdefault((name, color), set()).update((endpoint, *(mapped[driver] for driver in drivers)))
        return FactorioSignalRead(endpoint, signals[root(drivers[0])], (color,))

    for addition in fabric.additions:
        endpoint = RealizedEndpoint(addition.entity, "in")
        first_drivers, second_drivers = fabric.drivers(addition.first), fabric.drivers(addition.second)
        first_bus = input_buses.get(first_drivers[0]) if first_drivers else None
        second_bus = input_buses.get(second_drivers[0]) if second_drivers else None
        first_color = opposite(second_bus.color) if first_bus is None and second_bus is not None else addition.first.colors[0] if isinstance(addition.first, FactorioSignalRead) else FactorioWireColor.RED
        first = source_read(addition.first, endpoint, first_color)
        if first_bus is not None and second_bus is not None and first_bus.color == second_bus.color and bus(first_bus) != bus(second_bus):
            adapter = f"interface:bridge:{addition.entity}"
            adapter_in = RealizedEndpoint(adapter, "in")
            second_source = source_read(addition.second, adapter_in)
            additions.append(FactorioAdd(adapter, second_source, FactorioSignalRead(adapter_in, "signal-A", (opposite(second_source.colors[0]),)), second_source.signal))
            entities[adapter] = RealizedEntity(adapter, "arithmetic-combinator", ("in", "out"))
            networks[adapter, opposite(first.colors[0])] = {RealizedEndpoint(adapter, "out"), endpoint}
            second = FactorioSignalRead(endpoint, second_source.signal, (opposite(first.colors[0]),))
        else:
            second = source_read(addition.second, endpoint, opposite(first.colors[0]) if isinstance(first, FactorioSignalRead) else opposite(first_color))
        additions.append(replace(addition, first=first, second=second, output_signal=signals[root(addition.output)]))

    observations = []
    for observation in fabric.observations:
        binding = by_port.get((PortDirection.OUTPUT, observation.name.removeprefix("port:")))
        if binding is None:
            observations.append(replace(observation, read=source_read(observation.read, observation.read.endpoint)))
            continue
        endpoint = endpoints[bus(binding)]
        replaced_entities[observation.read.endpoint.entity] = endpoint.entity
        del entities[observation.read.endpoint.entity]
        if observation.name in direct:
            driver = mapped[fabric.drivers(observation.read)[0]]
        else:
            adapter = f"interface:rename:{binding.port}"
            adapter_in = RealizedEndpoint(adapter, "in")
            first = source_read(observation.read, adapter_in)
            additions.append(FactorioAdd(adapter, first, FactorioSignalRead(adapter_in, "signal-A", (opposite(first.colors[0]),)), binding.signal))
            entities[adapter] = RealizedEntity(adapter, "arithmetic-combinator", ("in", "out"))
            driver = RealizedEndpoint(adapter, "out")
        networks.setdefault((bus(binding), binding.color), set()).update((driver, endpoint))
        observations.append(replace(observation, read=FactorioSignalRead(endpoint, binding.signal, (binding.color,))))
    for identifier, endpoint in endpoints.items():
        networks.setdefault((identifier, groups[identifier][0].color), set()).add(endpoint)
    merged = []
    for (name, color), members in sorted(networks.items()):
        names = {name}
        following = []
        for other_names, other_color, other_members in merged:
            if color == other_color and members & other_members:
                members |= other_members
                names |= other_names
            else:
                following.append((other_names, other_color, other_members))
        following.append((names, color, members))
        merged = following
    domains = []
    for names, color, members in merged:
        buses = sorted(name for name in names if name.startswith("interface:"))
        if len(buses) > 1:
            raise RealizationError("Interface routing would merge distinct declared buses")
        domains.append(FactorioCircuitDomain(buses[0] if buses else min(names) + "/" + color.value, color, tuple(sorted(members))))
    observation_endpoints = {item.name: item.read.endpoint for item in observations}
    provenance = tuple(replace(item, entities=tuple(sorted({replaced_entities.get(identifier, identifier) for identifier in item.entities}))) for item in fabric.design.provenance)
    design = replace(fabric.design, entities=tuple(entities.values()), observations=tuple(replace(item, endpoints=(observation_endpoints[item.name],)) for item in fabric.design.observations), provenance=(*provenance, RealizationProvenance("factorio/direct-interfaces/v2", tuple(entities))))
    wires = tuple(FactorioDomainWire(domain.identifier, domain.endpoints[0], endpoint) for domain in domains for endpoint in domain.endpoints[1:])
    return FactorioCircuitFabric(design, tuple(domains), wires, tuple(inputs), tuple(additions), tuple(observations))


def propose_combinational_fabrics(
    material: MaterialDesign,
    capture: BehaviorCapture,
    *,
    max_rewrites: int = 8,
) -> tuple[FactorioCircuitFabric, ...]:
    if isinstance(max_rewrites, bool) or not isinstance(max_rewrites, int) or max_rewrites < 0:
        raise RealizationError("Rewrite limit must be a nonnegative integer")
    bind_material_behavior(material, capture)
    interface = _interface_bindings(capture)
    graph = capture.graph
    nodes = {node.identifier: node for node in graph.nodes}
    operations = {BehaviorOperation.ADD: "+", BehaviorOperation.BIT_AND: "AND", BehaviorOperation.BIT_OR: "OR", BehaviorOperation.BIT_XOR: "XOR", BehaviorOperation.BIT_NOT: "XOR"}
    if any(node.width != 32 or node.operation not in {BehaviorOperation.INPUT, BehaviorOperation.CONSTANT, *operations} for node in graph.nodes):
        from gateforge.providers.factorio.conditional import propose_conditional_fabric
        return (propose_conditional_fabric(material, capture),)
    consumers = Counter(operand for node in graph.nodes for operand in node.operands)
    observed = {item.value for item in graph.observations}
    native_lamps = {
        f"cell:{item.source.name}:{item.port}" for item in capture.cells
        if item.source.expected_type == "GF_Lamp" and item.port == "in"
    }
    removable = sorted(
        node.identifier for node in graph.nodes
        if node.operation == BehaviorOperation.ADD and len(set(node.operands)) == 2
        and all(
            nodes[operand].operation == BehaviorOperation.ADD
            and consumers[operand] == 1 and operand not in observed
            for operand in node.operands
        )
    )[:max_rewrites]

    def build(removed: str | None) -> FactorioCircuitFabric:
        entities: list[RealizedEntity] = []
        inputs: list[FactorioInputEmission] = []
        additions: list[FactorioAdd] = []
        observations: list[FactorioObservationRead] = []
        bindings: list[RealizedObservation] = []
        members: dict[tuple[str, FactorioWireColor], set[RealizedEndpoint]] = {}

        def output(value: str) -> RealizedEndpoint:
            return RealizedEndpoint(f"value:{value}", "out")

        def connect(value: str, endpoint: RealizedEndpoint, color: FactorioWireColor, *, observation: bool = False) -> FactorioSignalRead | FactorioConstantRead:
            if nodes[value].operation == BehaviorOperation.CONSTANT and not observation:
                literal = nodes[value].value
                return FactorioConstantRead(endpoint, literal if literal < (1 << 31) else literal - MODULUS)
            drivers = nodes[value].operands if value == removed else (value,)
            members.setdefault((value, color), set()).update((endpoint, *(output(driver) for driver in drivers)))
            return FactorioSignalRead(endpoint, "signal-A", (color,))

        for node in graph.nodes:
            identifier = f"value:{node.identifier}"
            if node.operation == BehaviorOperation.INPUT:
                entities.append(RealizedEntity(identifier, "input-interface", ("out",)))
                inputs.append(FactorioInputEmission(node.name, output(node.identifier), "signal-A"))
            elif node.operation == BehaviorOperation.CONSTANT:
                if node.identifier in observed:
                    entities.append(RealizedEntity(identifier, "arithmetic-combinator", ("in", "out")))
                    endpoint = RealizedEndpoint(identifier, "in")
                    additions.append(FactorioArithmetic(identifier, connect(node.identifier, endpoint, FactorioWireColor.RED), FactorioConstantRead(endpoint, 0), "signal-A"))
            elif node.identifier != removed:
                entities.append(RealizedEntity(identifier, "arithmetic-combinator", ("in", "out")))
                endpoint = RealizedEndpoint(identifier, "in")
                additions.append(FactorioAdd(
                    identifier,
                    connect(node.operands[0], endpoint, FactorioWireColor.RED),
                    FactorioConstantRead(endpoint, -1) if node.operation == BehaviorOperation.BIT_NOT else connect(node.operands[1], endpoint, FactorioWireColor.GREEN),
                    "signal-A", operations[node.operation],
                ))
        for observation in graph.observations:
            identifier = f"observe:{observation.name}"
            endpoint = RealizedEndpoint(identifier, "circuit")
            entities.append(RealizedEntity(
                identifier, "junction", ("circuit",),
                ProviderConfiguration.from_canonical_data({"native_lamp": True})
                if observation.name in native_lamps else ProviderConfiguration(),
            ))
            observations.append(FactorioObservationRead(
                observation.name, connect(observation.value, endpoint, FactorioWireColor.RED, observation=True)
            ))
            bindings.append(RealizedObservation(observation.name, observation.value, (endpoint,)))
        domains = tuple(
            FactorioCircuitDomain(f"{value}/{color.value}", color, tuple(sorted(endpoints)))
            for (value, color), endpoints in sorted(members.items())
        )
        wires = tuple(
            FactorioDomainWire(domain.identifier, domain.endpoints[0], endpoint)
            for domain in domains for endpoint in domain.endpoints[1:]
        )
        provenance = (RealizationProvenance(
            "factorio/whole-source-add32-cover/v1",
            tuple(entity.identifier for entity in entities),
            tuple(item.identifier for item in material.objects), tuple(nodes),
        ),)
        if removed is not None:
            provenance += (RealizationProvenance(
                "factorio/wired-sum/v1",
                tuple(output(operand).entity for operand in nodes[removed].operands),
                values=(removed, *nodes[removed].operands),
            ),)
        design = RealizationDesign(
            "factorio", material.get_digest(), graph.get_digest(),
            tuple(entities), tuple(bindings), provenance,
        )
        fabric = FactorioCircuitFabric(
            design, domains, wires, tuple(inputs), tuple(additions), tuple(observations)
        )
        fabric = _bind_interfaces(fabric, interface)
        fabric.validate(material, graph)
        return fabric

    return (build(None), *(build(removed) for removed in removable))


propose_add_fabrics = propose_combinational_fabrics