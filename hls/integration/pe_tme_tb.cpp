// ---------------------------------------------------------------------------
// pe_tme_tb.cpp — the extractor -> matcher seam, in C simulation.
//
//   vitis-run.bat --mode hls --tcl run_csim.tcl
//   (generate first: ../.venv/Scripts/python.exe pe_tme_generate_golden.py)
//
// Both cores are verified separately and neither testbench can fail this way,
// because neither one contains the other.  What runs here is the PS loop that
// joins them — contract §7.1's open item — written the way the driver will
// have to write it, and then checked.
//
// THE ONE FACT THIS FILE EXISTS FOR
//
//   meta_out  carries one record per INPUT DESCRIPTOR.
//   patch_out carries pixels for VALID candidates ONLY.
//
// They are different lengths whenever anything is rejected, so the PS keeps
// TWO cursors, not one.  A loop that reads a record and a patch together is
// correct on every batch in which nothing is rejected — which is every batch
// anyone builds by hand — and permanently misaligned on the first one that
// isn't.  Nothing downstream detects it: the geometry is well-formed, the
// score is a plausible number, no status bit is set, and the answer is for
// the wrong candidate.  `mid-batch-reject` in the manifest is a rejected
// descriptor sitting between two valid ones so that bug has room to happen,
// and negative_control() below performs it deliberately and requires the
// answer to come out WRONG.  A suite that cannot fail on the bug it was
// written for is decoration; that check is what stops this becoming that.
//
// Also asserted, one manifest case each:
//   - the matcher's patch_w/patch_h come from the metadata RECORD (post-clip),
//     not from re-deriving §4.5 on the descriptor  -> `clipped-left`
//   - the reported location is in PAGE coordinates: record (x0,y0) + the
//     matcher's local (u,v)                        -> every valid case
//   - TLAST lands exactly on beat patch_w*patch_h.  `tme_top` ignores TLAST
//     and reads the count it was told, so a framing disagreement is silent in
//     the matcher and corrupts the NEXT patch instead.
//   - the pixels are the page bytes at (x0,y0), so the join did not reorder
//     anything the extractor's own TB proved correct in isolation.
// ---------------------------------------------------------------------------

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <vector>
#include "patch_extract_core.h"
#include "tme_top.h"

// Matches MAX_SCORE_ERR in tme_tb.cpp and SCORE_TOL in the bring-up driver.
static const float SCORE_TOL = 0.005f;
static const unsigned char SENTINEL = 0xA5;

struct Row {
    int index, last, valid;
    unsigned long long packed;
    unsigned reason;
    int x0, y0, pw, ph;
    int templ_id, templ_off, tw, th;
    double score;
    int page_x, page_y, ux, uy;
    double margin;
    char category[32], tag[64];
};

static int g_reported = 0;
static bool report_ok() { return g_reported++ < 40; }

// ---------------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------------

static unsigned char* load_file(const char* path, int expect_bytes)
{
    FILE* fp = fopen(path, "rb");
    if (!fp) { printf("ERROR: cannot open %s\n", path); return 0; }
    fseek(fp, 0, SEEK_END);
    long n = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (expect_bytes >= 0 && n != expect_bytes) {
        printf("ERROR: %s is %ld B, manifest says %d\n", path, n, expect_bytes);
        fclose(fp); return 0;
    }
    unsigned char* buf = (unsigned char*)malloc((size_t)n);
    if (fread(buf, 1, (size_t)n, fp) != (size_t)n) {
        printf("ERROR: short read on %s\n", path);
        fclose(fp); free(buf); return 0;
    }
    fclose(fp);
    return buf;
}

// ---------------------------------------------------------------------------
// Stream helpers
// ---------------------------------------------------------------------------

static void write_cand(hls::stream<cand_stream_t>& s,
                       unsigned long long packed, int last)
{
    cand_stream_t d;
    d.data = (ap_uint<64>)packed;
    d.keep = -1; d.strb = -1;
    d.last = (ap_uint<1>)last;
    s.write(d);
}

static void push_pixels(hls::stream<pix_stream_t>& s,
                        const unsigned char* p, int n)
{
    for (int i = 0; i < n; i++) {
        pix_stream_t px;
        px.data = (ap_uint<8>)p[i];
        px.keep = -1; px.strb = -1;
        px.last = (ap_uint<1>)(i == n - 1);
        s.write(px);
    }
}

