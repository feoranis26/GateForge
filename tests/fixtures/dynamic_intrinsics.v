module top(
    input counter_in,
    input counter_reset,
    input random_in,
    input selector_cycle,
    input [2:0] selector_in,
    output counter_out,
    output [2:0] random_out,
    output [2:0] selector_out
);
    GF_Counter #(
        .TARGET(20)
    ) counter (
        .in(counter_in),
        .reset(counter_reset),
        .out(counter_out)
    );

    GF_Randomizer #(
        .OUTPUTS(3),
        .MODE("TOGGLE"),
        .INPUT_ACTION("OVERRIDE_PATTERN"),
        .NEW_PICK(1),
        .ON_MIN_DS(10),
        .ON_MAX_DS(20),
        .OFF_MIN_DS(0),
        .OFF_MAX_DS(0)
    ) randomizer (
        .in(random_in),
        .out(random_out)
    );

    GF_Selector #(
        .WIDTH(3)
    ) selector (
        .cycle(selector_cycle),
        .in(selector_in),
        .out(selector_out)
    );
endmodule