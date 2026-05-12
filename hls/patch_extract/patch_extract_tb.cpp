// Testbench for patch_extract_core.
//
// Strategy: build a synthetic 256x256 image where every pixel encodes its
// own (y, x) coordinates: img[y][x] = (y*7 + x*3) & 0xFF. Push a small
// set of hand-crafted candidates and verify that every byte coming out of
// patch_out matches the expected pattern at the (y, x) the HLS code
// should have read.
//
// This catches three classes of bugs that are otherwise nearly invisible:
//   1. Wrong patch_box math       -> output pixels offset from expected (y,x)
//   2. Wrong cand_stream_t bit    -> wrong ep_x/ep_y/side/max_tw decoded
//      packing
//   3. Wrong m_axi flat indexing  -> wrong row stride; pixels from a
//      (e.g. using patch_w instead    different row than expected
//      of img_w)
//
// Test cases:
//   1. center-left    : centered candidate, left side
//   2. center-right   : centered candidate, right side  (verifies side flip)
//   3. top-left-clamp : near (0,0)        (verifies x0/y0 clamping to 0)
//   4. bot-right-clamp: near (255,255)    (verifies x1/y1 clamping to img)
//
// The final candidate's last pixel must assert TLAST on patch_out.

#include <cstdio>
#include <cstdint>
#include "patch_extract_core.h"

static const int IMG_W = 256;
static const int IMG_H = 256;

// Synthetic pattern - 7 and 3 are coprime to 256, so every (y,x) produces
// a recognisable value and any off-by-one in indexing is visible.
static inline ap_uint<8> expected_pixel(int y, int x) {
    return (ap_uint<8>)((y * 7 + x * 3) & 0xFF);
}

struct TestCand {
    const char* name;
    int  ep_x, ep_y;
    int  side;       // 0=left, 1=right
    int  max_tw;
    int  max_th;
};

// Mirror the HLS box math (patch_extract_core.cpp:69..90) exactly.
// Any divergence here breaks the test, which is the point - the test
// pins down the contract.
static void expected_box(const TestCand& t,
                         int img_w, int img_h,
                         int& x0, int& y0, int& x1, int& y1)
{
    int outward_w = (t.max_tw * 307) >> 7;   // ~ x2.4
    int inward_w  = (t.max_tw * 179) >> 7;   // ~ x1.4
    int patch_h   = (t.max_th * 410) >> 7;   // ~ x3.2
    if (t.side == 0) {
        x0 = t.ep_x - outward_w;
        x1 = t.ep_x + inward_w;
    } else {
        x0 = t.ep_x - inward_w;
        x1 = t.ep_x + outward_w;
    }
    y0 = t.ep_y - patch_h / 2;
    y1 = t.ep_y + patch_h / 2;
    if (x0 < 0)       x0 = 0;
    if (y0 < 0)       y0 = 0;
    if (x1 > img_w)   x1 = img_w;
    if (y1 > img_h)   y1 = img_h;
    if (x1 <= x0 + 1) x1 = x0 + 2;
    if (y1 <= y0 + 1) y1 = y0 + 2;
}

// Pack a candidate using the SAME bit positions as cand_stream_t in
// patch_extract_core.h (ep_x:16, ep_y:16, side:2, max_tw:14, max_th:16).
// Mismatches between this and the HLS decode are caught immediately.
static cand_stream_t pack_candidate(const TestCand& t, bool last) {
    cand_stream_t c;
    c.data = 0;
    c.data.range(15,  0) = (ap_uint<16>)t.ep_x;
    c.data.range(31, 16) = (ap_uint<16>)t.ep_y;
    c.data.range(33, 32) = (ap_uint<2>) t.side;
    c.data.range(47, 34) = (ap_uint<14>)t.max_tw;
    c.data.range(63, 48) = (ap_uint<16>)t.max_th;
    c.last = last ? 1 : 0;
    c.keep = 0xFF;
    return c;
}

int main() {
    // ---- Build synthetic image -----------------------------------------
    static ap_uint<8> img[IMG_H * IMG_W];   // 64 KB, BSS
    for (int y = 0; y < IMG_H; y++) {
        for (int x = 0; x < IMG_W; x++) {
            img[y * IMG_W + x] = expected_pixel(y, x);
        }
    }

    // ---- Test set ------------------------------------------------------
    TestCand tests[] = {
        {"center-left",    128, 128, 0, 20, 10},
        {"center-right",   128, 128, 1, 20, 10},
        {"top-left-clamp",  10,  10, 0, 20, 10},
        {"bot-right-clamp",250, 250, 1, 20, 10},
    };
    const int N = (int)(sizeof(tests) / sizeof(tests[0]));

    // ---- Push all candidates -------------------------------------------
    hls::stream<cand_stream_t> cand_in("cand");
    hls::stream<ppix_stream_t> patch_out("patch");
    for (int i = 0; i < N; i++) {
        cand_in.write(pack_candidate(tests[i], i == N - 1));
    }

    // ---- Run DUT -------------------------------------------------------
    patch_extract_core(cand_in, patch_out, img,
                       (ap_uint<16>)IMG_W, (ap_uint<16>)IMG_H);

    // ---- Verify each candidate's patch ---------------------------------
    int total_err = 0;
    bool saw_final_tlast = false;

    for (int i = 0; i < N; i++) {
        const TestCand& t = tests[i];
        int x0, y0, x1, y1;
        expected_box(t, IMG_W, IMG_H, x0, y0, x1, y1);
        int pw = x1 - x0, ph = y1 - y0;
        printf("[%s] ep=(%d,%d) side=%d -> patch (%d,%d)-(%d,%d)  %dx%d\n",
               t.name, t.ep_x, t.ep_y, t.side, x0, y0, x1, y1, pw, ph);

        int errs = 0;
        for (int r = 0; r < ph; r++) {
            for (int c = 0; c < pw; c++) {
                if (patch_out.empty()) {
                    printf("  FAIL: stream underflow at r=%d c=%d "
                           "(only %d/%d pixels delivered)\n",
                           r, c, r * pw + c, pw * ph);
                    return 1;
                }
                ppix_stream_t px = patch_out.read();
                ap_uint<8> exp = expected_pixel(y0 + r, x0 + c);
                if (px.data != exp) {
                    if (errs < 5) {
                        printf("  MISMATCH r=%d c=%d  got=%3d exp=%3d  "
                               "(img y=%d x=%d)\n",
                               r, c, (int)px.data, (int)exp,
                               y0 + r, x0 + c);
                    }
                    errs++;
                }
                bool is_last_pix = (r == ph - 1) && (c == pw - 1);
                if (is_last_pix && i == N - 1 && (int)px.last == 1) {
                    saw_final_tlast = true;
                }
            }
        }
        if (errs == 0) {
            printf("  OK (%d pixels)\n", pw * ph);
        } else {
            printf("  FAIL: %d mismatches out of %d pixels\n", errs, pw * ph);
        }
        total_err += errs;
    }

    if (!saw_final_tlast) {
        printf("FAIL: final candidate's last pixel did not assert TLAST\n");
        total_err++;
    }

    int leftover = 0;
    while (!patch_out.empty()) { patch_out.read(); leftover++; }
    if (leftover) {
        printf("FAIL: %d leftover pixels in patch_out after all "
               "candidates processed\n", leftover);
        total_err++;
    }

    if (total_err == 0) {
        printf("TESTBENCH PASSED\n");
        return 0;
    } else {
        printf("TESTBENCH FAILED (%d errors)\n", total_err);
        return 1;
    }
}
