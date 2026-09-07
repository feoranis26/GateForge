# Stateful Banks and Generated Microchips

## Objective

Add stateful compilation without allowing generated storage circuitry to dominate
the parent board:

- Accept and preserve arbitrary feedback.
- Place cyclic graphs deterministically using SCC condensation.
- Map compatible Yosys registers as maximal shared-control banks before
  `techmap` destroys vector structure.
- Keep the verified non-Memorizer DFF as a permanent fallback.
- Package substantial generated implementations into nested microchips.
- Keep generated physical groups separate from source HDL module provenance.
- Allow future Memorizer and direct-FSM implementations to compete through the
  existing mapping search.

## Implementation Status

Completed in the first implementation pass:

- SCC-condensed placement accepts self-loops and arbitrary feedback while
  preserving all material dependencies.
- Material schema v2 stores generated implementation occurrences separately
  from HDL modules; schema v1 artifacts remain readable.
- Mapping proposals and durable claims carry implementation names and
  `INLINE`/`AUTO`/`CONTAINER` packaging hints.
- LBP containerization supports orthogonal generated hierarchy modes:
  `inline`, `auto`, and `all`.
- Mapping-provider execution is stage-aware.
- Coarse `$dff`, `$adff`, and `$sdff` cells form maximal shared-control banks at
  `post-fsm`.
- Scalar `$_DFF_*` and `$_SDFF_*` cells regroup into equivalent leaf fallback
  banks.
- Compact non-Memorizer banks use one target-1 self-resetting Counter per bank
  plus one NOT, two write ANDs, and one state Selector per bit.
- The captured two-phase implementation remains selectable as the hardened
  fallback through `--register-style hardened`.
- `scratch/test.v` compiles and places as one width-9 asynchronous-reset bank;
  default generated hierarchy exports it as one `register-bank[9]` child
  microchip.
- No-reset, active-low asynchronous reset-to-one, and synchronous reset-to-zero
  source variants all map and materialize structurally.
- Compact Counter-based banks are the default and the two-phase implementation
  remains selectable with `--register-style hardened`.
- DFFE/ADFFE/SDFFE/SDFFCE families have structural coarse/scalar support,
  independent enable subgroups, hold feedback, and reset/enable priority.
- Generated register children use deterministic bit-slice tiling, and parent
  containers are reflown with retained child microchips treated as compound
  nodes.
- `scratch/test.v` now realizes as 68 material objects rather than 140. The
  generated width-nine child is bounded by `997.5 × 787.5`, while the reflown
  root board is `945 × 630`.

Still pending:

- In-game validation of edge capture and the synthesized synchronous and
  asynchronous reset paths.
- Memorizer support and direct FSM mapping.
- In-game acceptance for hardened, reset, enable, and priority behavior.

## Verified Baseline

The current evidence establishes the following facts.

- `scratch/test.v` reaches `post-fsm` with:
  - One width-3 `$adff`, `ARST_VALUE=001`.
  - One width-6 `$adff`, `ARST_VALUE=000000`.
  - Both use the same positive-edge clock and active-high asynchronous reset.
- After `techmap`, these become eight `$_DFF_PP0_` cells and one
  `$_DFF_PP1_` cell. All combinational cells already map successfully.
- Yosys naming distinguishes reset timing:
  - `$_DFF_[NP]_`: edge-triggered DFF without reset.
  - `$_DFF_[NP][NP][01]_`: DFF with asynchronous reset/set.
  - `$_SDFF_[NP][NP][01]_`: DFF with synchronous reset/set.
- `tests/fixtures/lbp/SDFF.json` is a one-bit, non-Memorizer DFF without reset.
  Its generated implementation uses:
  - UID 14 as a shared two-phase Selector.
  - UIDs 6 and 10 as per-bit storage Selectors.
  - UIDs 4, 5, 8, and 9 as per-bit write gates.
  - UIDs 12 and 13 plus OR UID 21 as the per-bit read path.
  - UIDs 7 and 11 as data-path inversion.
  - UIDs 15-20 as routing/test harness objects that generated output can omit.
- The captured Thing graph has no explicit wire SCC. State is internal to the
  Selector gadgets.
- `MaterialGraph` already preserves cyclic connectivity. Only the topological
  placer currently rejects SCCs.

## Architectural Decisions

### Feedback Policy

GateForge accepts combinational and stateful feedback. It does not claim that an
arbitrary combinational loop converges or behaves usefully; it preserves the
user's requested circuit.

Structural errors remain errors:

- Unknown or invalid ports.
- Incompatible signal/network types.
- Multiple LBP producers on one material net.
- Unsupported X/Z configuration values.

Cycle detection is a placement concern, not an admission rule.

### Generated Groups Are Not HDL Modules

