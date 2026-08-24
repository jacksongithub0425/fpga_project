#include "class_score_core.h"

// ###########################################################################
// ##  NOT INTEGRATION-READY — DO NOT CONNECT, DO NOT EXPORT AS IP          ##
// ###########################################################################
//
// Known defects, recorded so a later repair starts from evidence rather than
// from a re-read.  Deliberately no line numbers — they go stale on the first
// edit; each entry names the construct instead.
//
// D1  Candidate boundary is detected one tuple too late.
//     The incoming tuple's score is merged into best_score[] BEFORE
//     last_for_cand is evaluated.  So when candidate N+1's first tuple
//     arrives it has already polluted candidate N's accumulator, and the
//     reset at the bottom of the branch then discards that score, so
//     candidate N+1 never sees it either.  Every boundary both contaminates
//     one result and drops one score.  Both halves are lossy: re-labelling
//     the records afterwards cannot reconstruct them.
//
// D2  The emitted result is labelled with the wrong candidate.
//     The cand_id written into the result field is the id of the tuple that
//     TRIGGERED the boundary — candidate N+1 — not the candidate the scores
//     belong to.  Compounds D1: the record is misattributed as well as wrong.
//
// D3  Taking the address of a temporary ap_range_ref halts synthesis.
//     *reinterpret_cast<float*>(&s.data.range(31,0)) prevents clean
//     synthesis, so this core produces NO RTL at all — not garbage RTL.
//     [Experiment evidence, not a retained log: Vitis HLS 2025.2 reported
//     HLS 207-4943, and the reverse float-to-bits cast passed an isolated
//     csim and synthesised to a direct 32-bit wire.  Neither observation is
//     preserved in a checked-in log; re-run before relying on the detail.]
//     The reverse cast is nonportable type-punning regardless.
//     The supported replacement is fp_struct<float> from
//     utils/x_hls_utils.h:
//         ap_uint<32> raw   = s.data.range(31, 0);
//         float       score = fp_struct<float>(raw).to_float();
//         ap_uint<32> bits  = fp_struct<float>(out_score).data();
//
// D4  The first-candidate sentinel collides with a legal id.
//     ap_uint<14> current_cand_id = 0xFFFF truncates to 0x3FFF, which is a
//     representable candidate_id.  Harmless at 64 candidates per page, wrong
//     in principle, and it silently changes meaning if the id field widens.
//
// D5  kind == 3 indexes past the end of best_score[3].
//     The header documents valid input kinds as 0-2, so the range is
//     specified — the defect is that hardware does not enforce it.  kind is
//     ap_uint<2> decoded straight from the wire, so 3 is representable, and
//     best_score[(int)kind] is then an out-of-bounds access with no guard.
//     A documented range that nothing checks is not a contract.
//
// D6  Three conflicting kind encodings.
//     This file uses 0=male, 1=female, 2=ferrule, 3=unknown.  The header's
//     result field and sw/tme_driver.py both document 0=unknown, 1=male,
//     2=female, 3=ferrule.  The core writes top1_kind (its own encoding)
//     into the result field the driver decodes with the other one.  Taken
//     ALONE that shifts every classification by one — male reads back as
//     unknown, female as male, ferrule as female, unknown as ferrule — but
//     that is the behaviour after D7's byte offsets are fixed, not the
//     current end-to-end behaviour.  Today the driver's kind byte also
//     carries the low 6 bits of cand_id (D7), so the observed value is
//     contaminated rather than merely shifted.  Fix D7 first, then D6.
//     Note the INPUT kind field legitimately uses the first encoding — the
//     two must be named apart, not merged.
//
// D7  The result ABI does not match the driver, at three levels.
//     SIZE: the PL emits 16-byte beats; the driver allocates and requests
//     only 14 bytes per result ("<fBBHHHH", whose own comment claims 16).
//     That is a DMA length mismatch.  The symptom depends on how the DMA
//     engine handles a transfer length that is not a whole number of beats:
//     a DMA error, a stall/short transfer, or truncated records are all
//     possible.  A buffer overwrite is one outcome, not the guaranteed one —
//     do not diagnose from that single symptom.
//     PLACEMENT: field offsets differ even for record 0.  cand_id is 14 bits
//     at [47:34] here but a 1-byte field at offset 5 there, i.e. bits
//     [47:40] — the TOP 8 bits of the 14-bit field.  For the candidate ids
//     this design actually uses (0-63) those bits are always zero, so the
//     driver decodes EVERY cand_id as 0.
//     CONTAMINATION: the driver's kind byte is bits [39:32], which holds
//     kind in [33:32] and the low 6 bits of cand_id in [39:34].  See D6.
//
// D8  Box coordinates are zeroed here and consumed by software.
//     res.data.range(127,48) = 0 is emitted unconditionally, and the driver
//     unpacks those bytes into a "box" tuple it returns to callers.  Either
//     this core fills them or the ABI drops them; silently returning (0,0,
//     0,0) as a real box is the worst of both.
//
// D9  TSTRB/TUSER/TID/TDEST are never driven.  Only res.keep is assigned,
//     so the remaining sidebands are UNSPECIFIED on this interface.  Do not
//     assume they inherit usable defaults — nothing here states a value and
//     nothing downstream is entitled to one.
//
// D10 No testbench exists.  run_hls.tcl runs csynth only, and there is no
//     retained successful core-level csim/csynth/cosim evidence for this
//     file.  Note this does NOT block all testing: a failure-characterisation
//     testbench pinning D1-D9 can be written today and is worth having.  Only
//     the integration-signoff testbench needs the frozen framing contract,
//     because only it depends on how a batch is delimited.
//
// Repair sequencing.  D1/D2 are NOT blocked by the framing contract: ordered
// cand_id transitions plus batch TLAST already identify candidate boundaries
// unambiguously, and the defect is purely that the new tuple is merged before
// the previous candidate is flushed (and the flush mislabelled).  Reordering
// the loop body — detect boundary, flush + label candidate N, then merge —
// fixes both today.  Explicit per-candidate framing (contract §5.1) remains
// preferable, chiefly because it is the only way an INVALID candidate, which
// produces no tuples at all, can hold its ordinal slot in the result stream.
// D6/D7/D8 are ABI decisions shared with the driver and stay downstream of
// the contract freeze (§6.3), which must also close two gaps beyond the
// layout: the record carries one score while software consumes
// male/female/ferrule scores, and the PS never receives the winning match
// location unless the PL returns it.
// ###########################################################################

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

        float        score      = *reinterpret_cast<float*>(&s.data.range(31, 0));
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
            ap_uint<32> score_bits;
            float out_score = top1;
            score_bits = *reinterpret_cast<ap_uint<32>*>(&out_score);

            res.data.range(31,   0) = score_bits;
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
