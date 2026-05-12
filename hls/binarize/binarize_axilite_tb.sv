// =====================================================================
// binarize_axilite_tb.sv
//
// SV testbench equivalent of step0_axilite_check.py — drives the
// HLS-generated AXI4-Lite slave on binarize_core directly in xsim.
// No board needed; runs entirely on the PC.
//
// Six checks (same as the Python step 0):
//   1. ap_idle high after reset
//   2. img_w   round-trip (16-bit field, walking patterns)
//   3. img_h   round-trip (16-bit field, walking patterns)
//   4. threshold round-trip (8-bit field, walking patterns)
//   5. threshold stable across 100 reads
//   6. ap_idle still high (no spurious start)
//
// Run from FPGA/hls/binarize/:
//   xvlog -sv binarize_axilite_tb.sv
//   xvlog binarize/solution1/impl/verilog/*.v
//   xelab -debug typical binarize_axilite_tb -s tb_sim
//   xsim tb_sim -R
// =====================================================================

`timescale 1ns / 1ps

module binarize_axilite_tb;

    // Clock & reset
    logic ap_clk = 0;
    logic ap_rst_n = 0;
    always #5 ap_clk = ~ap_clk;   // 100 MHz

    // AXI-Lite signals (6-bit address, 32-bit data — matches HLS)
    logic        awvalid;
    logic        awready;
    logic [5:0]  awaddr;
    logic        wvalid;
    logic        wready;
    logic [31:0] wdata;
    logic [3:0]  wstrb;
    logic [1:0]  bresp;
    logic        bvalid;
    logic        bready;
    logic        arvalid;
    logic        arready;
    logic [5:0]  araddr;
    logic [1:0]  rresp;
    logic        rvalid;
    logic        rready;
    logic [31:0] rdata;

    // Block-level handshake (driven inert from outside)
    logic ap_start = 0;
    wire  ap_done, ap_idle, ap_ready;

    // AXIS gray_in (held inactive — IP must remain idle)
    logic        gray_tvalid = 0;
    wire         gray_tready;
    logic [7:0]  gray_tdata  = 0;
    logic        gray_tlast  = 0;
    logic        gray_tkeep  = 0;
    logic        gray_tstrb  = 0;
    logic        gray_tuser  = 0;
    logic        gray_tid    = 0;
    logic        gray_tdest  = 0;

    // AXIS bin_out (sink ready)
    wire         bin_tvalid;
    logic        bin_tready  = 1;
    wire  [7:0]  bin_tdata;
    wire         bin_tlast, bin_tkeep, bin_tstrb, bin_tuser, bin_tid, bin_tdest;

    // ---------- DUT ----------
    binarize_core dut (
        .ap_clk             (ap_clk),
        .ap_rst_n           (ap_rst_n),
        .ap_start           (ap_start),
        .ap_done            (ap_done),
        .ap_idle            (ap_idle),
        .ap_ready           (ap_ready),

        .gray_in_TDATA      (gray_tdata),
        .gray_in_TVALID     (gray_tvalid),
        .gray_in_TREADY     (gray_tready),
        .gray_in_TKEEP      (gray_tkeep),
        .gray_in_TSTRB      (gray_tstrb),
        .gray_in_TUSER      (gray_tuser),
        .gray_in_TLAST      (gray_tlast),
        .gray_in_TID        (gray_tid),
        .gray_in_TDEST      (gray_tdest),

        .bin_out_TDATA      (bin_tdata),
        .bin_out_TVALID     (bin_tvalid),
        .bin_out_TREADY     (bin_tready),
        .bin_out_TKEEP      (bin_tkeep),
        .bin_out_TSTRB      (bin_tstrb),
        .bin_out_TUSER      (bin_tuser),
        .bin_out_TLAST      (bin_tlast),
        .bin_out_TID        (bin_tid),
        .bin_out_TDEST      (bin_tdest),

        .s_axi_CTRL_AWVALID (awvalid),
        .s_axi_CTRL_AWREADY (awready),
        .s_axi_CTRL_AWADDR  (awaddr),
        .s_axi_CTRL_WVALID  (wvalid),
        .s_axi_CTRL_WREADY  (wready),
        .s_axi_CTRL_WDATA   (wdata),
        .s_axi_CTRL_WSTRB   (wstrb),
        .s_axi_CTRL_ARVALID (arvalid),
        .s_axi_CTRL_ARREADY (arready),
        .s_axi_CTRL_ARADDR  (araddr),
        .s_axi_CTRL_RVALID  (rvalid),
        .s_axi_CTRL_RREADY  (rready),
        .s_axi_CTRL_RDATA   (rdata),
        .s_axi_CTRL_RRESP   (rresp),
        .s_axi_CTRL_BVALID  (bvalid),
        .s_axi_CTRL_BREADY  (bready),
        .s_axi_CTRL_BRESP   (bresp)
    );

    // ---------- AXI-Lite BFM tasks ----------
    task automatic axil_write(input logic [5:0] addr, input logic [31:0] data);
        // Address phase (slave is in WRIDLE)
        @(posedge ap_clk);
        awvalid <= 1'b1; awaddr <= addr;
        do @(posedge ap_clk); while (!awready);
        awvalid <= 1'b0;
        // Data phase (slave moves to WRDATA after AW handshake)
        wvalid <= 1'b1; wdata <= data; wstrb <= 4'hF;
        do @(posedge ap_clk); while (!wready);
        wvalid <= 1'b0;
        // Response phase (slave moves to WRRESP after W handshake)
        bready <= 1'b1;
        do @(posedge ap_clk); while (!bvalid);
        bready <= 1'b0;
    endtask

    task automatic axil_read(input logic [5:0] addr, output logic [31:0] data);
        @(posedge ap_clk);
        arvalid <= 1'b1; araddr <= addr;
        rready  <= 1'b1;
        do @(posedge ap_clk); while (!arready);
        arvalid <= 1'b0;
        do @(posedge ap_clk); while (!rvalid);
        data = rdata;
        rready <= 1'b0;
    endtask

    // ---------- Register offsets (from HLS) ----------
    localparam logic [5:0] ADDR_AP_CTRL  = 6'h00;
    localparam logic [5:0] ADDR_IMG_W    = 6'h10;
    localparam logic [5:0] ADDR_IMG_H    = 6'h18;
    localparam logic [5:0] ADDR_THRESH   = 6'h20;

    int errors = 0;
    int passes = 0;

    task automatic check_rw(
        input logic [5:0]  addr,
        input logic [31:0] wval,
        input logic [31:0] mask,
        input string       label);
        logic [31:0] rval;
        axil_write(addr, wval);
        axil_read(addr, rval);
        if ((rval & mask) !== (wval & mask)) begin
            $display("  [FAIL] %s @ 0x%02h  wrote=0x%08h  read=0x%08h  mask=0x%08h",
                     label, addr, wval, rval, mask);
            errors++;
        end else begin
            $display("  [PASS] %s @ 0x%02h  val=0x%08h", label, addr, wval & mask);
            passes++;
        end
    endtask

    // ---------- Test patterns ----------
    logic [31:0] pat16 [8] = '{32'h0000, 32'hFFFF, 32'h5555, 32'hAAAA,
                               32'h0001, 32'h8000, 32'd2480, 32'd9792};
    logic [31:0] pat8  [7] = '{32'h00, 32'hFF, 32'h55, 32'hAA,
                               32'h80, 32'h7F, 32'h01};

    initial begin
        // Init
        awvalid=0; awaddr=0; wvalid=0; wdata=0; wstrb=0; bready=0;
        arvalid=0; araddr=0; rready=0;

        // Hold reset 10 cycles
        repeat (10) @(posedge ap_clk);
        ap_rst_n = 1;
        repeat (5) @(posedge ap_clk);

        $display("======================================================");
        $display("  Step 0 (sim) - binarize_core AXI-Lite check");
        $display("======================================================");

        // ---- 1. ap_idle high after reset (read block-level pin;
        //         this HLS slave does not expose AP_CTRL at offset 0x00) ----
        if (ap_idle === 1'b1) begin
            $display("  [PASS] ap_idle high after reset (block-level pin)");
            passes++;
        end else begin
            $display("  [FAIL] ap_idle low after reset (block-level pin = %b)", ap_idle);
            errors++;
        end

        // ---- 2. img_w (16-bit) ----
        $display("\n  -- img_w (16-bit) --");
        foreach (pat16[i])
            check_rw(ADDR_IMG_W, pat16[i], 32'h0000_FFFF, "img_w");

        // ---- 3. img_h (16-bit) ----
        $display("\n  -- img_h (16-bit) --");
        foreach (pat16[i])
            check_rw(ADDR_IMG_H, pat16[i], 32'h0000_FFFF, "img_h");

        // ---- 4. threshold (8-bit) ----
        $display("\n  -- threshold (8-bit) --");
        foreach (pat8[i])
            check_rw(ADDR_THRESH, pat8[i], 32'h0000_00FF, "threshold");

        // ---- 5. Stability — 100 reads of same value ----
        $display("\n  -- stability --");
        axil_write(ADDR_THRESH, 32'h5A);
        begin
            logic [31:0] r;
            int drift = 0;
            for (int i = 0; i < 100; i++) begin
                axil_read(ADDR_THRESH, r);
                if ((r & 32'hFF) !== 32'h5A) drift++;
            end
            if (drift == 0) begin
                $display("  [PASS] threshold stable across 100 reads");
                passes++;
            end else begin
                $display("  [FAIL] threshold drifted in %0d/100 reads", drift);
                errors++;
            end
        end

        // ---- 6. ap_idle still high (no spurious start) ----
        if (ap_idle === 1'b1) begin
            $display("  [PASS] ap_idle still high after all writes (block-level pin)");
            passes++;
        end else begin
            $display("  [FAIL] ap_idle dropped (block-level pin = %b)", ap_idle);
            errors++;
        end

        // ---- Summary ----
        $display("\n======================================================");
        $display("  SUMMARY: %0d pass, %0d fail", passes, errors);
        if (errors == 0)
            $display("  STEP 0 (sim) PASS");
        else
            $display("  STEP 0 (sim) FAIL");
        $display("======================================================");

        $finish;
    end

    // Watchdog
    initial begin
        #500_000;
        $display("  [FAIL] watchdog timeout");
        $finish;
    end

endmodule
