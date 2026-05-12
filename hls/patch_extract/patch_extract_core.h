#pragma once
#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

// Candidate struct packed into 64 bits for AXI4-Stream transport:
//   bits [15:0]  endpoint_x  (pixel coordinate)
//   bits [31:16] endpoint_y
//   bits [33:32] side        (0=left, 1=right)
//   bits [47:34] max_templ_w (post-scale, at largest scale = 216)
//   bits [63:48] max_templ_h (post-scale, at largest scale = 96)
typedef ap_axiu<64, 1, 1, 1> cand_stream_t;

// Output: raw patch pixels, row-major, uint8
typedef ap_axiu<8, 1, 1, 1>  ppix_stream_t;

static const int PE_MAX_PATCH_W = 1024;
static const int PE_MAX_PATCH_H = 320;
static const int PE_MAX_IMG_W   = 2560;
static const int PE_MAX_IMG_H   = 3600;

// Maximum number of bytes the m_axi master may touch in one invocation.
// Used by HLS for cosim and to size the burst FIFO. PE_MAX_IMG_W * PE_MAX_IMG_H
// is the worst-case full-image footprint.
static const int PE_MAX_IMG_BYTES = PE_MAX_IMG_W * PE_MAX_IMG_H;

void patch_extract_core(
    hls::stream<cand_stream_t>& cand_in,     // candidate descriptors from PS
    hls::stream<ppix_stream_t>& patch_out,   // patch pixels to template_match_core
    const ap_uint<8>* bin_image,             // DDR3 base address (set via s_axilite offset)
    ap_uint<16> img_w,
    ap_uint<16> img_h
);
