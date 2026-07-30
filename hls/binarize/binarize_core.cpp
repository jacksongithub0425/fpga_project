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
// Matches: cv2.GaussianBlur(gray, (3,3), 0) then
//          cv2.threshold(blur, threshold, 255, THRESH_BINARY_INV)
// Output pixel = 255 when blurred value <= threshold (dark lines on
// white schematic backgrounds become white in binary image).
//
// Manual 2-row line buffer (hls_video.h was removed in Vitis HLS 2021+).
// After 2-row fill latency: outputs one binary pixel per clock cycle.
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

            // 4. Compute Gaussian sum (only valid when window is fully filled)
            bpix_t out_px;
            out_px.keep = 0xFF;
            out_px.strb = 0xFF;
            out_px.last = (row == ih-1 && col == iw-1) ? 1 : 0;

            if (row >= 2 && col >= 2) {
                ap_uint<12> sum =
                    (ap_uint<12>)w0[0] + 2*(ap_uint<12>)w0[1] + (ap_uint<12>)w0[2] +
                    2*(ap_uint<12>)w1[0] + 4*(ap_uint<12>)w1[1] + 2*(ap_uint<12>)w1[2] +
                    (ap_uint<12>)w2[0] + 2*(ap_uint<12>)w2[1] + (ap_uint<12>)w2[2];

                ap_uint<8> blurred = (ap_uint<8>)(sum >> 4);  // ÷16
                out_px.data = (blurred <= threshold) ? (ap_uint<8>)255 : (ap_uint<8>)0;
            } else {
                out_px.data = 0;  // border pixels: output black
            }

            bin_out.write(out_px);
        }
    }
}
