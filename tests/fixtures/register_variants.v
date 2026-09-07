module top(
    input d,
    input clk,
    input async_reset_n,
    input sync_reset,
    output reg plain_q,
    output reg async_q,
    output reg sync_q
);
    always @(posedge clk) begin
        plain_q <= d;
    end

    always @(negedge clk or negedge async_reset_n) begin
        if (!async_reset_n)
            async_q <= 1'b1;
        else
            async_q <= d;
    end

    always @(posedge clk) begin
        if (sync_reset)
            sync_q <= 1'b0;
        else
            sync_q <= d;
    end
endmodule