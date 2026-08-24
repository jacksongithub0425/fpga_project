// Testbench for patch_extract_core — final DDR + metadata/framing interface.
//
// Loads the manifest and golden pixels written by
// patch_extract_generate_golden.py, streams every candidate through the DUT
// in a single batch, and requires exact agreement — no tolerance.  The
// reference build_endpoint_patch() plus the Python §4 validation model are
// bit-exact with the core, so any mismatch is a real defect, not rounding.
//
// Checked per metadata record: cand_id, status (valid | reason<<1), x0, y0,
// patch_w, patch_h, TKEEP/TSTRB (0xFFFF), TUSER/TID/TDEST zero, and TLAST
// on exactly the final record of the batch.
// Checked per pixel beat: value, ordering, TKEEP, TSTRB, TLAST asserted on
// exactly the final pixel of EACH valid patch, sidebands zero.  Invalid
// candidates must contribute zero pixel beats.
// Checked per batch: status registers (flags, rejected, processed), total
// beat counts, and that both streams drain empty.
//
// Beyond the manifest suite:
//   - a compact-stride pass (stride == img_w) over the same golden, so a
//     core that hardcodes either stride fails one of the two passes;
//   - a high-coordinate pass on a procedural 9800x6400 page with stride
//     9856, exercising bx/by above the old 12-bit width and the
//     patch-width reason bit (270 -> 1026 overruns patch_buf only on a
//     page this wide);
//   - four globally-invalid image configurations (§4.3), including one
//     whose stride*img_h wraps 32 bits — a footprint computed in a narrow
//     type PASSES the buffer check there, so this is the §2.1 catch;
//   - early- and late-TLAST batches (§5: num_cands is the authority, TLAST
//     is a cross-check that must set PE_SF_TLAST_MISMATCH).  ONE DEFECT
//     EACH, on purpose: the early case delivers all num_cands descriptors
//     with a spurious marker on an interior ordinal AND the correct marker
//     on the last, so the extra marker is the only anomaly; the late case
//     omits the final marker and adds nothing else.  A case carrying both
//     at once is passed by a core that implements either one.  The early
//     case then runs a second, correctly framed batch on the same streams
//     WITHOUT draining, so a core that truncates fails by handing batch 2
//     the leftovers of batch 1 — the actual hazard, not just a status flag.
//     Note there is deliberately NO short-stream case — fewer beats than
//     num_cands is a feeder fault that blocks in cand_in.read() by design,
//     so a testbench for it would simply hang;
//   - a two-pass re-invocation check with distinct endpoints, to catch
//     state surviving a return;
//   - an empty batch (num_cands == 0), which must emit nothing.
//
// C simulation uses everything; co-simulation drops the compact-stride,
// high-coordinate and empty-batch passes and uses the small cosim manifest,
// selected by passing "cosim" as an argument:
//
//   csim_design
//   cosim_design -rtl verilog -argv "cosim"
//
// This is deliberately a runtime argument rather than a compile-time macro.
// Vitis HLS 2025.2 does NOT define __RTL_SIMULATION__ when it compiles this
// testbench for co-simulation, so a macro switch silently falls through to
// the full suite and pushes millions of beats through RTL.
//
// The DDR buffer handed to the DUT is the static ddr_buf below, sized
// PE_COSIM_BUF_BYTES to match the m_axi depth pragma — the cosim wrapper
// snapshots exactly depth bytes from the pointer, so every cosim-mode call
// must pass a buffer at least that large.  The csim-only high-coordinate
// pass uses its own heap buffer, which cosim never sees.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "patch_extract_core.h"

static const char* CSIM_CASES     = "tb_patch_extract_cases_csim.txt";
static const char* CSIM_GOLDEN    = "tb_patch_extract_golden_csim.bin";
static const char* COSIM_CASES    = "tb_patch_extract_cases_cosim.txt";
static const char* COSIM_GOLDEN   = "tb_patch_extract_golden_cosim.bin";
static const char* HICOORD_CASES  = "tb_patch_extract_cases_hicoord.txt";
static const char* HICOORD_GOLDEN = "tb_patch_extract_golden_hicoord.bin";
static const char* IMAGE_FILE     = "tb_patch_extract_image.bin";

// Written into every DDR-buffer byte outside the placed img_w x img_h
// region — including the stride padding at the end of each row.  The
// generator guarantees this value never occurs in a valid pixel, so reading
// it back proves an out-of-region or stride-confused access.
static const unsigned char SENTINEL = 0xA5;

// The manifest suites run with this non-compact stride (img_w is 1009, so
// 15 padding bytes of sentinel end every row).  Must divide into
// PE_COSIM_BUF_BYTES with room for img_h rows.
static const unsigned STRIDE_A = 1024;

static const int MAX_CANDS = 256;
static const int MAX_REPORTED = 12;   // cap on detailed mismatch lines

struct Case {
    int index, last, ep_x, ep_y, side, max_tw, max_th;
    int x0, y0, x1, y1, offset, count, valid, tme_legal;
    unsigned reason;
    unsigned long long packed;
    char category[32];
    char tag[64];
};

// The DUT's DDR buffer for all cosim-capable passes.  Static, not stack.
static unsigned char ddr_buf[PE_COSIM_BUF_BYTES];

