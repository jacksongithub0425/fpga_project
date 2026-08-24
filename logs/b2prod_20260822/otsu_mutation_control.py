"""Mutation control for the Otsu transcription.

Five plausible ways to get `getThreshVal_Otsu_8u` slightly wrong.  Each is run
over the SAME case population `otsu_exactness.py` uses.  A variant that no
case catches means the population proves nothing about that convention.
"""
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))

import cv2
import numpy as np

import pl_backends as B

rng = np.random.default_rng(20260822)
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


def image_with_histogram(hist):
    vals = np.repeat(np.arange(256, dtype=np.uint8),
                     np.asarray(hist, dtype=np.int64))
    return vals.reshape(1, -1)


# The same population as otsu_exactness.py.
cases = {}
c = {"single bin": [0] * 256, "two adjacent bins": [0] * 256,
     "flat": [1] * 256, "tied maxima": [0] * 256,
     "one outlier": [0] * 256, "empty tails": [0] * 256}
c["single bin"][7] = 1000
c["two adjacent bins"][100] = 500
c["two adjacent bins"][101] = 500
c["tied maxima"][0] = 100
c["tied maxima"][128] = 100
c["tied maxima"][255] = 100
c["one outlier"][0] = 1
c["one outlier"][255] = 10_000_000
c["empty tails"][3] = 7
c["empty tails"][250] = 7
cases.update(c)
for k in range(300):
    hist = [0] * 256
    for _ in range(int(rng.integers(1, 6))):
        hist[int(rng.integers(0, 256))] += int(rng.integers(1, 50))
    cases[f"sparse[{k}]"] = hist
for trial in range(400):
    h = int(rng.integers(3, 60))
    w = int(rng.integers(3, 60))
    style = trial % 4
    if style == 0:
        gray = rng.integers(0, 256, (h, w), dtype=np.uint8)
    elif style == 1:
        gray = np.where(rng.random((h, w)) < 0.7,
                        rng.integers(200, 256, (h, w)),
                        rng.integers(0, 60, (h, w))).astype(np.uint8)
    elif style == 2:
        gray = rng.integers(118, 138, (h, w), dtype=np.uint8)
    else:
        gray = rng.choice(np.array([0, 17, 130, 255], np.uint8), (h, w))
    blur = B.truncating_blur(gray)
    if blur.size:
        cases[f"image[{trial}]"] = list(
            np.bincount(blur.reshape(-1), minlength=256))

truth = {}
for label, hist in cases.items():
    img = image_with_histogram(hist)
    if img.size == 0:
        continue
    thr, _ = cv2.threshold(img, 0, 255, cv2.THRESH_OTSU)
    truth[label] = int(thr)

# The shipped transcription first: it must agree everywhere.
bad = [k for k, v in truth.items()
       if B.otsu_from_histogram(np.array(cases[k], np.int64)) != v]
print(f"population: {len(truth)} histograms")
print(f"shipped transcription: {len(bad)} mismatch(es)")
if bad:
    for k in bad[:5]:
        print(f"  {k}: {B.otsu_from_histogram(np.array(cases[k], np.int64))} "
              f"vs {truth[k]}")
    raise SystemExit(1)

missed = 0
for name, kw in VARIANTS.items():
    caught = [k for k, v in truth.items()
              if variant(np.array(cases[k], np.int64), **kw) != v]
    mark = "OK  " if caught else "MISS"
    if not caught:
        missed += 1
    example = f" e.g. {caught[0]}" if caught else ""
    print(f"  [{mark}] {name:<38} caught by {len(caught):>3} case(s)"
          f"{example}")

print("\n" + ("OTSU MUTATION CONTROL PASSED: every wrong convention is "
              "caught by the population" if missed == 0 else
              f"OTSU MUTATION CONTROL FAILED: {missed} variant(s) undetected"))
raise SystemExit(1 if missed else 0)
