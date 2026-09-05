import argparse
import time
import tracemalloc

from gateforge.source import DesignSnapshot


def make_chain(cell_count: int) -> dict[str, object]:
    cells: dict[str, object] = {}
    previous_bit = 2
    for index in range(cell_count):
        output_bit = index + 4
        cells[f"gate_{index}"] = {
            "type": "$_AND_",
            "parameters": {},
            "attributes": {},
            "port_directions": {
                "A": "input",
                "B": "input",
                "Y": "output",
            },
            "connections": {
                "A": [previous_bit],
                "B": [3],
                "Y": [output_bit],
            },
        }
        previous_bit = output_bit

    return {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "parameter_default_values": {},
                "ports": {
                    "a": {"direction": "input", "bits": [2]},
                    "b": {"direction": "input", "bits": [3]},
                    "y": {"direction": "output", "bits": [previous_bit]},
                },
                "cells": cells,
            }
        }
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark snapshot indexing.")
    parser.add_argument("--cells", type=int, default=100_000)
    args = parser.parse_args()
    if args.cells <= 0:
        parser.error("--cells must be positive")

    design = make_chain(args.cells)
    tracemalloc.start()
    started = time.perf_counter()
    snapshot = DesignSnapshot.from_json(design, revision=1)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    module = snapshot.module("top")
    if len(module.cells) != args.cells:
        raise RuntimeError("Snapshot did not retain every generated cell")

    print(f"cells={len(module.cells)}")
    print(f"indexed_bits={len(module.endpoints)}")
    print(f"seconds={elapsed:.3f}")
    print(f"peak_mib={peak / (1024 * 1024):.1f}")


if __name__ == "__main__":
    main()