static int g_reported = 0;
static bool report_ok() { return g_reported++ < MAX_REPORTED; }

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// C++ twin of pattern_block() in patch_extract_generate_golden.py — used
// only by the high-coordinate pass, and cross-checked against the probe
// triples in that manifest before anything runs.  Keep in sync.
static unsigned char pattern_px(int x, int y)
{
    unsigned v = (29u * (unsigned)x + 17u * (unsigned)y
                  + (unsigned)(x ^ y) + (unsigned)(x % 3)) & 0xFFu;
    if (v == 0xA5u) v = 0xA4u;
    return (unsigned char)v;
}

static unsigned long long pack64(int ep_x, int ep_y, int side, int tw, int th)
{
    return (unsigned long long)(unsigned)ep_x
         | ((unsigned long long)(unsigned)ep_y  << 16)
         | ((unsigned long long)(unsigned)side  << 32)
         | ((unsigned long long)(unsigned)tw    << 34)
         | ((unsigned long long)(unsigned)th    << 48);
}

static void write_cand(hls::stream<cand_stream_t>& cs,
                       unsigned long long packed, int last)
{
    cand_stream_t w;
    w.data = packed;
    w.last = last;
    // Drive every sideband explicitly rather than leaning on ap_axiu's
    // default constructor, so the input contract is stated in the source.
    // Mask width is ceil(TDATA/8), so ap_axiu<64,1,1,1> carries 8-bit
    // TKEEP/TSTRB.  The core reads just .data and .last.
    w.keep = 0xFF;
    w.strb = 0xFF;
    w.user = 0;
    w.id   = 0;
    w.dest = 0;
    cs.write(w);
}

// Place a compact iw x ih image into a strided buffer, sentinel-prefilled.
static void place_image(const unsigned char* compact, int iw, int ih,
                        unsigned stride, unsigned char* buf, unsigned buf_bytes)
{
    memset(buf, SENTINEL, buf_bytes);
    for (int y = 0; y < ih; y++)
        memcpy(buf + (size_t)y * stride, compact + (size_t)y * iw, iw);
}

// Check one metadata record against expectations.  Returns error count.
static int check_meta(pmeta_stream_t m, int ordinal, int exp_valid,
                      unsigned exp_reason, int exp_x0, int exp_y0,
                      int exp_pw, int exp_ph, int exp_last, const char* tag)
{
    int errors = 0;
    unsigned got_id     = (unsigned)m.data.range(15, 0);
    unsigned got_status = (unsigned)m.data.range(31, 16);
    unsigned got_x0     = (unsigned)m.data.range(47, 32);
    unsigned got_y0     = (unsigned)m.data.range(63, 48);
    unsigned got_pw     = (unsigned)m.data.range(79, 64);
    unsigned got_ph     = (unsigned)m.data.range(95, 80);
    unsigned got_res    = (unsigned)m.data.range(127, 96);
    unsigned exp_status = (unsigned)exp_valid | (exp_reason << 1);

    if (got_id != (unsigned)ordinal || got_status != exp_status ||
        got_x0 != (unsigned)exp_x0 || got_y0 != (unsigned)exp_y0 ||
        got_pw != (unsigned)exp_pw || got_ph != (unsigned)exp_ph ||
        got_res != 0) {
        errors++;
        if (report_ok())
            printf("  META %s [%d]: got id=%u status=0x%03x box=(%u,%u %ux%u) "
                   "res=0x%x, expected status=0x%03x box=(%d,%d %dx%d)\n",
                   tag, ordinal, got_id, got_status, got_x0, got_y0, got_pw,
                   got_ph, got_res, exp_status, exp_x0, exp_y0, exp_pw, exp_ph);
    }
    if ((int)m.last != exp_last) {
        errors++;
        if (report_ok())
            printf("  META %s [%d]: TLAST=%d expected %d\n",
                   tag, ordinal, (int)m.last, exp_last);
    }
    // ap_axiu<128,1,1,1> carries 16-bit TKEEP/TSTRB; TUSER/TID/TDEST are
    // 1-bit and pinned to zero so a future sideband use is deliberate.
    if (m.keep != 0xFFFF || m.strb != 0xFFFF ||
        m.user != 0 || m.id != 0 || m.dest != 0) {
        errors++;
        if (report_ok())
            printf("  META %s [%d]: keep=0x%x strb=0x%x user=%d id=%d dest=%d\n",
                   tag, ordinal, (unsigned)m.keep, (unsigned)m.strb,
                   (int)m.user, (int)m.id, (int)m.dest);
    }
    return errors;
}

