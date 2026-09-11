(* blackbox, gateforge_intrinsic = "timer", gateforge_intrinsic_version = 1 *)
module GF_Timer #(
    parameter integer TIME_DS = 50,
    parameter MODE = "ON_OFF"
) (
    input in,
    input reset,
    output out
);
endmodule

(* blackbox, gateforge_intrinsic = "counter", gateforge_intrinsic_version = 1 *)
module GF_Counter #(
    parameter integer TARGET = 10
) (
    input in,
    input reset,
    output out
);
endmodule

(* blackbox, gateforge_intrinsic = "randomizer", gateforge_intrinsic_version = 1 *)
module GF_Randomizer #(
    parameter integer OUTPUTS = 2,
    parameter MODE = "ADD",
    parameter INPUT_ACTION = "TRIGGER",
    parameter integer NEW_PICK = 0,
    parameter integer ON_MIN_DS = 10,
    parameter integer ON_MAX_DS = 10,
    parameter integer OFF_MIN_DS = 0,
    parameter integer OFF_MAX_DS = 0
) (
    input in,
    output [OUTPUTS-1:0] out
);
endmodule

(* blackbox, gateforge_intrinsic = "selector", gateforge_intrinsic_version = 1 *)
module GF_Selector #(
    parameter integer WIDTH = 2
) (
    input cycle,
    input [WIDTH-1:0] in,
    output [WIDTH-1:0] out
);
endmodule

(* blackbox, gateforge_intrinsic = "lamp", gateforge_intrinsic_version = 1 *)
module GF_Lamp (
    input [31:0] in
);
endmodule