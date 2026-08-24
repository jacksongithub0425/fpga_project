"""The five wrong transcriptions, shared by the control and the hunt."""
import numpy as np
import pl_backends as B

FLT_EPSILON = B._FLT_EPSILON
DBL_EPSILON = 2.220446049250313e-16


def variant(hist, *, tie="first", eps=FLT_EPSILON, guards=True,
            dtype=float, naive_mu1=False):
    h = [int(v) for v in hist]
    total = sum(h)
    scale = dtype(1.0) / dtype(total)
    mu = dtype(0.0)
    for i in range(256):
        mu += dtype(i) * dtype(h[i])
    mu *= scale
    mu1 = dtype(0.0)
    q1 = dtype(0.0)
    max_sigma = dtype(0.0)
    max_val = 0
    acc = dtype(0.0)                      # for the naive mu1 variant
    for i in range(256):
        p_i = dtype(h[i]) * scale
        if naive_mu1:
            acc += dtype(i) * p_i
        else:
            mu1 *= q1
        q1 += p_i
        q2 = dtype(1.0) - q1
        if guards and (min(q1, q2) < eps or max(q1, q2) > dtype(1.0) - eps):
            continue
        if q1 == 0 or q2 == 0:
            continue
        mu1 = acc / q1 if naive_mu1 else (mu1 + dtype(i) * p_i) / q1
        mu2 = (mu - q1 * mu1) / q2
        sigma = q1 * q2 * (mu1 - mu2) * (mu1 - mu2)
        if (sigma >= max_sigma) if tie == "last" else (sigma > max_sigma):
            max_sigma = sigma
            max_val = i
    return int(max_val)


VARIANTS = {
    "tie rule `>=` (last maximiser wins)": dict(tie="last"),
    "DBL_EPSILON instead of FLT_EPSILON": dict(eps=DBL_EPSILON),
    "no degenerate-tail guards at all":   dict(guards=False),
    "float32 accumulators":               dict(dtype=np.float32),
    "mu1 recomputed rather than running": dict(naive_mu1=True),
}