// Check one pixel beat.  want_last is per-patch framing (§5).
static int check_pix(ppix_stream_t px, unsigned char exp, int want_last,
                     const char* tag, int beat, int* sentinel_hit)
{
    int errors = 0;
    unsigned char dut = (unsigned char)px.data;
    if (dut == SENTINEL) (*sentinel_hit)++;
    if (dut != exp) {
        errors++;
        if (report_ok())
            printf("  PIX %s beat %d: got 0x%02X expected 0x%02X%s\n",
                   tag, beat, dut, exp,
                   (dut == SENTINEL) ? "  <-- SENTINEL: outside image/stride"
                                     : "");
    }
    if ((int)px.last != want_last) {
        errors++;
        if (report_ok())
            printf("  PIX %s beat %d: TLAST=%d expected %d\n",
                   tag, beat, (int)px.last, want_last);
    }
    if (px.keep != 1 || px.strb != 1 ||
        px.user != 0 || px.id != 0 || px.dest != 0) {
        errors++;
        if (report_ok())
            printf("  PIX %s beat %d: keep=%d strb=%d user=%d id=%d dest=%d\n",
                   tag, beat, (int)px.keep, (int)px.strb,
                   (int)px.user, (int)px.id, (int)px.dest);
    }
    return errors;
}

static int check_status(ap_uint<32> flags, ap_uint<32> rejected,
                        ap_uint<32> processed, unsigned exp_flags,
                        unsigned exp_rejected, unsigned exp_processed,
                        const char* label)
{
    if ((unsigned)flags == exp_flags && (unsigned)rejected == exp_rejected &&
        (unsigned)processed == exp_processed)
        return 0;
    printf("  STATUS %s: flags=0x%x rejected=%u processed=%u, "
           "expected 0x%x/%u/%u\n", label, (unsigned)flags,
           (unsigned)rejected, (unsigned)processed,
           exp_flags, exp_rejected, exp_processed);
    return 1;
}

// ---------------------------------------------------------------------------
// Manifest-driven batch: drive all candidates, verify metadata, pixels,
// framing and status registers.
// ---------------------------------------------------------------------------
static int run_batch(const Case* cases, int ncand,
                     const unsigned char* golden, int total_bytes,
                     const unsigned char* buf, int iw, int ih,
                     unsigned stride, unsigned buffer_bytes,
                     const char* label, bool print_cases)
{
    hls::stream<cand_stream_t>  cand_stream("cand");
    hls::stream<ppix_stream_t>  patch_stream("patch");
    hls::stream<pmeta_stream_t> meta_stream("meta");

    for (int i = 0; i < ncand; i++)
        write_cand(cand_stream, cases[i].packed, cases[i].last);

    ap_uint<32> flags = 0xDEAD, rejected = 0xDEAD, processed = 0xDEAD;
    patch_extract_core(cand_stream, patch_stream, meta_stream,
                       buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)ncand, flags, rejected, processed);

    int errors = 0, sentinel_hit = 0;
    int n_invalid = 0;
    int emitted = (int)patch_stream.size();

    for (int i = 0; i < ncand; i++) {
        const Case& c = cases[i];
        int cand_err = 0;
        int pw = c.x1 - c.x0, ph = c.y1 - c.y0;
        if (!c.valid) n_invalid++;

        // ---- Metadata record --------------------------------------------
        if (meta_stream.empty()) {
            errors++; cand_err++;
            if (report_ok())
                printf("  META %s: stream ran dry at record %d\n", c.tag, i);
        } else {
            int e = check_meta(meta_stream.read(), i, c.valid, c.reason,
                               c.x0, c.y0, pw, ph,
                               (i == ncand - 1) ? 1 : 0, c.tag);
            errors += e; cand_err += e;
        }

        // ---- Pixel payload (valid candidates only) ----------------------
        for (int k = 0; k < c.count; k++) {
            if (patch_stream.empty()) {
                errors++; cand_err++;
                if (report_ok())
                    printf("  SHORT %s: pixel stream dry after %d of %d\n",
                           c.tag, k, c.count);
                break;
            }
            int want_last = (k == c.count - 1) ? 1 : 0;   // per-patch TLAST
            int e = check_pix(patch_stream.read(), golden[c.offset + k],
                              want_last, c.tag, k, &sentinel_hit);
            errors += e; cand_err += e;
        }

        if (print_cases)
            printf("  [%2d] %-24s s%d %3dx%-3d box=(%d,%d,%d,%d) v=%d "
                   "r=0x%03x %-14s %s\n",
                   c.index, c.tag, c.side, c.max_tw, c.max_th,
                   c.x0, c.y0, c.x1, c.y1, c.valid, c.reason,
                   c.category, cand_err ? "FAIL" : "ok");
    }

    int leftover_pix  = (int)patch_stream.size();
    int leftover_meta = (int)meta_stream.size();
    if (emitted != total_bytes) {
        errors++;
        printf("  %s: emitted %d pixel beats, expected %d\n",
               label, emitted, total_bytes);
    }
    if (leftover_pix || leftover_meta) {
        errors++;
        printf("  %s: leftover beats — %d pixel, %d metadata\n",
               label, leftover_pix, leftover_meta);
    }
    if (sentinel_hit) {
        errors++;
        printf("  %s: %d sentinel reads (outside image or stride padding)\n",
               label, sentinel_hit);
    }
    errors += check_status(flags, rejected, processed,
                           0, (unsigned)n_invalid, (unsigned)ncand, label);

    printf("Batch %-18s: %s (%d cands, %d valid, %d pixel beats)\n",
           label, errors ? "FAIL" : "ok", ncand, ncand - n_invalid, emitted);
    return errors;
}

