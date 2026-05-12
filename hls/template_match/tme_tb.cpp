// Testbench for tme_top.
//
// Usage: compile alongside tme_top.cpp and run.  Golden data is generated
// by running tme_generate_golden.py, which writes:
//   tb_patch.bin    — raw uint8 patch pixels, row-major
//   tb_templ.bin    — raw uint8 encoded template (int8+128), row-major
//   tb_golden.txt   — "score x y patch_w patch_h templ_w templ_h"
//
// The testbench streams both buffers through the HLS function and
// asserts that the output score is within MAX_SCORE_ERR of the golden.

#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <cassert>
#include "tme_top.h"

static const float MAX_SCORE_ERR = 0.005f;

// Helper: read a binary file into a flat buffer
static int read_bin(const char* path, unsigned char* buf, int max_bytes)
{
    FILE* f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "Cannot open %s\n", path); return -1; }
    int n = (int)fread(buf, 1, max_bytes, f);
    fclose(f);
    return n;
}

int main()
{
    // ---- Load golden data ----------------------------------------------
    float   gold_score;
    int     gold_x, gold_y;
    int     pw, ph, tw, th;

    FILE* fg = fopen("tb_golden.txt", "r");
    if (!fg) { fprintf(stderr, "tb_golden.txt not found — run tme_generate_golden.py first\n"); return 1; }
    fscanf(fg, "%f %d %d %d %d %d %d", &gold_score, &gold_x, &gold_y, &pw, &ph, &tw, &th);
    fclose(fg);

    printf("Golden: score=%.4f  loc=(%d,%d)  patch=%dx%d  templ=%dx%d\n",
           gold_score, gold_x, gold_y, pw, ph, tw, th);

    // ---- Load pixel data -----------------------------------------------
    static unsigned char patch_raw[MAX_PATCH_H * MAX_PATCH_W];
    static unsigned char templ_raw[MAX_TEMPL_H * MAX_TEMPL_W];

    int patch_bytes = read_bin("tb_patch.bin", patch_raw, sizeof(patch_raw));
    int templ_bytes = read_bin("tb_templ.bin", templ_raw, sizeof(templ_raw));

    assert(patch_bytes == pw * ph);
    assert(templ_bytes == tw * th);

    // ---- Build HLS streams ---------------------------------------------
    hls::stream<pix_stream_t> patch_stream("patch");
    hls::stream<pix_stream_t> templ_stream("templ");

    for (int i = 0; i < ph; i++) {
        for (int j = 0; j < pw; j++) {
            pix_stream_t px;
            px.data = patch_raw[i * pw + j];
            px.last = (i == ph-1 && j == pw-1) ? 1 : 0;
            px.keep = 0xFF;
            patch_stream.write(px);
        }
    }

    for (int i = 0; i < th; i++) {
        for (int j = 0; j < tw; j++) {
            pix_stream_t px;
            px.data = templ_raw[i * tw + j];
            px.last = (i == th-1 && j == tw-1) ? 1 : 0;
            px.keep = 0xFF;
            templ_stream.write(px);
        }
    }

    // ---- Run DUT -------------------------------------------------------
    float      dut_score = 0.0f;
    ap_uint<16> dut_x = 0, dut_y = 0;

    tme_top(patch_stream, templ_stream,
            (ap_uint<16>)pw, (ap_uint<16>)ph,
            (ap_uint<16>)tw, (ap_uint<16>)th,
            dut_score, dut_x, dut_y);

    printf("DUT:    score=%.4f  loc=(%d,%d)\n",
           dut_score, (int)dut_x, (int)dut_y);

    // ---- Check ---------------------------------------------------------
    float err = fabsf(dut_score - gold_score);
    printf("Score error: %.6f  (limit %.3f)  — %s\n",
           err, MAX_SCORE_ERR, (err <= MAX_SCORE_ERR) ? "PASS" : "FAIL");

    if (err > MAX_SCORE_ERR) {
        fprintf(stderr, "TESTBENCH FAILED: score error %.6f > %.3f\n", err, MAX_SCORE_ERR);
        return 1;
    }

    printf("TESTBENCH PASSED\n");
    return 0;
}