// ---------------------------------------------------------------------------
// Run one candidate through the matcher exactly as the PS will.
// ---------------------------------------------------------------------------

static void run_matcher(const unsigned char* patch_bytes, int pw, int ph,
                        const unsigned char* templ_bytes, int tw, int th,
                        float& score, ap_uint<16>& rx, ap_uint<16>& ry)
{
    hls::stream<pix_stream_t> ps("mt_patch"), ts("mt_templ");
    push_pixels(ps, patch_bytes, pw * ph);
    push_pixels(ts, templ_bytes, tw * th);
    tme_top(ps, ts, (ap_uint<16>)pw, (ap_uint<16>)ph,
            (ap_uint<16>)tw, (ap_uint<16>)th, score, rx, ry);
}

// ---------------------------------------------------------------------------
// Negative controls.
//
// Every assertion above is a claim that the RIGHT inputs give the right
// answer.  None of them is a claim that a WRONG input would have been caught,
// and those are different claims — a suite whose cases all pass under the bug
// it was written for is decoration.  So each of the two bugs this file exists
// for is performed here deliberately, on the same candidate, and required to
// produce a different answer from golden.
//
//   1. geometry:  a PS that re-derives §4.5 on the descriptor instead of
//                 reading the record programs the UNCLIPPED width, and its
//                 DMA therefore lands this patch's bytes followed by whatever
//                 came next -- the length is what the PS asked for, not what
//                 the extractor emitted.
//   2. cursor:    a PS that advances the pixel cursor once per RECORD rather
//                 than once per VALID record skips the rejected descriptor's
//                 reported patch_w*patch_h bytes, which it never emitted, and
//                 reads this candidate from the wrong offset.
//
// Both must come out wrong.  If either comes out right, the corresponding
// PASS above is measuring nothing and this file should not be believed about
// it.
// ---------------------------------------------------------------------------

static int negative_control(const char* what, const char* why,
                            const std::vector<unsigned char>& emitted,
                            int off, int pw, int ph,
                            const unsigned char* templ, int tw, int th,
                            const Row& r, int& ran)
{
    int want = pw * ph;
    if (off < 0 || off + want > (int)emitted.size()) {
        printf("  [FAIL] negative control (%s) could not run: needs bytes "
               "[%d,%d) of %d emitted. It is not optional — without it the "
               "%s case is untested against %s.\n", what, off, off + want,
               (int)emitted.size(), r.tag, why);
        return 1;
    }
    float score; ap_uint<16> rx, ry;
    run_matcher(&emitted[(size_t)off], pw, ph, templ, tw, th, score, rx, ry);
    int page_x = r.x0 + (int)rx, page_y = r.y0 + (int)ry;
    ran++;
    bool same = (fabs((double)score - r.score) <= SCORE_TOL
                 && page_x == r.page_x && page_y == r.page_y);
    if (same) {
        printf("  [FAIL] negative control (%s): %s still produced the golden "
               "answer (score %.6f at page (%d,%d)). The %s case cannot "
               "detect it, so its PASS above means nothing.\n",
               what, why, (double)score, page_x, page_y, r.tag);
        return 1;
    }
    printf("  [PASS] negative control (%s): %s gives score %.6f at page "
           "(%d,%d), not the golden %.6f at (%d,%d) — the bug is detectable\n",
           what, why, (double)score, page_x, page_y,
           r.score, r.page_x, r.page_y);
    return 0;
}

