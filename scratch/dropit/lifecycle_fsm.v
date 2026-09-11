// Lifecycle FSM
// Manages the game state
// Takes input from the game FSM about the desired state

// States:
// STOPPED, SELF_CHECK, SPAWN_START, IDLE, WAIT_ANIM, ENTER_PLAYERS, RUNNING, WAIT_STOP, DESTROY_ARTIFACTS

// Inputs:
// enable, run, fault, game_reset
// self_check_complete, spawn_start_complete, wait_anim_complete, enter_players_complete, wait_stop_complete, destroy_artifacts_complete

// Outputs:
// stopped, self_check, spawn_start, idle, wait_anim, enter_players, running, wait_stop, destroy_artifacts

// Enable: should perform self check, spawn start, and enter idle state
// Run: Shlould transition towards game active state
// Fault / Game Reset: Stop ASAP if players have not entered the arena, otherwise follow normal exit sequence

// NORMAL TRANSITIONS
// STOPPED -> SELF_CHECK
//     on enable
// SELF_CHECK -> SPAWN_START
//     on self_check_complete
// SPAWN_START -> IDLE
//     on spawn_start_complete
// SPAWN_START -> WAIT_ANIM
//     on run
// WAIT_ANIM -> ENTER_PLAYERS
//     on wait_anim_complete
// ENTER_PLAYERS -> RUNNING
//     on enter_players_complete
//     or !run, !enable, fault, game_reset
// RUNNING -> WAIT_STOP
//     on !run, fault, game_reset, !enable
// WAIT_STOP -> DESTROY_ARTIFACTS
//     on wait_stop_complete
// DESTROY_ARTIFACTS -> IDLE
//     on destroy_artifacts_complete

// IDLE -> STOPPED
//     on !enable or fault or game_reset

// ERROR TRANSITIONS
// (SELF_CHECK, SPAWN_START, WAIT_ANIM) -> DESTROY_ARTIFACTS
//     on fault, game_reset, !enable, !run

module lifecycle_fsm (
    output stopped,
    output self_check,
    output spawn_start,
    output idle,
    output wait_anim,
    output enter_players,
    output running,
    output wait_stop,
    output destroy_artifacts,
    input enable,
    input run,
    input fault,
    input game_reset,
    input self_check_complete,
    input spawn_start_complete,
    input wait_anim_complete,
    input enter_players_complete,
    input wait_stop_complete,
    input destroy_artifacts_complete,
    input clk,
    input reset
);

    localparam STOPPED = 4'b0000;
    localparam SELF_CHECK = 4'b0001;
    localparam SPAWN_START = 4'b0010;
    localparam IDLE = 4'b0011;
    localparam WAIT_ANIM = 4'b0100;
    localparam ENTER_PLAYERS = 4'b0101;
    localparam RUNNING = 4'b0110;
    localparam WAIT_STOP = 4'b0111;
    localparam DESTROY_ARTIFACTS = 4'b1000;

    reg [3:0] state;
    reg [3:0] next_state;

    always @(posedge clk or posedge reset) begin
        if (reset) begin
            state <= STOPPED;
        end else begin
            state <= next_state;
        end
    end

    always @(*) begin
        next_state = state;
        case (state)
            STOPPED: begin
                if (enable) next_state = SELF_CHECK;
            end
            SELF_CHECK: begin
                if (self_check_complete) next_state = SPAWN_START;
                else if (fault || game_reset || !enable || !run) next_state = DESTROY_ARTIFACTS;
            end
            SPAWN_START: begin
                if (spawn_start_complete) next_state = IDLE;
                else if (run) next_state = WAIT_ANIM;
                else if (fault || game_reset || !enable || !run) next_state = DESTROY_ARTIFACTS;
            end
            IDLE: begin
                if (run) next_state = WAIT_ANIM;
                else if (fault || !enable) next_state = STOPPED;
            end
            WAIT_ANIM: begin
                if (wait_anim_complete) next_state = ENTER_PLAYERS;
                else if (fault || game_reset || !enable || !run) next_state = DESTROY_ARTIFACTS;
            end
            ENTER_PLAYERS: begin
                if (enter_players_complete) next_state = RUNNING;
                else if (fault || game_reset || !enable || !run) next_state = WAIT_STOP;
            end
            RUNNING: begin
                if (!run || fault || game_reset || !enable) next_state = WAIT_STOP;
            end
            WAIT_STOP: begin
                if (wait_stop_complete) next_state = DESTROY_ARTIFACTS;
            end
            DESTROY_ARTIFACTS: begin
                if (destroy_artifacts_complete) next_state = IDLE;
            end
        endcase
    end

    assign stopped = (state == STOPPED);
    assign self_check = (state == SELF_CHECK);
    assign spawn_start = (state == SPAWN_START);
    assign idle = (state == IDLE);
    assign wait_anim = (state == WAIT_ANIM);
    assign enter_players = (state == ENTER_PLAYERS);
    assign running = (state == RUNNING);
    assign wait_stop = (state == WAIT_STOP);
    assign destroy_artifacts = (state == DESTROY_ARTIFACTS);
endmodule