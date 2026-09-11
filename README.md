# GateForge

GateForge is a compiler for turning synthesizable Verilog into logic built from the native components of logic-based construction environments.

It uses Yosys as its Verilog frontend and provides a target-provider architecture for mapping synthesized logic onto platform-specific objects, placement rules, and export formats.

GateForge currently targets **LittleBigPlanet 3** and **Factorio 2.0**. GateForge can compile Verilog into native target objects, place and visualize them, and export either Craftworld Toolkit-compatible PLAN JSON or direct Factorio blueprint JSON.

```text
Verilog source --->
---> Yosys 
---> Gateforge 
---> Mapper 
---> Yosys 
---> Gateforge 
---> Mapper (repeat for each lowering pass) 
---> Material export 
---> Placement 
---> Platform output writer
```

GateForge is under active development. File formats, command-line options, mappings, and internal APIs are not yet stable.

## Current status

```text
The LBP3 backend currently supports:
Combinational logic:                        YES
Combinatorial optimization:                 YES
Clocked registers:                          YES
Register enable/reset variants:             YES
Hierarchical modules:                       YES
Modules as nested microchips:               YES
Automatic placement:                        YES (Non-geometrically optimized)
Placement visualization:                    YES
Craftworld Toolkit export:                  YES
Native intrinsics for LBP objects:          YES

Wire routing / crossing optimization:       NO
Tag sensors / tags for IO:                  NO
Live export / loading in game:              NO
Native .PLAN output:                        NO
```

```text
The Factorio backend currently supports:
Native 32-bit addition:                     YES
Native output-lamp intrinsic:               YES
Automatic tile-aware placement:             YES
Red/green circuit routing:                  YES
Explicit copper power links:                YES
Generated input constant combinators:       YES
Generated output lamps:                     YES
Factorio 2.0 blueprint JSON export:         YES

General synthesized logic mapping:          NO
Blueprint import:                           NO
Encoded blueprint strings:                  NO
```

The compiler keeps target-specific objects outside its common representation so additional providers do not make LBP or Factorio concepts universal compiler primitives.

## Requirements

GateForge requires:

* Python 3.12 or newer
* `uv`

GateForge uses `pyosys`, so a separate Yosys executable is not required for normal compilation.

The graphical Workbench additionally requires PySide6. Graphviz is optional and is only required for rendering Yosys schematic views in the Workbench.

## Installation

GateForge is currently intended to be run from source.

Install the compiler dependencies:

```bash
uv sync
```

To also install the Qt Workbench:

```bash
uv sync --extra workbench
```

## Workbench

The Workbench provides a graphical interface for compiling, inspecting, placing, visualizing, and exporting a design.

Open a Verilog source file with:

```bash
uv run --extra workbench gateforge workbench design.v
```

LBP is selected by default. Open a Factorio session with:

```bash
uv run --extra workbench gateforge workbench --target factorio design.v
```

The Workbench exposes intermediate compiler stages, target placement defaults, provider-realized views, and target-aware export. Factorio sessions also expose generated input combinators, per-port input values, and generated output lamps. The interactive viewer and Workbench use Qt through PySide6.

## Command-line usage

### Compile and place a design

```bash
uv run gateforge compile design.v \
    --emit-material build/design.material.json \
    --emit-placement build/design.placement.json
```

`--emit-material` writes the target-mapped design before physical placement.

`--emit-placement` performs placement and writes the resulting physical layout.

### Export to LittleBigPlanet

A placed LBP design can be exported as Craftworld Toolkit-compatible PLAN JSON:

```bash
uv run gateforge export lbp-toolkit \
    build/design.material.json \
    build/design.placement.json \
    --output build/design.object.json
```

The resulting object can then be added to an LBP profile using Craftworld Toolkit.

**Back up the profile before modifying it. Do not modify an active profile while LittleBigPlanet is running.** The game may retain cached profile data and overwrite or desynchronize external changes.

### Export to Factorio

Compile and place a Factorio design, then export direct Factorio 2.0 blueprint JSON:

```bash
uv run gateforge compile add32.v \
    --target factorio \
    --emit-material build/add32.material.json \
    --emit-placement build/add32.placement.json

uv run gateforge export factorio-blueprint \
    build/add32.material.json \
    build/add32.placement.json \
    --output build/add32.blueprint.json \
    --label "GateForge add32" \
    --add-input-combinators \
    --input-value a=0xffffffff \
    --input-value b=2 \
    --add-output-lamps
```

The Factorio-only `GF_Lamp` intrinsic connects one packed 32-bit input to a native small lamp:

```verilog
GF_Lamp result_lamp (
    .in(result)
);
```

Its circuit condition is fixed to the assigned virtual signal `> 0`. **Values use signed 32-bit Factorio signal semantics**, so `32'hffffffff` is `-1` and does not light the lamp.

### Visualize a placed design

```bash
uv run gateforge visualize \
    build/design.material.json \
    build/design.placement.json
```

Add `--watch` to automatically reload the visualization when the artifacts change:

```bash
uv run gateforge visualize \
    build/design.material.json \
    build/design.placement.json \
    --watch
```

## LittleBigPlanet target

The LBP provider maps synthesized logic onto actual LBP logic objects rather than treating the game as a generic gate-level simulator.

This allows GateForge to use native gadgets when they provide a better implementation than synthesizing the same behavior from primitive logic.

