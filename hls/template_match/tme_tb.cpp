// Manifest-driven testbench for tme_top.
//
// Golden data comes from tme_generate_golden.py, which writes three suites:
//   tb_tme_cases_{csim,cosim,hw}.txt    — manifest (see row format below)
//   tb_tme_patches_{csim,cosim,hw}.bin  — patch pixels, concatenated row-major
//   tb_tme_templs_{csim,cosim,hw}.bin   — RAW template pixels, concatenated
//
// The suite is selected by argument, defaulting to csim:
//   "cosim" — run_hls.tcl's `cosim_design -argv "cosim"`.  Small enough to
//             finish in RTL; anything only in the csim manifest never
//             reaches RTL.
//   "hw"    — the vectors sw/tme_standalone_bringup.py sends to the board:
//             the cosim cases plus both 820x307 stress cases.  One carries
//             the 251,740-byte patch that makes the silicon run test contract
//             §3.1's single-DMA-transfer bound; the other fills the maximum
//             817x304 result map.  Running them through csim first
//             (`csim_design -argv "hw"`) is cheap and means a board failure
//             is a hardware finding, not a bad golden.
//
// Every case asserts BOTH the score (within MAX_SCORE_ERR) and the EXACT
// best-match location.  The generator guarantees exact-loc is well-posed:
// randomised cases are seed-searched until the peak clears the runner-up by
// a margin, and tie-breaking (first occurrence, row-major) is identical in
// cv2.minMaxLoc, the generator's argmax, and the DUT's strict > update.
// This is what discharges run_hls.tcl's item 2: the old TB compared score
// only, against a sole 0.0 @ (0,0) golden an always-zero DUT passed.
//
// Manifest header:  n_cases patch_blob_bytes templ_blob_bytes
// Manifest row:     index pw ph tw th patch_off templ_off score x y margin
//                   category tag
//
// Cases run back-to-back through one DUT instance, so the suite also
// exercises re-invocation: stale contents of the static patch/template
// BRAMs and column accumulators from a larger previous case must never
// leak into a smaller later one.
//
// Two DIRECT DUT tests run before the manifest loop, in every suite (see
// run_direct_tests, contract §4.6).  They exist outside the manifest because
// the generator cannot express them: it raises ValueError on a flat template
// rather than writing a golden for input the ABI forbids.  A third check is
// pure arithmetic in the DUT's own integer types: the dt and ±num extremes,
// operand preservation, and the §4.6 width-minimum witness.  Their failures
// are counted separately from the manifest cases so the suite totals stay
// comparable, but any of them fails the testbench.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "tme_top.h"

static const float MAX_SCORE_ERR = 0.005f;
static const int   TB_MAX_CASES  = 64;

struct tb_case_t {
    int  pw, ph, tw, th;
    long patch_off, templ_off;
    float gold_score;
    int  gold_x, gold_y;
    float margin;
    char category[32];
    char tag[64];
};

static unsigned char* read_blob(const char* path, long expect_bytes)
{
    FILE* f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "Cannot open %s\n", path); return NULL; }
    unsigned char* buf = (unsigned char*)malloc(expect_bytes ? expect_bytes : 1);
    long n = (long)fread(buf, 1, expect_bytes, f);
    int extra = fgetc(f);   // must be EOF: blob size is part of the contract
    fclose(f);
    if (n != expect_bytes || extra != EOF) {
        fprintf(stderr, "%s: size mismatch (read %ld, manifest says %ld%s)\n",
                path, n, expect_bytes, extra != EOF ? ", trailing bytes" : "");
        free(buf);
        return NULL;
    }
    return buf;
}

static void stream_image(hls::stream<pix_stream_t>& s,
                         const unsigned char* base, int w, int h)
{
    for (int r = 0; r < h; r++) {
        for (int c = 0; c < w; c++) {
            pix_stream_t px;
            px.data = base[r * w + c];
            px.last = (r == h - 1 && c == w - 1) ? 1 : 0;
            px.keep = 1;
            px.strb = 1;
            px.user = 0;
            px.id   = 0;
            px.dest = 0;
            s.write(px);
        }
    }
}

