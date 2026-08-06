#include "tme_top.h"
#include <hls_math.h>

// -----------------------------------------------------------------------
// Template Matching Engine — top-level HLS function
//
// Computes TM_CCOEFF_NORMED between a search patch and a scaled template.
// cv2's definition, with T̄ the template mean and Ī the per-window mean:
//
//   R(u,v) = Σ (T−T̄)(I−Ī) / sqrt( Σ(T−T̄)² · Σ(I−Ī)² )
//
// Multiplying numerator and denominator by N = tw·th turns every term into
// an integer sum the hardware can accumulate exactly:
//
//   R(u,v) = (N·ΣTI − ΣT·ΣI) / sqrt( (N·ΣT² − (ΣT)²) · (N·ΣI² − (ΣI)²) )
//
// so the template streams as RAW uint8 pixels — no offline mean-subtraction,
// no int8 re-encoding (the previous int8+128 encoding wrapped for binary
// 0/255 templates, whose mean-subtracted range is ±255).  ΣT and ΣT² are
// computed during template load; ΣI and ΣI² ride the same sliding window
// that already existed for the old (mean-less, i.e. wrong) denominator.
// Float arithmetic enters once, at the final sqrt/divide — every sum
// feeding it is exact, and the sums fit their types by construction (see
// tme_top.h).
//
// Degenerate inputs (contract §4.6):
//   di == 0  flat search WINDOW — legal input, contractual score +0.0.
//            cv2 agrees: its zero denominator falls through the clamp
//            ladder to num = 0.
//   dt == 0  flat TEMPLATE — illegal input, rejected by host software
//            before the first DMA.  The zero below is a defensive fallback
//            that keeps a NaN out of result_score; it is NOT a contract
//            value, and it must not be read as matching cv2.  cv2 may return
//            ones or a patch-dependent numerical result, INCLUDING zero; no
//            contractual agreement exists on this illegal domain.  (Its
//            templNorm < DBL_EPSILON early return fills the map with ones,
//            but templNorm is a double-scaled variance, so a flat template
//            does not always reach that branch: a 7x7 of 2s computes
//            4.44e-16 > DBL_EPSILON and gets correlated like any other.)
//
// On dt > 0 && di > 0 this is the mathematical TM_CCOEFF_NORMED expression.
// cv2 evaluates it along a different numerical path (float64 integral
// images, plus a near-boundary clamp to ±1), so agreement with cv2 is
// tolerance-based, not bit-exact; the generator's high-precision integer
// oracle is what adjudicates a disagreement.
//
// Operating mode: max-loc-only.
// Returns the (score, x, y) of the best match.  The full result map
// is never stored — only the running maximum is tracked.  Ties keep the
// first (row-major) occurrence, same as cv2.minMaxLoc.
//
// AXI4-Stream inputs:
//   patch_stream  — search patch pixels, row-major, uint8
//   templ_stream  — template pixels, row-major, uint8, raw
//
// AXI4-Lite (bundle CTRL — §7.1.2: every s_axilite port, return included,
// names the same bundle):
//   patch_w/patch_h/templ_w/templ_h in, result_score/result_x/result_y out
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
#pragma HLS INTERFACE s_axilite port=templ_w     bundle=CTRL
#pragma HLS INTERFACE s_axilite port=templ_h     bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result_score bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result_x     bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result_y     bundle=CTRL
#pragma HLS INTERFACE s_axilite port=return       bundle=CTRL

    // ---- Buffers --------------------------------------------------------
    static ap_uint<8> patch_buf[MAX_PATCH_H][MAX_PATCH_W];
    static ap_uint<8> templ_buf[MAX_TEMPL_H][MAX_TEMPL_W];
#pragma HLS BIND_STORAGE variable=patch_buf type=RAM_2P impl=BRAM
#pragma HLS BIND_STORAGE variable=templ_buf type=RAM_2P impl=BRAM
    // Column-dimension partition lets correlation_core read PAR_COLS
    // elements per cycle from the same row.
