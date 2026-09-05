module child #(
    parameter WIDTH = 1
) (
    input [WIDTH-1:0] a,
    input [WIDTH-1:0] b,
    output [WIDTH-1:0] y
);
    assign y = a & b;
endmodule

module top(
    input [2:0] a,
    input [2:0] b,
    output [2:0] y
);
    (* gateforge_id = "narrow" *)
    child #(.WIDTH(1)) narrow(.a(a[0]), .b(b[0]), .y(y[0]));

    (* gateforge_id = "wide" *)
    child #(.WIDTH(2)) wide(.a(a[2:1]), .b(b[2:1]), .y(y[2:1]));
endmodule