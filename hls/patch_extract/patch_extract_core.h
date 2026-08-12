#pragma once
#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

// ---------------------------------------------------------------------------
// patch_extract_core — final interface per docs/pl_interface_contract.md.
//
// The binary page lives in PS DDR. binarize_core v2 schedules its AXIS output
// directly in the LOGICAL frame of contract §1; simple-mode S2MM stores that
// compact stream, and the PS writes the same destination buffer's physical
// address into this core's m_axi offset=slave CTRL register (matching
// _REG_BIN_ADDR in sw/tme_driver.py). The row stride is explicit and runtime
// (§2); it must never be assumed equal to img_w. A later writer may add row
// padding, but the current binarizer/S2MM boundary uses stride == img_w.
// ---------------------------------------------------------------------------

// Candidate descriptor packed into 64 bits for AXI4-Stream transport (§6.1):
//   bits [15:0]  endpoint_x  (logical page coordinate)
//   bits [31:16] endpoint_y
//   bits [33:32] side        (0=left, 1=right; >1 is INVALID, not "right")
//   bits [47:34] max_templ_w (post-round, at largest scale — see §4.5)
//   bits [63:48] max_templ_h
// The wire fields are wider than the legal ranges below on purpose; §4
// validation is what closes that gap.  Do not narrow the wire fields.
//
// FRAMING (§5): the feeder must deliver exactly num_cands beats with TLAST on
// the last one.  The core reads exactly num_cands beats regardless of where
// TLAST lands, and reports any disagreement as PE_SF_TLAST_MISMATCH — it
// never truncates the batch on an early TLAST, because doing so would leave
// real descriptors queued for the next invocation.  A stream SHORTER than
// num_cands therefore stalls the core rather than completing; that is a
// feeder fault by construction.
typedef ap_axiu<64, 1, 1, 1> cand_stream_t;

// Output: raw patch pixels, row-major, uint8.  TLAST asserts on the FINAL
// pixel of EACH valid patch (§5) — per-patch framing, not per-batch.
// Invalid candidates emit no pixel beats at all.
typedef ap_axiu<8, 1, 1, 1>  ppix_stream_t;

// Patch metadata record, 128-bit, one per input descriptor in input order
// (§6.2).  TLAST asserts on the last record of the batch.
//   [15:0]    cand_id       ordinal of the descriptor within the batch
//   [31:16]   status        = valid | (reason << 1) — one 16-bit word;
//                           software unpacks it whole and masks
//   [47:32]   x0            post-clip logical page coordinate
//   [63:48]   y0
//   [79:64]   patch_w       post-clip; reported even when valid=0 so the
//   [95:80]   patch_h       reject reason can be diagnosed from the record
//   [127:96]  reserved, zero
// Every record describes a descriptor that was actually read, so valid=0
// always carries at least one reason bit and status == 0 exactly is
// unreachable.  Globally-invalid batches (§4.3) set reason bit 8 and report
// zeroed geometry.  (An earlier revision used status == 0 to mark a filler
// ordinal emitted after an early TLAST; that path is gone — see the FRAMING
// banner in patch_extract_core.cpp for why.)
typedef ap_axiu<128, 1, 1, 1> pmeta_stream_t;

// ---- Image geometry limits (§2) — match binarize_core.h ------------------
static const int PE_MAX_IMG_W   = 9856;   // >= 9792 (sample page at ZOOM=4.0)
static const int PE_MAX_IMG_H   = 6400;   // >= 6336
static const int PE_MIN_IMG_DIM = 3;      // a 3x3 kernel needs 3 rows/cols