`MaterialModuleOccurrence` remains source-authentic. Generated implementations
use a separate `MaterialImplementationOccurrence` model.

Every accepted prefab already has a stable `OccurrenceId`; all objects produced
by that prefab share it. That occurrence becomes the authoritative generated
group identity.

### Register Fusion Rule

Registers are fused maximally within one source-module occurrence when they have
compatible shared control semantics:

- Same storage/reset family.
- Same clock net and polarity.
- Same reset net, polarity, and synchronous/asynchronous behavior.
- Same reset-versus-enable priority when enable support is added.

Width and reset value are not partition keys. Different reset bits coexist in
one bank as a reset-value vector.

Registers are not fused across preserved source-module boundaries. Synthesis
flattening remains the explicit way to permit cross-module fusion.

### Generated Microchip Policy

Generated grouping is independent of `--physical-hierarchy`:

- `inline`: never create generated microchips.
- `auto`: contain explicit `CONTAINER` implementations and `AUTO`
  implementations with at least three objects.
- `all`: contain every multi-object implementation.

Register banks always request `CONTAINER`. A compatible bank therefore appears
as one microchip on its parent board, regardless of bit width.

## Phase 1: SCC-Aware Placement

**Status: implemented and covered by automated tests.**

### Implementation

1. Replace cycle rejection in `src/gateforge/placement/topological.py` with a
   deterministic strongly connected component decomposition.
2. Collapse SCCs into a condensation DAG.
3. Apply the existing output-biased longest-path column assignment to SCC
   super-nodes.
4. Preserve existing coordinates for singleton acyclic SCCs.
5. Arrange members of nontrivial SCCs in a deterministic bounded local grid:
   - Sort by `material_subject_key`.
   - Respect provider object geometry.
   - Treat self-loops as one-member cyclic SCCs.
6. Validate left-to-right ordering only across SCC boundaries. Internal SCC
   edges may point in any direction.
7. Preserve every original `MaterialDependency` for routing and export.

### Tests

- Self-loop.
- Two- and three-object rings.
- Fan-in and fan-out around a ring.
- Multiple disconnected SCCs.
- Nested feedback structures.
- Deterministic repeated placement.
- No geometry overlap within an SCC.
- Every feedback edge survives Toolkit export.
- Existing acyclic placement tests retain their coordinates.

### Exit Criteria

The topological placer accepts arbitrary material SCCs, produces deterministic
coordinates, and does not remove or rewrite connectivity.

## Phase 2: Durable Implementation Occurrences

**Status: durable schema, materialization, policy, and LBP containerization are
implemented. Generated register children receive dense local bit-slice layouts,
and parent containers are reflown with retained children treated as compound
nodes.**

### Data Model

Add to `src/gateforge/material.py`:

```python
class ImplementationPackaging(StrEnum):
    INLINE = "inline"
    AUTO = "auto"
    CONTAINER = "container"

@dataclass(frozen=True, slots=True)
class MaterialImplementationPort:
    name: str
    bit: int
    direction: PortDirection
    net: MaterialNetId

@dataclass(frozen=True, slots=True)
class MaterialImplementationOccurrence:
    path: str
    occurrence: OccurrenceId
    owner_module: str
    prefab: PrefabId
    provider: str
    mapper: str
    rule: str
    name: str
    packaging: ImplementationPackaging
    ports: tuple[MaterialImplementationPort, ...]
    objects: tuple[MaterialObjectId, ...]
```

Add `implementations` to `MaterialDesign` and bump the material schema. Continue
reading schema v1 artifacts as designs with no implementation occurrences.

### Proposal and Claim Metadata

Add to `MappingProposal` and `ClaimDefinition`:

- Human-readable implementation name.
- Packaging hint.

Include these values in proposal fingerprints and state canonical data. Old
state artifacts decode to `AUTO` and a deterministic rule-based name.

### Materialization

For every accepted claim occurrence:

1. Reuse its existing `OccurrenceId`.
2. Record all objects produced by that prefab occurrence.
3. Resolve each semantic prefab boundary bit to the final merged
   `MaterialNetId`.
4. Record source-module ownership separately from the generated group path.
5. Validate common occurrence, prefab, provider, and owner across all members.

### Placement and Containerization

1. Place selected implementation occurrences internally first.
2. Expose each implementation's bounds as one compound subject to its parent
   layout.
3. Flatten local transforms into `PlacedDesign` while retaining group metadata.
4. Build one LBP container tree from retained HDL modules plus selected
   implementation groups.
5. Parent a generated group below its nearest retained source-module owner.
6. Derive child microchip pins from stored implementation ports, not by
   re-discovering cuts during export.

### Tests

