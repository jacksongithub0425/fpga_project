"""Are the three unfalsified Otsu conventions observable on the REAL corpus?

The random and adversarial populations separate the two SEMANTIC conventions
(the strict `>` tie rule, and the presence of the degenerate-tail guards) but
not the three NUMERIC ones: the epsilon constant, float64 vs float32, and
whether mu1 is accumulated or recomputed.  A broader synthetic search was
abandoned -- probing FLT_EPSILON needs histograms with ~10^9 counts, and
feeding one to `cv2` means materialising a 1 GB image per case.

So this asks the question that actually decides the project: on the 36 pages
this build has to binarise, does any of the three change the threshold?

Each real page is checked BOTH ways -- the shipped transcription against
`cv2.threshold` on the whole-page blur, and every variant against `cv2` -- so
a page where they disagree names itself.
"""
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import fitz
import numpy as np

import pl_backends as B
from otsu_variants import VARIANTS, variant

root = Path(r"C:\Users\lychee\Desktop\FPGA\sample")
pdfs = sorted({str(p).lower(): p for p in root.glob("*.pdf")}.values())

shipped_bad = []
variant_bad = {name: [] for name in VARIANTS}
pages = 0

for pdf in pdfs:
    doc = fitz.open(pdf)
    for pno in range(len(doc)):
        pix = doc[pno].get_pixmap(matrix=fitz.Matrix(4.0, 4.0), alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        if pix.n == 4:
            bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        else:
            bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        del img, bgr, pix

        hist = B.blur_histogram(gray)                 # streamed, 8 MB peak
        blur = B.truncating_blur(gray)                # whole page, this PC only
        ref, _ = cv2.threshold(blur.astype(np.uint8), 0, 255, cv2.THRESH_OTSU)
        ref = int(ref)
        hist_ref = np.bincount(blur.reshape(-1), minlength=256).astype(np.int64)
        del blur, gray

        label = f"{pdf.name} p{pno + 1}"
        pages += 1
        if not np.array_equal(hist, hist_ref):
            shipped_bad.append(f"{label}: streamed histogram differs")
        got = B.otsu_from_histogram(hist)
        if got != ref:
            shipped_bad.append(f"{label}: shipped {got} vs cv2 {ref}")
        for name, kw in VARIANTS.items():
            if variant(hist, **kw) != ref:
                variant_bad[name].append(f"{label}: {variant(hist, **kw)} "
                                         f"vs {ref}")
        print(f"  {label:<34} threshold {ref:>3}"
              + ("" if got == ref else f"   SHIPPED DISAGREES ({got})"))
    doc.close()

print(f"\n{pages} real page(s)")
print(f"shipped transcription: {len(shipped_bad)} disagreement(s) with cv2"
      + ("" if not shipped_bad else ":"))
for b in shipped_bad[:10]:
    print("  " + b)
print("\nvariants, on the corpus that decides this project:")
for name in VARIANTS:
    hits = variant_bad[name]
    mark = "SEPARATED  " if hits else "unfalsified"
    print(f"  [{mark}] {name:<38} {len(hits)} page(s)"
          + (f" e.g. {hits[0]}" if hits else ""))
raise SystemExit(1 if shipped_bad else 0)