// ---------------------------------------------------------------------------
// Synthetic passes.  All use tiny 4x4-template descriptors whose geometry
// is trivially derivable: tw=4 gives outward 2*4+floor(8/5)=9, inward
// 4+floor(8/5)=5, so a left-side interior endpoint yields
// x0=ep_x-9, pw=14; th=4 gives patch_h 3*4+floor(4/5)=12, y0=ep_y-6.
// ---------------------------------------------------------------------------
static const int T4_PW = 14, T4_PH = 12;

static int expect_tiny_patch(hls::stream<ppix_stream_t>& ps,
                             const unsigned char* buf, unsigned stride,
                             int ep_x, int ep_y, const char* tag)
{
    int errors = 0, sentinel_hit = 0;
    int x0 = ep_x - 9, y0 = ep_y - 6;
    for (int k = 0; k < T4_PW * T4_PH; k++) {
        if (ps.empty()) {
            errors++;
            if (report_ok())
                printf("  SHORT %s: dry after %d of %d\n", tag, k, T4_PW * T4_PH);
            break;
        }
        int r = k / T4_PW, c = k % T4_PW;
        unsigned char exp = buf[(size_t)(y0 + r) * stride + (x0 + c)];
        int want_last = (k == T4_PW * T4_PH - 1) ? 1 : 0;
        errors += check_pix(ps.read(), exp, want_last, tag, k, &sentinel_hit);
    }
    if (sentinel_hit) errors++;
    return errors;
}

// §4.3: globally invalid image configuration.  All descriptors consumed,
// reason bit 8 on every record with zeroed geometry, no pixels, batch error
// flag latched, normal completion.
static int run_invalid_config(const unsigned char* buf, int iw, int ih,
                              unsigned stride, unsigned buffer_bytes,
                              const char* label)
{
    const int N = 3;
    hls::stream<cand_stream_t>  cs("ic_cand");
    hls::stream<ppix_stream_t>  ps("ic_patch");
    hls::stream<pmeta_stream_t> ms("ic_meta");
    for (int i = 0; i < N; i++)
        write_cand(cs, pack64(100 + i, 100, 0, 4, 4), (i == N - 1) ? 1 : 0);

    ap_uint<32> flags = 0, rejected = 0, processed = 0;
    patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)N, flags, rejected, processed);

    int errors = 0;
    for (int i = 0; i < N; i++) {
        if (ms.empty()) {
            errors++;
            if (report_ok()) printf("  META %s: dry at %d\n", label, i);
            break;
        }
        errors += check_meta(ms.read(), i, 0, (1u << PE_R_GLOBAL),
                             0, 0, 0, 0, (i == N - 1) ? 1 : 0, label);
    }
    if (!ps.empty()) {
        errors++;
        printf("  %s: %d pixel beats from an invalid config (must be 0)\n",
               label, (int)ps.size());
        while (!ps.empty()) ps.read();
    }
    if (!cs.empty()) {
        errors++;
        printf("  %s: %d descriptors left unconsumed (§4.3 requires draining)\n",
               label, (int)cs.size());
        while (!cs.empty()) cs.read();
    }
    errors += check_status(flags, rejected, processed,
                           1u << PE_SF_GLOBAL_INVALID, N, N, label);
    printf("Invalid config %-10s: %s\n", label, errors ? "FAIL" : "ok");
    return errors;
}