- Existing two-object ANDNOT remains inline under `auto`.
- A five-object synthetic prefab becomes one child microchip.
- Source hierarchy and generated grouping compose correctly.
- Flat source hierarchy may still contain generated microchips.
- Boundary net order and routing are deterministic.
- Material and placement round trips preserve implementation occurrences.

### Exit Criteria

Any substantial multi-object mapping can request one generated microchip without
pretending to be a source Verilog module.

## Phase 3: Stage-Aware Register Mapping

**Status: implemented for coarse and scalar DFF/ADFF/SDFF families.**

### Mapping API

Pass stage context to mapping providers:

```python
@dataclass(frozen=True, slots=True)
class MappingRequest:
    snapshot: DesignSnapshot
    stage: str
    terminal: bool
```

- Intrinsics map at `source` as REQUIRED.
- Direct FSM proposals map at `extracted-fsm`.
- Coarse register banks map at `post-fsm`.
- Scalar register fallback maps at `leaf`.

### Typed Register Model

Add `RegisterBankSpec` and `RegisterBitBinding` containing:

- Family: DFF, ADFF, or SDFF.
- Width.
- Clock source and polarity.
- Optional reset source, polarity, and synchrony.
- Per-bit reset value.
- Ordered D and Q source endpoints.
- Later: enable source, polarity, and priority.

Extend `YosysParameterValue` with width-aware binary-vector decoding. Explicitly
test that Yosys parameter strings and LSB-first port bits align correctly.

### Coarse Bank Enumeration

At `post-fsm`:

1. Enumerate `$dff`, `$adff`, and `$sdff` cells.
2. Partition by source module and compatible shared-control key.
3. Sort cells by stable anchor, then provenance/name fallback.
4. Preserve each cell's LSB-first bit order.
5. Concatenate D, Q, and reset-value vectors in the same order.
6. Emit one maximal proposal per group.

The prefab interface is:

```text
D[W-1:0]
Q[W-1:0]
CLK
RESET        optional
```

The proposal claims every source register in the bank, uses packaging
`CONTAINER`, and is named `register-bank[W]`.

### Scalar Fallback

At `leaf`, decode and regroup:

- `$_DFF_[NP]_`
- `$_DFF_[NP][NP][01]_`
- `$_SDFF_[NP][NP][01]_`

This guarantees a terminal fallback when the coarse proposal was deferred.

### Integration Target

For `scratch/test.v`, the lowered branch must fuse the width-3 reset `001` bank
and width-6 reset `000000` bank into one width-9 implementation because their
clock/reset semantics match.

### Exit Criteria

The search frontier contains valid coarse and scalar register-bank alternatives,
and no DFF remains unmapped at the terminal stage.

## Phase 4: Non-Memorizer Register-Bank Prefab

**Status: compact Counter-based banks are implemented as the default; the
capture-parity two-phase implementation remains available as `hardened`.**

Build a multi-object `SemanticPrefab`, not an opaque target object. Internal
gadgets remain visible to validation, cost, placement, and Toolkit serialization
but are hidden from the parent board by the implementation container.

### Compact Default

Per bank:

- One target-1 Counter receiving the normalized clock.
- Counter output fed back to its RESET input to produce a one-frame pulse.
- Pulse fanout shared by every bit in the bank.

Per bit:

- One data NOT.
- Two AND gates combining pulse with D and !D.
- One state Selector selected by those two pulse paths.

Generated register children are tiled by bit and shared controls are placed on a
dedicated row. Parent containers are independently reflown around the child
microchip. The width-nine integration child is bounded by `997.5 × 787.5`; its
root board is bounded by `945 × 630`.

The edge detector is a target-1 Counter whose output is fed back to its own
reset input and fanned out as the bank-wide one-frame pulse. Generic mapping
sees only DFF semantics; this frame-level mechanism remains provider-owned.

### Hardened Fallback

Per bank:

- One shared two-phase Selector equivalent to UID 14.
- Shared clock polarity normalization when required.

Per bit:

- Two storage Selectors equivalent to UIDs 6 and 10.
- Four phase-qualified write gates.
- Two phase-qualified read gates.
- One output OR.
- Initially preserve both captured D-path inversions.

Omit fixture harness UIDs 15-20. Material nets provide fanout and external pins.

Select with `--register-style hardened`. Keep it permanently available for
timing-sensitive designs and compare it against compact and future Memorizer
implementations after its smoke test passes.

### Stateful Test Model

Add a discrete event evaluator with explicit D, clock, reset, and Q state. Test:

- Rising- and falling-edge capture.
- Hold while clock is stable.
- D changes on both clock levels.
- Repeated edges.
- Multi-bit independence under one shared clock.

### Exit Criteria

A no-reset vector DFF bank behaves like its Yosys source model and exports as one
generated microchip.

## Phase 5: Synchronous and Asynchronous Reset