### Combinational logic

Basic Boolean logic is mapped to LBP gates. GateForge also recognizes associative AND, OR, and XOR networks and can map them directly onto multi-input LBP gates rather than constructing unnecessary binary-gate trees.

### Registers

GateForge supports common Yosys register forms including:

* `$dff`
* `$dffe`
* `$adff`
* `$adffe`
* `$sdff`
* `$sdffe`
* `$sdffce`

Compatible registers are grouped into banks where possible so that clock and reset circuitry can be shared instead of duplicated for every bit.

The default implementation uses a compact Counter-based edge detector and Selector-based storage. A larger alternative implementation is available with:

```bash
--register-style hardened
```

The compact implementation uses frame-dependent logic in the form of a self-resetting 1-count counter as an edge detector, and results in 4+5n gates per n bits. The hardened implementation does not but results in 2+11n gates per n bits. No in-game differences between the two have been observed so far.

### Native intrinsics

GateForge provides reserved Verilog modules for LBP gadgets that do not have a useful direct representation in ordinary Verilog.

No include file is required.

For example:

```verilog
GF_Timer #(
    .TIME_DS(50),
    .MODE("START_COUNT_UP")
) timer (
    .in(trigger),
    .reset(reset),
    .out(done)
);
```

Currently available intrinsics are:

* `GF_Timer`
* `GF_Counter`
* `GF_Randomizer`
* `GF_Selector`

These are mapped directly to the corresponding native LBP objects.

## Hierarchy

GateForge can preserve HDL hierarchy and use it when constructing the physical target design.

For LBP, retained module instances can become nested microchips. Other providers and placers may or may not have an analog for this, they may treat hierarchy as placement boundaries, or ignore it entirely.

For example:

```bash
uv run gateforge compile design.v \
    --synthesis-hierarchy preserve \
    --physical-hierarchy preserve-all \
    --emit-material build/design.material.json \
    --emit-placement build/design.placement.json
```

Synthesis hierarchy controls how much of the Verilog module hierarchy remains visible to the mapper:

* `preserve` — preserve module boundaries during optimization
* `flat` — flatten the design before mapping
* `min-cells` — flatten modules below a primitive-cell threshold

Note that optimization will not take place across module boundaries. You can have a series of 934 pointless NOT gates if they are in separate modules.

Physical hierarchy controls which preserved modules become physical containers:

* `flat` — place everything on one root board
* `preserve-all` — retain all preserved module instances
* `min-objects` — retain modules above an object-count threshold
* `min-cost` — retain modules above a provider-estimated physical cost

Generated implementations such as register banks have a separate hierarchy policy:

* `inline`
* `auto`
* `all`

The default is `auto`.

## Placement

GateForge includes a deterministic topological placer shared by the compiler pipeline.

The placer attempts to arrange the design with inputs toward the left, outputs toward the right, and dependent logic progressing across the board. Cyclic paths are preserved and may run in the opposite direction where necessary.

Placement currently accounts for target-provided object dimensions and nested containers.

It does **not** yet perform full wire routing, crossing minimization, or general geometric optimization. The current placer is intended to produce valid layouts rather than globally optimal beautiful humanr eadable ones.

Placement can also be rerun without recompiling the Verilog:

```bash
uv run gateforge place build/design.material.json \
    --output build/design.placement.json
```

Spacing and hierarchy options are available through `gateforge place --help`.

## Mapping

GateForge can evaluate multiple possible implementations for portions of a design.

The available mapping search modes are:

```text
greedy
beam
exhaustive
```

`greedy` is the default and is suitable for normal use.

Beam and exhaustive search are primarily useful for experimenting with alternative mappings and compiler development:

```bash
uv run gateforge compile design.v \
    --mapping-search beam \
    --mapping-beam-width 16 \
    --emit-material build/design.material.json
```

A detailed search report can be written with `--emit-search-report`.

## Intermediate artifacts

GateForge can expose several intermediate representations for debugging and tooling:

```bash
--emit-json
--emit-state
--emit-material
--emit-placement
--emit-search-report
```

For normal use, only the material and placement files are needed for either target export workflow.

The other artifacts are primarily intended for compiler development, debugging, and external tooling.

## Target providers

Platform-specific behavior is implemented through target providers.

A provider defines the objects available on a target and the operations required to turn mapped logic into a physical design. This includes areas such as:

* available object types
* object configuration
* mapping implementations
* validation
* object dimensions and placement constraints
* physical design elaboration
* visualization
* target-specific export

The rest of GateForge operates on common compiler representations so that target-specific concepts do not need to be hard-coded throughout the compiler.

## Development

Install dependencies:

```bash
uv sync
```

Run the test suite:

```bash
uv run python -m unittest discover -s tests -v
```

Run the large-design benchmark:

```bash
uv run python benchmarks/snapshot_100k.py
```

## Known limitations

* Not every construct that Yosys can synthesize has a valid LBP mapping.
* Compilation fails if unsupported synthesized logic remains after mapping.
* Stateful logic has less in-game validation than combinational logic.
* Placement is functional but not yet optimized for compactness or wire quality.
* Direct deployment to a running game is not currently part of GateForge.
* The CLI and intermediate artifact formats may change as the compiler develops.

For the current set of options, use:

```bash
uv run gateforge --help
uv run gateforge compile --help
uv run gateforge place --help
```