// ---------------------------------------------------------------------------
// Contract §4.6 — direct DUT tests, outside the manifest loop.
//
// These cannot be manifest cases.  tme_generate_golden.py *rejects* a flat
// template (it raises ValueError rather than writing a golden nobody can
// justify), so the only way to put one in front of the DUT is to build it
// here.  The negative control is the mirror image: it is the smallest legal
// non-flat template, and its whole point is to fail if anyone ever "fixes"
// the flat-template rejection by turning `min == max` into a variance
// threshold — dt = 15 would be the first thing such a threshold swallowed.
// ---------------------------------------------------------------------------

static unsigned score_bits(float f)
{
    unsigned u;
    memcpy(&u, &f, sizeof u);   // the AXI4-Lite view: raw IEEE-754
    return u;
}

// One direct invocation, checking the raw score bit pattern, the exact
// location, and that both streams drained.
static bool direct_case(const char* name,
                        const unsigned char* patch, int pw, int ph,
                        const unsigned char* templ, int tw, int th,
                        unsigned want_bits, int want_x, int want_y)
{
    hls::stream<pix_stream_t> patch_stream("patch_direct");
    hls::stream<pix_stream_t> templ_stream("templ_direct");
    stream_image(patch_stream, patch, pw, ph);
    stream_image(templ_stream, templ, tw, th);

    float dut_score = -99.0f;
    ap_uint<16> dut_x = 0xFFFF, dut_y = 0xFFFF;
    tme_top(patch_stream, templ_stream,
            (ap_uint<16>)pw, (ap_uint<16>)ph, (ap_uint<16>)tw, (ap_uint<16>)th,
            dut_score, dut_x, dut_y);

    unsigned got = score_bits(dut_score);
    bool bits_ok = (got == want_bits);
    bool loc_ok  = ((int)dut_x == want_x && (int)dut_y == want_y);
    bool drained = patch_stream.empty() && templ_stream.empty();
    bool ok = bits_ok && loc_ok && drained;

    printf("  %-32s patch %3dx%-3d templ %2dx%-2d  raw 0x%08X @(%d,%d)  "
           "want 0x%08X @(%d,%d)  %s%s%s%s\n",
           name, pw, ph, tw, th, got, (int)dut_x, (int)dut_y,
           want_bits, want_x, want_y,
           ok ? "PASS" : "FAIL",
           bits_ok ? "" : " [score bits]",
           loc_ok ? "" : " [loc]",
           drained ? "" : " [beats left in stream]");
    return ok;
}