// ---------------------------------------------------------------------------
int main()
{
    // ---- manifest -------------------------------------------------------
    FILE* fp = fopen("tb_pe_tme_cases.txt", "r");
    if (!fp) { printf("ERROR: cannot open tb_pe_tme_cases.txt\n"); return 1; }
    int ncand, img_w, img_h, stride, buffer_bytes, n_templ, templ_bytes;
    if (fscanf(fp, "%d %d %d %d %d %d %d", &ncand, &img_w, &img_h, &stride,
               &buffer_bytes, &n_templ, &templ_bytes) != 7) {
        printf("ERROR: bad manifest header\n"); fclose(fp); return 1;
    }
    std::vector<Row> rows((size_t)ncand);
    for (int i = 0; i < ncand; i++) {
        Row& r = rows[(size_t)i];
        if (fscanf(fp, "%d %llx %d %d %x %d %d %d %d %d %d %d %d %lf %d %d "
                       "%d %d %lf %31s %63s",
                   &r.index, &r.packed, &r.last, &r.valid, &r.reason,
                   &r.x0, &r.y0, &r.pw, &r.ph, &r.templ_id, &r.templ_off,
                   &r.tw, &r.th, &r.score, &r.page_x, &r.page_y,
                   &r.ux, &r.uy, &r.margin, r.category, r.tag) != 21) {
            printf("ERROR: bad manifest row %d\n", i); fclose(fp); return 1;
        }
    }
    fclose(fp);

    unsigned char* image = load_file("tb_pe_tme_image.bin", img_w * img_h);
    unsigned char* templs = load_file("tb_pe_tme_templs.bin", templ_bytes);
    if (!image || !templs) return 1;

    // ---- strided DDR buffer, as the binarizer's writer leaves it --------
    // Padding is the sentinel: a stride bug lands on 0xA5, which the page
    // pattern cannot produce (pattern_block replaces it).
    unsigned char* buf = (unsigned char*)malloc((size_t)buffer_bytes);
    memset(buf, SENTINEL, (size_t)buffer_bytes);
    for (int y = 0; y < img_h; y++)
        memcpy(buf + (size_t)y * stride, image + (size_t)y * img_w,
               (size_t)img_w);

    printf("=== extractor -> matcher seam, %d descriptors on a %dx%d page "
           "(stride %d) ===\n", ncand, img_w, img_h, stride);

    // ---- one extractor invocation for the whole batch ------------------
    hls::stream<cand_stream_t>  cs("cand");
    hls::stream<ppix_stream_t>  ps("patch");
    hls::stream<pmeta_stream_t> ms("meta");
    for (int i = 0; i < ncand; i++)
        write_cand(cs, rows[(size_t)i].packed, rows[(size_t)i].last);

    ap_uint<32> flags = 0xDEAD, rejected = 0xDEAD, processed = 0xDEAD;
    patch_extract_core(cs, ps, ms, buf, (ap_uint<16>)img_w, (ap_uint<16>)img_h,
                       (ap_uint<32>)stride, (ap_uint<32>)buffer_bytes,
                       (ap_uint<16>)ncand, flags, rejected, processed);

    int errors = 0;
    int total_beats = (int)ps.size();

    // Drain the pixel stream into a flat buffer, recording where each TLAST
    // fell.  The PS's S2MM DMA does exactly this — it lands bytes in DDR and
    // the framing information survives only as a position.
    std::vector<unsigned char> emitted;
    std::vector<int> tlast_at;
    emitted.reserve((size_t)total_beats);
    for (int k = 0; k < total_beats; k++) {
        ppix_stream_t px = ps.read();
        emitted.push_back((unsigned char)px.data);
        if (px.last) tlast_at.push_back((int)emitted.size());
    }

    // ---- the PS loop ----------------------------------------------------
    // TWO cursors.  `i` walks metadata records (one per descriptor);
    // `patch_off` walks the pixel blob (valid candidates only).  They are
    // advanced in different places on purpose — see the banner.
    int patch_off = 0, n_patches_seen = 0;
    int neg_ctl_ran = 0, neg_ctl_errors = 0;
    // What a PS that advanced its pixel cursor once per RECORD would have
    // skipped: the rejected descriptors' REPORTED geometry, which they never
    // emitted a byte of.  Accumulated so the cursor negative control can be
    // run against a real wrong offset rather than an invented one.
    int naive_skip = 0;

    for (int i = 0; i < ncand; i++) {
        const Row& r = rows[(size_t)i];

        if (ms.empty()) {
            printf("  [FAIL] %s: metadata stream ran dry at record %d\n",
                   r.tag, i);
            errors++; break;
        }
        pmeta_stream_t m = ms.read();
        int m_id     = (int)m.data.range(15, 0);
        unsigned st  = (unsigned)m.data.range(31, 16);
        int m_x0     = (int)m.data.range(47, 32);
        int m_y0     = (int)m.data.range(63, 48);
        int m_pw     = (int)m.data.range(79, 64);
        int m_ph     = (int)m.data.range(95, 80);
        int m_valid  = (int)(st & 1u);
        unsigned m_reason = st >> 1;

        int rec_err = 0;
        if (m_id != i)                 { rec_err++; }
        if (m_valid != r.valid)        { rec_err++; }
        if (m_reason != r.reason)      { rec_err++; }
        if (m_x0 != r.x0 || m_y0 != r.y0) { rec_err++; }
        if (m_pw != r.pw || m_ph != r.ph) { rec_err++; }
        if ((int)m.last != r.last)     { rec_err++; }
        if (rec_err) {
            errors += rec_err;
            if (report_ok())
                printf("  [FAIL] %s: record id=%d valid=%d reason=0x%x "
                       "(%d,%d) %dx%d last=%d, expected id=%d valid=%d "
                       "reason=0x%x (%d,%d) %dx%d last=%d\n",
                       r.tag, m_id, m_valid, m_reason, m_x0, m_y0, m_pw, m_ph,
                       (int)m.last, i, r.valid, r.reason, r.x0, r.y0,
                       r.pw, r.ph, r.last);
        }

        // A rejected descriptor produced NO pixels.  The pixel cursor must
        // NOT move here.  This `continue` is the whole point of the file.
        if (!m_valid) {
            naive_skip += m_pw * m_ph;      // what the WRONG loop would skip
            printf("  [PASS] %s: rejected (reason 0x%x), record present, "
                   "pixel cursor held at %d (a per-record cursor would have "
                   "advanced it by %d)\n", r.tag, m_reason, patch_off,
                   m_pw * m_ph);
            continue;
        }

        int want = m_pw * m_ph;         // from the RECORD, never re-derived
        if (patch_off + want > (int)emitted.size()) {
            printf("  [FAIL] %s: needs %d pixel bytes at offset %d but only "
                   "%d were emitted in total\n", r.tag, want, patch_off,
                   (int)emitted.size());
            errors++; break;
        }

        // ---- framing: TLAST must be exactly here -----------------------
        int want_tlast = patch_off + want;
        bool tlast_ok = (n_patches_seen < (int)tlast_at.size()
                         && tlast_at[(size_t)n_patches_seen] == want_tlast);
        if (!tlast_ok) {
            errors++;
            int got = n_patches_seen < (int)tlast_at.size()
                      ? tlast_at[(size_t)n_patches_seen] : -1;
            printf("  [FAIL] %s: TLAST at beat %d, expected %d (patch_w*"
                   "patch_h = %d*%d). tme_top ignores TLAST, so this does not "
                   "fail in the matcher — it corrupts the NEXT patch.\n",
                   r.tag, got, want_tlast, m_pw, m_ph);
        }

        // ---- pixels are the page bytes at (x0, y0) ---------------------
        int px_err = 0;
        for (int rr = 0; rr < m_ph && px_err == 0; rr++)
            for (int cc = 0; cc < m_pw; cc++) {
                unsigned char got = emitted[(size_t)(patch_off + rr * m_pw + cc)];
                unsigned char exp = image[(size_t)(m_y0 + rr) * img_w
                                          + (size_t)(m_x0 + cc)];
                if (got != exp) {
                    px_err++;
                    if (report_ok())
                        printf("  [FAIL] %s: pixel (%d,%d) = 0x%02x, page has "
                               "0x%02x%s\n", r.tag, cc, rr, got, exp,
                               got == SENTINEL ? "  (SENTINEL — stride bug)" : "");
                    break;
                }
            }
        errors += px_err;

        // ---- the matcher, driven from the record ------------------------
        float score; ap_uint<16> rx, ry;
        run_matcher(&emitted[(size_t)patch_off], m_pw, m_ph,
                    templs + r.templ_off, r.tw, r.th, score, rx, ry);

        int page_x = m_x0 + (int)rx, page_y = m_y0 + (int)ry;
        bool s_ok = fabs((double)score - r.score) <= SCORE_TOL;
        bool l_ok = ((int)rx == r.ux && (int)ry == r.uy);
        bool p_ok = (page_x == r.page_x && page_y == r.page_y);
        if (!(s_ok && l_ok && p_ok)) {
            errors++;
            printf("  [FAIL] %s: score %.6f (want %.6f), local (%d,%d) want "
                   "(%d,%d), page (%d,%d) want (%d,%d)\n", r.tag,
                   (double)score, r.score, (int)rx, (int)ry, r.ux, r.uy,
                   page_x, page_y, r.page_x, r.page_y);
        } else {
            printf("  [PASS] %s: patch %dx%d @(%d,%d) from the record, "
                   "score %+.6f, local (%d,%d) -> page (%d,%d), TLAST at %d\n",
                   r.tag, m_pw, m_ph, m_x0, m_y0, (double)score,
                   (int)rx, (int)ry, page_x, page_y, want_tlast);
        }

        // Both controls run on the clipped candidate: it sits after the
        // reject (so the cursor bug reaches it) AND its record geometry
        // differs from the re-derived formula (so the geometry bug does too).
        if (strcmp(r.tag, "clipped-left") == 0) {
            // §4.5 unclipped width for these descriptors: outward + inward.
            int tw_2fifths = (2 * 40) / 5;
            int unclipped = (2 * 40 + tw_2fifths) + (40 + tw_2fifths);
            if (unclipped <= m_pw) {
                errors++;
                printf("  [FAIL] clipped-left is not clipping any more "
                       "(formula %d <= record %d); the geometry control has "
                       "no wrong input to feed\n", unclipped, m_pw);
            } else {
                neg_ctl_errors += negative_control(
                    "geometry", "re-deriving §4.5 instead of reading the record",
                    emitted, patch_off, unclipped, m_ph,
                    templs + r.templ_off, r.tw, r.th, r, neg_ctl_ran);
            }
            if (naive_skip <= 0) {
                errors++;
                printf("  [FAIL] nothing was rejected before this candidate; "
                       "the cursor control has no wrong offset to feed\n");
            } else {
                neg_ctl_errors += negative_control(
                    "cursor", "advancing the pixel cursor once per RECORD",
                    emitted, patch_off + naive_skip, m_pw, m_ph,
                    templs + r.templ_off, r.tw, r.th, r, neg_ctl_ran);
            }
        }

        patch_off += want;              // ONLY here
        n_patches_seen++;
    }

    // ---- nothing left over ---------------------------------------------
    if (patch_off != (int)emitted.size()) {
        errors++;
        printf("  [FAIL] %d pixel bytes unconsumed (%d of %d). The record and "
               "pixel cursors disagree about how many patches exist.\n",
               (int)emitted.size() - patch_off, patch_off, (int)emitted.size());
    }
    if (n_patches_seen != (int)tlast_at.size()) {
        errors++;
        printf("  [FAIL] %d patches consumed but %d TLASTs seen\n",
               n_patches_seen, (int)tlast_at.size());
    }
    if (!ms.empty()) {
        errors++;
        printf("  [FAIL] %d metadata records left unread\n", (int)ms.size());
    }
    errors += neg_ctl_errors;
    if (neg_ctl_ran != 2) {
        errors++;
        printf("  [FAIL] %d of 2 negative controls ran. Until both do, "
               "nothing here has shown this suite can fail on either of the "
               "two PS bugs it was written for.\n", neg_ctl_ran);
    }

    // ---- extractor status registers -------------------------------------
    unsigned exp_rejected = 0;
    for (int i = 0; i < ncand; i++) if (!rows[(size_t)i].valid) exp_rejected++;
    if (flags != 0 || rejected != exp_rejected || processed != (unsigned)ncand) {
        errors++;
        printf("  [FAIL] status: flags=0x%x rejected=%u processed=%u, "
               "expected 0x0/%u/%d\n", (unsigned)flags, (unsigned)rejected,
               (unsigned)processed, exp_rejected, ncand);
    } else {
        printf("  [PASS] status: flags=0x0 rejected=%u processed=%u\n",
               (unsigned)rejected, (unsigned)processed);
    }

    free(buf); free(image); free(templs);

    // Report what was actually exercised, not how many lines printed PASS.
    // "7/7" invites reading a line count as a case count; the numbers that
    // mean something are how many descriptors went in, how many reached the
    // matcher, and how many injected bugs were required to fail.
    printf("\n%s (%d error%s): %d descriptors, %d matcher runs, "
           "%d injected-bug controls\n",
           errors ? "SEAM TEST FAILED" : "SEAM TEST PASSED",
           errors, errors == 1 ? "" : "s",
           ncand, n_patches_seen, neg_ctl_ran);
    return errors ? 1 : 0;
}
