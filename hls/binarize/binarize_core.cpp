#include "binarize_core.h"

// -----------------------------------------------------------------------
// binarize_core
//
// Streaming 3×3 Gaussian blur + THRESH_BINARY_INV threshold.
//
// Gaussian kernel (integer, sum=16):
//   1 2 1
//   2 4 2
//   1 2 1
//
// The Gaussian division is truncating (sum >> 4), so this is intentionally
// not bit-exact with cv2.GaussianBlur(), which rounds.
// Output pixel = 255 when blurred value <= threshold (dark lines on
// white schematic backgrounds become white in binary image).
//
// Manual 2-row line buffer (hls_video.h was removed in Vitis HLS 2021+).
// After 2-row fill latency it produces one raw binary pixel per clock.
//
// COMPACT DDR LAYOUT BOUNDARY
// ---------------------------
// The result computed at raw stream coordinate (r,c) belongs at logical
// coordinate (r-1,c-1). bin_out feeds an unchanged simple-mode S2MM channel,
// so output beat order performs the boundary transform:
//
//   * consume raw row 0 without output;
//   * discard raw column 0;
//   * emit raw (r,c), r>=1,c>=1, as logical (r-1,c-1);
//   * emit one zero suffix after each mapped logical row; and
//   * append the mandatory all-zero final logical row.
//
// The result is exactly img_w*img_h beats in compact logical row-major order.
// TLAST is asserted only on the last beat. Padded stride is intentionally not
// represented by this interface: simple-mode S2MM stores a compact raster.
// -----------------------------------------------------------------------

void binarize_core(
    hls::stream<bpix_t>& gray_in,
    hls::stream<bpix_t>& bin_out,
    ap_uint<16> img_w,
    ap_uint<16> img_h,
    ap_uint<8>  threshold)
{
#pragma HLS INTERFACE axis       port=gray_in
#pragma HLS INTERFACE axis       port=bin_out
#pragma HLS INTERFACE s_axilite  port=img_w      bundle=CTRL
#pragma HLS INTERFACE s_axilite  port=img_h      bundle=CTRL
#pragma HLS INTERFACE s_axilite  port=threshold  bundle=CTRL
#pragma HLS INTERFACE mode=s_axilite port=return bundle=CTRL

    // Two stored rows (oldest and previous).
    // The current row is consumed live from gray_in.
    static ap_uint<8> line0[BINARIZE_MAX_W];   // two rows ago
    static ap_uint<8> line1[BINARIZE_MAX_W];   // one row ago
#pragma HLS BIND_STORAGE variable=line0 type=RAM_2P impl=BRAM
#pragma HLS BIND_STORAGE variable=line1 type=RAM_2P impl=BRAM

    // Column shift registers for the 3×3 window (3 columns × 3 rows).
    // Fully partitioned → pure registers, zero latency on window access.
    ap_uint<8> w0[3], w1[3], w2[3];  // w0=oldest row, w2=current row
#pragma HLS ARRAY_PARTITION variable=w0 complete dim=1
#pragma HLS ARRAY_PARTITION variable=w1 complete dim=1
#pragma HLS ARRAY_PARTITION variable=w2 complete dim=1

    int iw = (int)img_w;
    int ih = (int)img_h;

    // Tripcount avg = benchmark page 9792×6336, max = BINARIZE_MAX_W/H.
    // LOOP_FLATTEN off: flattened col-wrap logic (col==iw → reset mux)
    // was the 6.179 ns critical path; per-row refill overhead is ~2 cycles
    // per 9792-pixel row, negligible.
    main_loop: for (int row = 0; row < ih; row++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=6400 avg=6336
        col_loop: for (int col = 0; col < iw; col++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_FLATTEN off
#pragma HLS LOOP_TRIPCOUNT min=1 max=9856 avg=9792

            // 1. Read new pixel from stream
            bpix_t in_px = gray_in.read();
            ap_uint<8> cur = in_px.data;

            // 2. Shift window columns left, insert new column on the right
            w0[0] = w0[1]; w0[1] = w0[2]; w0[2] = line0[col];
            w1[0] = w1[1]; w1[1] = w1[2]; w1[2] = line1[col];
            w2[0] = w2[1]; w2[1] = w2[2]; w2[2] = cur;

            // 3. Update line buffers (write current pixel into older row slots)
            line0[col] = line1[col];
            line1[col] = cur;

            // 4. Compute the raw Gaussian/threshold result. The output
            //    scheduler below places it at logical (row-1,col-1).
            ap_uint<8> raw_binary;

            if (row >= 2 && col >= 2) {
                ap_uint<12> sum =
                    (ap_uint<12>)w0[0] + 2*(ap_uint<12>)w0[1] + (ap_uint<12>)w0[2] +
                    2*(ap_uint<12>)w1[0] + 4*(ap_uint<12>)w1[1] + 2*(ap_uint<12>)w1[2] +
                    (ap_uint<12>)w2[0] + 2*(ap_uint<12>)w2[1] + (ap_uint<12>)w2[2];

                ap_uint<8> blurred = (ap_uint<8>)(sum >> 4);  // ÷16
                raw_binary = (blurred <= threshold) ? (ap_uint<8>)255 : (ap_uint<8>)0;
            } else {
                raw_binary = 0;  // pipeline fill: no complete 3×3 window
            }

            // 5. Discard raw row/column 0. Every remaining result maps to
            //    logical (row-1,col-1) and is already in logical row-major
            //    order. There is at most one output write per input beat.
            if (row >= 1 && col >= 1) {
                bpix_t out_px;
                out_px.data = raw_binary;
                out_px.keep = 1;
                out_px.strb = 1;
                out_px.last = 0;
                out_px.user = 0;
                out_px.id   = 0;
                out_px.dest = 0;
                bin_out.write(out_px);
            }
        }

        // Raw rows 1..img_h-1 map to logical rows 0..img_h-2. Finish
        // each mapped row with its mandatory zero final-column pixel. This
        // sits outside the II=1 input loop, avoiding a second conditional
        // stream action in any pipelined input iteration.
        if (row >= 1) {
            bpix_t suffix_px;
            suffix_px.data = 0;
            suffix_px.keep = 1;
            suffix_px.strb = 1;
            suffix_px.last = 0;
            suffix_px.user = 0;
            suffix_px.id   = 0;
            suffix_px.dest = 0;
            bin_out.write(suffix_px);
        }
    }

    // Logical row img_h-1 is never generated by the raw 3×3 pipeline and
    // must be overwritten with zeros. TLAST belongs only to its last pixel.
    bpix_t zero_px;
    zero_px.data = 0;
    zero_px.keep = 1;
    zero_px.strb = 1;
    zero_px.last = 0;
    zero_px.user = 0;
    zero_px.id   = 0;
    zero_px.dest = 0;
    final_row: for (int col = 0; col < iw; col++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=3 max=9856 avg=9792
        zero_px.last = (col == iw-1) ? 1 : 0;
        bin_out.write(zero_px);
    }
}
