module top(
    input [31:0] a,
    input [31:0] b,
    output [31:0] y
);
    assign y = a + b;

    GF_Lamp lamp (
        .in(y)
    );
endmodule