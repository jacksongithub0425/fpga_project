#include "tme_top.h"

// Tiled 16-lane MAC correlation.
//
// For each tile of PAR_COLS output columns:
//   1. Load a contiguous segment of patch_line into fully-partitioned
//      registers (seg[PAR_COLS + MAX_TEMPL_W - 1]).  One sequential read
//      per cycle — no memory-port conflict.
//   2. Pipeline the template-column (x) loop at II=1:
//        lane p accumulates: patch_line[u0+p+x] * (templ_row[x]-128)
//      seg is in registers so all 16 reads per cycle are free.
//   3. Write back PAR_COLS partial sums into acc[] (also partitioned).
//
// Resource cost: PAR_COLS DSP48E1 (16) + adder tree ≈ 40 DSPs total.
// Throughput: ~(SEG_W + MAX_TEMPL_W) * n_tiles cycles per template row.

static const int SEG_W = PAR_COLS + MAX_TEMPL_W;  // 16+216 = 232

void correlation_core(
    ap_uint<8>   patch_line[MAX_PATCH_W],
    ap_uint<8>   templ_row[MAX_TEMPL_W],
    acc_t        acc[MAX_RESULT_W],
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

    // Segment register file: fully partitioned so 16 reads are free each cycle.
    ap_uint<8> seg[SEG_W];
#pragma HLS ARRAY_PARTITION variable=seg complete dim=1

    // Per-lane accumulators for the current tile.
    acc_t lane_acc[PAR_COLS];
#pragma HLS ARRAY_PARTITION variable=lane_acc complete dim=1

    tile_loop: for (int t = 0; t * PAR_COLS < MAX_RESULT_W; t++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=52 avg=26

        int u0 = t * PAR_COLS;
        if (u0 >= rw) break;

        // --- Phase 1: load patch segment into registers (sequential BRAM read) ---
        load_seg: for (int i = 0; i < SEG_W; i++) {
#pragma HLS PIPELINE II=1
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
        // templ_row[x] is one BRAM read, broadcast to all 16 lanes.
        mac_loop: for (int x = 0; x < MAX_TEMPL_W; x++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=216 avg=80
            if (x >= tw) break;
            ap_int<9> tv = (ap_int<9>)templ_row[x] - 128;
            for (int p = 0; p < PAR_COLS; p++) {
#pragma HLS UNROLL
                lane_acc[p] += (acc_t)((ap_int<9>)seg[p + x] * tv);
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