// The §4.6 width extremes, in the DUT's own types.  Not a DUT invocation:
// these are properties of the typedefs in tme_top.h, evaluated with exactly
// the expressions tme_top.cpp writes.
//
// WHAT THIS IS NOT.  It does not claim that a narrower wide_t would corrupt
// dt, and an earlier version of this comment said so wrongly.  Fixed-width
// subtraction is modular — (A mod 2^W − B mod 2^W) mod 2^W = (A − B) mod 2^W —
// so two equal all-255 operands cancel to zero at ANY width, truncated or not.
// The minimum that actually matters is the width of the RESULT: dt and |num|
// both stay under 2^43, so 44 bits suffices for either.
//
// WHAT IT IS.  A preservation-policy test.  48 bits holds each 45-bit product
// outright, so `dt` and `num` can be believed without re-deriving the modular
// argument; these checks pin that the products really are preserved rather
// than merely cancelling.  Plus the three extremes of `num` itself — the
// Cauchy-Schwarz bound reached positive and negative, and the all-255
// cancellation — because those are the values `norm_cols` must not wrap, and
// no manifest case comes close to them.
//
// The last two checks are the failures this section exists for.  The joint
// minimum is (wide_t, num_t) = (44u, 44s) — ONE decision about a pair, because
// `num` casts (num_t)(wide_t)(...) and the truncation lands before the
// subtraction.  Both ways of misreading it as two independent budgets are run
// here as witnesses that assert the BREAKAGE:
//   (43u, 44s)  narrowing wide_t to dt's own result width — dt stays CORRECT,
//               so checking dt alone would wave the change through
//   (44u, 45s)  widening num_t "for safety" — wrong on an ordinary legal input
// The rule is equal modular widths ≥ 44, or operands preserved outright
// (≥45u / ≥46s, which the implemented 48/48 does).
static bool bound_case()
{
    const int tw = MAX_TEMPL_W, th = MAX_TEMPL_H;
    const int n  = tw * th;                       // 20,736
    ap_uint<16> n_px = (ap_uint<16>)(tw * th);

    // ---- dt at the maximum: 216x96, half 0 / half 255 -------------------
    sum_t   t_sum = 0;      // ΣT
    sumsq_t t_sq  = 0;      // ΣT²
    for (int i = 0; i < n; i++) {
        ap_uint<8> tv = (i < n / 2) ? (ap_uint<8>)0 : (ap_uint<8>)255;
        t_sum += tv;
        t_sq  += (sumsq_t)(tv * tv);
    }

    // Exactly the expression tme_top.cpp evaluates, in exactly its types.
    wide_t lhs = (wide_t)(n_px * t_sq);
    wide_t rhs = (wide_t)(t_sum * t_sum);
    wide_t dt  = lhs - rhs;

    const unsigned long long WANT_DT = 6989889945600ULL;   // ⌊N²/4⌋·255², 43 b

    unsigned long long got_dt  = (unsigned long long)dt;
    unsigned long long got_lhs = (unsigned long long)lhs;
    unsigned long long got_rhs = (unsigned long long)rhs;
    bool dt_ok = (got_dt == WANT_DT);

    // ---- operand preservation: all-255, where both products are 45 bits --
    // N·ΣT² and (ΣT)² are both 27,959,559,782,400 and cancel to zero.  The
    // cancellation is not the claim here — it would hold under truncation too.
    // The claim is that wide_t reports each product UNWRAPPED.
    sum_t   f_sum = 0;
    sumsq_t f_sq  = 0;
    for (int i = 0; i < n; i++) { f_sum += (ap_uint<8>)255;
                                  f_sq  += (sumsq_t)(255 * 255); }
    wide_t f_lhs = (wide_t)(n_px * f_sq);
    wide_t f_rhs = (wide_t)(f_sum * f_sum);
    wide_t f_dt  = f_lhs - f_rhs;
    const unsigned long long WANT_WIDE = 27959559782400ULL;  // 45 bits
    bool wide_ok = ((unsigned long long)f_lhs == WANT_WIDE &&
                    (unsigned long long)f_rhs == WANT_WIDE &&
                    (unsigned long long)f_dt  == 0ULL);

    printf("  %-32s dt = %llu (want %llu)  %s\n",
           "bound-half0-half255-216x96", got_dt, WANT_DT,
           dt_ok ? "PASS" : "FAIL");
    printf("  %-32s N*ST2 = %llu, (ST)^2 = %llu, dt = %llu (want %llu/%llu/0)"
           "  %s\n",
           "preserve-operands-all255",
           (unsigned long long)f_lhs, (unsigned long long)f_rhs,
           (unsigned long long)f_dt, WANT_WIDE, WANT_WIDE,
           wide_ok ? "PASS" : "FAIL");
    if (!dt_ok)
        fprintf(stderr, "  intermediates were N*ST2 = %llu, (ST)^2 = %llu "
                        "(§4.6)\n", got_lhs, got_rhs);

    // ---- num at both Cauchy-Schwarz extremes -----------------------------
    // |num| ≤ √(dt·di) ≤ max(dt), attained when the window equals the template
    // (+) or its complement (−).  Both use the half 0 / half 255 template
    // above, so dt = di = 6,989,889,945,600 and |num| = that same bound.
    //
    //   window == template:      ΣTI = 255²·N/2,  num = +6,989,889,945,600
    //   window == complement:    ΣTI = 0,         num = −6,989,889,945,600
    //
    // The negative one is the case a wrongly-unsigned num_t would turn into a
    // huge positive score, and no manifest case reaches this magnitude.
    sumsq_t sti_pos = 0, sti_neg = 0;
    sum_t   si_pos  = 0, si_neg  = 0;
    for (int i = 0; i < n; i++) {
        ap_uint<8> tv = (i < n / 2) ? (ap_uint<8>)0 : (ap_uint<8>)255;
        ap_uint<8> iv_pos = tv;                                  // identical
        ap_uint<8> iv_neg = (ap_uint<8>)(255 - tv);              // complement
        sti_pos += (sumsq_t)(tv * iv_pos);
        sti_neg += (sumsq_t)(tv * iv_neg);
        si_pos  += iv_pos;
        si_neg  += iv_neg;
    }
    // Exactly norm_cols' expression, in exactly its types.
    num_t num_pos = (num_t)(wide_t)(n_px * sti_pos)
                  - (num_t)(wide_t)(si_pos * t_sum);
    num_t num_neg = (num_t)(wide_t)(n_px * sti_neg)
                  - (num_t)(wide_t)(si_neg * t_sum);
    // All-255 template against an all-255 window: both operands are the same
    // 45-bit value and num must be exactly zero.
    sumsq_t sti_flat = 0;
    for (int i = 0; i < n; i++) sti_flat += (sumsq_t)(255 * 255);
    num_t num_flat = (num_t)(wide_t)(n_px * sti_flat)
                   - (num_t)(wide_t)(f_sum * f_sum);

    const long long WANT_NUM = 6989889945600LL;
    bool num_pos_ok  = ((long long)num_pos  ==  WANT_NUM);
    bool num_neg_ok  = ((long long)num_neg  == -WANT_NUM);
    bool num_flat_ok = ((long long)num_flat ==  0LL);

    printf("  %-32s num = %lld (want %lld)  %s\n",
           "num-max-positive-216x96", (long long)num_pos, WANT_NUM,
           num_pos_ok ? "PASS" : "FAIL");
    printf("  %-32s num = %lld (want %lld)  %s\n",
           "num-max-negative-216x96", (long long)num_neg, -WANT_NUM,
           num_neg_ok ? "PASS" : "FAIL");
    printf("  %-32s num = %lld (want 0)  %s\n",
           "num-cancel-all255", (long long)num_flat,
           num_flat_ok ? "PASS" : "FAIL");

    // ---- witness 1: (43u, 44s) -------------------------------------------
    // Not the implemented types: wide_t sized from dt's 43-bit result while
    // num_t keeps the 44 bits its own result needs.  dt still comes out right;
    // num does not.  This is the check that makes "43 bits is enough, look at
    // dt" a visible failure rather than a plausible-sounding change.  It
    // asserts the BREAKAGE, so it fails if the relationship ever stops holding
    // and the comments above go stale.
    typedef ap_uint<43> narrow_wide_t;
    typedef ap_int<44>  min_num_t;
    narrow_wide_t nw_dt = (narrow_wide_t)(n_px * t_sq)
                        - (narrow_wide_t)(t_sum * t_sum);
    min_num_t nw_num = (min_num_t)(narrow_wide_t)(n_px * sti_pos)
                     - (min_num_t)(narrow_wide_t)(si_pos * t_sum);
    // And the same expression at the joint minimum, (44u, 44s), which must be
    // exact — the modular cancellation working as advertised.
    typedef ap_uint<44> min_wide_t;
    min_num_t mn_num = (min_num_t)(min_wide_t)(n_px * sti_pos)
                     - (min_num_t)(min_wide_t)(si_pos * t_sum);

    // The wrong value is PINNED, not merely required to differ from the right
    // one.  §4.6 quotes it, and a toolchain whose ap_int semantics drifted
    // would otherwise keep passing this test with some other wrong number
    // while the contract's worked example silently stopped being true.
    const long long WRONG_43_44 = -1806203076608LL;
    bool witness_ok = ((unsigned long long)nw_dt == WANT_DT) &&   // dt survives
                      ((long long)nw_num == WRONG_43_44) &&       // num does not
                      ((long long)mn_num == WANT_NUM);            // 44/44 exact
    printf("  %-32s 43/44: dt = %llu ok, num = %lld (pinned %lld); 44/44: "
           "num = %lld  %s\n",
           "width-minimum-witness",
           (unsigned long long)nw_dt, (long long)nw_num, WRONG_43_44,
           (long long)mn_num, witness_ok ? "PASS" : "FAIL");

    // ---- witness 2: (44u, 45s) — WIDENING num_t ALONE ALSO BREAKS --------
    // The minimum is a PAIR, and reading it as "each type needs ≥ 44 bits"
    // invites exactly this: keep wide_t at the minimum, give num_t a bit more
    // for safety, and get a wrong answer.  The inner cast truncates at 2^44
    // before the subtraction, and a 45-bit result never reduces it back.
    //
    // The vector is deliberately ORDINARY, not an extreme: a 216x96 template
    // with 14,000 of its 20,736 pixels at 255 (the rest 0), matched against
    // itself.  Legal input, unremarkable statistics, and it still trips —
    // N*STI = 18,877,017,600,000 is just over 2^44 while ST*SI =
    // 12,744,900,000,000 is just under, so exactly one operand wraps.
    const int a255 = 14000;
    sum_t   w_sum = 0;          // ST = SI
    sumsq_t w_sti = 0;          // STI = STT (window == template)
    for (int i = 0; i < n; i++) {
        ap_uint<8> tv = (i < a255) ? (ap_uint<8>)255 : (ap_uint<8>)0;
        w_sum += tv;
        w_sti += (sumsq_t)(tv * tv);
    }
    const long long WANT_NUM2 = 6132117600000LL;   // = N*STI - ST*SI, exact

    typedef ap_int<45> wider_num_t;
    num_t       ok48 = (num_t)(wide_t)(n_px * w_sti)
                     - (num_t)(wide_t)(w_sum * w_sum);
    min_num_t   ok44 = (min_num_t)(min_wide_t)(n_px * w_sti)
                     - (min_num_t)(min_wide_t)(w_sum * w_sum);
    wider_num_t bad45 = (wider_num_t)(min_wide_t)(n_px * w_sti)
                      - (wider_num_t)(min_wide_t)(w_sum * w_sum);

    const long long WRONG_44_45 = -11460068444416LL;        // pinned, as above
    bool coupling_ok = ((long long)ok48  == WANT_NUM2) &&   // implemented types
                       ((long long)ok44  == WANT_NUM2) &&   // joint minimum
                       ((long long)bad45 == WRONG_44_45);   // widening num_t
    printf("  %-32s 48/48: %lld, 44/44: %lld, 44/45: %lld (pinned %lld, want "
           "%lld)  %s\n",
           "width-coupling-witness",
           (long long)ok48, (long long)ok44, (long long)bad45, WRONG_44_45,
           WANT_NUM2, coupling_ok ? "PASS" : "FAIL");

    return dt_ok && wide_ok && num_pos_ok && num_neg_ok && num_flat_ok &&
           witness_ok && coupling_ok;
}

