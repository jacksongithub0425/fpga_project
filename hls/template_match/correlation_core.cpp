#include "tme_top.h"

// Tiled 16-lane MAC correlation.
//
// For each tile of PAR_COLS output columns:
//   1. Load a contiguous segment of patch_line into fully-partitioned
//      registers (seg[PAR_COLS + tw - 1]).  One sequential read per cycle —
//      no memory-port conflict.  The length is the RUNTIME requirement, not
//      the compiled envelope; see SEG_W below.
//   2. Pipeline the template-column (x) loop at II=1:
//        lane p accumulates: patch_line[u0+p+x] * templ_row[x]
//      seg is in registers so all 16 reads per cycle are free.
//   3. Write back PAR_COLS partial sums into acc[] (also partitioned).
//
// Template pixels are RAW uint8 — the product is unsigned 8×8 and the
// accumulated quantity is ΣTI, exact by construction.  Mean subtraction
// happens algebraically in tme_top's normalisation (N·ΣTI − ΣT·ΣI), not
// per-pixel here.  Per-tile bound: 216 products of ≤ 255² keeps lane_acc
// under 2^24; acc[] accumulates ≤ 96 tiles of that, under 2^31 (sumsq_t).
//
// Throughput: T*(2*tw + 41) + 1 cycles per (output row, template row), a
// MEASURED term -- see the note on seg_len below for what the runtime bound
// costs and why the naive reading of it is wrong.  Before B1 the load was a
// constant SEG_W of 232 whatever tw was, so a 20-wide template paid for 216
// columns of template it did not have: at the Phase-S workload that is
// a 26.334292108222 s/page workload projection instead of 36.476
// (sw/tme_cycle_model.py, variant "B1"; the exact aggregate is frozen as
// FROZEN["b1"]["aggregate_cycles"] = 118,504,314,487).

// The MAC reads seg[p + x] for p < PAR_COLS and x < tw, so the highest index
// a tile can touch is (PAR_COLS - 1) + (tw - 1) = tw + PAR_COLS - 2, and a
// segment of tw + PAR_COLS - 1 pixels is exactly sufficient.  ONE SHORTER AND
// LANE 15 READS AN ELEMENT NO TILE EVER WROTE — a defect that is invisible to
// a score-tolerance assert and to any suite whose peaks avoid lane 15.  The
// case that detects it is build_lane15 in tme_generate_production.py: a PAIR
// of lane-15 windows whose ordering the mutation reverses for all 256 possible
// stale-register values, since no single window is safe against every one.
//
// SEG_W is the compile-time BOUND on that quantity, reached at tw =
// MAX_TEMPL_W.  seg_len below is the per-invocation value.  Keeping the loop's
// static bound at SEG_W and stopping early on seg_len means the write can
// never leave the array however the scalars are programmed.
static const int SEG_W = PAR_COLS + MAX_TEMPL_W - 1;  // 16+216-1 = 231

