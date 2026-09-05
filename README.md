# GateForge

GateForge is a staged compiler from synthesizable Verilog to semantic object and
network graphs for unconventional construction fabrics. The first target is
LittleBigPlanet 3 logic.

Its current mapper covers low-level combinational AND, AND-NOT, NAND, OR, NOR, NOT, and buffer cells.

For LBP combinational objects, `width` means the number of scalar input ports.
A width-three gate exposes `IN_0`, `IN_1`, `IN_2`, and scalar `OUT`. Object port schemas are generated lazily when an accepted type is validated; mapping proposals carry only the parameter-complete object type identifier.

## Target Type Identifiers

Provider object types use canonical hierarchical paths. For example, an LBP two-input AND gate is identified as:

```text
GATE(invert=false):VARIABLE_WIDTH(width=2):AND
```

The provider and type version remain separate fields on `ObjectTypeIdentifier`.
Each hierarchy class owns one `TYPE_KEY` and the parameters for its segment.
Generic parsing and registration live in `gateforge.type_codec`; provider type modules register classes with decorators that read that class-owned key. The LBP types package explicitly imports supported family modules to activate those registrations, avoiding both a central decode switch and import-order-dependent
`__subclasses__()` discovery.

Type names are opaque to prefab and state serialization. The current early format intentionally has no compatibility decoder for older flat LBP names.
Schemas are decoded, generated, and cached only when accepted types are validated.

## Compiler Pipeline

1. Yosys reads the source and retains module hierarchy.
2. Configurable mapping stages run Yosys lowering passes, export one immutable
	`DesignSnapshot`, and ask every mapping provider for proposals.
3. Framework arbitration selects non-overlapping proposals at each stage.
4. The framework validates each exact source cut, creates an opaque blackbox,
	and removes the accepted source cells before the next lowering stage.
5. The default stages offer source, extracted-FSM, post-FSM, and optimized leaf
	mapping checkpoints. ABC is optional and runs before leaf mapping.
6. Yosys uniquifies module occurrences and flattens hierarchy while preserving
	claim attributes.
7. GateForge expands each flattened claim occurrence into physical objects and
	 merges their external prefab nets through the final RTLIL connectivity.

Providers never mutate RTLIL. Only framework code owns live Yosys objects and applies accepted proposals.

## Data Lifetimes

- `DesignSnapshot`, `SnapshotBitRef`, and `MappingProposal` belong to one design
	revision. Snapshot bit IDs must never be persisted across Yosys passes.
- `SemanticPrefab` and `ClaimDefinition` are durable semantic records. Claims
	bind blackbox formal ports to prefab ports and do not refer to removed cells.
- `PhysicalObject` and `PhysicalNet` represent final flattened occurrences.

Prefab IDs hash the complete canonical object, interface, and internal network semantics. Claim IDs additionally hash source provenance and mapper rule identity. Physical object IDs combine an occurrence, prefab ID, and prefab-local object role. A semantic prefab change therefore invalidates object-level overrides by design.

## HDL Anchors

Use `gateforge_id` for identities that should survive HDL instance renames. Anchors are sibling-local and compose into a hierarchy path.

```verilog
(* gateforge_id = "logic-output" *) output y;

(* gateforge_id = "left-slot" *)
child left(.a(a), .b(b), .y(y));
```

For logic expressions lowered by Yosys, attach the anchor to the driven net or output. GateForge copies that anchor to the lowered driver after `techmap`. Direct cell and module-instance attributes are read from the cell itself. Duplicate sibling cell anchors are rejected. Unanchored generated cells fall back to source-location provenance.

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
- `--emit-state` writes schema-versioned semantic prefabs and durable claims.
	Loading verifies every prefab content hash.
- `--emit-physical` writes the final physical object and network graph.

The compiler rejects a physical build if unsupported RTLIL cells remain.

## Development

GateForge requires Python 3.12 and uses `uv`.

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv run python benchmarks/snapshot_100k.py
```