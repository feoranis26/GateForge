module top(
    input trigger,
    input reset,
    output [5:0] done
);
    GF_Timer #(.TIME_DS(50), .MODE("ON_OFF")) timer_on_off (
        .in(trigger), .reset(reset), .out(done[0])
    );
    GF_Timer #(.TIME_DS(50), .MODE("SPEED_SCALE")) timer_speed_scale (
        .in(trigger), .reset(reset), .out(done[1])
    );
    GF_Timer #(.TIME_DS(50), .MODE("FORWARD_BACKWARD")) timer_direction (
        .in(trigger), .reset(reset), .out(done[2])
    );
    GF_Timer #(.TIME_DS(50), .MODE("START_COUNT_UP")) timer_count_up (
        .in(trigger), .reset(reset), .out(done[3])
    );
    GF_Timer #(.TIME_DS(50), .MODE("START_COUNT_DOWN")) timer_count_down (
        .in(trigger), .reset(reset), .out(done[4])
    );
    GF_Timer #(.TIME_DS(50), .MODE("POSITIONAL")) timer_positional (
        .in(trigger), .reset(reset), .out(done[5])
    );
endmodule