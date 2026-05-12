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
static const int MAX_PATCH_W  = 816;    // max search width (~806), rounded to 16-lane bank boundary
static const int MAX_PATCH_H  = 320;    // rows in search region
static const int MAX_TEMPL_W  = 216;    // template width (post-scale)
static const int MAX_TEMPL_H  = 96;     // template height (post-scale)
static const int MAX_RESULT_W = MAX_PATCH_W - 4;   // worst-case result cols
static const int MAX_RESULT_H = MAX_PATCH_H - 4;   // worst-case result rows

// Parallelism: inner x-loop unroll factor inside the pipelined u-loop.
// 16 parallel MACs per cycle; effective II ≈ ceil(MAX_TEMPL_W/16) = 14.
// DSP budget: 16 MACs + adder tree ≈ 40 DSPs total — well within 220.
static const int PAR_COLS = 16;

// Integer accumulator width. The worst-case sum-of-squares window is
// 216*96*255^2 = ~1.35e9, and numerator magnitudes are smaller than that.
// 48 bits leaves margin for larger templates without overflowing the score path.
typedef ap_int<48> acc_t;

// AXI4-Stream pixel type (8-bit, no sideband data needed for image streams)
typedef ap_axiu<8, 1, 1, 1>  pix_stream_t;

// Result struct returned per candidate (packed into 96 bits for AXI transport)
struct tme_result_t {
    float    score;     // best TM_CCOEFF_NORMED score in [0,1]
    ap_uint<16> loc_x;  // column of best match in patch
    ap_uint<16> loc_y;  // row    of best match in patch
};

// -----------------------------------------------------------------------
// Top-level function declarations
// -----------------------------------------------------------------------

void correlation_core(
    ap_uint<8> patch_line[MAX_PATCH_W],
    ap_uint<8> templ_row[MAX_TEMPL_W],
    acc_t      acc[MAX_RESULT_W],
    ap_uint<16> patch_w,
    ap_uint<16> templ_w
);

float norm_rsqrt(float x);

void tme_top(
    hls::stream<pix_stream_t>& patch_stream,   // image patch, row by row
    hls::stream<pix_stream_t>& templ_stream,   // scaled template, row by row
    ap_uint<16> patch_w,
    ap_uint<16> patch_h,
    ap_uint<16> templ_w,
    ap_uint<16> templ_h,
    float&      result_score,
    ap_uint<16>& result_x,
    ap_uint<16>& result_y
);
