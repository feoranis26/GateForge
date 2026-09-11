module bitwise32(
    (* factorio_signal = "signal-A", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] a,
    (* factorio_signal = "signal-B", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] b,
    (* factorio_signal = "signal-C", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] mask,
    (* factorio_signal = "signal-D", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] flags,
    (* factorio_signal = "signal-Y", factorio_circuit = "outputs", factorio_color = "red" *)
    output [31:0] y,
    (* factorio_signal = "signal-Z", factorio_circuit = "outputs", factorio_color = "red" *)
    output [31:0] inverted
);
    assign y = (((a + b) & mask) ^ flags) | 32'h80000000;
    assign inverted = ~a;
endmodule