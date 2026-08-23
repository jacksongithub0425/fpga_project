"""Striped RGB rendering broke byte-equality.  Which variant does not?

The plain `clip=` stripe is exact in GEOMETRY (every band's irect came back
exactly as asked) but not in PIXELS: the differing rows cluster in the last
~15 rows of each band, which is MuPDF antialiasing the content against the
clip edge.  Content that straddles a band boundary is clipped, and the partial
coverage lands in those rows.

Four candidates, each measured against the current whole-page path:

  A  clip= with an OVERLAP margin, keeping only the interior rows.  If the
     contamination is the clip edge, moving the edge away from the rows we
     keep should remove it.
  B  a Pixmap allocated on the band's irect, with the page run into it and NO
     clip.  MuPDF then rasterises with full context and simply does not write
     outside the pixmap's rect -- a pixel scissor rather than a clip path.
  C  stripe the CONVERSION, not the render: one full-page pixmap read through
     `samples_mv`, RGB -> BGR -> GRAY a band at a time into one gray page.
     Exact by construction (both cvtColors are per-pixel, no neighbourhood),
     and it removes the 186 MB `samples` copy and the 186 MB BGR.
  D  `COLOR_RGB2GRAY` in one call instead of RGB2BGR then BGR2GRAY -- worth
     knowing whether the two-step dance is load-bearing.
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
    if n == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif n == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def reference(page, zoom=ZOOM):
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    return rgb_to_gray(img, pix.n).copy()


def variant_a(page, rows, margin, zoom=ZOOM):
    """clip= stripes with an overlap margin; keep the interior."""
    mat = fitz.Matrix(zoom, zoom)
    inv = ~mat
    full = (page.rect * mat).irect
    H, W = full.height, full.width
    gray = np.empty((H, W), dtype=np.uint8)
    for y0 in range(0, H, rows):
        y1 = min(y0 + rows, H)
        ry0 = max(0, y0 - margin)
        ry1 = min(H, y1 + margin)
        band = fitz.IRect(full.x0, full.y0 + ry0, full.x1, full.y0 + ry1)
        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(band) * inv,
                              alpha=False)
        if pix.irect != band:
            raise AssertionError(f"{band} -> {pix.irect}")
        img = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        gray[y0:y1, :] = rgb_to_gray(img, pix.n)[y0 - ry0:y0 - ry0 + (y1 - y0)]
        del img, pix
    return gray


def variant_b(page, rows, zoom=ZOOM):
    """A Pixmap on the band's irect, page run into it, no clip path."""
    mat = fitz.Matrix(zoom, zoom)
    full = (page.rect * mat).irect
    H, W = full.height, full.width
    gray = np.empty((H, W), dtype=np.uint8)
    for y0 in range(0, H, rows):
        y1 = min(y0 + rows, H)
        band = fitz.IRect(full.x0, full.y0 + y0, full.x1, full.y0 + y1)
        pix = fitz.Pixmap(fitz.csRGB, band, False)
        pix.clear_with(255)
        dev = fitz.Device(pix, fitz.Identity)
        page.run(dev, mat)
        dev.close()
        img = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        gray[y0:y1, :] = rgb_to_gray(img, pix.n)
        del img, pix, dev
    return gray


def variant_c(page, rows, zoom=ZOOM):
    """One full-page pixmap; stripe only the colour conversion."""
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    H, W, n = pix.height, pix.width, pix.n
    src = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(H, W, n)
    gray = np.empty((H, W), dtype=np.uint8)
    for y0 in range(0, H, rows):
        y1 = min(y0 + rows, H)
        gray[y0:y1, :] = rgb_to_gray(src[y0:y1], n)
    del src, pix
    return gray


def variant_d(page, zoom=ZOOM):
    """One-step COLOR_RGB2GRAY instead of RGB2BGR then BGR2GRAY."""
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).copy()
    return rgb_to_gray(img, pix.n).copy()


def report(label, got, ref):
    if got.shape != ref.shape:
        print(f"      {label:<28} SHAPE {got.shape} vs {ref.shape}")
        return False
    d = np.abs(got.astype(np.int16) - ref.astype(np.int16))
    nd = int(np.count_nonzero(d))
    if nd == 0:
        print(f"      {label:<28} EXACT")
        return True
    print(f"      {label:<28} {nd:,} px differ, max {int(d.max())}")
    return False


root = Path(r"C:\Users\lychee\Desktop\FPGA\sample")
# Selected by LABEL, not by drawing-number fragment.  Naming them by
# fragment left three six-digit pieces of confidential filenames in a
# tracked file, somewhere the structural gate could not look: a bare
# fragment is not shaped like a drawing number, and widening the shape
# to catch it would match any `NN-wwwwww-NN` in ordinary prose.
# doc_002 is the Stage 2 page, doc_003 the Stage 3 page, doc_035 the
# small page -- which is why these three and not any three.
sample = [p for p in (CL.resolve(lab, root)
                      for lab in ("doc_002", "doc_003", "doc_035"))
          if p is not None]

ok = {}
for pdf in sample:
    doc = fitz.open(pdf)
    page = doc[0]
    ref = reference(page)
    print(f"  {pdf.name:<34} {ref.shape[1]}x{ref.shape[0]}")
    for margin in (16, 64, 256):
        try:
            got = variant_a(page, 1024, margin)
        except AssertionError as exc:
            print(f"      A margin={margin:<20} TILING {exc}")
            ok.setdefault(f"A/{margin}", []).append(False)
            continue
        ok.setdefault(f"A margin={margin}", []).append(
            report(f"A margin={margin}", got, ref))
        del got
    try:
        got = variant_b(page, 1024)
        ok.setdefault("B pixmap-scissor", []).append(
            report("B pixmap-scissor", got, ref))
        del got
    except Exception as exc:                                 # noqa: BLE001
        print(f"      {'B pixmap-scissor':<28} {type(exc).__name__}: {exc}")
        ok.setdefault("B pixmap-scissor", []).append(False)
    for rows in (256, 1024):
        got = variant_c(page, rows)
        ok.setdefault(f"C conv-stripe {rows}", []).append(
            report(f"C conv-stripe rows={rows}", got, ref))
        del got
    got = variant_d(page)
    ok.setdefault("D one-step RGB2GRAY", []).append(
        report("D one-step RGB2GRAY", got, ref))
    del got, ref
    doc.close()

print("\nsummary over the sampled pages:")
for k, v in ok.items():
    print(f"  {k:<28} {'EXACT everywhere' if all(v) else 'DIFFERS'}")