// Early TLAST, FULL-LENGTH stream (§5).  num_cands=4 and all four descriptors
// are delivered, but the feeder asserts TLAST on ordinal 1 — an off-by-one in
// a count-derived TLAST generator does exactly this.  The count is the
// authority, so the core must consume all four, emit four real records, frame
// four patches and flag the mismatch.
//
// This case replaces one that supplied only two beats for num_cands=4.  That
// version could not distinguish "stop at TLAST" from "read all N", because
// with a genuinely short stream there was nothing left to strand — which is
// precisely the behaviour that needed testing.  The two assertions this turns
// on are the cs-empty check below and the second batch that follows it.
static int run_tlast_early(const unsigned char* buf, int iw, int ih,
                           unsigned stride, unsigned buffer_bytes)
{
    const int N = 4;
    const int eps[N][2] = { {100,100}, {200,200}, {300,300}, {400,400} };
    hls::stream<cand_stream_t>  cs("te_cand");
    hls::stream<ppix_stream_t>  ps("te_patch");
    hls::stream<pmeta_stream_t> ms("te_meta");
    // TLAST on ordinal 1 AND on ordinal N-1.  The final marker is where it
    // belongs, so the ONLY anomaly is the spurious early one — which is what
    // isolates this case.  Marking only ordinal 1 (as an earlier version did)
    // trips two conditions at once, early-and-misplaced plus final-missing,
    // and a core that implemented just one of the two would still pass here.
    // The missing-final condition on its own is run_tlast_late's job.
    for (int i = 0; i < N; i++)
        write_cand(cs, pack64(eps[i][0], eps[i][1], 0, 4, 4),
                   (i == 1 || i == N - 1) ? 1 : 0);

    ap_uint<32> flags = 0, rejected = 0, processed = 0;
    patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)N, flags, rejected, processed);

    int errors = 0;
    for (int i = 0; i < N; i++) {
        if (ms.empty()) {
            errors++;
            if (report_ok()) printf("  META early-tlast: dry at %d\n", i);
            break;
        }
        errors += check_meta(ms.read(), i, 1, 0,
                             eps[i][0] - 9, eps[i][1] - 6, T4_PW, T4_PH,
                             (i == N - 1) ? 1 : 0, "early-tlast");
    }
    for (int p = 0; p < N; p++)
        errors += expect_tiny_patch(ps, buf, stride, eps[p][0], eps[p][1],
                                    "early-tlast");
    if (!ps.empty() || !ms.empty()) {
        errors++;
        printf("  early-tlast: leftover %d pixel / %d meta beats\n",
               (int)ps.size(), (int)ms.size());
    }
    // The assertion the old two-beat case could not make: a misplaced TLAST
    // must not leave descriptors behind for whoever runs next.  Deliberately
    // NOT drained — draining here would repair the damage before the second
    // batch below could observe it, which would make that batch's claim to
    // detect cross-batch corruption vacuous.  A failure is meant to cascade.
    if (!cs.empty()) {
        errors++;
        printf("  early-tlast: %d descriptors left queued — the next batch "
               "will consume them as its own (§5)\n", (int)cs.size());
    }
    errors += check_status(flags, rejected, processed,
                           1u << PE_SF_TLAST_MISMATCH, 0, N, "early-tlast");

    // Second batch on the SAME stream objects, correctly framed this time,
    // and queued behind whatever the first batch failed to consume.  Endpoints
    // are distinct from every endpoint above, so a stranded descriptor is read
    // here in place of eps2[0] and shows up as a metadata and pixel mismatch
    // rather than as a silent coincidence — the same reasoning as
    // run_reinvocation.  This is the end-to-end statement of the hazard: not
    // "the core reported a mismatch", but "the next batch got the wrong
    // patches".
    const int M = 2;
    const int eps2[M][2] = { {500,250}, {600,320} };
    for (int i = 0; i < M; i++)
        write_cand(cs, pack64(eps2[i][0], eps2[i][1], 0, 4, 4),
                   (i == M - 1) ? 1 : 0);

    flags = 0; rejected = 0; processed = 0;
    patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)M, flags, rejected, processed);

    for (int i = 0; i < M; i++) {
        if (ms.empty()) {
            errors++;
            if (report_ok()) printf("  META post-early-tlast: dry at %d\n", i);
            break;
        }
        errors += check_meta(ms.read(), i, 1, 0,
                             eps2[i][0] - 9, eps2[i][1] - 6, T4_PW, T4_PH,
                             (i == M - 1) ? 1 : 0, "post-early-tlast");
    }
    for (int p = 0; p < M; p++)
        errors += expect_tiny_patch(ps, buf, stride, eps2[p][0], eps2[p][1],
                                    "post-early-tlast");
    if (!ps.empty() || !ms.empty() || !cs.empty()) {
        errors++;
        printf("  post-early-tlast: leftover %d pixel / %d meta / %d cand\n",
               (int)ps.size(), (int)ms.size(), (int)cs.size());
    }
    // A clean batch must report clean status — in particular no mismatch
    // flag carried over from the first invocation.
    errors += check_status(flags, rejected, processed, 0, 0, M,
                           "post-early-tlast");
    printf("Early TLAST          : %s\n", errors ? "FAIL" : "ok");
    return errors;
}

// Late TLAST: the final descriptor lacks TLAST.  The count register still
// terminates the batch (no hang — that is the point of §5); the mismatch
// flag must be set and everything else must proceed normally.
static int run_tlast_late(const unsigned char* buf, int iw, int ih,
                          unsigned stride, unsigned buffer_bytes)
{
    const int N = 2;
    const int eps[2][2] = { { 120, 140 }, { 300, 200 } };
    hls::stream<cand_stream_t>  cs("tl_cand");
    hls::stream<ppix_stream_t>  ps("tl_patch");
    hls::stream<pmeta_stream_t> ms("tl_meta");
    write_cand(cs, pack64(eps[0][0], eps[0][1], 0, 4, 4), 0);
    write_cand(cs, pack64(eps[1][0], eps[1][1], 0, 4, 4), 0);  // missing TLAST

    ap_uint<32> flags = 0, rejected = 0, processed = 0;
    patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)N, flags, rejected, processed);

    int errors = 0;
    for (int i = 0; i < N; i++) {
        if (ms.empty()) { errors++; break; }
        errors += check_meta(ms.read(), i, 1, 0,
                             eps[i][0] - 9, eps[i][1] - 6, T4_PW, T4_PH,
                             (i == N - 1) ? 1 : 0, "late-tlast");
    }
    for (int p = 0; p < 2; p++)
        errors += expect_tiny_patch(ps, buf, stride, eps[p][0], eps[p][1],
                                    "late-tlast");
    if (!ps.empty() || !ms.empty()) errors++;
    errors += check_status(flags, rejected, processed,
                           1u << PE_SF_TLAST_MISMATCH, 0, 2, "late-tlast");
    printf("Late TLAST           : %s\n", errors ? "FAIL" : "ok");
    return errors;
}

