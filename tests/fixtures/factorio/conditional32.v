module conditional32(
    (* factorio_signal = "signal-A", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] a,
    (* factorio_signal = "signal-B", factorio_circuit = "inputs", factorio_color = "green" *)
    input [31:0] b,
    (* factorio_signal = "signal-E", factorio_circuit = "inputs", factorio_color = "green" *)
    input enable,
    (* factorio_signal = "signal-Y", factorio_circuit = "outputs", factorio_color = "red" *)
    output reg [31:0] y,
    (* factorio_signal = "signal-R", factorio_circuit = "outputs", factorio_color = "red" *)
    output ready
);
    assign ready = enable && (a >= b);
    always @* begin
        if (ready)
            y = a + 32'd1;
        else
            y = b;
    end
endmodule