**Status: structural reset synthesis and mixed reset-vector mapping are
implemented; event-level and in-game behavior remain pending.**

Support arbitrary compile-time reset vectors. Reset values do not split banks.

### Synchronous Reset

Condition each data bit before normal edge capture:

```text
D_eff[i] = RESET ? RESET_VALUE[i] : D[i]
```

Optimize fixed values:

- Reset to 0: `D_eff = D & !RESET`.
- Reset to 1: `D_eff = D | RESET`.

Normalize reset polarity once per bank.

### Asynchronous Reset

Reset must override both clock phases and immediately force both storage banks.
For each phase `P` and bit value `V`:

```text
set_path   = (RESET & V)  | (!RESET & P & D)
clear_path = (RESET & !V) | (!RESET & P & !D)
```

Widen existing gates where safe and share normalized reset/phase signals across
the bank. Preserve reset priority over normal writes.

### Initialization

- Resettable banks initialize Selector state consistently with the reset vector.
- Uninitialized `$dff` banks use a documented deterministic LBP default because
  LBP cannot represent HDL X state.
- Initial state and reset behavior remain distinct configuration fields.

### Tests

- Active-high and active-low reset.
- Reset to 0 and 1.
- Mixed vectors including `001`.
- Async assertion without a clock edge.
- Sync reset changing only on an active edge.
- Reset assertion near an active edge.
- Deassertion followed by normal capture.

In-game acceptance is required because the supplied DFF capture has no reset
path.

### Exit Criteria

`scratch/test.v` compiles with its exact active-high asynchronous reset vector
and passes event-level behavior tests.

## Phase 6: Enable and DFFE Families

**Status: structural coarse/scalar mapping, hold feedback, independent enable
subgroups, reset-versus-enable priority, SCC placement, and export are
implemented. In-game priority and hold behavior remain pending.**

Add coarse and scalar support for:

- `$dffe`, `$adffe`, `$sdffe`, `$sdffce`
- `$_DFFE_*`, `$_SDFFE_*`, `$_SDFFCE_*`

Implement hold as:

```text
D_eff[i] = ENABLE ? D[i] : Q[i]
```

The full bank continues sharing clock/reset phase logic. Different enable
signals or polarities form deterministic control subgroups inside that one bank
instead of automatically producing separate microchips. Split only when one
container cannot preserve reset-versus-enable priority semantics.

Tests cover:

- Multiple enable groups under one shared clock/reset bank.
- Enable polarity.
- Hold behavior.
- Async reset while disabled.
- Reset-priority SDFFE.
- Enable-priority SDFFCE.

## Phase 7: Search, Cost, and End-to-End Acceptance

### Cost Model

Proposal estimates include:

- Shared phase/reset/enable overhead.
- Per-bit slice cost.

Terminal scoring includes:

- Every internal gadget.
- Generated microchip overhead.
- Child-board area.
- Boundary pins and estimated wire cost.

Search reports include bank width, grouped source cells, shared controls, reset
vector, implementation name, estimated cost, realized object count, and
container overhead.

### Required End-to-End Result

Compiling `scratch/test.v` must produce:

- No unmapped DFF cells.
- One width-9 shared-control register-bank implementation in the lowered branch.
- One generated register-bank microchip on the parent board.
- All unrelated combinational logic outside that microchip unless independently
  grouped by its own implementation occurrence.
- Successful placement even when Q-to-D logic creates SCCs.
- Deterministic material, placement, search-report, and Toolkit artifacts.

Add a deliberate combinational-ring fixture and verify every loop edge survives
placement and export.

## Phase 8: Memorizer and Direct FSM Alternatives

Implement Memorizer support as another proposal for the same register-bank
source region and semantic interface. Keep the non-Memorizer fallback
permanently available.

After register banks are stable, add direct `$fsm` proposals at
`extracted-fsm`. Search then compares:

- Direct Selector-based FSM.
- Deferred `fsm_map` plus shared register bank and combinational gates.
- Future Memorizer-backed state.

Test cases must allow each alternative to win based on exact terminal cost.

## Validation Commands

Run after each phase:

```sh
uv run python -m unittest discover -s tests -v
git diff --check
```

Also require:

- Clean Pylance workspace diagnostics.
- Wheel contains packaged intrinsic and future stateful assets.
- Deterministic repeated compilation.
- Toolkit JSON round-trip checks.
- In-game smoke tests for edge capture, synchronous reset, asynchronous reset,
  mixed reset values, and eventually Memorizer parity.

## Scope Boundaries

- No metastability or setup/hold model in this stage.
- No proof that arbitrary combinational feedback converges.
- No X/Z reset-value realization until LBP semantics are defined.
- Direct FSM mapping and Memorizer support follow the shared register-bank
  substrate and do not block the non-Memorizer fallback.