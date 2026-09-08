# GateForge

GateForge is a staged compiler from synthesizable Verilog to material object and
network graphs for unconventional construction fabrics. The first target is
LittleBigPlanet 3 logic.

Its current mapper covers low-level combinational gates, fanout-safe associative
AND/OR/XOR cones, and native LBP Timer, Counter, Randomizer, and Selector
intrinsics.

## Compiler Pipeline

1. Yosys reads the source
2. GateForge checkpoints the complete design as deterministic RTLIL
3. GateForge starts lowering stages and requests proposals from mapping providers at each stage
3. Greedy, beam, or exhaustive search selects compatible proposal sets. Beam and exhaustive modes preserve accept/defer branches across lowering stages
4. GateForge validates each exact source cut, creates an opaque blackbox, and removes the accepted source cells before the next lowering stage
5. The default stages offer source, extracted-FSM, post-FSM, and optimized leaf mapping checkpoints. ABC is optional and runs before leaf mapping.
6. Yosys uniquifies module occurrences and flattens hierarchy while preserving claim attributes
7. GateForge expands each flattened claim occurrence into material objects and merges their external prefab nets through the final RTLIL connectivity.
8. A target provider projects ordering dependencies from the neutral material nets.
9. GateForge outputs material netlist for placement.

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

Register banks use the compact Counter-based implementation by default. Select
the larger two-phase fallback with `--register-style hardened`.

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

Export a placed LBP design as Craftworld Toolkit-compatible PLAN JSON:

```sh
uv run gateforge export lbp-toolkit \
	build/material.json \
	build/placement.json \
	--output build/object.json
```

Inspect the neutral placement and the exact provider-realized layout before
exporting:

```sh
uv run gateforge visualize \
	build/material.json \
	build/placement.json \
	--watch
```

The view selector switches between material placement and registered provider
views. Placed microchips are separate tabs; double-clicking a child microchip
opens its tab. Click an element to inspect its properties and connected nets,
drag with the middle or right mouse button to pan, and use the wheel to zoom.
The toolbar can fit the active scene and toggle wires, labels, bounds, collision
markers, and artifact watching. Hierarchy is already fixed in the loaded
placement; rerun `gateforge place` to change it. On Debian or Ubuntu systems,
install `python3-tk` if Python does not provide Tkinter.

## Compiler Workbench

Install the optional Qt 6 frontend and open a Verilog source:

```sh
uv sync --extra workbench
uv run --extra workbench gateforge workbench scratch/test.v
```

The read-only workbench can advance one named mapping stage at a time or continue
through the search, inspect retained, pruned, and deduplicated branches, compare
proposals and claims, and render any retained Yosys checkpoint as an SVG. After
materialization it can place the design and display the same provider-neutral
placement and physical scenes used by the Tk artifact viewer. Its placement
toolbar selects source/generated hierarchy, spacing, and routing gaps before the
single placement pass. Scene controls toggle wires, labels, object bounds, and
collision markers.

Pyosys and the live compiler session run in a spawned worker process, not in the
Qt process. Stop terminates that worker and discards its in-memory session;
opening or reloading a source creates a fresh session. Material, state, search
report, placement, visualization, and LBP Toolkit files are written only through
an explicit Save or Export command. Graphviz `dot` is required only for the
Yosys schematic tab; compilation and other views remain usable without it.

Preserve HDL module occurrences as nested LBP microchips:

```sh
uv run gateforge compile scratch/basic_nested.v \
	--synthesis-hierarchy preserve \
	--physical-hierarchy preserve-all \
	--emit-material build/nested.material.json \
	--emit-placement build/nested.placement.json

uv run gateforge export lbp-toolkit \
	build/nested.material.json \
	build/nested.placement.json \
	--output build/nested.object.json
```

Synthesis hierarchy controls optimization boundaries before mapping:

- `preserve` keeps the source module hierarchy during per-module optimization.
- `flat` flattens before mapping so optimization may cross every boundary.
- `min-cells --synthesis-threshold N` flattens module instances whose recursive
  primitive-cell count is below `N`.