static int run_direct_tests()
{
    printf("--- direct DUT tests (contract §4.6) ---\n");
    int failures = 0;

    // 1. FLAT TEMPLATE, varied patch.  dt == 0, so every window takes the
    //    defensive fallback and scores +0.0; best_score starts at -2.0, so the
    //    first window wins and the reported location is (0,0).
    //
    //    This is the DUT's documented behaviour for input the ABI forbids.
    //    It is NOT agreement with OpenCV, which has no one answer here: its
    //    `templNorm < DBL_EPSILON` early return fills the whole result map with
    //    ONES, but templNorm comes from meanStdDev's float64 cancellation, so a
    //    flat template that computes a tiny nonzero variance (a 7x7 of 2s gives
    //    4.44e-16) misses the branch and gets correlated instead (§4.6).  The
    //    test pins what the hardware does so a future change to it is
    //    deliberate, not so the two can be called equivalent.
    {
        static unsigned char patch[12 * 10];
        for (int r = 0; r < 10; r++)
            for (int c = 0; c < 12; c++)
                patch[r * 12 + c] = (unsigned char)((r * 37 + c * 91) & 0xFF);
        unsigned char templ[16];
        for (int i = 0; i < 16; i++) templ[i] = 127;   // flat
        failures += !direct_case("flat-templ-4x4 (illegal input)",
                                 patch, 12, 10, templ, 4, 4,
                                 0x00000000u, 0, 0);
    }

    // 2. NEGATIVE CONTROL against threshold creep.  Fifteen 127s and one 128
    //    is the smallest legal non-flat template: dt = 16·258319 − 2033² = 15,
    //    the global legal minimum (§4.6).  The patch is the same 4x4, so the
    //    result map is 1x1, di == dt == 15, num == 15, and the score is
    //    exactly 1.0 — 0x3F800000, bit-for-bit, since 15/sqrt(225) is exact in
    //    float.
    //
    //    If anyone ever replaces the `min == max` rejection with a variance
    //    threshold, or widens the DUT's `dt == 0` test to `dt < something`,
    //    this is the case that stops scoring 1.0 and starts scoring 0.0.
    {
        unsigned char t[16];
        for (int i = 0; i < 16; i++) t[i] = 127;
        t[15] = 128;
        failures += !direct_case("min-nonflat dt=15 (must score 1.0)",
                                 t, 4, 4, t, 4, 4,
                                 0x3F800000u, 0, 0);
    }

    // 3. The §4.6 width extremes and preservation policy, in the DUT's types.
    failures += !bound_case();

    printf("--- direct tests: %d failure(s) ---\n\n", failures);
    return failures;
}

