//Basic combinatorics test, no registers, no edge-triggered logic
//Contains two nested logic cells

module myinverter (
    output wire o,
    input wire i
);

    assign o = ~i;
endmodule

module test_with_inverters(
    output theoutput,
    input myinput,
    input myotherinput,
    input a,
    input b,
    input c
);

    wire inverted_myinput, inverted_myotherinput;

    myinverter inv1(
        .o(inverted_myinput),
        .i(myinput)
    );
    myinverter inv2(
        .o(inverted_myotherinput),
        .i(myotherinput)
    );

    wire i, j, k;

    assign i = a & b;
    assign j = b & c;
    assign k = a & c;
    assign theoutput = (i | j | k) & !inverted_myinput & !inverted_myotherinput;

endmodule