// Two-invocation check.  Hardware runs the core once per page; state that
// survives a return — a stale counter, an undrained stream, a latched flag —
// only shows on the second call.  The two passes use different endpoints on
// purpose: repeating one descriptor would prove the FSM restarts, but a
// stale descriptor register would still pass, because the retained value and
// the expected value coincide.  With distinct coordinates, pass 1 reading
// pass 0's endpoint is a pixel mismatch.
static int run_reinvocation(const unsigned char* buf, int iw, int ih,
                            unsigned stride, unsigned buffer_bytes)
{
    const int ep[2][2] = { { iw / 2, ih / 2 }, { iw / 4, ih / 4 } };
    int errors = 0;
    for (int pass = 0; pass < 2; pass++) {
        hls::stream<cand_stream_t>  cs("ri_cand");
        hls::stream<ppix_stream_t>  ps("ri_patch");
        hls::stream<pmeta_stream_t> ms("ri_meta");
        write_cand(cs, pack64(ep[pass][0], ep[pass][1], 0, 4, 4), 1);

        ap_uint<32> flags = 0, rejected = 0, processed = 0;
        patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                           (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                           (ap_uint<16>)1, flags, rejected, processed);

        if (ms.empty()) { errors++; continue; }
        errors += check_meta(ms.read(), 0, 1, 0,
                             ep[pass][0] - 9, ep[pass][1] - 6, T4_PW, T4_PH,
                             1, "reinvoke");
        errors += expect_tiny_patch(ps, buf, stride, ep[pass][0], ep[pass][1],
                                    "reinvoke");
        if (!ps.empty() || !ms.empty()) errors++;
        errors += check_status(flags, rejected, processed, 0, 0, 1, "reinvoke");
    }
    printf("Re-invocation        : %s (passes at (%d,%d) and (%d,%d))\n",
           errors ? "FAIL" : "ok", ep[0][0], ep[0][1], ep[1][0], ep[1][1]);
    return errors;
}

// num_cands == 0: nothing on either stream, zeroed status.
static int run_empty_batch(const unsigned char* buf, int iw, int ih,
                           unsigned stride, unsigned buffer_bytes)
{
    hls::stream<cand_stream_t>  cs("eb_cand");
    hls::stream<ppix_stream_t>  ps("eb_patch");
    hls::stream<pmeta_stream_t> ms("eb_meta");
    ap_uint<32> flags = 0xDEAD, rejected = 0xDEAD, processed = 0xDEAD;
    patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)iw, (ap_uint<16>)ih,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)0, flags, rejected, processed);
    int errors = 0;
    if (!ps.empty() || !ms.empty()) {
        errors++;
        printf("  empty-batch: emitted %d pixel / %d meta beats\n",
               (int)ps.size(), (int)ms.size());
    }
    errors += check_status(flags, rejected, processed, 0, 0, 0, "empty-batch");
    printf("Empty batch          : %s\n", errors ? "FAIL" : "ok");
    return errors;
}

// ---------------------------------------------------------------------------
// Manifest loading + prevalidation
// ---------------------------------------------------------------------------
static int load_rows(FILE* fp, Case* cases, int ncand, const char* fname)
{
    for (int i = 0; i < ncand; i++) {
        Case& c = cases[i];
        int n = fscanf(fp,
                "%d %llx %d %d %d %d %d %d %d %d %d %d %d %d %d %x %d %31s %63s",
                &c.index, &c.packed, &c.last,
                &c.ep_x, &c.ep_y, &c.side, &c.max_tw, &c.max_th,
                &c.x0, &c.y0, &c.x1, &c.y1,
                &c.offset, &c.count, &c.valid, &c.reason, &c.tme_legal,
                c.category, c.tag);
        if (n != 19) {
            fprintf(stderr, "%s: malformed row %d (read %d fields)\n",
                    fname, i, n);
            return 1;
        }
        if (c.index != i) {
            fprintf(stderr, "%s: row %d has index %d\n", fname, i, c.index);
            return 1;
        }
    }
    return 0;
}

// The core's descriptor reads block, so a manifest whose final candidate
// lacks TLAST would flag a mismatch rather than hang (the count register is
// the §5 fix) — but a malformed manifest should still fail loudly here, not
// as a confusing DUT diff.
static int prevalidate(const Case* cases, int ncand, int iw, int ih,
                       int total_bytes, const char* fname)
{
    int bad = 0, running = 0;
    if (ncand < 1) { fprintf(stderr, "%s: ncand=%d\n", fname, ncand); bad++; }
    if (iw < PE_MIN_IMG_DIM || ih < PE_MIN_IMG_DIM) {
        fprintf(stderr, "%s: image %dx%d is degenerate\n", fname, iw, ih); bad++;
    }
    for (int i = 0; i < ncand; i++) {
        const Case& c = cases[i];
        int want_last = (i == ncand - 1) ? 1 : 0;
        int pw = c.x1 - c.x0, ph = c.y1 - c.y0;
        if (c.last != want_last) {
            fprintf(stderr, "%s: %s has last=%d, expected %d\n",
                    fname, c.tag, c.last, want_last);
            bad++;
        }
        if (!(0 <= c.x0 && c.x0 < c.x1 && c.x1 <= iw) ||
            !(0 <= c.y0 && c.y0 < c.y1 && c.y1 <= ih)) {
            fprintf(stderr, "%s: %s box (%d,%d,%d,%d) outside %dx%d\n",
                    fname, c.tag, c.x0, c.y0, c.x1, c.y1, iw, ih);
            bad++;
        }
        if ((c.valid != 0) != (c.reason == 0)) {
            fprintf(stderr, "%s: %s valid=%d but reason=0x%x\n",
                    fname, c.tag, c.valid, c.reason);
            bad++;
        }
        if (c.tme_legal != c.valid) {
            fprintf(stderr, "%s: %s tme_legal=%d != valid=%d\n",
                    fname, c.tag, c.tme_legal, c.valid);
            bad++;
        }
        int want_count = c.valid ? pw * ph : 0;
        if (c.count != want_count) {
            fprintf(stderr, "%s: %s count=%d, expected %d\n",
                    fname, c.tag, c.count, want_count);
            bad++;
        }
        if (c.offset != running) {
            fprintf(stderr, "%s: %s offset=%d, expected %d\n",
                    fname, c.tag, c.offset, running);
            bad++;
        }
        running += c.count;
    }
    if (running != total_bytes) {
        fprintf(stderr, "%s: offsets total %d, header says %d\n",
                fname, running, total_bytes);
        bad++;
    }
    return bad;
}

