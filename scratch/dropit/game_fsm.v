// Game FSM:
// Supplies the desired game state.

// IDLE, ACTIVE, FINISHED, FAULT
// IDLE -> ACTIVE on:
//     start signal
//     game ready signal

// ACTIVE -> FINISHED on:
//     game complete signal

// ANY -> FAULT on:
//     fault signal

// ANY -> IDLE on:
//     game_reset signal

// ANY EXCEPT ACTIVE -> FAULT on:
//     game complete signal


module game_fsm (
    output wire idle,
    output wire active,
    output wire finished,
    output wire fault,
    input wire game_reset,
    input wire start,
    input wire game_ready,
    input wire game_complete,
    input wire fault,
    input wire clk,
    input wire reset
);
    localparam IDLE = 2'b00;
    localparam ACTIVE = 2'b01;
    localparam FINISHED = 2'b10;
    localparam FAULT = 2'b11;

    reg [1:0] state;
    reg [1:0] next_state;

    always @(posedge clk or posedge reset) begin
        if (reset) begin
            state <= IDLE;
        end else begin
            state <= next_state;
        end
    end

    always @(*) begin
        if (game_reset) begin
            next_state <= IDLE;
        end else if (fault) begin
            next_state <= FAULT;
        end else if (game_complete && state != ACTIVE) begin
            next_state <= FAULT;
        end else begin
            case (state)
                IDLE: begin
                    if (start && game_ready) next_state <= ACTIVE;
                end
                ACTIVE: begin
                    if (game_complete) next_state <= FINISHED;
                    else if (fault) next_state <= FAULT;
                end
                FINISHED: begin
                    if (game_reset) next_state <= IDLE;
                    else if (fault) next_state <= FAULT;
                end
                FAULT: begin
                    if (game_reset) next_state <= IDLE;
                end
            endcase
        end
    end

    assign idle = (state == IDLE);
    assign active = (state == ACTIVE);
    assign finished = (state == FINISHED);
    assign fault = (state == FAULT);
endmodule