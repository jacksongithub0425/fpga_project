#pragma once
#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

// -----------------------------------------------------------------------
// Compile-time limits — derived from actual template sizes at scale 1.5×
//   Largest template: ferrule_right_02.png = 141×58 px
//   At scale 1.50 → 212×87 px
//   Patch: outward_w = 212×2.4 = 509, inward_w = 212×1.4 = 297 → width = 806
//          patch_h   = 87×3.2  = 278
// -----------------------------------------------------------------------
// MAX_PATCH is the EXACT reachable envelope, not a round number.  Contract
// §4.1 caps max_tw at 216 and max_th at 96, and patch_extract_core's integer
// geometry then bounds a patch at exactly:
//     w = (2*216 + floor(2*216/5)) + (216 + floor(2*216/5)) = 518 + 302 = 820
//     h =  3*96  + floor(96/5)                              = 288 +  19 = 307
// Every column past 820 and row past 307 is storage the extractor cannot
// address.  This is not a micro-optimisation: patch_buf is 16 cyclic banks
// (PAR_COLS), and 820x307 puts each bank at ~15,964 words, just under the
// 16,384 power-of-two depth step.  At the previous 1024x320 each bank held
// 20,480 words, rounded up to 32,768, costing 16 BRAM18K per bank instead of
// 8 — 352 BRAM18K total against 280 available on the xc7z020, i.e. not
// implementable at all.  At 820x307 the design uses 224 (80%).
// The saving is a cliff, not a slope: any intermediate value still above
// 16,384 words per bank saves nothing.  Do not "round these up for safety" —
// see hls/template_match/ab_bram/ for the retained A/B synthesis.
static const int MAX_PATCH_W  = 820;    // row width of search region
static const int MAX_PATCH_H  = 307;    // rows in search region
static const int MAX_TEMPL_W  = 216;    // template width (post-scale)
static const int MAX_TEMPL_H  = 96;     // template height (post-scale)
// Worst-case result map: full valid correlation is (pw - tw + 1) columns by
// (ph - th + 1) rows, and the smallest legal template is 4x4 (contract §4.1).
static const int MAX_RESULT_W = MAX_PATCH_W - 4 + 1;   // 817
static const int MAX_RESULT_H = MAX_PATCH_H - 4 + 1;   // 304

// Parallelism: inner x-loop unroll factor inside the pipelined u-loop.
// 16 parallel MACs per cycle; effective II ≈ ceil(MAX_TEMPL_W/16) = 14.
static const int PAR_COLS = 16;

// -----------------------------------------------------------------------
// Integer sum types.  All sums are over one template-sized window, so the
// bounds come from the 216×96 = 20,736-pixel envelope with 8-bit pixels:
//
//   sum_t    Σ px           ≤ 20736·255  =      5,287,680  → 23 bits
//   sumsq_t  Σ px², Σ T·I   ≤ 20736·255² =  1,348,358,400  → 31 bits
//   wide_t   N·sumsq, sum²  ≤ 20736·1.349e9 ≈    2.797e13  → 45 bits
//
// num = N·ΣTI − ΣT·ΣI is a signed difference of two wide_t values.
// The previous types here were ap_fixed<48,24> accumulators and a Q16.16
// normalisation path; both WRAP at these magnitudes (8.4e6 and 32768
// integer ceilings respectively).  They only ever passed csim because the
// sole golden case was an all-zero patch.  Integer sums are exact by
// construction; float enters once, at the final sqrt/divide.
// -----------------------------------------------------------------------
typedef ap_uint<24> sum_t;
typedef ap_uint<32> sumsq_t;
typedef ap_uint<48> wide_t;
typedef ap_int<48>  num_t;

// AXI4-Stream pixel type (8-bit, no sideband data needed for image streams)
typedef ap_axiu<8, 1, 1, 1>  pix_stream_t;

// -----------------------------------------------------------------------
// Top-level function declarations
// -----------------------------------------------------------------------

void correlation_core(
    ap_uint<8> patch_line[MAX_PATCH_W],
    ap_uint<8> templ_row[MAX_TEMPL_W],
    sumsq_t    acc[MAX_RESULT_W],
    ap_uint<16> patch_w,
    ap_uint<16> templ_w
);

void tme_top(
    hls::stream<pix_stream_t>& patch_stream,   // image patch, row by row
    hls::stream<pix_stream_t>& templ_stream,   // RAW template pixels, row by row
    ap_uint<16> patch_w,
    ap_uint<16> patch_h,
    ap_uint<16> templ_w,
    ap_uint<16> templ_h,
    float&      result_score,
    ap_uint<16>& result_x,
    ap_uint<16>& result_y
);
