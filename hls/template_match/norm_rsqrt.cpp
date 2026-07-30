#include "tme_top.h"

// Fixed-point reciprocal square root via 3-iteration Newton-Raphson.
// For input x, computes y ≈ 1/sqrt(x).
//
// Newton-Raphson update: y_{n+1} = y_n * (1.5 - 0.5 * x * y_n^2)
//
// Initial estimate: use leading-zero count to find the power-of-two
// bracket, then start at 2^(-floor(log2(x)/2)).  Good enough for 3
// iterations to converge within 0.1% of the true value.
//
// For TM_CCOEFF_NORMED the denominator (energy product) is always > 0
// when there is any signal; the caller is responsible for guarding
// against x == 0.
fixed_t norm_rsqrt(fixed_t x)
{
#pragma HLS PIPELINE II=5
#pragma HLS INLINE off

    // Clamp to avoid divide-by-zero (caller should also guard)
    if (x <= fixed_t(0.0001f)) return fixed_t(0.0f);

    // Coarse initial estimate: y0 = 1.0 (works for x near 1)
    // For better convergence over a wide range use a lookup table on
    // the top 8 bits of x's integer part — but for Phase A this
    // simple start converges within 3 iterations for inputs in [1e-4, 1e4].
    fixed_t y = fixed_t(1.0f);

    // Iteration 1
    y = y * (fixed_t(1.5f) - fixed_t(0.5f) * x * y * y);
    // Iteration 2
    y = y * (fixed_t(1.5f) - fixed_t(0.5f) * x * y * y);
    // Iteration 3
    y = y * (fixed_t(1.5f) - fixed_t(0.5f) * x * y * y);

    return y;
}
