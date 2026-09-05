# GateForge

GateForge is a staged compiler from synthesizable Verilog to semantic object and
network graphs for unconventional construction fabrics. The first target is
LittleBigPlanet 3 logic.

Its current mapper covers low-level combinational AND, AND-NOT, NAND, OR, NOR, NOT, and buffer cells.

## Compiler Pipeline

1. Yosys reads the source
2. GateForge runs reduction passes on the design, and requests proposals from mapping providers on each pass
3. GateForge arbitration selects best proposals at each stage, current algorithm is simple first-come first-serve non-overlapping resolution
4. GateForge validates each exact source cut, creates an opaque blackbox, and removes the accepted source cells before the next lowering stage.
5. The default stages offer source, extracted-FSM, post-FSM, and optimized leaf mapping checkpoints. ABC is optional and runs before leaf mapping.
6. Yosys uniquifies module occurrences and flattens hierarchy while preserving claim attributes.
7. GateForge expands each flattened claim occurrence into physical objects and merges their external prefab nets through the final RTLIL connectivity.

## Artifacts

Compile the included combinational example with:

```sh
uv run gateforge scratch/basic.v \
	--emit-json build/design.json \
	--emit-state build/state.json \
	--emit-physical build/physical.json
```

Pass `--no-abc` to inspect or map the pre-ABC leaf network instead.

- `--emit-json` writes the final flattened Yosys design.
- `--emit-state` writes semantic prefabs and durable claims.
- `--emit-physical` writes the final physical object and network graph.

The compiler will fail if it exhausts all lowering passes but unclaimed RTLIL objects remain

## HDL Anchors

Use `gateforge_id` for identities that should survive HDL instance renames. Anchors are sibling-local and compose into a hierarchy path.

```verilog
(* gateforge_id = "logic-output" *) output y;

(* gateforge_id = "left-slot" *)
child left(.a(a), .b(b), .y(y));
```

For logic expressions lowered by Yosys, attach the anchor to the driven net or output. GateForge copies that anchor to the lowered driver after `techmap`. Direct cell and module-instance attributes are read from the cell itself. Duplicate sibling cell anchors are rejected. Unanchored generated cells fall back to source-location provenance.

## Development

GateForge requires Python 3.12 and uses `uv`.

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv run python benchmarks/snapshot_100k.py
```