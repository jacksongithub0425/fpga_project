#include "tme_top.h"

// -----------------------------------------------------------------------
// Template Matching Engine — top-level HLS function
//
// Computes TM_CCOEFF_NORMED between a search patch and a scaled template:
//
//   R(u,v) = Σ T'(x,y)·I(u+x,v+y) / sqrt( Σ T'² · Σ I(u+x,v+y)² )
//
// where T' = T - mean(T).  For binary images mean(T) ≈ constant, so
// T' is precomputed offline (embedded in the stream as signed bytes).
//
// Operating mode: max-loc-only.
// Returns the (score, x, y) of the best match.  The full result map
// is never stored — only the running maximum is tracked.
//
// AXI4-Stream inputs:
//   patch_stream  — search patch pixels, row-major, uint8
//   templ_stream  — template pixels (mean-subtracted, as int8 cast to uint8+128),
//                   followed by 4-byte trailer: {templ_mean_u8, templ_energy_hi,
//                   templ_energy_lo_hi, templ_energy_lo_lo} (big-endian float32)
//
// AXI4-Lite outputs (s_axilite):
//   result_score, result_x, result_y
// -----------------------------------------------------------------------

void tme_top(
    hls::stream<pix_stream_t>& patch_stream,
    hls::stream<pix_stream_t>& templ_stream,
    ap_uint<16> patch_w,
    ap_uint<16> patch_h,
    ap_uint<16> templ_w,
    ap_uint<16> templ_h,
    float&      result_score,
    ap_uint<16>& result_x,
    ap_uint<16>& result_y)
{
#pragma HLS INTERFACE axis      port=patch_stream
#pragma HLS INTERFACE axis      port=templ_stream
#pragma HLS INTERFACE s_axilite port=patch_w      bundle=CTRL
#pragma HLS INTERFACE s_axilite port=patch_h      bundle=CTRL
#pragma HLS INTERFACE s_axilite port=templ_w      bundle=CTRL
#pragma HLS INTERFACE s_axilite port=templ_h      bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result_score bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result_x     bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result_y     bundle=CTRL
#pragma HLS INTERFACE ap_ctrl_hs port=return

    // ---- Buffers --------------------------------------------------------
    static ap_uint<8> patch_buf[MAX_PATCH_H][MAX_PATCH_W];
    static ap_uint<8> templ_buf[MAX_TEMPL_H][MAX_TEMPL_W];
#pragma HLS BIND_STORAGE variable=patch_buf type=RAM_2P impl=BRAM
#pragma HLS BIND_STORAGE variable=templ_buf type=RAM_2P impl=BRAM
    // Column-dimension partition lets correlation_core read PAR_COLS
    // elements per cycle from the same row.
#pragma HLS ARRAY_PARTITION variable=patch_buf cyclic factor=PAR_COLS dim=2

    // Numerator accumulator (cross-correlation), one per output column.
    static acc_t num_acc[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=num_acc cyclic factor=PAR_COLS dim=1

    // Per-column sum-of-squares of patch pixels in the current window.
    static acc_t isq_col[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=isq_col cyclic factor=PAR_COLS dim=1

    int pw = (int)patch_w;
    int ph = (int)patch_h;
    int tw = (int)templ_w;
    int th = (int)templ_h;
    // Full valid-correlation dimensions.  The +1 matters: without it the
    // final possible row and column of every search are silently omitted and
    // pw == tw yields zero positions instead of one.  cv2.matchTemplate's
    // result is (pw-tw+1) x (ph-th+1), so the golden model already assumes
    // this.  correlation_core computes its own rw the same way — the two
    // must stay in lockstep (contract §4.4, option 1).
    int rw = pw - tw + 1;   // result map width
    int rh = ph - th + 1;   // result map height

    // ---- 1. Read patch into BRAM ----------------------------------------
    load_patch: for (int r = 0; r < ph; r++) {
        for (int c = 0; c < pw; c++) {
#pragma HLS PIPELINE II=1
            pix_stream_t px = patch_stream.read();
            patch_buf[r][c] = px.data;
        }
    }

    // ---- 2. Read template into BRAM + compute template energy -----------
    // Template is streamed as mean-subtracted int8 values stored as
    // (val + 128) so they fit in uint8.  Decode back here.
    acc_t templ_energy = 0;
    load_templ: for (int r = 0; r < th; r++) {
        for (int c = 0; c < tw; c++) {
#pragma HLS PIPELINE II=1
            pix_stream_t px = templ_stream.read();
            // Decode: stored as uint8 = actual_int8 + 128
            ap_int<9> val = (ap_int<9>)px.data - 128;
            templ_buf[r][c] = px.data;   // keep encoded form for MAC
            templ_energy += (acc_t)(val * val);
        }
    }

    // ---- 3. Sliding window correlation + normalization ------------------
    float best_score = -1.0f;
    ap_uint<16> best_x = 0, best_y = 0;

    slide_v: for (int v = 0; v < rh; v++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=MAX_RESULT_H avg=150

        // Reset column accumulators for this output row band
        reset_acc: for (int u = 0; u < rw; u++) {
#pragma HLS PIPELINE II=1
            num_acc[u] = 0;
            isq_col[u] = 0;
        }

        // Accumulate over template height rows
        accum_rows: for (int dy = 0; dy < th; dy++) {
#pragma HLS LOOP_TRIPCOUNT min=4 max=MAX_TEMPL_H avg=50
            int pr = v + dy;

            // Build mean-subtracted template row (decode from stored uint8+128)
            ap_uint<8> t_row[MAX_TEMPL_W];
#pragma HLS ARRAY_PARTITION variable=t_row complete dim=1
            for (int c = 0; c < tw; c++) {
#pragma HLS UNROLL
                t_row[c] = templ_buf[dy][c];
            }

            // Accumulate numerator (cross-correlation)
            correlation_core(patch_buf[pr], t_row, num_acc, patch_w, templ_w);

            // Accumulate I² for normalization denominator.
            // Incremental sliding-window: O(patch_w) per row instead of O(patch_w*templ_w).
            //   isq_win = sum_{x=0}^{tw-1} patch[pr][u+x]^2
            //   isq_win[u+1] = isq_win[u] + patch[pr][u+tw]^2 - patch[pr][u]^2
            acc_t isq_win = 0;
            isq_init: for (int x = 0; x < MAX_TEMPL_W; x++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=216 avg=80
                if (x >= tw) break;
                ap_uint<8> pv = patch_buf[pr][x];
                isq_win += (acc_t)(pv * pv);
            }
            if (rw > 0) isq_col[0] += isq_win;

            isq_slide: for (int u = 1; u < MAX_RESULT_W; u++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=808 avg=400
                if (u >= rw) break;
                ap_uint<8> pv_in  = patch_buf[pr][u + tw - 1];
                ap_uint<8> pv_out = patch_buf[pr][u - 1];
                isq_win = isq_win + (acc_t)(pv_in * pv_in)
                                  - (acc_t)(pv_out * pv_out);
                isq_col[u] += isq_win;
            }
        }

        // Compute normalized score for each output column
        norm_cols: for (int u = 0; u < rw; u++) {
#pragma HLS PIPELINE II=6
#pragma HLS LOOP_TRIPCOUNT min=1 max=MAX_RESULT_W avg=300
            fixed_t denom_sq = (fixed_t)templ_energy * (fixed_t)isq_col[u];
            fixed_t rsqrt_val = norm_rsqrt(denom_sq);
            fixed_t score_fx  = (fixed_t)num_acc[u] * rsqrt_val;

            // Clamp to [-1, 1] (floating-point representation for output)
            float score = (float)score_fx;
            if (score > 1.0f)  score = 1.0f;
            if (score < -1.0f) score = -1.0f;

            if (score > best_score) {
                best_score = score;
                best_x     = (ap_uint<16>)u;
                best_y     = (ap_uint<16>)v;
            }
        }
    }

    result_score = best_score;
    result_x     = best_x;
    result_y     = best_y;
}
