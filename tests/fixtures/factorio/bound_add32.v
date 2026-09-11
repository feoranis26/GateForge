module bound_add32(
    (* factorio_signal = "signal-A", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] a,
    (* factorio_signal = "signal-B", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] b,
    (* factorio_signal = "signal-C", factorio_circuit = "outputs", factorio_color = "red" *)
    output [31:0] y,
    (* factorio_signal = "signal-D", factorio_circuit = "outputs", factorio_color = "red" *)
    output [31:0] tap
);
    assign y = a + b;
    assign tap = a;
endmodule