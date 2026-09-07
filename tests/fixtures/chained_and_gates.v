module test(
    output theoutput,
    input a,
    input b,
    input c
);
    assign theoutput = a & !b & c;
endmodule