import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypeAlias

from pyosys import libyosys as ys


SignalId: TypeAlias = int | str


@dataclass(frozen=True, slots=True)
class Object:
    id: int
    type: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Endpoint:
    object_id: int
    port: str
    bit: int


@dataclass(frozen=True, slots=True)
class Connection:
    id: int
    signal_id: SignalId
    source: Endpoint
    target: Endpoint


@dataclass(frozen=True, slots=True)
class ObjectNetlist:
    objects: tuple[Object, ...]
    connections: tuple[Connection, ...]


def design_preprocess(path: str) -> ys.Design:
    design = ys.Design()

    ys.run_pass(f"read_verilog {path}", design)
    ys.run_pass("hierarchy -check -auto-top", design)
    ys.run_pass("proc", design)
    ys.run_pass("fsm -nomap", design)
    ys.run_pass("techmap", design)

    return design


def design_json(design: ys.Design) -> dict[str, Any]:
    with TemporaryDirectory(prefix="gateforge-") as directory:
        output_path = Path(directory) / "design.json"
        ys.run_pass(f"write_json {output_path}", design)

        print(output_path.read_text())
        return json.loads(output_path.read_text())


def build_object_netlist(design: ys.Design) -> ObjectNetlist:
    top = design.top_module()
    if top is None:
        raise ValueError("Design has no top module")

    top_name = top.name.str().removeprefix("\\")
    module = design_json(design)["modules"][top_name]

    objects: list[Object] = []
    drivers: dict[SignalId, list[Endpoint]] = defaultdict(list)
    sinks: dict[SignalId, list[Endpoint]] = defaultdict(list)

    def add_object(object_type: str, name: str | None = None) -> int:
        object_id = len(objects)
        objects.append(Object(object_id, object_type, name))
        return object_id

    for port_name, port in sorted(module["ports"].items()):
        direction = port["direction"]
        object_id = add_object(f"${direction}", port_name)

        if direction == "input":
            endpoint_table = drivers
            endpoint_port = "OUT"
        elif direction == "output":
            endpoint_table = sinks
            endpoint_port = "IN"
        else:
            raise NotImplementedError("Inout module ports are not supported yet")

        for bit_index, signal_id in enumerate(port["bits"]):
            endpoint_table[signal_id].append(
                Endpoint(object_id, endpoint_port, bit_index)
            )

    for cell_name, cell in sorted(module["cells"].items()):
        visible_name = None if cell["hide_name"] else cell_name
        object_id = add_object(cell["type"], visible_name)
        directions = cell.get("port_directions")

        if directions is None:
            raise ValueError(f"Cell {cell_name!r} has no known port directions")

        for port_name, signal_ids in sorted(cell["connections"].items()):
            direction = directions[port_name]

            if direction == "input":
                endpoint_table = sinks
            elif direction == "output":
                endpoint_table = drivers
            else:
                raise NotImplementedError(
                    f"Inout port {cell_name}.{port_name} is not supported yet"
                )

            for bit_index, signal_id in enumerate(signal_ids):
                endpoint_table[signal_id].append(
                    Endpoint(object_id, port_name, bit_index)
                )

    for signal_id in sorted(sinks, key=lambda value: (isinstance(value, str), value)):
        if isinstance(signal_id, str) and signal_id not in drivers:
            object_id = add_object("$constant", signal_id)
            drivers[signal_id].append(Endpoint(object_id, "OUT", 0))

    connections: list[Connection] = []
    for signal_id in sorted(sinks, key=lambda value: (isinstance(value, str), value)):
        signal_drivers = drivers.get(signal_id, [])
        if not signal_drivers:
            raise ValueError(f"Signal {signal_id!r} has sinks but no driver")
        if len(signal_drivers) > 1:
            raise ValueError(f"Signal {signal_id!r} has multiple drivers")

        source = signal_drivers[0]
        for target in sinks[signal_id]:
            connections.append(
                Connection(len(connections), signal_id, source, target)
            )

    return ObjectNetlist(tuple(objects), tuple(connections))


def print_object_netlist(netlist: ObjectNetlist) -> None:
    print("OBJECTS")
    for object_ in netlist.objects:
        name = f" {object_.name}" if object_.name is not None else ""
        print(f"  {object_.id}: {object_.type}{name}")

    print("CONNECTIONS")
    for connection in netlist.connections:
        source = connection.source
        target = connection.target
        print(
            f"  {connection.id}: "
            f"{source.object_id}.{source.port}[{source.bit}] -> "
            f"{target.object_id}.{target.port}[{target.bit}] "
            f"(signal {connection.signal_id})"
        )


path = os.path.abspath("scratch/basic.v")
design = design_preprocess(path)
print_object_netlist(build_object_netlist(design))
ys.run_pass("show", design)