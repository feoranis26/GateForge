module chained_add32(
    (* factorio_signal = "signal-A", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] a, 
    (* factorio_signal = "signal-B", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] b, 
    (* factorio_signal = "signal-C", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] c, 
    (* factorio_signal = "signal-D", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] d, 
    (* factorio_signal = "signal-E", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] e,
    (* factorio_signal = "signal-Y", factorio_circuit = "outputs", factorio_color = "green" *)
    output [31:0] y
);
    wire [31:0] left_sum = a + b;
    wire [31:0] right_sum = c + d;
    wire [31:0] subtotal = left_sum + right_sum;
    assign y = subtotal + e;
endmodule