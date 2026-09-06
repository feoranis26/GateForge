# GateForge

GateForge is a staged compiler from synthesizable Verilog to material object and
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
7. GateForge expands each flattened claim occurrence into material objects and merges their external prefab nets through the final RTLIL connectivity.
8. A target provider projects ordering dependencies from the neutral material nets.
9. An optional placer assigns coordinates without coupling placement to deployment.

## Artifacts

Compile and place the included combinational example with:

```sh
uv run gateforge compile scratch/basic.v \
	--emit-json build/design.json \
	--emit-state build/state.json \
	--emit-material build/material.json \
	--emit-placement build/placement.json
```

Pass `--no-abc` to inspect or map the pre-ABC leaf network instead.
Pass `--show` to open Yosys's graph visualization.

- `--emit-json` writes the final flattened Yosys design.
- `--emit-state` writes semantic prefabs and durable claims.
- `--emit-material` writes the reusable material object and network graph.
- `--emit-placement` writes a topological placement linked to that material
  artifact by its SHA-256 digest.

Placement can be rerun without compiling the Verilog again:

```sh
uv run gateforge place build/material.json \
	--output build/placement.json \
	--column-pitch 262.5 \
	--row-pitch 262.5
```

Without `--output`, `place` writes placement JSON to stdout and status messages
to stderr.

## Placement Model

Material nets remain provider-neutral hyperedges. A target provider projects
the dependency edges required by a placer; provider-specific electrical rules
remain in provider validation. This allows LBP to enforce one producer per wire
without imposing that restriction on targets whose networks combine multiple
sources.

The initial topological placer requires an acyclic dependency graph. It places
inputs and constants on the left, outputs on the right, and material objects in
deterministic longest-path layers. Coordinates represent center points only;
object dimensions, collision avoidance, wire routing, and crossing reduction
are deliberately deferred.

`MaterialDesign` and `PlacedDesign` are separate durable artifacts. Deployment
backends must load both and verify the placement's material digest before
realizing a target-specific design.

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