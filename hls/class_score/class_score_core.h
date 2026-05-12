#pragma once
#include <ap_fixed.h>
#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

// Score input from template_match_core (one per (candidate, template_type)):
//   bits [31:0]  score as IEEE-754 float32
//   bits [33:32] kind  (0=male, 1=female, 2=ferrule)
//   bits [47:34] candidate_id
typedef ap_axiu<48, 1, 1, 1> score_stream_t;

// Result output (one per candidate), packed into 128 bits:
//   bits [31:0]   best_score (float32)
//   bits [33:32]  tentative_kind (0=unknown, 1=male, 2=female, 3=ferrule_tentative)
//   bits [47:34]  candidate_id
//   bits [63:48]  box_x (pixel)
//   bits [79:64]  box_y
//   bits [95:80]  box_w
//   bits [111:96] box_h
//   bits [127:112] reserved
typedef ap_axiu<128, 1, 1, 1> result_stream_t;

void class_score_core(
    hls::stream<score_stream_t>&  score_in,
    hls::stream<result_stream_t>& result_out,
    ap_uint<16> score_thresh_q88,    // score_thresh  as Q8.8 (e.g. 0.33 → 0x0054)
    ap_uint<16> ferrule_thresh_q88,  // ferrule_score_thresh as Q8.8
    ap_uint<16> score_margin_q88     // score_margin as Q8.8
);
