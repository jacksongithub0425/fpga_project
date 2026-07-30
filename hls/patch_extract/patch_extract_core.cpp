#include "patch_extract_core.h"

// -----------------------------------------------------------------------
// patch_extract_core
//
// Reads candidate descriptors from the PS (via AXI4-Stream DMA), validates
// them per contract §4, computes patch boundaries using the same formula as
// build_endpoint_patch() in terminal_counter_endpoint_first.py (line 502),
// emits one 128-bit metadata record per descriptor (§6.2), and for VALID
// descriptors streams the patch pixels from the binary page in DDR to
// template_match_core with TLAST on each patch's final pixel (§5).
//
// Patch boundary formula — bit-exact with build_endpoint_patch():
//   outward_w = max_templ_w * 12/5   (exactly 2.4)
//   inward_w  = max_templ_w *  7/5   (exactly 1.4)
//   patch_h   = max_templ_h * 16/5   (exactly 3.2)
//   if side==LEFT:  x0 = ep_x - outward_w,  x1 = ep_x + inward_w
//   if side==RIGHT: x0 = ep_x - inward_w,   x1 = ep_x + outward_w
//   y0 = ep_y - patch_h/2,  y1 = y0 + patch_h
//   This half-open interval contains exactly patch_h rows before clipping.
//   Clamp x0/y0 into [0, iw-2] / [0, ih-2] and x1/y1 to iw / ih, so the
//   minimum-size bump can never address outside the image.
//
// Both sides are integer-only, which is what makes the equality exact:
//   - the rationals above rather than a Q?.7 approximation (x307>>7 and
//     friends drift +/-1 for roughly 87% of inputs),
//   - Python quantizes the endpoint with int(round(...)) before applying any
//     offset, matching the ap_uint<16> ep_x/ep_y packed into the AXIS word,
//   - Python caps x0/y0 at img_w-2/img_h-2, not img_w-1/img_h-1, so it can no
//     longer emit a box that numpy would silently clip but the address
//     arithmetic here cannot.
// Verified equal over 3.2M interior candidates, so C simulation should demand
// zero mismatch rather than a tolerance.  Keep both sides in sync.
//
// -----------------------------------------------------------------------
// IMAGE INTERFACE — m_axi pointer + explicit stride (contract §2)
//
// bin_image is an AXI master with offset=slave: the PS writes the DDR
// buffer's physical address into the CTRL bundle (the driver's
// _REG_BIN_ADDR).  The pixel at logical (x, y) lives at byte offset
// y * stride_bytes + x.  stride_bytes is runtime and MUST NOT be assumed
// equal to img_w — the previous 2D-array signature hardcoded a 2560-byte
// stride, and that assumption is exactly what this port replaced.
//
// The coordinate frame is LOGICAL (§1): the binarizer's stream-to-DDR
// writer owns the raw->logical shift, so this core does no coordinate
// correction at all.
//
// §2.1 overflow rule: stride_bytes * img_h is computed in ap_uint<48> —
// wider than either operand — and validated against buffer_bytes AND the
// 32-bit linear-offset range before any descriptor is processed.  A product
// that silently wrapped would pass every later comparison, so the check
// rejects explicitly (reason bit 8) rather than relying on a downstream
// failure.  Once it holds, every pixel read is provably in range:
//   (y0 + r) <= img_h - 1  and  (x0 + c) <= img_w - 1 < stride_bytes
//   =>  offset < img_h * stride_bytes <= min(buffer_bytes, 2^32).
//
// -----------------------------------------------------------------------
// FRAMING (contract §5)
//
// num_cands is the authority; TLAST is a pure cross-check.  The core reads
// EXACTLY num_cands descriptors — always, unconditionally — and each beat is
// checked against the position TLAST is required to occupy:
//
//     mismatch |= (cand.last != (i == num_cands - 1))
//
// which catches both an early TLAST and a missing final one, without ever
// changing how many beats are consumed.  Exactly num_cands metadata records
// go out, every one describing a descriptor that was actually read, and the
// last carries TLAST.
//
// An earlier revision treated early TLAST as end-of-stream: it stopped
// reading and emitted filler records for the remaining ordinals.  That was
// wrong in the one case the testbench could not see.  TLAST is a wire bit,
// not a beat count — the feeder can deliver all num_cands descriptors and
// still mis-place TLAST (an off-by-one in a count-derived TLAST generator
// does exactly this).  Stopping early then left real descriptors queued in
// the DMA, and the NEXT invocation read them as its own batch: silent
// cross-batch corruption, reported only as a mismatch flag on the run that
// caused it.  Draining is what makes an invocation self-contained.
//
// The deliberate trade: cand_in.read() is blocking, so a feeder that
// delivers FEWER than num_cands beats now stalls here with ap_done low
// instead of completing.  That is the intended failure mode — §5 makes the
// feeder derive TLAST from the same count register, so a short stream is a
// feeder fault, and a visible timeout/reset beats a batch that completes
// with plausible-looking filler.  The hazard §5 actually set out to remove —
// a stream that never asserts TLAST at all — is still gone, because the loop
// bound is the count and not TLAST (see the late-TLAST testbench case).
//
// num_cands == 0 emits nothing on either stream and completes with zeroed
// status.  The driver should not start an empty batch.
//
// Globally invalid image configuration (§4.3): all num_cands descriptors
// are still consumed, each metadata record carries reason bit 8 with zeroed
// geometry, NO DDR read is issued, PE_SF_GLOBAL_INVALID and the rejected
// count are latched, and the core completes normally — draining the feeder
// instead of stranding it.
// -----------------------------------------------------------------------

