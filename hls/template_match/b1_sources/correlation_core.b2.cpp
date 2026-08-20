#include "tme_top.h"

// Tiled 16-lane MAC correlation.
//
// For each tile of PAR_COLS output columns:
//   1. Make seg[] hold the contiguous patch segment this tile needs, in
//      fully-partitioned registers (seg[PAR_COLS + tw - 1]).  The FIRST tile
//      loads it outright; every later tile SHIFTS the PAR_COLS-overlapped
//      remainder down and refills only the PAR_COLS pixels that are new.
//      See the B2 note below.
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
// Throughput history, per (output row, template row), all MEASURED by paired
// RTL co-simulation on the same fourteen invocations:
//
//     cur   T*(tw + 257)               constant 232-pixel load, every tile
//     B1    T*(2*tw + 41) + 1          load shortened to the runtime seg_len
//     B2    see the term recorded below
//
// The workload projections that follow from those terms live in
// sw/tme_cycle_model.py; none of them is a measured page time.

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
// MAX_TEMPL_W.  seg_len below is the per-invocation value.  Keeping each
// loop's static bound at a compile-time constant and stopping on the runtime
// value means no write can leave the array however the scalars are programmed.
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
    // This guard bounds the tile break, the shift and the writeback; if it
    // lags tme_top's rw by one, the last output column is never written but is
    // still read by norm_cols (contract §4.4, option 1).
    int rw = pw - tw + 1;
    // B1: the segment is loaded to the width THIS INVOCATION needs, rather
    // than the compiled 232.  Measured cost T*(2*tw + 41) + 1 per (output row,
    // template row), against a projected T*(2*tw + 40) — the projection was
    // optimistic by T + 1, in the direction that flattered the change.  The
    // shape of that overhead is measured; its MECHANISM is not, and the one
    // obvious guess is ruled out: solution `b1b` removed the per-iteration
    // `i >= seg_len` predicate entirely and produced a byte-identical
    // transaction report.  Do not quote a cause.  See
    // hls/template_match/b1_sources/README.md and sw/tme_b1_ab.py.
    int seg_len = tw + PAR_COLS - 1;

    // ------------------------------------------------------------------
    // B2: HORIZONTAL OVERLAP REUSE
    // ------------------------------------------------------------------
    // Consecutive tiles start PAR_COLS apart and each needs seg_len pixels,
    // so tile t's segment and tile t-1's OVERLAP in seg_len - PAR_COLS =
    // tw - 1 pixels.  B1 re-read all seg_len from patch_line every tile; B2
    // re-reads only the PAR_COLS that are actually new:
    //
    //     tile 0     load seg[0 .. seg_len-1]            from patch_line
    //     tile t>0   seg[i] = seg[i + PAR_COLS]          shift, i < tw-1
    //                seg[tw-1 .. tw+14]                  from patch_line
    //
    // WHY THE SHIFT IS THE RIGHT FORM.  The alternative — leaving the pixels
    // where they are and rotating the READ index — would turn every one of the
    // 16 lanes' seg[p + x] into a runtime rotation over 231 registers.  The
    // shift instead puts the variable part on the WRITE side, where seg
    // already has a decoder because load_seg indexes it by a loop counter, and
    // leaves the MAC's read addressing bit-for-bit what B1 had.  That is also
    // what keeps the paired co-simulation attributable: the only thing that
    // moved is how seg gets its contents.
    //
    // ZERO-PADDING SURVIVES THE SHIFT, and it has to be checked rather than
    // assumed, because the shift is the one place a tile inherits state from
    // its predecessor.  Tile t-1 wrote seg[j] = (u0 - PAR_COLS + j < pw) ?
    // patch_line[...] : 0.  Reading that at j = i + PAR_COLS gives exactly
    // (u0 + i < pw) ? patch_line[u0 + i] : 0 — the same value B1's load would
    // have produced at seg[i].  The out-of-patch zeros therefore propagate
    // correctly into the final, partially-masked tile instead of being
    // recomputed there.
    //
    // WHAT THE TERM COSTS is recorded in sw/tme_cycle_model.py (variant "B2")
    // and adjudicated by sw/tme_b2_ab.py against the `b1` report over the same
    // fourteen invocations.  The projection this replaced was written in the
    // same style as B1's withdrawn one, so it was NOT assumed to hold: see
    // that file for the measured term and for what the prediction missed by.
    //
    // NOTHING HERE IS CARRIED ACROSS INVOCATIONS.  seg is a local automatic
    // array and tile 0 always takes the full-load branch, so the registers a
    // previous correlation_core call left behind are overwritten before any
    // lane reads them.  Every reuse is WITHIN one call, between tiles.  That
    // is the property an overlap-reuse rewrite is most likely to break, and it
    // is NOT tested by a new vector suite: sw/tme_b2_mutants.py shows that the
    // already-pinned b1 suite breaks on the `skip_first_full` mutant -- reuse
    // carried across tile 0, hence across template rows, output rows and calls
    // -- for all 256 possible stale register fills, on eight of its twelve
    // cases.  Keeping the stimulus identical to B1's is also what makes the
    // paired co-simulation a comparison rather than two separate runs.
    //
    // The number of pixels carried over is tw - 1, which is ZERO at tw = 1.
    // The shift then copies nothing and the refill writes seg[0 .. 15], i.e.
    // the whole segment — so the degenerate template is the ordinary path with
    // an empty overlap, not a special case.  (tw >= 4 by contract §4.1; the
    // bound is noted because the loops must stay correct at it, not because a
    // 1-wide template is legal.)
    int overlap = seg_len - PAR_COLS;      // = tw - 1, the reused pixels
    int refill0 = overlap;                 // first index the refill writes

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

        // --- Phase 1: make seg[] hold patch_line[u0 .. u0+seg_len-1] -------
        if (t == 0) {
            // First tile: nothing to reuse.  Identical to B1's load.
            load_seg: for (int i = 0; i < SEG_W; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=19 max=231 avg=95
                if (i >= seg_len) break;
                int idx = u0 + i;
                seg[i] = (idx < pw) ? patch_line[idx] : (ap_uint<8>)0;
            }
        } else {
            // Later tiles: slide the overlap down by PAR_COLS, then refill.
            //
            // The guard is not decoration.  Without it the unrolled copy would
            // read seg[i + PAR_COLS] for i up to SEG_W - PAR_COLS - 1, i.e.
            // past seg_len - 1, which for tw < MAX_TEMPL_W is a register no
            // load has ever written.  The values would never reach a lane, but
            // "reads uninitialised storage and the result happens to be
            // discarded" is not a property worth relying on in either C
            // simulation or RTL.  In hardware it is a clock enable per
            // register, on a mux the shift needs anyway.
            shift_seg: for (int i = 0; i < SEG_W - PAR_COLS; i++) {
#pragma HLS UNROLL
                if (i < overlap) seg[i] = seg[i + PAR_COLS];
            }

            // Exactly PAR_COLS new pixels, at a COMPILE-TIME trip count: this
            // loop has no runtime bound and no exit test.  refill0 is loop
            // invariant, so the address is a constant offset plus the counter.
            refill_seg: for (int k = 0; k < PAR_COLS; k++) {
#pragma HLS PIPELINE II=1
                int j   = refill0 + k;
                int idx = u0 + j;
                seg[j] = (idx < pw) ? patch_line[idx] : (ap_uint<8>)0;
            }
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
