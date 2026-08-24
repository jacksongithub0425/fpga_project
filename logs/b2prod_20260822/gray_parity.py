"""Is native grayscale rendering byte-identical to the current BGR2GRAY path?

The plan's step 2. `render_page()` currently costs about 620 MB on a
9792x6336 page -- the Pixmap's own buffer, the `pix.samples` bytes COPY, BGR,
and gray, all alive at once. Rendering straight to `csGRAY` would cut that to
the pixmap plus one gray array.

But it is only allowed if the bytes are IDENTICAL. MuPDF renders *into* the
gray colorspace, so antialiasing and blending happen in gray space rather than
being computed in RGB and converted afterwards, and its RGB->Gray weights are
not OpenCV's. A single grey level of difference moves the Otsu threshold, and
the threshold moves every downstream detection.

So this measures three things per page, over the whole corpus:

  1. `pix.samples` vs `pix.samples_mv` -- the zero-copy read must be the same
     bytes, or step 1 is unsafe on its own;
  2. native gray vs the current path -- exact equality, and if not, HOW far
     off and whether it moves the threshold;
  3. what each path would actually cost in bytes.

Reports the answer; changes nothing.
"""
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))

import cv2
import fitz
import numpy as np

import corpus_labels as CL
import pl_backends as B

ZOOM = 4.0
root = Path(r"C:\Users\lychee\Desktop\FPGA\sample")
# CL.documents() IS the ordering this line always used -- sorted by
# lower-cased name -- taken from one place now so the iteration order
# and the label numbering cannot drift apart.
pdfs = CL.documents(root)
labels = CL.labels(root)

mv_mismatch = []
exact_pages = 0
pages = 0
worst = {"max_abs": 0, "label": None}
thr_moved = []
rows = []

for pdf in pdfs:
    doc = fitz.open(pdf)
    for pno in range(len(doc)):
        page = doc[pno]
        # The LABEL, not the filename: this string is what reaches the
        # committed transcript.
        label = labels.page(pdf.name, pno + 1)
        mat = fitz.Matrix(ZOOM, ZOOM)

        # -- the current path, exactly as render_page() does it -------------
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        if pix.n == 4:
            bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        else:
            bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        gray_ref = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        # -- step 1: samples_mv must be the same bytes ----------------------
        mv = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        if not np.array_equal(mv, img):
            mv_mismatch.append(label)
        h, w, n = pix.height, pix.width, pix.n
        del img, mv, bgr, pix

        # -- step 2: native grayscale --------------------------------------
        gpix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
        gray_nat = np.frombuffer(gpix.samples_mv, dtype=np.uint8).reshape(
            gpix.height, gpix.width)
        same_shape = gray_nat.shape == gray_ref.shape
        if not same_shape:
            rows.append((label, w, h, "SHAPE", gray_nat.shape, gray_ref.shape))
            del gray_nat, gpix, gray_ref
            pages += 1
            continue

        diff = np.abs(gray_nat.astype(np.int16) - gray_ref.astype(np.int16))
        ndiff = int(np.count_nonzero(diff))
        maxd = int(diff.max()) if diff.size else 0
        pct = 100.0 * ndiff / diff.size

        thr_ref = B.otsu_on_truncating_blur(gray_ref)
        thr_nat = B.otsu_on_truncating_blur(gray_nat)
        if thr_ref != thr_nat:
            thr_moved.append((label, thr_ref, thr_nat))
        if maxd > worst["max_abs"]:
            worst.update(max_abs=maxd, label=label)
        if ndiff == 0:
            exact_pages += 1
        pages += 1
        rows.append((label, w, h, ndiff, pct, maxd, thr_ref, thr_nat))
        print(f"  {label:<34} {w}x{h}  diff {ndiff:>10,} px "
              f"({pct:6.3f}%)  max {maxd:>3}  Otsu {thr_ref}->{thr_nat}"
              + ("" if thr_ref == thr_nat else "   THRESHOLD MOVED"))
        del gray_nat, gpix, gray_ref, diff
    doc.close()

print(f"\n{pages} page(s)")
print(f"samples_mv == samples            : "
      f"{'YES on every page' if not mv_mismatch else mv_mismatch}")
print(f"native gray byte-identical       : {exact_pages}/{pages} page(s)")
print(f"worst absolute difference        : {worst['max_abs']} "
      f"({worst['label']})")
print(f"pages whose Otsu threshold moves : {len(thr_moved)}")
for label, a, b_ in thr_moved[:10]:
    print(f"    {label}: {a} -> {b_}")

W, H = 9792, 6336
print(f"\nbytes at {W}x{H}:")
print(f"  current  : pixmap {3*W*H:,} + samples copy {3*W*H:,} "
      f"+ BGR {3*W*H:,} + gray {W*H:,} = {3*W*H*3 + W*H:,}")
print(f"  samples_mv only (still RGB+BGR): "
      f"{3*W*H + 3*W*H + W*H:,}")
print(f"  native gray + samples_mv       : "
      f"pixmap {W*H:,} + gray copy {W*H:,} = {2*W*H:,}")

print("\nVERDICT: native grayscale is "
      + ("BYTE-IDENTICAL and may replace the current path."
         if exact_pages == pages and not mv_mismatch
         else "NOT byte-identical; the striped RGB fallback is required."))
raise SystemExit(0 if exact_pages == pages else 3)
