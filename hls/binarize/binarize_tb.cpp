// Exact, self-contained C testbench for binarize_core.
//
// The oracle independently computes the HLS integer Gaussian (sum >> 4),
// thresholds it, and places each result directly in the logical DDR frame.
// There is no OpenCV reference and no mismatch tolerance: every output byte,
// sideband, beat count, and TLAST position must agree exactly.

#include <cstdio>
#include <vector>

#include "binarize_core.h"

static unsigned char pattern_pixel(int row, int col, int salt)
{
    return (unsigned char)((row * 53 + col * 29 + row * col * 7
                            + salt * 41 + 17) & 0xFF);
}

static std::vector<unsigned char> make_image(int h, int w, int salt)
{
    std::vector<unsigned char> gray(h * w);
    for (int row = 0; row < h; ++row) {
        for (int col = 0; col < w; ++col) {
            gray[row * w + col] = pattern_pixel(row, col, salt);
        }
    }
    return gray;
}

static std::vector<unsigned char> logical_golden(
    const std::vector<unsigned char>& gray,
    int h,
    int w,
    int threshold)
{
    // Logical row/column 0 and the final logical row/column stay zero.
    // For an interior logical (row,col), the raw result arrived while the
    // input window was centred at that same logical coordinate.
    std::vector<unsigned char> gold(h * w, 0);
    for (int row = 1; row < h - 1; ++row) {
        for (int col = 1; col < w - 1; ++col) {
            unsigned int sum =
                gray[(row - 1) * w + col - 1]
              + 2u * gray[(row - 1) * w + col]
              +      gray[(row - 1) * w + col + 1]
              + 2u * gray[row * w + col - 1]
              + 4u * gray[row * w + col]
              + 2u * gray[row * w + col + 1]
              +      gray[(row + 1) * w + col - 1]
              + 2u * gray[(row + 1) * w + col]
              +      gray[(row + 1) * w + col + 1];
            unsigned int blurred = sum >> 4;  // exact HLS truncation
            gold[row * w + col] = blurred <= (unsigned int)threshold
                                 ? 255 : 0;
        }
    }
    return gold;
}

static int run_case(const char* name, int h, int w, int threshold, int salt)
{
    const int total = h * w;
    std::vector<unsigned char> gray = make_image(h, w, salt);
    if (salt < 0) {
        // Explicit truncation witness: weighted sum = 1608, so HLS computes
        // 1608 >> 4 = 100 while a rounding implementation would produce 101.
        // At threshold 100 the correct THRESH_BINARY_INV result is therefore
        // 255 at the sole interior logical pixel.
        for (int i = 0; i < total; ++i) gray[i] = 100;
        gray[(h / 2) * w + (w / 2)] = 102;
    }
    std::vector<unsigned char> gold = logical_golden(
        gray, h, w, threshold);
    std::vector<unsigned char> observed(total, 0);

    hls::stream<bpix_t> gray_stream("gray");
    hls::stream<bpix_t> bin_stream("bin");

    for (int i = 0; i < total; ++i) {
        bpix_t px;
        px.data = gray[i];
        px.keep = 1;
        px.strb = 1;
        px.last = (i == total - 1) ? 1 : 0;
        px.user = 0;
        px.id   = 0;
        px.dest = 0;
        gray_stream.write(px);
    }

    binarize_core(gray_stream, bin_stream,
                  (ap_uint<16>)w, (ap_uint<16>)h,
                  (ap_uint<8>)threshold);

    int errors = 0;
    for (int i = 0; i < total; ++i) {
        if (bin_stream.empty()) {
            std::fprintf(stderr,
                         "[%s] under-produced: stream empty at beat %d/%d\n",
                         name, i, total);
            return 1;
        }
        bpix_t px = bin_stream.read();
        unsigned char got = (unsigned char)px.data;
        observed[i] = got;
        bool expected_last = (i == total - 1);
        if (got != gold[i]) {
            int row = i / w;
            int col = i % w;
            std::fprintf(stderr,
                         "[%s] data mismatch logical (%d,%d): got=%u expected=%u\n",
                         name, row, col, (unsigned int)got,
                         (unsigned int)gold[i]);
            ++errors;
        }
        if ((bool)px.last != expected_last) {
            std::fprintf(stderr,
                         "[%s] TLAST mismatch at beat %d: got=%d expected=%d\n",
                         name, i, (int)(bool)px.last, (int)expected_last);
            ++errors;
        }
        if ((unsigned int)px.keep != 1u || (unsigned int)px.strb != 1u
            || (unsigned int)px.user != 0u || (unsigned int)px.id != 0u
            || (unsigned int)px.dest != 0u) {
            std::fprintf(stderr, "[%s] sideband mismatch at beat %d\n",
                         name, i);
            ++errors;
        }
    }

    if (!bin_stream.empty()) {
        std::fprintf(stderr, "[%s] over-produced beyond %d beats\n",
                     name, total);
        ++errors;
    }
    if (!gray_stream.empty()) {
        std::fprintf(stderr, "[%s] did not consume the complete input\n",
                     name);
        ++errors;
    }

    // Contract boundary's stale-memory guard: both mandatory borders are
    // checked on observed DUT output, independently of the all-pixel compare.
    for (int col = 0; col < w; ++col) {
        if (observed[(h - 1) * w + col] != 0) {
            std::fprintf(stderr,
                         "[%s] final row nonzero at logical (%d,%d)\n",
                         name, h - 1, col);
            ++errors;
        }
    }
    for (int row = 0; row < h; ++row) {
        if (observed[row * w + (w - 1)] != 0) {
            std::fprintf(stderr,
                         "[%s] final column nonzero at logical (%d,%d)\n",
                         name, row, w - 1);
            ++errors;
        }
    }

    std::printf("[%s] %s: %dx%d threshold=%d, %d exact beats\n",
                name, errors ? "FAIL" : "PASS", w, h, threshold, total);
    return errors ? 1 : 0;
}

int main()
{
    int failed = 0;

    // Minimum legal geometry: one valid Gaussian result plus the borders.
    failed += run_case("minimum-3x3", 3, 3, 128, 0);

    // This fails if sum >> 4 is accidentally replaced by rounded division.
    failed += run_case("truncation-witness", 3, 3, 100, -1);

    // Both orientations exercise compact row suffixes with odd/even geometry.
    failed += run_case("wide-5x4-mixed", 4, 5, 150, 1);
    failed += run_case("tall-4x5-mixed", 5, 4, 130, 2);

    // Legal geometry limits are cheap here (~49k pixels total) and exercise
    // the last line-buffer column and the full 16-bit row-control path.
    failed += run_case("max-width", 3, BINARIZE_MAX_W, 127, 3);
    failed += run_case("max-height", BINARIZE_MAX_H, 3, 127, 4);

    // Threshold extremes pin both sides of THRESH_BINARY_INV.
    failed += run_case("threshold-zero", 8, 9, 0, 5);
    failed += run_case("threshold-255", 8, 9, 255, 6);

    // A second, differently shaped invocation catches stale static line
    // buffer/window contents leaking across starts.
    failed += run_case("restart-different-shape", 5, 13, 173, 7);

    if (failed) {
        std::fprintf(stderr, "BINARIZE EXACT SUITE FAILED: %d case(s)\n",
                     failed);
        return 1;
    }
    std::printf("BINARIZE EXACT SUITE PASSED: 9/9 cases\n");
    return 0;
}
