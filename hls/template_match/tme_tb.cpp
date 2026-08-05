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
    if (failures) {
        fprintf(stderr, "TESTBENCH FAILED: %d case(s)\n", failures);
        return 1;
    }
    printf("TESTBENCH PASSED\n");
    return 0;
}
