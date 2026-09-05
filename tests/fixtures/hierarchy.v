module child(
    input a,
    input b,
    (* gateforge_id = "logic" *) output y
);
    assign y = a & b;
endmodule

module top(
    input a,
    input b,
    output y0,
    output y1
);
    (* gateforge_id = "left-slot" *) child left(.a(a), .b(b), .y(y0));
    (* gateforge_id = "right-slot" *) child right(.a(a), .b(b), .y(y1));
endmodule