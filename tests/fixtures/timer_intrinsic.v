module top(
    input trigger,
    input reset,
    output done
);
    GF_Timer #(
        .TIME_DS(50),
        .MODE("START_COUNT_UP")
    ) timer (
        .in(trigger),
        .reset(reset),
        .out(done)
    );
endmodule