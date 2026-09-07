module top(
    input d0,
    input d1,
    input d2,
    input da,
    input clk,
    input reset,
    input en0,
    input en1,
    output reg q0,
    output reg q1,
    output reg q2,
    output reg qa
);
    always @(posedge clk) begin
        q0 <= d0;
        if (en0)
            q1 <= d1;
        if (en1)
            q2 <= d2;
    end

    always @(posedge clk or posedge reset) begin
        if (reset)
            qa <= 1'b1;
        else if (en0)
            qa <= da;
    end
endmodule