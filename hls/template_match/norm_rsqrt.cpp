#include "tme_top.h"
#include <hls_math.h>

// Reciprocal square root for the normalized score denominator.
// Float keeps the Phase A HLS model aligned with the Python golden while
// avoiding fixed-point overflow in templ_energy * isq_col.
float norm_rsqrt(float x)
{
#pragma HLS PIPELINE II=8
#pragma HLS INLINE off

    if (x <= 1.0e-4f) return 0.0f;

    return 1.0f / hls::sqrtf(x);
}
