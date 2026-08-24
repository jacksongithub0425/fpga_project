"""The new `render_page` must produce the OLD bytes, on every corpus page.

The old form, reproduced here verbatim so the comparison is against what
actually shipped rather than against a paraphrase of it:

    pix   = page.get_pixmap(matrix=mat, alpha=False)
    img   = np.frombuffer(pix.samples, ...)        # a COPY
    bgr   = cvtColor(img, RGB2BGR)
    gray  = cvtColor(bgr, BGR2GRAY)

Three things are checked per page:

  1. `keep_bgr=True` still returns the old BGR and the old grey exactly --
     this is the frozen CPU path and it may not move at all;
  2. `keep_bgr=False` returns the same GREY, which is the whole point: the
     striped conversion must not be a different image;
  3. the Otsu threshold and the core-equivalent binary agree, because that is
     what the difference would actually reach if there were one.
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
import terminal_counter_endpoint_first as det

ZOOM = 4.0


def legacy_render(page, zoom=ZOOM):
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return bgr, gray


root = Path(r"C:\Users\lychee\Desktop\FPGA\sample")
# CL.documents() IS the ordering this line always used -- sorted by
# lower-cased name -- taken from one place now so the iteration order
# and the label numbering cannot drift apart.
pdfs = CL.documents(root)
labels = CL.labels(root)

bad = []
pages = 0
print(f"HAVE_SAMPLES_MV = {det.HAVE_SAMPLES_MV}, "
      f"GRAY_STRIPE_BYTES = {det.GRAY_STRIPE_BYTES:,}")

for pdf in pdfs:
    doc = fitz.open(pdf)
    for pno in range(len(doc)):
        page = doc[pno]
        # The LABEL, not the filename: this string is what reaches the
        # committed transcript.
        label = labels.page(pdf.name, pno + 1)
        pages += 1

        old_bgr, old_gray = legacy_render(page)
        new_bgr, new_gray, _ = det.render_page(page, zoom=ZOOM, keep_bgr=True)
        free_bgr, free_gray, _ = det.render_page(page, zoom=ZOOM,
                                                 keep_bgr=False)

        checks = {
            "keep_bgr=True BGR": np.array_equal(new_bgr, old_bgr),
            "keep_bgr=True grey": np.array_equal(new_gray, old_gray),
            "keep_bgr=False is None": free_bgr is None,
            "keep_bgr=False grey": np.array_equal(free_gray, old_gray),
        }
        thr_old = B.otsu_on_truncating_blur(old_gray)
        thr_new = B.otsu_on_truncating_blur(free_gray)
        checks["Otsu threshold"] = thr_old == thr_new
        checks["core binary"] = np.array_equal(
            B.cpu_binary_like_core(old_gray, thr_old),
            B.cpu_binary_like_core(free_gray, thr_new))

        failed = [k for k, v in checks.items() if not v]
        if failed:
            bad.append((label, failed))
        print(f"  {label:<34} {old_gray.shape[1]}x{old_gray.shape[0]} "
              f"thr {thr_old:>3} "
              + ("ALL EXACT" if not failed else f"FAILED: {failed}"))
        del old_bgr, old_gray, new_bgr, new_gray, free_gray
    doc.close()

print(f"\n{pages} page(s), {len(bad)} with a difference")
for label, failed in bad[:10]:
    print(f"  {label}: {failed}")
print("\n" + ("RENDERER PARITY: the new render_page is byte-identical to the "
              "old one in both modes." if not bad
              else "RENDERER PARITY FAILED."))
raise SystemExit(1 if bad else 0)
