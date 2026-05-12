#include "patch_extract_core.h"

// -----------------------------------------------------------------------
// patch_extract_core
//
// Reads candidate descriptors from the PS (via AXI4-Stream DMA), computes
// patch boundaries using the same formula as build_endpoint_patch() in
// terminal_counter_endpoint_first.py (line 502), then streams the patch
// pixels read from the binary image in PS DDR3 to template_match_core.
//
// Patch boundary formula (must stay in sync with Python):
//   outward_w = max_templ_w * 2.4
//   inward_w  = max_templ_w * 1.4
//   patch_h   = max_templ_h * 3.2
//   if side==LEFT:  x0 = ep_x - outward_w,  x1 = ep_x + inward_w
//   if side==RIGHT: x0 = ep_x - inward_w,   x1 = ep_x + outward_w
//   y0 = ep_y - patch_h/2,  y1 = ep_y + patch_h/2
//   Clamp all to [0, img_w/img_h]
//
// Memory model: the binary image lives in PS DDR3, written by binarize_core
// (and optionally text-suppressed by the PS driver). This core accesses it
// via an AXI4-Full master port (m_axi), wired to S_AXI_HP1 in the block
// design. The base physical address is loaded by the PS into the
// auto-generated CTRL register `bin_image` before raising START.
//
// Burst behaviour: the inner read loop walks contiguous addresses within
// one image row, which HLS recognises as a single burst per row. The patch
// is at most PE_MAX_PATCH_W bytes wide, so each row is one burst no longer
// than PE_MAX_PATCH_W cycles. Outstanding-read depth lets the next row's
// burst be issued while the previous one is still streaming.
// -----------------------------------------------------------------------

void patch_extract_core(
    hls::stream<cand_stream_t>& cand_in,
    hls::stream<ppix_stream_t>& patch_out,
    const ap_uint<8>* bin_image,
    ap_uint<16> img_w,
    ap_uint<16> img_h)
{
#pragma HLS INTERFACE axis        port=cand_in
#pragma HLS INTERFACE axis        port=patch_out
#pragma HLS INTERFACE m_axi       port=bin_image offset=slave bundle=DDR \
                                  depth=PE_MAX_IMG_BYTES \
                                  max_read_burst_length=256 \
                                  num_read_outstanding=4
#pragma HLS INTERFACE s_axilite   port=bin_image bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=img_w     bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=img_h     bundle=CTRL
#pragma HLS INTERFACE ap_ctrl_hs  port=return

    int iw = (int)img_w;
    int ih = (int)img_h;

    // Process candidates one at a time until stream is exhausted
    process_cands: while (!cand_in.empty() || true) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=64 avg=30

        cand_stream_t cand = cand_in.read();

        // Decode packed candidate
        ap_uint<16> ep_x      = cand.data.range(15,  0);
        ap_uint<16> ep_y      = cand.data.range(31, 16);
        ap_uint<2>  side      = cand.data.range(33, 32);  // 0=left, 1=right
        ap_uint<14> max_tw    = cand.data.range(47, 34);
        ap_uint<16> max_th    = cand.data.range(63, 48);

        // Compute patch boundaries (fixed-point multiply by 2.4, 1.4, 3.2)
        // Use Q8.8 arithmetic: multiply integer by scaled integer, then shift
        int outward_w = ((int)max_tw * 307) >> 7;   // ×2.4 ≈ ×307/128
        int inward_w  = ((int)max_tw * 179) >> 7;   // ×1.4 ≈ ×179/128
        int patch_h   = ((int)max_th * 410) >> 7;   // ×3.2 ≈ ×410/128

        int x0, x1;
        if (side == 0) {   // left
            x0 = (int)ep_x - outward_w;
            x1 = (int)ep_x + inward_w;
        } else {           // right
            x0 = (int)ep_x - inward_w;
            x1 = (int)ep_x + outward_w;
        }
        int y0 = (int)ep_y - patch_h / 2;
        int y1 = (int)ep_y + patch_h / 2;

        // Clamp to image bounds
        if (x0 < 0)  x0 = 0;
        if (y0 < 0)  y0 = 0;
        if (x1 > iw) x1 = iw;
        if (y1 > ih) y1 = ih;
        if (x1 <= x0 + 1) x1 = x0 + 2;
        if (y1 <= y0 + 1) y1 = y0 + 2;

        int actual_pw = x1 - x0;
        int actual_ph = y1 - y0;

        // Stream patch pixels row by row from DDR3 to template_match_core.
        // row_base is computed once per row so the inner loop is a pure
        // linear address increment - HLS infers an AXI4 burst from this.
        // LOOP_FLATTEN off: without this, HLS collapses (r,c) into a single
        // 81-bit flat counter (because pw*ph fits up to ~327k but the
        // counter is widened defensively), and the 81-bit add+icmp chain
        // breaks timing badly (Fmax drops to ~99 MHz). Keeping the loops
        // nested keeps the inner counter narrow.
        stream_patch_rows: for (int r = 0; r < actual_ph; r++) {
#pragma HLS LOOP_FLATTEN off
#pragma HLS LOOP_TRIPCOUNT min=1 max=PE_MAX_PATCH_H avg=200
            ap_uint<32> row_base = (ap_uint<32>)(y0 + r) * (ap_uint<32>)iw
                                 + (ap_uint<32>)x0;
            stream_patch_cols: for (int c = 0; c < actual_pw; c++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=PE_MAX_PATCH_W avg=400
                ppix_stream_t out_px;
                out_px.data = bin_image[row_base + c];
                out_px.last = (r == actual_ph-1 && c == actual_pw-1 && cand.last) ? 1 : 0;
                out_px.keep = 0xFF;
                patch_out.write(out_px);
            }
        }

        // Exit when this was the last candidate
        if (cand.last) break;
    }
}
