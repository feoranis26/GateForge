module myinverter (
    output wire o,
    input wire i
);

    assign o = ~i;
endmodule

module test (
    output reg a,
    output reg b,
    output wire c,
    output reg [5:0] garbage,
    input wire goto_a,
    input wire goto_b,
    input wire goto_c,
    input wire clk,
    input wire reset
);

    reg [1:0] cur_state;
    reg [1:0] next_state;

    reg c_i;

    myinverter invert_c (c, c_i);

    always @(posedge clk or posedge reset) begin
        if (reset) begin
            cur_state <= 2'b00;
        end else begin
            cur_state <= next_state;
        end
    end

    always @(*) begin
        next_state = cur_state;

        if (goto_a) begin
            next_state = 2'b00;
        end else if (goto_b) begin
            next_state = 2'b01;
        end else if (goto_c) begin
            next_state = 2'b10;
        end
    end

    always @(*) begin
        case (cur_state)
            2'b00: begin
                a = 1'b1;
                b = 1'b0;
                c_i = 1'b0;
            end
            2'b01: begin
                a = 1'b0;
                b = 1'b1;
                c_i = 1'b0;
            end
            2'b10: begin
                a = 1'b0;
                b = 1'b0;
                c_i = 1'b1;
            end
            default: begin
                a = 1'b0;
                b = 1'b0;
                c_i = 1'b0;
            end
        endcase
    end

    always @(posedge clk or posedge reset) begin
        if (reset) begin
            garbage <= 6'b0;
        end else begin
            garbage <= garbage + 1'b1;
        end
    end
endmodule