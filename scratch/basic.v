//Basic combinatorics test, no registers, no edge-triggered logic

module test(
    output theoutput,
    input myinput,
    input myotherinput,
    input a,
    input b,
    input c
);

wire i, j, k;

assign i = a & b;
assign j = b & c;
assign k = a & c;
assign theoutput = (i | j | k) & !myinput & myotherinput;

endmodule