#pragma HLS ARRAY_PARTITION variable=patch_buf cyclic factor=PAR_COLS dim=2

    // Per-output-column window sums, accumulated across template rows:
    // ΣTI (cross-correlation), ΣI² and ΣI (denominator/numerator terms).
    static sumsq_t sti_col[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=sti_col cyclic factor=PAR_COLS dim=1
    static sumsq_t sii_col[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=sii_col cyclic factor=PAR_COLS dim=1
    static sum_t si_col[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=si_col cyclic factor=PAR_COLS dim=1

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

    // ---- 2. Read template into BRAM + template-side sums ----------------
    sum_t   t_sum = 0;   // ΣT
    sumsq_t t_sq  = 0;   // ΣT²
    load_templ: for (int r = 0; r < th; r++) {
        for (int c = 0; c < tw; c++) {
#pragma HLS PIPELINE II=1
            pix_stream_t px = templ_stream.read();
            ap_uint<8> tv = px.data;
            templ_buf[r][c] = tv;
            t_sum += tv;
            t_sq  += (sumsq_t)(tv * tv);
        }
    }

    ap_uint<16> n_px = (ap_uint<16>)(tw * th);   // N ≤ 20736

    // Template variance term N·ΣT² − (ΣT)², constant for the whole search.
    // ap_uint products widen automatically (16×32→48, 24×24→48); the
    // difference is ≥ 0 by the variance inequality.
    //
    // dt itself needs only 43 bits (max 6,989,889,945,600 at N = 20,736,
    // half 0 / half 255), while BOTH intermediates reach 27,959,559,782,400 —
    // 45 bits — on an all-255 template, where they are equal and cancel.
    // 48 bits holds each intermediate outright.  That is a preservation
    // POLICY, not a correctness floor: fixed-width subtraction is modular, so
    // equal truncation of both operands still cancels, and the joint minimum
    // is (wide_t, num_t) = (44u, 44s) — the result, not the operands, is what
    // must fit.  Resize both together or not at all: with wide_t under 45, a
    // num_t of any OTHER width breaks `num` below while leaving dt correct.
    // Contract §4.6.
    wide_t dt   = (wide_t)(n_px * t_sq) - (wide_t)(t_sum * t_sum);
    float  dt_f = (float)dt;

    // ---- 3. Sliding window correlation + normalization ------------------
    // Start below any reachable score so the first window always wins the
    // initial comparison — best-so-far semantics identical to cv2.minMaxLoc.
    float best_score = -2.0f;
    ap_uint<16> best_x = 0, best_y = 0;

    slide_v: for (int v = 0; v < rh; v++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=MAX_RESULT_H avg=150

        // Reset column accumulators for this output row band
        reset_acc: for (int u = 0; u < rw; u++) {
#pragma HLS PIPELINE II=1
            sti_col[u] = 0;
            sii_col[u] = 0;
            si_col[u]  = 0;
        }

        // Accumulate over template height rows
        accum_rows: for (int dy = 0; dy < th; dy++) {
#pragma HLS LOOP_TRIPCOUNT min=4 max=MAX_TEMPL_H avg=50
            int pr = v + dy;

            // Stage template row into registers for correlation_core
            ap_uint<8> t_row[MAX_TEMPL_W];
#pragma HLS ARRAY_PARTITION variable=t_row complete dim=1
            for (int c = 0; c < tw; c++) {
#pragma HLS UNROLL
                t_row[c] = templ_buf[dy][c];
            }

            // Accumulate ΣTI (cross-correlation numerator sum)
            correlation_core(patch_buf[pr], t_row, sti_col, patch_w, templ_w);

            // Accumulate ΣI² and ΣI for the window statistics.
            // Incremental sliding-window: O(patch_w) per row instead of
            // O(patch_w*templ_w).  Row-window bounds: ΣI² ≤ 216·255² <
            // 2^24, ΣI ≤ 216·255 < 2^16 — one spare bit each.
            //   win[u+1] = win[u] + f(patch[pr][u+tw]) - f(patch[pr][u])
            ap_uint<25> rsq_win = 0;
            ap_uint<17> rs_win  = 0;
            isq_init: for (int x = 0; x < MAX_TEMPL_W; x++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=216 avg=80
                if (x >= tw) break;
                ap_uint<8> pv = patch_buf[pr][x];
                rsq_win += (ap_uint<25>)(pv * pv);
                rs_win  += pv;
            }
            if (rw > 0) {
                sii_col[0] += rsq_win;
                si_col[0]  += rs_win;
            }

            isq_slide: for (int u = 1; u < MAX_RESULT_W; u++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=808 avg=400
                if (u >= rw) break;
                ap_uint<8> pv_in  = patch_buf[pr][u + tw - 1];
                ap_uint<8> pv_out = patch_buf[pr][u - 1];
                rsq_win = rsq_win + (ap_uint<25>)(pv_in * pv_in)
                                  - (ap_uint<25>)(pv_out * pv_out);
                rs_win  = rs_win + pv_in - pv_out;
                sii_col[u] += rsq_win;
                si_col[u]  += rs_win;
            }
        }

        // Compute normalized score for each output column
        norm_cols: for (int u = 0; u < rw; u++) {
#pragma HLS PIPELINE II=4
#pragma HLS LOOP_TRIPCOUNT min=1 max=MAX_RESULT_W avg=300
            sumsq_t sti = sti_col[u];
            sumsq_t sii = sii_col[u];
            sum_t   si  = si_col[u];

            // Exact integer numerator and window variance term.
            num_t  num = (num_t)(wide_t)(n_px * sti) - (num_t)(wide_t)(si * t_sum);
            wide_t di  = (wide_t)(n_px * sii) - (wide_t)(si * si);

            float score;
            if (di == 0 || dt == 0) {
                // di == 0: flat window — legal, and +0.0 is the contract
                //          value (§4.6).  cv2 returns 0 here too.
                // dt == 0: flat template — ILLEGAL input that host software
                //          must have rejected before the first DMA.  This
                //          zero only keeps 0/0 out of result_score.  cv2 may
                //          return ones or a patch-dependent value, including
                //          zero; a match with this 0.0f would be coincidence,
                //          not agreement (§4.6).
                score = 0.0f;
            } else {
                score = (float)num / hls::sqrtf(dt_f * (float)di);
            }

            // Clamp float rounding to the mathematical range
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
