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
// so the template streams as RAW uint8 — no offline mean-subtraction,
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
//
// =======================================================================
// PRIORITY 6 (B0b) — SHADOW MODE.  THIS FILE IS NOT THE SHIPPED CORE.
// =======================================================================
// It is the `shadow` snapshot of the A/B build described in
// b0b_sources/README.md.  It is `b2` PLUS a second, independent computation
// of the window statistics, run alongside the existing one and compared at
// every result position.  NOTHING IS REMOVED HERE.  The score still comes
// from the original si_col/sii_col, so on agreement this core's output is
// bit-identical to `b2` on every input.
//
// WHAT B0b CHANGES, AND WHY A SHADOW COMES FIRST
// ----------------------------------------------
// The shipped core recomputes ΣI and ΣI² for EVERY (output row v, template
// row dy) pair: `isq_init` scans tw pixels, `isq_slide` scans rw-1 more, and
// that happens rh*th times.  The window over rows [v, v+th) and the window
// over rows [v+1, v+1+th) share th-1 rows, so almost all of that work is
// repeated.  B0b hoists it: maintain si_col/sii_col ACROSS v, and at each new
// output row subtract the row that leaves the window and add the row that
// enters it.
//
//     v == 0     scan rows 0 .. th-1                   th scans
//     v > 0      scan row v-1 (SUB), scan row v+th-1 (ADD)   2 scans
//
// One scan costs tw + (rw - 1) = pw iterations, so the whole pass is
//
//     I = pw * [th + 2*(rh - 1)]  ==  pw * (2*ph - th)
//
// which is the iteration count sw/tme_cycle_model.py already derives
// (`b0b_count_pass_iterations`).  What that model does NOT know is the
// achieved initiation interval or the per-scan overhead; it brackets the II
// at [1, 3] and says so.  Measuring it is what this file is for.
//
// WHY THE SUBTRACT AND ADD ARE SEPARATE SCANS.  Fusing them into one loop
// over u would cost pw*ph iterations instead of pw*(2*ph - th) — fewer.  But
// a fused body reads FOUR patch pixels per iteration: the outgoing and
// incoming column of the outgoing row and of the incoming row.  patch_buf is
// RAM_2P, cyclic-partitioned by PAR_COLS on the column dimension, so the two
// reads at column u-1 land in the SAME bank and the two at u+tw-1 land in
// another same bank — two reads per bank, which is exactly the port budget,
// UNLESS tw is a multiple of PAR_COLS, in which case all four collide on two
// banks and the loop cannot hold II=1.  The separate scans read two pixels
// per iteration and are structurally the loop that already achieves II=1 in
// the shipped core.  The fused form is a real candidate for a later variant;
// it is not free, and it is not what the frozen endpoints describe.
//
// COUNTS OR SUMS?  The priority list calls this the "foreground count" pass,
// which is what it would be if the patch were guaranteed binary: for pixels
// in {0, 255}, ΣI = 255·C and ΣI² = 255²·C, so one counter would serve both.
// THE CONTRACT DOES NOT GUARANTEE THAT.  §4.1 bounds the geometry and says
// nothing about pixel VALUES, and the vector suites deliberately contain
// grayscale patches — `stress-max-result` is grayscale "by necessity" (a 4x4
// binary window recurs within a few hundred of its 248,368 positions, so
// there would be no unique peak to assert), and it is one of the two cases
// that reach the 817x304 map and T = 52 on silicon.  A count-only core would
// be an ABI narrowing and would invalidate those vectors.  So this pass
// carries the general ΣI and ΣI², exactly the quantities the shipped loops
// carry.  The iteration count is identical either way — the model's I does
// not depend on which statistic rides the scan — so the count-only
// specialisation remains available as a later, cheaper-per-iteration variant
// if the measured II makes it worth an ABI change.  It is not assumed here.
//
// UNDERFLOW IS STRUCTURALLY IMPOSSIBLE, AND THAT IS A CHOICE.  The outgoing
// row is subtracted BEFORE the incoming row is added, so the intermediate
// value of every accumulator is the exact sum over the th-1 rows that stay
// in the window — non-negative, and smaller than either endpoint.  Reversing
// the order would leave a th+1-row intermediate: still inside sum_t and
// sumsq_t (97*216*255 = 5,342,760 < 2^23 and 97*216*255² = 1,362,403,800 <
// 2^31), so it would not wrap either, but it would rely on a width argument
// instead of on a sign argument.  Do not reorder these two calls.
//
// HOW A DISAGREEMENT IS REPORTED
// ------------------------------
// `norm_cols` compares the shadow accumulators against the shipped ones at
// every u < rw of every v, and a single mismatch anywhere sets a sticky flag
// that replaces the result with the sentinel
//
//     result_score = -3.0f,  result_x = result_y = 0xFFFF
//
// -3.0f is unreachable by construction: best_score starts at -2.0f and every
// written score is clamped to [-1, +1].  So the sentinel needs no new port —
// it travels the existing AXI4-Lite result registers, and EVERY existing
// consumer already fails on it: the testbench compares score bits and exact
// location, the RTL co-simulation compares against the same goldens, and the
// board runner's tolerance check rejects it.  A shadow build that passes the
// suites is a shadow build in which the two computations agreed at every one
// of the positions the suites visit.
//
// The csim-only counter below is the other half of that claim.  "No mismatch"
// is also what a comparison that never ran would report, so the number of
// positions actually compared is printed per invocation and must be rh*rw.
// -----------------------------------------------------------------------