void correlation_core(
    ap_uint<8>   patch_line[MAX_PATCH_W],
    ap_uint<8>   templ_row[MAX_TEMPL_W],
    sumsq_t      acc[MAX_RESULT_W],
    ap_uint<16>  patch_w,
    ap_uint<16>  templ_w)
{
#pragma HLS INLINE off
#pragma HLS ARRAY_PARTITION variable=templ_row complete dim=1

    int pw = (int)patch_w;
    int tw = (int)templ_w;
    // Full valid-correlation width — must match rw in tme_top.cpp exactly.
    // This guard bounds both the tile break and the writeback; if it lags
    // tme_top's rw by one, the last output column is never written but is
    // still read by norm_cols (contract §4.4, option 1).
    int rw = pw - tw + 1;
    // B1: the segment is loaded to the width THIS INVOCATION needs.  The tile
    // count, the lane masking, the MAC schedule and the writeback are all
    // untouched, so the whole latency difference is this one bound.
    //
    // IT IS NOT FREE, AND THE NAIVE ARITHMETIC IS WRONG.  The obvious reading
    // is that shortening the load saves exactly the pixels it skips,
    // T*(232 - seg_len) per (output row, template row).  Paired RTL
    // co-simulation of this file against the unmodified one (sw/tme_b1_ab.py,
    // 14/14 transactions) says the saving is short of that by exactly
    //
    //     T + 1     cycles per (output row, template row)
    //
    // WHAT IS ESTABLISHED IS THE SHAPE, NOT THE MECHANISM.  One term scaling
    // with the tile count and one constant per correlation_core call: that is
    // what the measurements pin, and it makes the tile term
    // T*(2*tw + 41) + 1.  The workload PROJECTION built from that term is
    // 26.334292108222 s/page, against 26.239696410444 projected before this
    // RTL existed.  (A projection over 20,680 modelled trials -- not a
    // measured page time.  Nothing has run a page.)
    //
    // WHERE the cycles go is NOT established, and one obvious guess is ruled
    // out.  Solution `b1b` hoists a clamped bound so the loop carries no
    // per-iteration `i >= seg_len` predicate at all, and its transaction
    // report is BYTE-IDENTICAL to this one's (at 44 more LUTs).
    //
    // READ THAT PRECISELY.  `b1b` still has a RUNTIME loop bound -- it just
    // spells it `i < seg_n` with seg_n computed once instead of testing a
    // predicate inside the body.  So what the experiment rules out is the
    // SOURCE-LEVEL FORM of the test: writing it one way or the other costs
    // nothing.  It does NOT rule out runtime-bounded control as the origin of
    // the cycles, because both variants have it.  A compile-time-bounded
    // control experiment would be needed to separate those, and none was run.
    //
    // So: the shape is measured, the mechanism stays unlocalized.  Do not
    // re-litigate the predicate form with another co-simulation, do not
    // promote "runtime bound" from a candidate to a cause, and do not assume
    // B2 or B0b pay the same shape, or only this shape.
    //
    // Consequence worth knowing before reading a board trace: at tw = 216 the
    // saving is 1 cycle per tile and the overhead is T + 1, so B1 is a NET LOSS
    // at the compiled maximum template width -- `phase-s-max` goes 23,476,737
    // -> 23,482,881 cycles.  Every template in the real workload is narrower,
    // which is where 36.476 -> 26.334 comes from.
    //
    // The `break` form is kept over the hoisted one because it costs fewer
    // LUTs for identical cycles, because the write cannot leave the array
    // however templ_w is programmed, and because it is the idiom mac_loop,
    // isq_init and isq_slide already use.
    int seg_len = tw + PAR_COLS - 1;

    // Segment register file: fully partitioned so 16 reads are free each cycle.
    ap_uint<8> seg[SEG_W];
#pragma HLS ARRAY_PARTITION variable=seg complete dim=1

    // Per-lane accumulators for the current tile.
    ap_uint<25> lane_acc[PAR_COLS];
#pragma HLS ARRAY_PARTITION variable=lane_acc complete dim=1

    tile_loop: for (int t = 0; t * PAR_COLS < MAX_RESULT_W; t++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=52 avg=26

        int u0 = t * PAR_COLS;
        if (u0 >= rw) break;

        // --- Phase 1: load patch segment into registers (sequential BRAM read) ---
        load_seg: for (int i = 0; i < SEG_W; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=19 max=231 avg=95
            if (i >= seg_len) break;
            int idx = u0 + i;
            seg[i] = (idx < pw) ? patch_line[idx] : (ap_uint<8>)0;
        }

        // --- Phase 2: reset lane accumulators ---
        for (int p = 0; p < PAR_COLS; p++) {
#pragma HLS UNROLL
            lane_acc[p] = 0;
        }

        // --- Phase 3: pipelined MAC over template columns ---
        // For x fixed per cycle: seg[p+x] is a register read (free for all p).
        // templ_row[x] is one register read, broadcast to all 16 lanes.
        mac_loop: for (int x = 0; x < MAX_TEMPL_W; x++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=216 avg=80
            if (x >= tw) break;
            ap_uint<8> tv = templ_row[x];
            for (int p = 0; p < PAR_COLS; p++) {
#pragma HLS UNROLL
                lane_acc[p] += (ap_uint<16>)(seg[p + x] * tv);
            }
        }

        // --- Phase 4: accumulate into output array ---
        writeback: for (int p = 0; p < PAR_COLS; p++) {
#pragma HLS UNROLL
            int u = u0 + p;
            if (u < rw) acc[u] += lane_acc[p];
        }
    }
}
