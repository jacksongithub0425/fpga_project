"""PYNQ helpers for verifying the A3.3 binarize_core DMA path.

Upload this file next to the notebook, bitstream, HWH, and optional
``tme_driver.py`` on the PYNQ board, then import the functions you need.
Nothing runs at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np


SENTINEL = np.uint8(0xAA)


@dataclass
class CompareResult:
    name: str
    matched: int
    total: int
    mismatches: int
    percent: float

    def __str__(self) -> str:
        return (
            f"[{self.name}] {self.matched}/{self.total} "
            f"({self.percent:.2f}%) match"
        )


def load_direct_overlay(bitfile: str = "terminal_counter.bit") -> Tuple[Any, Any, Any]:
    """Load the A3.3 direct overlay and return ``overlay, core, dma``."""
    from pynq import Overlay  # type: ignore

    overlay = Overlay(bitfile)
    return overlay, overlay.binarize_core_0, overlay.axi_dma_0


def cpu_golden(gray: np.ndarray, threshold: int) -> np.ndarray:
    """CPU reference for the binarize HLS core."""
    import cv2

    gray_u8 = np.ascontiguousarray(gray, dtype=np.uint8)
    blur = cv2.GaussianBlur(gray_u8, (3, 3), 0)
    return cv2.threshold(blur, int(threshold), 255, cv2.THRESH_BINARY_INV)[1]


def otsu_threshold_downsampled(gray: np.ndarray, downsample: int = 4) -> int:
    """Match the project strategy: compute Otsu threshold on a downsampled page."""
    import cv2

    h, w = gray.shape
    small = cv2.resize(gray, (max(1, w // downsample), max(1, h // downsample)))
    threshold, _ = cv2.threshold(small, 0, 255, cv2.THRESH_OTSU)
    return int(threshold)


def align_pl_output(raw: np.ndarray) -> np.ndarray:
    """Shift raw PL binarize output up-left by one pixel to align with cv2."""
    aligned = np.roll(raw, shift=(-1, -1), axis=(0, 1)).copy()
    aligned[-1, :] = 0
    aligned[:, -1] = 0
    return aligned


def compare_region(name: str, observed: np.ndarray, expected: np.ndarray) -> CompareResult:
    """Compare two same-shaped arrays and return a compact result."""
    if observed.shape != expected.shape:
        raise ValueError(f"shape mismatch: observed={observed.shape}, expected={expected.shape}")
    mismatches = int(np.count_nonzero(observed != expected))
    total = int(observed.size)
    matched = total - mismatches
    percent = 100.0 * matched / total if total else 100.0
    return CompareResult(name, matched, total, mismatches, percent)


def run_direct_binarize(core: Any, dma: Any, gray: np.ndarray, threshold: int) -> Tuple[np.ndarray, int]:
    """Run one direct ``AXI DMA -> binarize_core -> AXI DMA`` transfer.

    Returns:
        ``raw_pl_output, remaining_sentinel_count``
    """
    from pynq import allocate  # type: ignore

    gray_u8 = np.ascontiguousarray(gray, dtype=np.uint8)
    if gray_u8.ndim != 2:
        raise ValueError("gray must be a 2-D uint8 image")

    h, w = gray_u8.shape
    n = h * w

    in_buf = allocate(shape=(n,), dtype=np.uint8)
    out_buf = allocate(shape=(n,), dtype=np.uint8)

    try:
        in_buf[:] = gray_u8.ravel()
        out_buf[:] = SENTINEL

        in_buf.flush()
        out_buf.flush()

        core.register_map.img_w = int(w)
        core.register_map.img_h = int(h)
        core.register_map.threshold = int(threshold)

        dma.recvchannel.transfer(out_buf)
        core.write(0x00, 0x01)
        dma.sendchannel.transfer(in_buf)

        dma.sendchannel.wait()
        dma.recvchannel.wait()

        out_buf.invalidate()
        raw = np.asarray(out_buf).reshape(h, w).copy()
        remaining_sentinel = int(np.count_nonzero(raw == SENTINEL))
        return raw, remaining_sentinel
    finally:
        try:
            in_buf.freebuffer()
            out_buf.freebuffer()
        except Exception:
            pass


def make_synthetic_image(h: int, w: int) -> np.ndarray:
    """Create a deterministic binary-stressing test image for any size."""
    yy, xx = np.indices((h, w))
    img = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    img[: h // 2, : w // 2] = 40
    img[: h // 2, w // 2 :] = 220
    if h > 4 and w > 4:
        img[(3 * h) // 4, (3 * w) // 4] = 255
    return img


def run_synthetic_check(
    core: Any,
    dma: Any,
    h: int,
    w: int,
    threshold: int = 128,
    verbose: bool = True,
) -> dict:
    """Run a synthetic image through raw and aligned PL comparisons."""
    img = make_synthetic_image(h, w)
    gold = cpu_golden(img, threshold)
    raw, sentinel_count = run_direct_binarize(core, dma, img, threshold)
    aligned = align_pl_output(raw)

    raw_shift = compare_region(
        "RAW_SHIFT",
        raw[2:, 2:],
        gold[1:-1, 1:-1],
    )
    aligned_match = compare_region(
        "ALIGNED",
        aligned[1:-1, 1:-1],
        gold[1:-1, 1:-1],
    )

    result = {
        "shape": raw.shape,
        "threshold": int(threshold),
        "remaining_0xAA": sentinel_count,
        "unique": np.unique(raw, return_counts=True),
        "raw_shift": raw_shift,
        "aligned": aligned_match,
        "raw": raw,
        "aligned_image": aligned,
        "gold": gold,
    }

    if verbose:
        print("pl shape:", result["shape"])
        print("remaining 0xAA:", sentinel_count)
        print("pl unique:", result["unique"])
        print(raw_shift)
        print(aligned_match)

    return result


def run_smoke_suite(bitfile: str = "terminal_counter.bit") -> list[dict]:
    """Run the standard A3.3 synthetic checks on the current overlay."""
    _, core, dma = load_direct_overlay(bitfile)
    return [
        run_synthetic_check(core, dma, 64, 64),
        run_synthetic_check(core, dma, 37, 31),
    ]


def render_pdf_page(pdf_path: str, zoom: float = 1.0, page_index: int = 0) -> np.ndarray:
    """Render a PDF page to grayscale using PyMuPDF and OpenCV."""
    import cv2

    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "PyMuPDF is not installed on this PYNQ image, so PDF rendering is unavailable. "
            "Upload a rendered PNG/JPG and call run_real_page_check() on that image, "
            "or install PyMuPDF separately."
        ) from exc

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    finally:
        doc.close()

    if img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif img.shape[2] == 3:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def load_grayscale_input(path: str, zoom: float = 1.0, page_index: int = 0) -> np.ndarray:
    """Load either a PDF page or an already-rendered image as grayscale."""
    import cv2

    input_path = Path(path)
    if input_path.suffix.lower() == ".pdf":
        return render_pdf_page(str(input_path), zoom=zoom, page_index=page_index)

    gray = cv2.imread(str(input_path), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise FileNotFoundError(f"Cannot read image/PDF input: {path}")
    if zoom != 1.0:
        h, w = gray.shape
        gray = cv2.resize(
            gray,
            (max(1, int(round(w * zoom))), max(1, int(round(h * zoom)))),
            interpolation=cv2.INTER_AREA if zoom < 1.0 else cv2.INTER_CUBIC,
        )
    return gray


def run_real_page_check(
    pdf_path: str,
    bitfile: str = "terminal_counter.bit",
    zoom: float = 1.0,
    page_index: int = 0,
    use_driver: bool = True,
    threshold: Optional[int] = None,
) -> dict:
    """Run one real PDF/image page through PL binarize and compare to CPU golden.

    If ``use_driver`` is true, this tests ``tme_driver.PLPipeline`` and expects
    ``tme_driver.py`` to be uploaded next to this module on PYNQ.
    """
    gray = load_grayscale_input(pdf_path, zoom=zoom, page_index=page_index)
    threshold = otsu_threshold_downsampled(gray) if threshold is None else int(threshold)
    gold = cpu_golden(gray, threshold)

    if use_driver:
        from tme_driver import PLPipeline  # type: ignore

        pipeline = PLPipeline(bitfile)
        pipeline.binarize_page(gray, threshold)
        observed = pipeline.binary_image()
    else:
        _, core, dma = load_direct_overlay(bitfile)
        raw, _ = run_direct_binarize(core, dma, gray, threshold)
        observed = align_pl_output(raw)

    match = compare_region(
        "REAL_PAGE_ALIGNED",
        observed[1:-1, 1:-1],
        gold[1:-1, 1:-1],
    )

    print("gray shape:", gray.shape)
    print("threshold:", threshold)
    print("pl unique:", np.unique(observed, return_counts=True))
    print(match)

    return {
        "gray": gray,
        "threshold": threshold,
        "gold": gold,
        "observed": observed,
        "match": match,
    }