// ---- Patch limits — bounded by the matcher, not the page -----------------
// tme_top's patch_buf is [MAX_PATCH_H][MAX_PATCH_W] (tme_top.h).  These MUST
// track that header: 820 x 307 is the exact reachable envelope implied by the
// 216 x 96 template cap below, and the matcher only fits the xc7z020 at that
// size (224 BRAM18K vs 352 at the former 1024 x 320 — see
// hls/template_match/ab_bram/).
//
// Consequence for reason bits 5/6 — what changed is the CO-FIRE THRESHOLD,
// not whether they can fire alone.  Bits 5/6 have never been reachable
// without bits 2/3: the smallest max_tw that overran the old 1024 bound was
// 270 (-> 1026) and the smallest max_th that overran 320 was 101 (-> 323),
// and both already violated the 216/96 template cap, so bit 2 or 3 was
// always set alongside.  Narrowing the bound only moved the pair that first
// co-fires, from 270/101 to 217/97.  They stay as independent checks (§4.1)
// precisely so a clipping or bump change that breaks the implication is
// still caught.
static const int PE_MAX_PATCH_W = 820;
static const int PE_MAX_PATCH_H = 307;

// ---- Legal template envelope (§4.1) --------------------------------------
// 4 is the driver's floor (terminal_counter_endpoint_first.py:557); 216x96 is
// the largest post-scale template.  Values must come from the POST-ROUND
// dimensions of the template actually transmitted (§4.5).
static const int PE_MIN_TEMPL   = 4;
static const int PE_MAX_TEMPL_W = 216;
static const int PE_MAX_TEMPL_H = 96;

// ---- Rejection reason bits (§4.2) ----------------------------------------
// Positions are within `reason` itself; on the wire reason bit n sits at
// status bit n+1 (status = valid | reason << 1).
static const int PE_R_EPX_OOR      = 0;  // ep_x >= img_w
static const int PE_R_EPY_OOR      = 1;  // ep_y >= img_h
static const int PE_R_TW_RANGE     = 2;  // max_tw outside [4, 216]
static const int PE_R_TH_RANGE     = 3;  // max_th outside [4, 96]
static const int PE_R_SIDE         = 4;  // side not in {0, 1}
static const int PE_R_PATCH_W      = 5;  // patch_w > 820 post-clip
static const int PE_R_PATCH_H      = 6;  // patch_h > 307 post-clip
static const int PE_R_PATCH_SMALL  = 7;  // patch_w < max_tw or patch_h <
                                         // max_th post-clip (equality is
                                         // legal — §4.4 option 1 adopted)
static const int PE_R_GLOBAL       = 8;  // global image config invalid

// ---- Status flag bits (§7), readable after ap_done -----------------------
static const int PE_SF_GLOBAL_INVALID = 0;  // batch-level error: §4.3 path ran
static const int PE_SF_TLAST_MISMATCH = 1;  // TLAST disagreed with num_cands

// Bytes the co-simulation testbench allocates for the DDR image buffer; the
// m_axi depth in patch_extract_core.cpp must match, because the cosim
// wrapper snapshots exactly that many bytes from the pointer the testbench
// passes.  C simulation ignores it (direct pointer) and may use larger
// heap buffers, e.g. the full-frame high-coordinate pass.
static const int PE_COSIM_BUF_BYTES = 1024 * 512;

void patch_extract_core(
    hls::stream<cand_stream_t>&  cand_in,      // candidate descriptors (PS)
    hls::stream<ppix_stream_t>&  patch_out,    // patch pixels -> matcher
    hls::stream<pmeta_stream_t>& meta_out,     // per-descriptor metadata
    const unsigned char*         bin_image,    // binary page in DDR (m_axi)
    ap_uint<16> img_w,                         // 3..9856
    ap_uint<16> img_h,                         // 3..6400
    ap_uint<32> stride_bytes,                  // >= img_w; not assumed == img_w
    ap_uint<32> buffer_bytes,                  // >= stride_bytes * img_h
    ap_uint<16> num_cands,                     // authoritative batch count (§5)
    ap_uint<32>& sts_flags,                    // PE_SF_* bits
    ap_uint<32>& sts_rejected,                 // records emitted with valid=0
    ap_uint<32>& sts_processed                 // descriptors read from cand_in
);
