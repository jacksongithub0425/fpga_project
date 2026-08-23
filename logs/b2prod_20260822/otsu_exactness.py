"""Prove the streamed Otsu is the SAME NUMBER as cv2.threshold(THRESH_OTSU).

Three populations, because each can break the other two's assumptions:

  1. randomised images        -- ordinary histograms, many of them
  2. adversarial histograms   -- flat, bimodal-with-ties, single-bin, two-bin,
                                and near-degenerate tails, where OpenCV's
                                FLT_EPSILON guards and its strict `>` tie rule
                                are what decide the answer
  3. the real production page -- the one this has to be right about

Population 2 is the one that matters: a transcription that used `>=`, or
DBL_EPSILON, or a last-maximiser convention, passes population 1 easily.
"""
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))

import cv2
import numpy as np

import corpus_labels as CL
import pl_backends as B

rng = np.random.default_rng(20260822)
fails = []


def cv_otsu(img_u8):
    thr, _ = cv2.threshold(img_u8, 0, 255, cv2.THRESH_OTSU)
    return int(thr)


def check(label, mine, theirs):
    if mine != theirs:
        fails.append(f"{label}: streamed {mine} vs cv2 {theirs}")


# -- 1. randomised images ---------------------------------------------------
n1 = 0
for trial in range(400):
    h = int(rng.integers(3, 60))
    w = int(rng.integers(3, 60))
    style = trial % 4
    if style == 0:
        gray = rng.integers(0, 256, (h, w), dtype=np.uint8)
    elif style == 1:                       # bimodal, like a real page
        gray = np.where(rng.random((h, w)) < 0.7,
                        rng.integers(200, 256, (h, w)),
                        rng.integers(0, 60, (h, w))).astype(np.uint8)
    elif style == 2:                       # low contrast
        gray = rng.integers(118, 138, (h, w), dtype=np.uint8)
    else:                                  # a few discrete levels
        gray = rng.choice(np.array([0, 17, 130, 255], np.uint8), (h, w))
    blur = B.truncating_blur(gray)
    if blur.size == 0:
        continue
    n1 += 1
    check(f"image[{trial}] {w}x{h}",
          B.otsu_on_truncating_blur(gray), cv_otsu(blur.astype(np.uint8)))
    # And every stripe height must give the same histogram as one shot.
    for rows in (1, 2, 3, 7, 10_000):
        hist = B.blur_histogram(gray, rows=rows)
        ref = np.bincount(blur.reshape(-1), minlength=256).astype(np.int64)
        if not np.array_equal(hist, ref):
            fails.append(f"image[{trial}] histogram differs at rows={rows}")
        stripes = [s for _y, s in B.blur_stripes(gray, rows=rows)]
        if not np.array_equal(np.concatenate(stripes, axis=0), blur):
            fails.append(f"image[{trial}] stripes != truncating_blur "
                         f"at rows={rows}")

# -- 2. adversarial histograms ---------------------------------------------
# Fed to cv2 as a synthetic image with exactly that histogram: Otsu depends on
# nothing else, so this compares the two implementations on the histogram
# itself rather than on whatever histograms random images happen to produce.
def image_with_histogram(hist):
    vals = np.repeat(np.arange(256, dtype=np.uint8),
                     np.asarray(hist, dtype=np.int64))
    return vals.reshape(1, -1)


cases = {
    "single bin": [0] * 256,
    "two adjacent bins": [0] * 256,
    "flat": [1] * 256,
    "tied maxima": [0] * 256,
    "one outlier": [0] * 256,
    "empty tails": [0] * 256,
}
cases["single bin"][7] = 1000
cases["two adjacent bins"][100] = 500
cases["two adjacent bins"][101] = 500
cases["tied maxima"][0] = 100
cases["tied maxima"][128] = 100
cases["tied maxima"][255] = 100
cases["one outlier"][0] = 1
cases["one outlier"][255] = 10_000_000
cases["empty tails"][3] = 7
cases["empty tails"][250] = 7

for k in range(300):                        # plus randomised sparse ones
    hist = [0] * 256
    for _ in range(int(rng.integers(1, 6))):
        hist[int(rng.integers(0, 256))] += int(rng.integers(1, 50))
    cases[f"sparse[{k}]"] = hist

for label, hist in cases.items():
    img = image_with_histogram(hist)
    if img.size == 0:
        continue
    check(f"hist {label}", B.otsu_from_histogram(np.array(hist, np.int64)),
          cv_otsu(img))

# -- 3. the real page -------------------------------------------------------
page_note = "skipped (no fitz)"
try:
    import fitz
    pdf = CL.resolve("doc_002")
    if pdf is None:
        raise RuntimeError("corpus document doc_002 is not on this machine")
    doc = fitz.open(pdf)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(4.0, 4.0), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    del img, bgr, pix
    streamed = B.otsu_on_truncating_blur(gray)
    blur = B.truncating_blur(gray)          # only affordable on this PC
    reference = cv_otsu(blur.astype(np.uint8))
    hist_ref = np.bincount(blur.reshape(-1), minlength=256).astype(np.int64)
    del blur
    check(f"page {gray.shape[1]}x{gray.shape[0]}", streamed, reference)
    if not np.array_equal(B.blur_histogram(gray), hist_ref):
        fails.append("page histogram differs from the whole-page blur")
    page_note = (f"{gray.shape[1]}x{gray.shape[0]}, threshold "
                 f"{streamed} (cv2 {reference}), "
                 f"stripe {B.stripe_rows(gray.shape[1])} rows = "
                 f"{B.stripe_rows(gray.shape[1]) * gray.shape[1] * 4 / 2**20:.1f} MB")
except Exception as exc:                                     # noqa: BLE001
    page_note = f"skipped ({type(exc).__name__}: {exc})"

print(f"randomised images   : {n1}")
print(f"histogram cases     : {len(cases)}")
print(f"production page     : {page_note}")
if fails:
    print(f"\nMISMATCHES ({len(fails)}):")
    for f in fails[:20]:
        print("  " + f)
    raise SystemExit(1)
print("\nEXACT: the streamed Otsu matched cv2.threshold(THRESH_OTSU) "
      "on every case.")