static unsigned char* load_file(const char* path, int expect_bytes)
{
    unsigned char* buf = new unsigned char[expect_bytes > 0 ? expect_bytes : 1];
    FILE* f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "Cannot open %s\n", path);
        delete[] buf;
        return NULL;
    }
    int got = (int)fread(buf, 1, expect_bytes, f);
    fclose(f);
    if (got != expect_bytes) {
        fprintf(stderr, "%s: read %d bytes, expected %d\n",
                path, got, expect_bytes);
        delete[] buf;
        return NULL;
    }
    return buf;
}

// ---------------------------------------------------------------------------
// High-coordinate pass (csim only): procedural 9800x6400 page, stride 9856.
// ---------------------------------------------------------------------------
static int run_hicoord()
{
    FILE* fp = fopen(HICOORD_CASES, "r");
    if (!fp) {
        fprintf(stderr, "Cannot open %s — run patch_extract_generate_golden.py\n",
                HICOORD_CASES);
        return 1;
    }
    int iw, ih, ncand, total_bytes, nprobes;
    unsigned stride;
    if (fscanf(fp, "%d %d %u %d %d %d", &iw, &ih, &stride, &ncand,
               &total_bytes, &nprobes) != 6) {
        fprintf(stderr, "%s: malformed header\n", HICOORD_CASES);
        fclose(fp);
        return 1;
    }

    // Fill the buffer procedurally, then verify the C++ pattern against the
    // Python-side probe triples BEFORE running anything — if the two
    // formulas ever diverge, this fails with a probe message instead of a
    // wall of pixel mismatches.
    unsigned buffer_bytes = stride * (unsigned)ih;
    unsigned char* buf = new unsigned char[buffer_bytes];
    memset(buf, SENTINEL, buffer_bytes);
    for (int y = 0; y < ih; y++)
        for (int x = 0; x < iw; x++)
            buf[(size_t)y * stride + x] = pattern_px(x, y);

    int errors = 0;
    for (int p = 0; p < nprobes; p++) {
        int px, py, pv;
        if (fscanf(fp, "%d %d %d", &px, &py, &pv) != 3) {
            fprintf(stderr, "%s: malformed probe %d\n", HICOORD_CASES, p);
            errors++;
            break;
        }
        if ((int)buf[(size_t)py * stride + px] != pv) {
            fprintf(stderr, "  PROBE (%d,%d): C++ pattern %d != Python %d — "
                    "pattern_px() drifted from pattern_block()\n",
                    px, py, (int)buf[(size_t)py * stride + px], pv);
            errors++;
        }
    }
    static Case cases[MAX_CANDS];
    if (ncand < 1 || ncand > MAX_CANDS) {
        fprintf(stderr, "%s: ncand=%d out of range\n", HICOORD_CASES, ncand);
        errors++;
    }
    if (errors == 0)
        errors += load_rows(fp, cases, ncand, HICOORD_CASES);
    fclose(fp);
    if (errors) { delete[] buf; return errors; }

    errors += prevalidate(cases, ncand, iw, ih, total_bytes, HICOORD_CASES);
    unsigned char* golden = load_file(HICOORD_GOLDEN, total_bytes);
    if (!golden) { delete[] buf; return errors + 1; }

    if (errors == 0)
        errors += run_batch(cases, ncand, golden, total_bytes, buf,
                            iw, ih, stride, buffer_bytes,
                            "hicoord (9856)", true);
    delete[] golden;
    delete[] buf;
    return errors;
}

