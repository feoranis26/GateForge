# GateForge

GateForge is a staged compiler from synthesizable Verilog to material object and
network graphs for unconventional construction fabrics. The first target is
LittleBigPlanet 3 logic.

Its current mapper covers low-level combinational gates, fanout-safe associative
AND/OR/XOR cones, and native LBP Timer, Counter, Randomizer, and Selector
intrinsics.

## Compiler Pipeline

1. Yosys reads the source
2. GateForge checkpoints the complete design as deterministic RTLIL and requests proposals from mapping providers at each stage.
3. Greedy, beam, or exhaustive search selects compatible proposal sets. Beam and exhaustive modes preserve accept/defer branches across lowering stages.
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

Use checkpoint-based mapping search and emit its decisions and terminal scores:

```sh
uv run gateforge compile scratch/basic.v \
	--mapping-search beam \
	--mapping-beam-width 16 \
	--mapping-stage-limit 16 \
	--emit-search-report build/search.json \
	--emit-material build/material.json
```

`greedy` remains the compatibility default. `beam` retains a bounded candidate
frontier, while `exhaustive` is intended for small designs and tests. Proposal
costs order the search; surviving complete candidates are materialized and the
lowest exact provider object cost wins.

- `--emit-json` writes the final flattened Yosys design.
- `--emit-state` writes semantic prefabs and durable claims.
- `--emit-material` writes the reusable material object and network graph.
- `--emit-placement` writes a topological placement linked to that material
  artifact by its SHA-256 digest.

Placement can be rerun without compiling the Verilog again:

```sh
uv run gateforge place build/material.json \
	--output build/placement.json \
	--column-pitch 210 \
	--row-pitch 105
```

Without `--output`, `place` writes placement JSON to stdout and status messages
to stderr.

Export a placed LBP design as Craftworld Toolkit-compatible PLAN JSON:

```sh
uv run gateforge export lbp-toolkit \
	build/material.json \
	build/placement.json \
	--output build/object.json
```

Preserve HDL module occurrences as nested LBP microchips:

```sh
uv run gateforge compile scratch/basic_nested.v \
	--synthesis-hierarchy preserve \
	--emit-material build/nested.material.json \
	--emit-placement build/nested.placement.json

uv run gateforge export lbp-toolkit \
	build/nested.material.json \
	build/nested.placement.json \
	--physical-hierarchy preserve-all \
	--output build/nested.object.json
```

Synthesis hierarchy controls optimization boundaries before mapping:

- `preserve` keeps the source module hierarchy during per-module optimization.
- `flat` flattens before mapping so optimization may cross every boundary.
- `min-cells --synthesis-threshold N` flattens module instances whose recursive
  primitive-cell count is below `N`.

Physical hierarchy controls only packaging during LBP export:

- `flat` emits one microchip.
- `preserve-all` emits every preserved occurrence as a nested microchip.
- `min-objects --hierarchy-threshold N` retains occurrences with at least `N`
  recursive material objects.
- `min-cost --hierarchy-threshold C` uses provider-estimated physical cost.

Synthesis flattening is irreversible. A physical export policy can collapse
additional preserved boundaries, but cannot recreate boundaries removed before
mapping.

The exporter validates that the material and placement digests match, realizes
LBP gates and wires, adds non-inverting NOT gates as temporary module I/O
buffers, places labeled notes outside those buffers, and uses batteries for
binary constants. Use `--title`,
`--description`, and `--creator` to override inventory metadata.

Treat Big Profile mutation as an offline operation: fully exit LBP, back up the
profile, import and save the generated PLAN with Craftworld Toolkit, and then
restart the game. Repacking a profile while LBP is running can leave its cached
resource state inconsistent until restart.

## Placement Model

Material nets remain provider-neutral hyperedges. A target provider projects
the dependency edges required by a placer; provider-specific electrical rules
remain in provider validation. This allows LBP to enforce one producer per wire
without imposing that restriction on targets whose networks combine multiple
sources.

The initial topological placer requires an acyclic dependency graph. It places
inputs and constants on the left, outputs on the right, and material objects in
deterministic longest-path layers. Coordinates represent center points only;
provider-supplied object geometry reserves enough vertical rows for wide gates.
The default 210-by-105 grid leaves one globally aligned routing row after every
five content rows. Override that policy with `--routing-group-size` and
`--routing-gap-rows`. Collision optimization, explicit wire routing, and
crossing reduction remain deliberately deferred.

`MaterialDesign` and `PlacedDesign` are separate durable artifacts. Deployment
backends must load both and verify the placement's material digest before
realizing a target-specific design.

The compiler will fail if it exhausts all lowering passes but unclaimed RTLIL objects remain

## GateForge Intrinsics

GateForge automatically loads packaged blackbox declarations for target-native
gadgets. Instantiate the reserved modules directly; no include directive is
required.

```verilog
GF_Timer #(.TIME_DS(50), .MODE("START_COUNT_UP")) timer (
	.in(trigger), .reset(reset), .out(done)
);

GF_Counter #(.TARGET(20)) counter (
	.in(increment), .reset(reset), .out(full)
);

GF_Randomizer #(
	.OUTPUTS(3),
	.MODE("TOGGLE"),
	.INPUT_ACTION("OVERRIDE_PATTERN"),
	.NEW_PICK(1),
	.ON_MIN_DS(10), .ON_MAX_DS(20),
	.OFF_MIN_DS(0), .OFF_MAX_DS(0)
) randomizer (.in(trigger), .out(random_bits));

GF_Selector #(.WIDTH(3)) selector (
	.cycle(cycle), .in(selector_inputs), .out(selector_outputs)
);
```

Timer modes are `ON_OFF`, `SPEED_SCALE`, `FORWARD_BACKWARD`,
`START_COUNT_UP`, `START_COUNT_DOWN`, and `POSITIONAL`. `ONESHOT` and
`ONE_SHOT` are accepted aliases for `START_COUNT_UP`. Randomizer modes are
`ADD`, `ADD_AND_RESET`, `TOGGLE`, and `ONE_AT_A_TIME`; input actions are
`TRIGGER` and `OVERRIDE_PATTERN`. All `*_DS` values are integer deciseconds.

Intrinsics are required source-stage mappings: unsupported names, modes,
parameters, or port widths fail instead of being silently lowered. Captured
runtime fields such as Timer progress and Counter current count are initialized
to clean state and are not source parameters.

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