// Scan modes for window_row_scan.  SET overwrites the accumulators (used for
// the first row of the v == 0 initialisation, so no separate zeroing pass is
// needed and none is modelled); ADD and SUB move a row into or out of the
// vertical window.
static const int B0B_SET = 0;
static const int B0B_ADD = 1;
static const int B0B_SUB = 2;

// One horizontal rolling scan of one patch row, applied to the per-column
// window accumulators.
//
// This is deliberately the SAME loop structure as the shipped isq_init /
// isq_slide pair — a tw-deep priming loop and an (rw-1)-deep slide, both at
// II=1, both reading two pixels of one row per iteration.  Keeping the shape
// identical is what makes the paired co-simulation attributable: the only
// thing B0b moves is HOW OFTEN this runs, not what one run costs per pixel.
//
// INLINE off, so the three call sites share one instance rather than
// replicating the multipliers three times, and so csynth reports this pass's
// II and latency as its own entry instead of folding it into slide_v.
static void window_row_scan(
    ap_uint<8>  patch_line[MAX_PATCH_W],
    sumsq_t     sii[MAX_RESULT_W],
    sum_t       si[MAX_RESULT_W],
    int         tw,
    int         rw,
    int         mode)
{
#pragma HLS INLINE off

    // Row-window bounds, unchanged from the shipped loops: ΣI² ≤ 216·255² <
    // 2^24, ΣI ≤ 216·255 < 2^16 — one spare bit each.
    ap_uint<25> rsq_win = 0;
    ap_uint<17> rs_win  = 0;

    scan_init: for (int x = 0; x < MAX_TEMPL_W; x++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=216 avg=80
        if (x >= tw) break;
        ap_uint<8> pv = patch_line[x];
        rsq_win += (ap_uint<25>)(pv * pv);
        rs_win  += pv;
    }

    if (rw > 0) {
        sumsq_t q0 = (mode == B0B_SET) ? (sumsq_t)0 : sii[0];
        sum_t   s0 = (mode == B0B_SET) ? (sum_t)0   : si[0];
        sii[0] = (mode == B0B_SUB) ? (sumsq_t)(q0 - rsq_win)
                                   : (sumsq_t)(q0 + rsq_win);
        si[0]  = (mode == B0B_SUB) ? (sum_t)(s0 - rs_win)
                                   : (sum_t)(s0 + rs_win);
    }

    scan_slide: for (int u = 1; u < MAX_RESULT_W; u++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=816 avg=400
        if (u >= rw) break;
        ap_uint<8> pv_in  = patch_line[u + tw - 1];
        ap_uint<8> pv_out = patch_line[u - 1];
        rsq_win = rsq_win + (ap_uint<25>)(pv_in * pv_in)
                          - (ap_uint<25>)(pv_out * pv_out);
        rs_win  = rs_win + pv_in - pv_out;

        sumsq_t q = (mode == B0B_SET) ? (sumsq_t)0 : sii[u];
        sum_t   s = (mode == B0B_SET) ? (sum_t)0   : si[u];
        sii[u] = (mode == B0B_SUB) ? (sumsq_t)(q - rsq_win)
                                   : (sumsq_t)(q + rsq_win);
        si[u]  = (mode == B0B_SUB) ? (sum_t)(s - rs_win)
                                   : (sum_t)(s + rs_win);
    }
}

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

    // B0b SHADOW: the same two quantities, produced by the hoisted pass.
    // Same declaration and same partitioning as the arrays they shadow, so
    // that a difference between the two paths cannot come from a difference
    // in storage.  These disappear in the real B0b, where the hoisted pass
    // writes sii_col/si_col directly and the loops below are deleted.
    static sumsq_t sii_shadow[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=sii_shadow cyclic factor=PAR_COLS dim=1
    static sum_t si_shadow[MAX_RESULT_W];
#pragma HLS ARRAY_PARTITION variable=si_shadow cyclic factor=PAR_COLS dim=1

    // Sticky: set by any disagreement at any result position, cleared only by
    // entering the function.  It is deliberately NOT reset per output row.
    bool shadow_bad = false;

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

#ifndef __SYNTHESIS__
    // Positive evidence that the comparison ran.  "No mismatch" is also what
    // a comparison that never executed would report, so csim counts the
    // positions compared and the testbench-visible line below must show
    // rh*rw.  Synthesis never sees this.
    long shadow_compared = 0;
    long shadow_mismatch = 0;
#endif

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

        // ---- B0b SHADOW PASS -------------------------------------------
        // The hoisted, vertically-reused window statistics.  Runs once per
        // output row; the shipped loops inside accum_rows still run th times
        // per output row and are what feeds the score.
        //
        // The shadow accumulators are NOT reset above.  At v == 0 the first
        // scan is a SET, which overwrites every u < rw and is what makes the
        // pass independent of whatever the previous invocation left in these
        // static arrays.  Entries at u >= rw stay stale and are never read,
        // exactly as sii_col/si_col's are.
        if (v == 0) {
            b0b_init_rows: for (int dy = 0; dy < th; dy++) {
#pragma HLS LOOP_TRIPCOUNT min=4 max=MAX_TEMPL_H avg=50
                window_row_scan(patch_buf[dy], sii_shadow, si_shadow,
                                tw, rw, dy == 0 ? B0B_SET : B0B_ADD);
            }
        } else {
            // SUB BEFORE ADD.  See the header note: this order makes every
            // intermediate the exact th-1-row sum, so no accumulator can
            // ever go negative and the modular-width argument is never
            // needed.  Reversing them would be correct but for a weaker
            // reason.
            window_row_scan(patch_buf[v - 1], sii_shadow, si_shadow,
                            tw, rw, B0B_SUB);
            window_row_scan(patch_buf[v + th - 1], sii_shadow, si_shadow,
                            tw, rw, B0B_ADD);
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

            // ---- B0b SHADOW COMPARISON ---------------------------------
            // Every result position, both statistics, exact equality.  The
            // score below is computed from the SHIPPED values, so this core
            // is bit-identical to b2 whenever the two agree.
            if (sii_shadow[u] != sii || si_shadow[u] != si) {
                shadow_bad = true;
#ifndef __SYNTHESIS__
                if (shadow_mismatch < 8) {
                    printf("  [b0b-shadow] MISMATCH v=%d u=%d  "
                           "sii %lu vs %lu   si %lu vs %lu\n",
                           v, u,
                           (unsigned long)sii,
                           (unsigned long)sii_shadow[u],
                           (unsigned long)si,
                           (unsigned long)si_shadow[u]);
                }
                shadow_mismatch++;
#endif
            }
#ifndef __SYNTHESIS__
            shadow_compared++;
#endif

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

#ifndef __SYNTHESIS__
    printf("  [b0b-shadow] compared %ld of %ld result positions, "
           "%ld mismatch(es)  %s\n",
           shadow_compared, (long)rh * (long)rw, shadow_mismatch,
           (shadow_compared == (long)rh * (long)rw && shadow_mismatch == 0)
               ? "OK" : "**FAIL**");
#endif

    // The sentinel.  -3.0f is unreachable: best_score starts at -2.0f and
    // every score written to it has been clamped into [-1, +1].  So this
    // needs no new port and no driver change — the existing score/location
    // checks in tme_tb.cpp, in RTL co-simulation and on the board all reject
    // it.  On agreement the outputs are bit-identical to b2.
    if (shadow_bad) {
        result_score = -3.0f;
        result_x     = (ap_uint<16>)0xFFFF;
        result_y     = (ap_uint<16>)0xFFFF;
    } else {
        result_score = best_score;
        result_x     = best_x;
        result_y     = best_y;
    }
}