Physical hierarchy selects retained containers before placement:

- `flat` places one root board.
- `preserve-all` places every preserved occurrence as a nested microchip.
- `min-objects --hierarchy-threshold N` retains occurrences with at least `N`
  recursive material objects.
- `min-cost --hierarchy-threshold C` uses provider-estimated physical cost.

Generated implementations use an independent placement policy:

- `--generated-hierarchy inline` leaves implementation gadgets on their HDL
  owner board.
- `--generated-hierarchy auto` is the default; explicit containers and
  substantial generated prefabs become nested microchips.
- `--generated-hierarchy all` contains every multi-object generated prefab.

For example, compatible DFFs sharing clock/reset semantics are mapped as one
register bank and exported as one generated microchip rather than exposing each
storage gate on the parent board.

Synthesis flattening is irreversible. A physical placement policy can collapse
additional preserved boundaries, but cannot recreate boundaries removed before
mapping.

Before coordinates are assigned, the LBP provider elaborates gates, batteries,
non-inverting module I/O buffers, labeled notes, boundary pins, and routed
container connections. Placement then fixes every component transform, child
transform, and board extent. The exporter validates the material digest and
serializes that immutable physical inventory without moving or resizing it. Use `--title`,
`--description`, and `--creator` to override inventory metadata.

DO NOT KEEP THE GAME RUNNING WHEN WRITING TO BIGFART!!! This will lead to desync
between game memory caches and can lead to undefined behavior and corruption!

## Placement Model

Material nets remain provider-neutral hyperedges. A target provider projects
the dependency edges required by a placer; provider-specific electrical rules
remain in provider validation. This allows LBP to enforce one producer per wire
without imposing that restriction on targets whose networks combine multiple
sources.

The topological placer condenses strongly connected components and places the
resulting DAG in deterministic longest-path layers. Within each nontrivial
component, deterministic cycle removal selects feedback edges and layers the
remaining DAG; feedback wires may therefore run right-to-left instead of
collapsing the whole component into one column. It places inputs and constants
on the left and outputs on the right. Coordinates represent center points only;
provider-supplied object geometry reserves each object's physical height,
including variable-height gates.
The default layout packs subjects on a 52.5-unit minimum pitch and leaves one
52.5-unit routing gap after each 250-unit content group. Override that policy
with `--routing-group-height`, `--row-pitch`, and `--routing-gap-rows`.
Collision optimization, explicit wire routing, and
crossing reduction remain deliberately deferred.

`MaterialDesign` and `PlacedDesign` are separate durable artifacts. Deployment
backends must load both and verify the placement's material digest before
serializing a target-specific design.

The compiler will fail if it exhausts all lowering passes but unclaimed RTLIL objects remain

## Stateful Logic

GateForge maps coarse `$dff`, `$dffe`, `$adff`, `$adffe`, `$sdff`, `$sdffe`, and
`$sdffce` cells before `techmap` and regroups their scalar equivalents as a leaf
fallback. Registers in the same source module share one bank when their clock,
reset signal, polarity, timing, and reset/enable priority match. Width and reset
value do not split a bank; mixed reset bits are retained as one reset vector.
Different enable sources become shared subgroups inside the same bank.

The default non-Memorizer realization is compact: one target-1 self-resetting
Counter generates a one-frame edge pulse for the entire bank, and each bit adds
one NOT, two AND gates, and one state Selector. Use
`--register-style hardened` to select the larger captured two-phase
implementation instead. Synchronous and asynchronous reset circuitry is
generated according to Yosys semantics. The compact style has been reported to
work in game; hardened and synthesized reset/enable behavior still require the
full in-game acceptance matrix.

Generated register microchips use the same recursive topological placement as
source-module containers. Each child board is placed bottom-up, then its fixed
façade is treated as one compound subject in its parent. Export does not compact
registers, reflow parents, or alter either transform.

Material feedback is preserved. The topological placer condenses strongly
connected components for ordering, selects deterministic feedback edges within
each component, and layers the remaining forward edges without deleting or
rewriting any loop edge.

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