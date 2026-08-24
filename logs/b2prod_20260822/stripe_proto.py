"""Prototype the striped RGB renderer and prove it tiles the page exactly.

Two questions, and the second is the one that decides it:

  1. Does `get_pixmap(matrix=mat, clip=...)` return exactly the device band
     asked for, or does the clip round and shift?
  2. Is stripe-by-stripe RGB -> BGR -> GRAY byte-identical to converting the
     whole page at once?  Rasterisation is deterministic per pixel, but a
     stroke straddling a band boundary is exactly where that could stop being
     true -- which is why this is measured rather than assumed.

Run on a few pages first; the corpus run follows only if this holds.
"""
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))

import cv2
import fitz
import numpy as np

import corpus_labels as CL

ZOOM = 4.0


def rgb_to_gray(img, n):
    """The current arithmetic, unchanged: RGB -> BGR -> GRAY."""
    if n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif n == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def whole_page_gray(page, zoom=ZOOM):
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    return rgb_to_gray(img, pix.n), pix.n


def striped_gray(page, zoom=ZOOM, rows=512, verbose=False):
    mat = fitz.Matrix(zoom, zoom)
    inv = ~mat
    full = (page.rect * mat).irect
    H, W = full.height, full.width
    gray = np.empty((H, W), dtype=np.uint8)
    got_bands = []
    for y0 in range(0, H, rows):
        y1 = min(y0 + rows, H)
        band = fitz.IRect(full.x0, full.y0 + y0, full.x1, full.y0 + y1)
        clip = fitz.Rect(band) * inv
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        got_bands.append((pix.irect, band, pix.height, pix.width))
        if pix.irect != band:
            raise AssertionError(f"band {band} came back as {pix.irect}")
        img = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        gray[y0:y1, :] = rgb_to_gray(img, pix.n)
        del img, pix
    if verbose:
        print(f"      {len(got_bands)} band(s), all irects exact")
    return gray


root = Path(r"C:\Users\lychee\Desktop\FPGA\sample")
# CL.documents() IS the ordering this line always used -- sorted by
# lower-cased name -- taken from one place now so the iteration order
# and the label numbering cannot drift apart.
pdfs = CL.documents(root)
labels = CL.labels(root)
sample = pdfs[:3] + pdfs[-1:]

fails = 0
for pdf in sample:
    doc = fitz.open(pdf)
    page = doc[0]
    ref, n = whole_page_gray(page)
    print(f"  {labels.doc(pdf.name):<34} {ref.shape[1]}x{ref.shape[0]} n={n}")
    for rows in (256, 512, 1024, 6336):
        try:
            got = striped_gray(page, rows=rows, verbose=(rows == 512))
        except AssertionError as exc:
            print(f"      rows={rows:<5} TILING FAILED: {exc}")
            fails += 1
            continue
        if got.shape != ref.shape:
            print(f"      rows={rows:<5} SHAPE {got.shape} vs {ref.shape}")
            fails += 1
            continue
        d = np.abs(got.astype(np.int16) - ref.astype(np.int16))
        nd = int(np.count_nonzero(d))
        print(f"      rows={rows:<5} {'EXACT' if nd == 0 else f'{nd:,} px differ, max {int(d.max())}'}")
        if nd:
            fails += 1
            ys = np.unique(np.nonzero(d)[0])
            print(f"            differing rows: {ys[:12]} "
                  f"(band boundaries at multiples of {rows})")
        del got, d
    del ref
    doc.close()

print(f"\n{'STRIPED RENDER IS EXACT' if not fails else f'{fails} FAILURE(S)'}")
raise SystemExit(1 if fails else 0)