int main(int argc, char** argv)
{
    // An unrecognised argument selects nothing and leaves the default in
    // place — silently running csim when "hw" was meant would report a pass
    // for a suite that never ran, so reject it instead.
    const char* suite = "csim";
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "csim")  == 0 ||
            strcmp(argv[i], "cosim") == 0 ||
            strcmp(argv[i], "hw")    == 0) {
            suite = argv[i];
        } else {
            fprintf(stderr, "unknown suite %s (expected csim, cosim or hw)\n",
                    argv[i]);
            return 1;
        }
    }

    char cases_file[64], patch_file[64], templ_file[64];
    snprintf(cases_file, sizeof(cases_file), "tb_tme_cases_%s.txt",   suite);
    snprintf(patch_file, sizeof(patch_file), "tb_tme_patches_%s.bin", suite);
    snprintf(templ_file, sizeof(templ_file), "tb_tme_templs_%s.bin",  suite);

    // ---- Load and validate the manifest BEFORE running anything ---------
    FILE* fm = fopen(cases_file, "r");
    if (!fm) {
        fprintf(stderr, "%s not found — run tme_generate_golden.py first\n",
                cases_file);
        return 1;
    }
    int n_cases = 0;
    long patch_bytes = 0, templ_bytes = 0;
    if (fscanf(fm, "%d %ld %ld", &n_cases, &patch_bytes, &templ_bytes) != 3
        || n_cases < 1 || n_cases > TB_MAX_CASES) {
        fprintf(stderr, "%s: bad header\n", cases_file);
        fclose(fm);
        return 1;
    }

    static tb_case_t cases[TB_MAX_CASES];
    int manifest_errors = 0;
    for (int i = 0; i < n_cases; i++) {
        tb_case_t& c = cases[i];
        int idx = -1;
        if (fscanf(fm, "%d %d %d %d %d %ld %ld %f %d %d %f %31s %63s",
                   &idx, &c.pw, &c.ph, &c.tw, &c.th,
                   &c.patch_off, &c.templ_off,
                   &c.gold_score, &c.gold_x, &c.gold_y, &c.margin,
                   c.category, c.tag) != 13 || idx != i) {
            fprintf(stderr, "manifest row %d: parse error\n", i);
            manifest_errors++;
            break;
        }
        // The DUT's BRAMs are sized to the contract envelope; a manifest
        // outside it would smash them, so reject before running.
        int rw = c.pw - c.tw + 1, rh = c.ph - c.th + 1;
        if (c.pw < c.tw || c.pw > MAX_PATCH_W ||
            c.ph < c.th || c.ph > MAX_PATCH_H ||
            c.tw < 4 || c.tw > MAX_TEMPL_W ||
            c.th < 4 || c.th > MAX_TEMPL_H ||
            c.patch_off + (long)c.pw * c.ph > patch_bytes ||
            c.templ_off + (long)c.tw * c.th > templ_bytes ||
            c.gold_x < 0 || c.gold_x >= rw ||
            c.gold_y < 0 || c.gold_y >= rh) {
            fprintf(stderr, "manifest row %d (%s): outside envelope/blob\n",
                    i, c.tag);
            manifest_errors++;
        }
    }
    fclose(fm);
    if (manifest_errors) {
        fprintf(stderr, "TESTBENCH FAILED: manifest error(s), DUT not run\n");
        return 1;
    }

    unsigned char* patches = read_blob(patch_file, patch_bytes);
    unsigned char* templs  = read_blob(templ_file, templ_bytes);
    if (!patches || !templs) return 1;

    printf("%s: %d cases, %ld patch bytes, %ld template bytes\n\n",
           cases_file, n_cases, patch_bytes, templ_bytes);

    // ---- §4.6 direct tests, before the manifest suite --------------------
    // Counted separately from the manifest cases so the suite totals stay
    // comparable across runs, but a failure here fails the testbench.
    int direct_failures = run_direct_tests();

    // ---- Run every case through the DUT ---------------------------------
    int failures = 0;
    for (int i = 0; i < n_cases; i++) {
        const tb_case_t& c = cases[i];

        hls::stream<pix_stream_t> patch_stream("patch");
        hls::stream<pix_stream_t> templ_stream("templ");
        stream_image(patch_stream, patches + c.patch_off, c.pw, c.ph);
        stream_image(templ_stream, templs + c.templ_off, c.tw, c.th);

        float dut_score = -99.0f;
        ap_uint<16> dut_x = 0xFFFF, dut_y = 0xFFFF;

        tme_top(patch_stream, templ_stream,
                (ap_uint<16>)c.pw, (ap_uint<16>)c.ph,
                (ap_uint<16>)c.tw, (ap_uint<16>)c.th,
                dut_score, dut_x, dut_y);

        float err = dut_score - c.gold_score;
        if (err < 0) err = -err;
        bool score_ok = (err <= MAX_SCORE_ERR);
        bool loc_ok   = ((int)dut_x == c.gold_x && (int)dut_y == c.gold_y);
        // A DUT that under-reads its streams desynchronises silently in
        // hardware; in csim it shows up as leftover beats.
        bool drained  = patch_stream.empty() && templ_stream.empty();

        bool ok = score_ok && loc_ok && drained;
        if (!ok) failures++;

        printf("[%2d] %-28s %-11s patch %4dx%-3d templ %3dx%-2d  "
               "gold %+.4f @(%3d,%3d)  dut %+.4f @(%3d,%3d)  %s%s%s%s\n",
               i, c.tag, c.category, c.pw, c.ph, c.tw, c.th,
               c.gold_score, c.gold_x, c.gold_y,
               dut_score, (int)dut_x, (int)dut_y,
               ok ? "PASS" : "FAIL",
               score_ok ? "" : " [score]",
               loc_ok ? "" : " [loc]",
               drained ? "" : " [beats left in stream]");
    }

    free(patches);
    free(templs);

    printf("\n%d/%d cases passed\n", n_cases - failures, n_cases);
    if (failures || direct_failures) {
        fprintf(stderr, "TESTBENCH FAILED: %d manifest case(s), %d direct "
                        "test(s)\n", failures, direct_failures);
        return 1;
    }
    printf("TESTBENCH PASSED\n");
    return 0;
}