// ---------------------------------------------------------------------------
int main(int argc, char** argv)
{
    // ---- Select the suite ------------------------------------------------
    bool use_cosim = false;
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], "cosim") == 0) use_cosim = true;

    const char* CASES_FILE  = use_cosim ? COSIM_CASES  : CSIM_CASES;
    const char* GOLDEN_FILE = use_cosim ? COSIM_GOLDEN : CSIM_GOLDEN;

    // ---- Load the manifest ----------------------------------------------
    FILE* fp = fopen(CASES_FILE, "r");
    if (!fp) {
        fprintf(stderr, "Cannot open %s — run patch_extract_generate_golden.py first\n",
                CASES_FILE);
        return 1;
    }
    int iw, ih, ncand, total_bytes;
    if (fscanf(fp, "%d %d %d %d", &iw, &ih, &ncand, &total_bytes) != 4) {
        fprintf(stderr, "%s: malformed header\n", CASES_FILE);
        fclose(fp);
        return 1;
    }
    if (ncand > MAX_CANDS) {
        fprintf(stderr, "%d candidates exceeds MAX_CANDS=%d\n", ncand, MAX_CANDS);
        fclose(fp);
        return 1;
    }
    if ((unsigned)iw > STRIDE_A ||
        STRIDE_A * (unsigned)ih > (unsigned)PE_COSIM_BUF_BYTES) {
        fprintf(stderr, "image %dx%d does not fit ddr_buf at stride %u\n",
                iw, ih, STRIDE_A);
        fclose(fp);
        return 1;
    }
    static Case cases[MAX_CANDS];
    if (load_rows(fp, cases, ncand, CASES_FILE)) { fclose(fp); return 1; }
    fclose(fp);
    printf("Manifest: %s — %d candidates, image %dx%d, %d golden bytes\n",
           CASES_FILE, ncand, iw, ih, total_bytes);

    // ---- Prevalidate before touching the DUT -----------------------------
    if (prevalidate(cases, ncand, iw, ih, total_bytes, CASES_FILE)) {
        fprintf(stderr, "TESTBENCH FAILED: manifest error(s), DUT not run\n");
        return 1;
    }

    // ---- Load golden + image --------------------------------------------
    unsigned char* golden = load_file(GOLDEN_FILE, total_bytes);
    if (!golden) return 1;
    unsigned char* image = load_file(IMAGE_FILE, iw * ih);
    if (!image) { delete[] golden; return 1; }

    int errors = 0;

    // ---- Pass 1: manifest suite, non-compact stride ----------------------
    place_image(image, iw, ih, STRIDE_A, ddr_buf, PE_COSIM_BUF_BYTES);
    errors += run_batch(cases, ncand, golden, total_bytes, ddr_buf,
                        iw, ih, STRIDE_A, PE_COSIM_BUF_BYTES,
                        "strided (1024)", true);

    if (!use_cosim) {
        // ---- Pass 2: same suite, compact stride --------------------------
        // Same golden: pixels are a function of the logical image only.  A
        // core with either stride baked in fails exactly one of the passes.
        place_image(image, iw, ih, (unsigned)iw, ddr_buf, PE_COSIM_BUF_BYTES);
        errors += run_batch(cases, ncand, golden, total_bytes, ddr_buf,
                            iw, ih, (unsigned)iw, PE_COSIM_BUF_BYTES,
                            "compact stride", false);
        // Restore the strided layout for the synthetic passes below.
        place_image(image, iw, ih, STRIDE_A, ddr_buf, PE_COSIM_BUF_BYTES);

        // ---- Pass 3: high coordinates (heap buffer, never in cosim) ------
        errors += run_hicoord();

        // ---- Empty batch -------------------------------------------------
        errors += run_empty_batch(ddr_buf, iw, ih, STRIDE_A,
                                  PE_COSIM_BUF_BYTES);
    }

    // ---- Globally invalid image configurations (§4.3) --------------------
    // (a) width below the 3-pixel minimum;
    // (b) stride narrower than the image;
    // (c) buffer one byte too small for stride*img_h;
    // (d) stride*img_h wraps 32 bits: 0xFFFFFF * 503 ≈ 8.44e9, whose low 32
    //     bits (≈4.14e9) are LESS than the 0xFFFFFFFF buffer — a footprint
    //     computed in a 32-bit type passes the buffer check and streams
    //     pixels, so this case exists to catch exactly that (§2.1).
    errors += run_invalid_config(ddr_buf, 2, ih, STRIDE_A,
                                 PE_COSIM_BUF_BYTES, "img_w=2");
    errors += run_invalid_config(ddr_buf, iw, ih, (unsigned)(iw - 9),
                                 PE_COSIM_BUF_BYTES, "stride<img_w");
    errors += run_invalid_config(ddr_buf, iw, ih, STRIDE_A,
                                 STRIDE_A * (unsigned)ih - 1, "buffer-short");
    errors += run_invalid_config(ddr_buf, iw, ih, 0x00FFFFFFu,
                                 0xFFFFFFFFu, "footprint-wrap");

    // ---- TLAST-vs-num_cands mismatches (§5) ------------------------------
    errors += run_tlast_early(ddr_buf, iw, ih, STRIDE_A, PE_COSIM_BUF_BYTES);
    errors += run_tlast_late(ddr_buf, iw, ih, STRIDE_A, PE_COSIM_BUF_BYTES);

    // ---- Re-invocation ---------------------------------------------------
    errors += run_reinvocation(ddr_buf, iw, ih, STRIDE_A, PE_COSIM_BUF_BYTES);

    delete[] golden;
    delete[] image;

    if (errors) {
        fprintf(stderr, "TESTBENCH FAILED (%d error groups)\n", errors);
        return 1;
    }
    printf("TESTBENCH PASSED\n");
    return 0;
}