void patch_extract_core(
    hls::stream<cand_stream_t>&  cand_in,
    hls::stream<ppix_stream_t>&  patch_out,
    hls::stream<pmeta_stream_t>& meta_out,
    const unsigned char*         bin_image,
    ap_uint<16> img_w,
    ap_uint<16> img_h,
    ap_uint<32> stride_bytes,
    ap_uint<32> buffer_bytes,
    ap_uint<16> num_cands,
    ap_uint<32>& sts_flags,
    ap_uint<32>& sts_rejected,
    ap_uint<32>& sts_processed)
{
#pragma HLS INTERFACE axis        port=cand_in
#pragma HLS INTERFACE axis        port=patch_out
#pragma HLS INTERFACE axis        port=meta_out
#pragma HLS INTERFACE m_axi       port=bin_image offset=slave bundle=BIN_IMG \
                                  depth=524288 max_read_burst_length=16
// CONTROL SURFACE (contract §7.1) — one AXI4-Lite slave, not two.
//
// offset=slave puts bin_image's 64-bit DDR base into an AXI-Lite register,
// but it does NOT choose which bundle.  Without the s_axilite line below,
// HLS invents a second slave named `control` for it, and `return` without
// s_axilite leaves ap_start/ap_done as raw top-level pins.  Synthesis then
// produced three separate control surfaces for one core — the block design
// would have to hook up two AXI-Lite masters and wire the handshake by hand,
// and the addresses collide confusingly (`control` 0x10 is the DDR base,
// `CTRL` 0x10 is img_w).  Neither csim nor cosim can see any of this; it is
// visible only in the synthesis interface report and the generated
// xpatch_extract_core_hw.h.  Bundling both into CTRL is what makes the
// generated header a single map the driver can be regenerated from.
#pragma HLS INTERFACE s_axilite   port=bin_image     bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=img_w         bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=img_h         bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=stride_bytes  bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=buffer_bytes  bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=num_cands     bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=sts_flags     bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=sts_rejected  bundle=CTRL
#pragma HLS INTERFACE s_axilite   port=sts_processed bundle=CTRL
#pragma HLS INTERFACE ap_ctrl_hs  port=return
#pragma HLS INTERFACE s_axilite   port=return        bundle=CTRL

    int iw = (int)img_w;
    int ih = (int)img_h;

    // ---- Global image configuration validation (§4.1, §2.1) --------------
    // The product is computed in a type wider than either operand and
    // checked against BOTH buffer_bytes and the 32-bit offset range.
    ap_uint<48> footprint = (ap_uint<48>)stride_bytes * (ap_uint<48>)img_h;
    bool global_invalid =
        (iw < PE_MIN_IMG_DIM) || (iw > PE_MAX_IMG_W)  ||
        (ih < PE_MIN_IMG_DIM) || (ih > PE_MAX_IMG_H)  ||
        (stride_bytes < (ap_uint<32>)img_w)           ||
        (footprint > (ap_uint<48>)buffer_bytes)       ||
        (footprint > (ap_uint<48>)0xFFFFFFFFULL);

    bool tlast_mismatch  = false;
    ap_uint<32> n_rejected  = 0;
    ap_uint<32> n_processed = 0;

    int n = (int)num_cands;

    process_cands: for (int i = 0; i < n; i++) {
        // LOOP_TRIPCOUNT is a LATENCY-ESTIMATE HINT ONLY.  It does not bound
        // the loop and it synthesises to nothing: num_cands is 16-bit, so the
        // hardware runs up to 65535 candidates and truncates none of them.
        // The 64 mirrors _MAX_CANDIDATES in sw/tme_driver.py, which is a
        // driver-side buffer-allocation limit enforced there (the driver
        // raises above it).  If that limit moves, update this for accurate
        // reports — nothing functional depends on it.
#pragma HLS LOOP_TRIPCOUNT min=1 max=64 avg=30
        // ---- Read one descriptor.  Always.  See the FRAMING banner: the
        // count is the authority, so the beat is consumed unconditionally and
        // TLAST is only compared against the position it must occupy.  Early
        // TLAST and a missing final TLAST are the two ways this differs, and
        // both are the same flag — neither truncates the batch.
        cand_stream_t cand = cand_in.read();
        ap_uint<64> d = cand.data;
        n_processed++;
        bool want_last = (i == n - 1);
        if ((cand.last != 0) != want_last)
            tlast_mismatch = true;

        // ---- Decode ------------------------------------------------------
        ap_uint<16> ep_x   = d.range(15,  0);
        ap_uint<16> ep_y   = d.range(31, 16);
        ap_uint<2>  side   = d.range(33, 32);   // 0=left, 1=right, >1 invalid
        ap_uint<14> max_tw = d.range(47, 34);
        ap_uint<16> max_th = d.range(63, 48);

        // ---- Validate (§4.1) and compute geometry ------------------------
        ap_uint<9> reason = 0;
        // Metadata geometry fields are 16-bit and carry the post-clip box
        // even for rejected descriptors (bounded by the image, never by the
        // matcher), so rejects can be diagnosed from the record alone.
        ap_uint<16> meta_x0 = 0, meta_y0 = 0, meta_pw = 0, meta_ph = 0;
        bool valid = false;

        if (global_invalid) {
            // §4.3: reason bit 8 only, zeroed geometry, no other checks.
            // Every ordinal still consumes its descriptor above, which is
            // what drains the feeder instead of stranding it.
            reason[PE_R_GLOBAL] = 1;
        } else {
            if ((int)ep_x >= iw)                                    reason[PE_R_EPX_OOR]  = 1;
            if ((int)ep_y >= ih)                                    reason[PE_R_EPY_OOR]  = 1;
            if ((int)max_tw < PE_MIN_TEMPL || (int)max_tw > PE_MAX_TEMPL_W) reason[PE_R_TW_RANGE] = 1;
            if ((int)max_th < PE_MIN_TEMPL || (int)max_th > PE_MAX_TEMPL_H) reason[PE_R_TH_RANGE] = 1;
            if (side > 1)                                           reason[PE_R_SIDE]     = 1;

            // Patch geometry.  2.4 / 1.4 / 3.2 are the exact rationals
            // 12/5, 7/5 and 16/5, but writing them as a divide by 5 makes
            // HLS emit a 41-bit reciprocal multiply that measured 6.445 ns
            // against a 3.65 ns effective budget.  Decompose instead; these
            // identities are exact because 3v, 2v and v are whole numbers:
            //     floor(12v/5) = 2v + floor(2v/5)
            //     floor( 7v/5) =  v + floor(2v/5)
            //     floor(16v/5) = 3v + floor( v/5)
            // and both quotients come from one reciprocal constant:
            //     floor( v/5) = (v * 52429) >> 18
            //     floor(2v/5) = (v * 52429) >> 17
            // Verified bit-identical to floor(num*v/5) across the entire
            // 16-bit descriptor range.  Do not "simplify" back to / 5.
            ap_uint<32> tw_q = max_tw * ap_uint<16>(52429);
            ap_uint<32> th_q = max_th * ap_uint<16>(52429);

            int tw_2fifths = (int)(tw_q >> 17);            // floor(2*max_tw/5)
            int outward_w  = 2 * (int)max_tw + tw_2fifths; // x2.4
            int inward_w   =     (int)max_tw + tw_2fifths; // x1.4
            int patch_h    = 3 * (int)max_th + (int)(th_q >> 18);  // x3.2

            // side==0 is left; anything else uses the right-side formula,
            // matching the reference — side>1 is already flagged above.
            int x0, x1;
            if (side == 0) {
                x0 = (int)ep_x - outward_w;
                x1 = (int)ep_x + inward_w;
            } else {
                x0 = (int)ep_x - inward_w;
                x1 = (int)ep_x + outward_w;
            }
            int y0 = (int)ep_y - patch_h / 2;
            int y1 = y0 + patch_h;

            // Clamp to image bounds.  x0/y0 need an upper clamp too: a
            // candidate past the right/bottom edge otherwise leaves x0 > iw,
            // and the minimum-size bump below then drives an out-of-range
            // read.  Capping at iw-2 / ih-2 keeps the bumped x1/y1 inside
            // the image.  Upper clamp runs first so the lower clamp still
            // wins if iw/ih are small (>= 3 is guaranteed here, so the
            // bump's 2-pixel floor always fits).
            if (x0 > iw - 2) x0 = iw - 2;
            if (y0 > ih - 2) y0 = ih - 2;
            if (x0 < 0)  x0 = 0;
            if (y0 < 0)  y0 = 0;
            if (x1 > iw) x1 = iw;
            if (y1 > ih) y1 = ih;
            // The minimum-size bump is deterministic extractor robustness
            // and stays (§4.1); the `valid` bit is what keeps a bumped 2x2
            // patch away from the matcher.
            if (x1 <= x0 + 1) x1 = x0 + 2;
            if (y1 <= y0 + 1) y1 = y0 + 2;

            int pw = x1 - x0;
            int ph = y1 - y0;

            // Post-clip checks.  Bits 5/6 are implied by bits 2/3 (a legal
            // 216x96 template yields at most 820x307) but kept independent
            // so a clipping or bump change that breaks the implication is
            // caught.  Bit 7 uses >=: equality became legal when the
            // matcher's +1 fix landed (§4.4 option 1).
            if (pw > PE_MAX_PATCH_W)                reason[PE_R_PATCH_W] = 1;
            if (ph > PE_MAX_PATCH_H)                reason[PE_R_PATCH_H] = 1;
            if (pw < (int)max_tw || ph < (int)max_th) reason[PE_R_PATCH_SMALL] = 1;

            meta_x0 = (ap_uint<16>)x0;
            meta_y0 = (ap_uint<16>)y0;
            meta_pw = (ap_uint<16>)pw;
            meta_ph = (ap_uint<16>)ph;
            valid = (reason == 0);
        }

        // ---- Emit the metadata record (§6.2) -----------------------------
        // Every record now describes a descriptor that was really read, so
        // status == 0 exactly is unreachable: valid=0 always carries at
        // least one reason bit.  (It used to mark a filler ordinal.)
        ap_uint<16> status = 0;
        status[0] = valid ? 1 : 0;
        status.range(9, 1) = reason;

        pmeta_stream_t meta;
        meta.data = 0;
        meta.data.range(15,  0) = (ap_uint<16>)i;   // cand_id = ordinal
        meta.data.range(31, 16) = status;
        meta.data.range(47, 32) = meta_x0;
        meta.data.range(63, 48) = meta_y0;
        meta.data.range(79, 64) = meta_pw;
        meta.data.range(95, 80) = meta_ph;
        // [127:96] reserved, already zero
        meta.last = (i == n - 1) ? 1 : 0;
        meta.keep = 0xFFFF;   // 16 byte lanes on ap_axiu<128,1,1,1>
        meta.strb = 0xFFFF;
        meta.user = 0;
        meta.id   = 0;
        meta.dest = 0;
        meta_out.write(meta);

        if (!valid) {
            n_rejected++;
            continue;           // no pixel payload, no DDR read (§4)
        }

        // ---- Stream the patch --------------------------------------------
        // WIDTH POLICY (§3) — different quantities, different widths:
        //
        //   page coordinate (bx, by)   16 bits.  Bounded by the image
        //                              (<= 9856/6400), matching the ep_x/
        //                              ep_y descriptor fields and img_w/
        //                              img_h ports, so the ABI carries one
        //                              width end to end.  12 bits died the
        //                              moment this core saw a real
        //                              binarizer page (9792 x 6336 at
        //                              ZOOM=4.0 — x0=5000 silently became
        //                              904).
        //
        //   patch counters (pw, ph)    11 / 9 bits — they SHRINK.  A patch
        //                              is bounded by what the matcher can
        //                              hold (patch_buf[307][820]), not by
        //                              the page.  §4 validation makes these
        //                              widths unreachable-by-construction
        //                              safe: valid implies pw <= 820 and
        //                              ph <= 307.  As plain ints, HLS
        //                              widened the column induction
        //                              variable to 63 bits — 6.670 ns on
        //                              the increment.  Do not widen these.
        //
        //   linear DDR offset          32 bits.  In range because the §2.1
        //                              footprint check passed (see banner).
        ap_uint<16> bx = meta_x0;
        ap_uint<16> by = meta_y0;
        ap_uint<11> pwn = (ap_uint<11>)meta_pw;
        ap_uint<9>  phn = (ap_uint<9>)meta_ph;
        ap_uint<9>  last_r = phn - 1;
        ap_uint<11> last_c = pwn - 1;

        // Stream the patch, holding the final pixel back.  Computing TLAST
        // as (r==ph-1 && c==pw-1) puts a comparator cone directly in front
        // of the AXIS write; emitting the last beat separately keeps TLAST
        // a hard 0 inside the loops.  Validation guarantees pw,ph >= 4, so
        // every loop below executes.
        full_rows: for (ap_uint<9> r = 0; r < last_r; r++) {
#pragma HLS LOOP_TRIPCOUNT min=3 max=PE_MAX_PATCH_H avg=300
            // Row base address: logical (bx, by+r) at byte offset
            // (by+r)*stride + bx.  One multiply per row, hoisted out of the
            // pixel loop so the per-pixel path stays an increment + add.
#pragma HLS LOOP_FLATTEN off
            ap_uint<32> row_base =
                (ap_uint<32>)((ap_uint<17>)by + r) * stride_bytes + bx;
            full_cols: for (ap_uint<11> c = 0; c < pwn; c++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=PE_MAX_PATCH_W avg=400
                ppix_stream_t out_px;
                out_px.data = bin_image[row_base + c];
                out_px.last = 0;
                out_px.keep = 1;   // 1-bit TKEEP/TSTRB on ap_axiu<8,1,1,1>
                out_px.strb = 1;
                out_px.user = 0;
                out_px.id   = 0;
                out_px.dest = 0;
                patch_out.write(out_px);
            }
        }

        ap_uint<32> last_row_base =
            (ap_uint<32>)((ap_uint<17>)by + last_r) * stride_bytes + bx;
        last_row: for (ap_uint<11> c = 0; c < last_c; c++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=3 max=PE_MAX_PATCH_W avg=400
            ppix_stream_t out_px;
            out_px.data = bin_image[last_row_base + c];
            out_px.last = 0;
            out_px.keep = 1;
            out_px.strb = 1;
            out_px.user = 0;
            out_px.id   = 0;
            out_px.dest = 0;
            patch_out.write(out_px);
        }

        // The one beat per patch that carries TLAST (§5: per-patch framing).
        ppix_stream_t final_px;
        final_px.data = bin_image[last_row_base + last_c];
        final_px.last = 1;
        final_px.keep = 1;
        final_px.strb = 1;
        final_px.user = 0;
        final_px.id   = 0;
        final_px.dest = 0;
        patch_out.write(final_px);
    }

    // ---- Latch status (§7) -----------------------------------------------
    ap_uint<32> flags = 0;
    flags[PE_SF_GLOBAL_INVALID] = global_invalid ? 1 : 0;
    flags[PE_SF_TLAST_MISMATCH] = tlast_mismatch ? 1 : 0;
    sts_flags     = flags;
    sts_rejected  = n_rejected;
    sts_processed = n_processed;
}
