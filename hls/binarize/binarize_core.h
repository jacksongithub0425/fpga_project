#pragma once
#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

// Maximum image dimensions — sample PDF at ZOOM=4.0 renders to 9792×6336 px.
// Round up to next 128-byte AXI burst boundary.
static const int BINARIZE_MAX_W = 9856;   // >= 9792
static const int BINARIZE_MAX_H = 6400;   // >= 6336

typedef ap_axiu<8, 1, 1, 1> bpix_t;

// Top-level: stream grayscale pixels in, stream binary pixels out.
// threshold is written via AXI4-Lite before START is asserted.
void binarize_core(
    hls::stream<bpix_t>& gray_in,
    hls::stream<bpix_t>& bin_out,
    ap_uint<16> img_w,
    ap_uint<16> img_h,
    ap_uint<8>  threshold
);
