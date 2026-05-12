#include "class_score_core.h"
#include <cstring>

// -----------------------------------------------------------------------
// class_score_core
//
// Replicates the scoring logic of classify_endpoint() (line 628-636 in
// terminal_counter_endpoint_first.py) in hardware.
//
// Receives a stream of (score, kind, candidate_id) tuples from
// template_match_core — one tuple per (template_type × variant) per
// candidate.  All tuples for a given candidate arrive before the next
// candidate starts (guaranteed by template_match_core FSM).
//
// For each candidate:
//   1. Track best score per kind (male / female / ferrule)
//   2. After all types scored (TLAST or candidate_id change):
//      - Rank the 3 kinds by score
//      - Apply threshold: if best < threshold → unknown
//      - Apply margin:    if best - second < margin → unknown
//      - Emit result
//
// "ferrule_tentative" output: PS postprocess_ps still runs
// ferrule_shape_metrics() to validate/override ferrule detections.
// -----------------------------------------------------------------------

static const int KIND_MALE    = 0;
static const int KIND_FEMALE  = 1;
static const int KIND_FERRULE = 2;
static const int KIND_UNKNOWN = 3;

void class_score_core(
    hls::stream<score_stream_t>&  score_in,
    hls::stream<result_stream_t>& result_out,
    ap_uint<16> score_thresh_q88,
    ap_uint<16> ferrule_thresh_q88,
    ap_uint<16> score_margin_q88)
{
#pragma HLS INTERFACE axis        port=score_in
#pragma HLS INTERFACE axis        port=result_out
#pragma HLS INTERFACE s_axilite   port=score_thresh_q88    bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=ferrule_thresh_q88  bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=score_margin_q88    bundle=CTRL
#pragma HLS INTERFACE ap_ctrl_hs  port=return

    // Convert Q8.8 thresholds to float for comparison
    float thresh_male_female = (float)score_thresh_q88   / 256.0f;
    float thresh_ferrule     = (float)ferrule_thresh_q88 / 256.0f;
    float margin             = (float)score_margin_q88   / 256.0f;

    // Accumulators for current candidate
    float best_score[3];  // indexed by KIND_*
    best_score[0] = best_score[1] = best_score[2] = -1.0f;

    ap_uint<14> current_cand_id = 0xFFFF;

    process_scores: while (true) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=720 avg=200

        score_stream_t s = score_in.read();

        ap_uint<32>  score_bits = s.data.range(31, 0);
        float        score;
        std::memcpy(&score, &score_bits, sizeof(float));
        ap_uint<2>   kind       = s.data.range(33, 32);
        ap_uint<14>  cand_id    = s.data.range(47, 34);

        // Update best score for this kind
        if (score > best_score[(int)kind]) {
            best_score[(int)kind] = score;
        }

        bool last_for_cand = s.last || (cand_id != current_cand_id && current_cand_id != (ap_uint<14>)0xFFFF);
        current_cand_id = cand_id;

        if (last_for_cand) {
            // --- Rank the 3 kinds ---
            // Find best and second-best
            float top1 = -2.0f, top2 = -2.0f;
            int   top1_kind = KIND_UNKNOWN;

            for (int k = 0; k < 3; k++) {
#pragma HLS UNROLL
                if (best_score[k] > top1) {
                    top2 = top1;
                    top1 = best_score[k];
                    top1_kind = k;
                } else if (best_score[k] > top2) {
                    top2 = best_score[k];
                }
            }

            // --- Apply threshold and margin ---
            float needed = (top1_kind == KIND_FERRULE) ? thresh_ferrule : thresh_male_female;
            ap_uint<2> final_kind;
            if (top1 < needed || (top1 - top2) < margin) {
                final_kind = KIND_UNKNOWN;
            } else {
                final_kind = (ap_uint<2>)top1_kind;
            }

            // --- Emit result ---
            result_stream_t res;
            float out_score = top1;
            ap_uint<32> out_score_bits;
            std::memcpy(&out_score_bits, &out_score, sizeof(float));

            res.data.range(31,   0) = out_score_bits;
            res.data.range(33,  32) = final_kind;
            res.data.range(47,  34) = cand_id;
            // box coordinates left as zero — PS postprocess_ps fills from
            // the match location returned via separate AXI4-Lite reads
            res.data.range(127, 48) = 0;
            res.last  = s.last ? 1 : 0;
            res.keep  = 0xFFFF;
            result_out.write(res);

            // Reset for next candidate
            best_score[0] = best_score[1] = best_score[2] = -1.0f;

            if (s.last) break;
        }
    }
}
