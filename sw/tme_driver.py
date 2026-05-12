"""
PLPipeline — PYNQ driver for the terminal matching PL accelerator.

Wraps the Vivado block design (terminal_counter.bit) and provides
three methods called by detect_page() in terminal_counter_endpoint_first.py:

    pl.binarize_page(gray_np, otsu_threshold)
    pl.suppress_text(words)
    pl.run_candidates(candidates, side_templates) -> list[dict]

Falls back silently to None if PYNQ is not available (CPU path used instead).
"""

from __future__ import annotations
from typing import Optional, Sequence

import numpy as np
import struct

# AXI4-Lite register offsets (must match axi_lite_regs in block design)
_REG_CTRL          = 0x00
_REG_STATUS        = 0x04
_REG_GRAY_ADDR     = 0x08
_REG_BIN_ADDR      = 0x0C
_REG_IMG_W         = 0x10
_REG_IMG_H         = 0x14
_REG_THRESHOLD     = 0x18
_REG_CAND_ADDR     = 0x20
_REG_RESULT_ADDR   = 0x24
_REG_NUM_CANDS     = 0x28
_REG_SCORE_THRESH  = 0x2C
_REG_FERRULE_THRESH= 0x30
_REG_SCORE_MARGIN  = 0x34
_REG_TEMPL_ADDR    = 0x38

_CTRL_START        = 0x01
_CTRL_RESET        = 0x02
_STATUS_ALL_DONE   = 0x08

_KIND_UNKNOWN  = 0
_KIND_MALE     = 1
_KIND_FEMALE   = 2
_KIND_FERRULE  = 3  # tentative — postprocess_ps validates via ferrule_shape_metrics

_KIND_NAMES = {0: "unknown", 1: "male", 2: "female", 3: "ferrule"}

# Candidate struct layout in DDR3 (little-endian, 8 bytes each):
#   uint16 ep_x, uint16 ep_y, uint8 side, uint8 pad,
#   uint16 max_tw, uint16 max_th
_CAND_STRUCT_FMT  = "<HHBBHHxx"   # 12 bytes with padding
_CAND_STRUCT_SIZE = struct.calcsize(_CAND_STRUCT_FMT)

# Result struct layout from PL (16 bytes each):
#   float32 score, uint8 kind, uint8 cand_id, uint16 box_x,
#   uint16 box_y, uint16 box_w, uint16 box_h
_RESULT_STRUCT_FMT  = "<fBBHHHH"
_RESULT_STRUCT_SIZE = struct.calcsize(_RESULT_STRUCT_FMT)

_MAX_CANDIDATES = 64
_MAX_TEMPL_W    = 216
_MAX_TEMPL_H    = 96
_MAX_PATCH_W    = 1024
_MAX_PATCH_H    = 320


def _float_to_q88(f: float) -> int:
    """Convert float to Q8.8 fixed-point integer."""
    return max(0, min(0xFFFF, int(round(f * 256.0))))


class PLPipeline:
    """PYNQ interface to the terminal-counter PL accelerator."""

    def __init__(self, bitfile: str = "/home/xilinx/terminal_counter.bit"):
        from pynq import Overlay, allocate  # type: ignore

        self._ol        = Overlay(bitfile)
        self._allocate  = allocate

        # AXI4-Lite control block (auto-detected from .hwh)
        self._binarize_core = getattr(self._ol, "binarize_core_0", None)
        self._binarize_dma = getattr(self._ol, "axi_dma_0", None)
        self._direct_binarize = self._binarize_core is not None and self._binarize_dma is not None

        self._ctrl = getattr(self._ol, "axi_lite_regs_0", None)

        # AXI DMA instances
        self._dma_gray = getattr(self._ol, "dma_gray", None)
        self._dma_results = getattr(self._ol, "dma_results", None)

        # Contiguous DMA-coherent buffers
        max_img = 2560 * 3600
        self._gray_buf    = allocate(shape=(max_img,),                     dtype=np.uint8)
        self._bin_buf     = allocate(shape=(max_img,),                     dtype=np.uint8)
        self._cand_buf    = allocate(shape=(_MAX_CANDIDATES * _CAND_STRUCT_SIZE,), dtype=np.uint8)
        self._result_buf  = allocate(shape=(_MAX_CANDIDATES * _RESULT_STRUCT_SIZE,), dtype=np.uint8)

        self._img_w: int = 0
        self._img_h: int = 0

        if self._ctrl is None and not self._direct_binarize:
            raise RuntimeError(
                "Overlay does not expose either binarize_core_0/axi_dma_0 "
                "or axi_lite_regs_0."
            )

        # Apply reset on init for the full-pipeline register block.
        if self._ctrl is not None:
            self._ctrl.write(_REG_CTRL, _CTRL_RESET)
            self._ctrl.write(_REG_CTRL, 0)

    # ------------------------------------------------------------------
    def binarize_page(self, gray_np: np.ndarray, threshold: int) -> None:
        """DMA grayscale image to PL, run binarize_core, result stays in DDR3."""
        h, w = gray_np.shape
        self._img_h = h
        self._img_w = w
        n = h * w
        if n > self._gray_buf.size:
            raise ValueError(
                f"Image {w}x{h} ({n} pixels) exceeds PL buffer capacity "
                f"({self._gray_buf.size} pixels)"
            )

        self._gray_buf[:n] = gray_np.ravel()
        try:
            self._gray_buf.flush()
        except AttributeError:
            pass

        if self._direct_binarize:
            self._run_direct_binarize_dma(w, h, threshold, n)
        else:
            self._run_pipeline_binarize(w, h, threshold, n)

        try:
            self._bin_buf.invalidate()
        except AttributeError:
            pass

        self._align_binarize_buffer(h, w, n)

    def _run_direct_binarize_dma(self, w: int, h: int, threshold: int, n: int) -> None:
        """Run the A3.3 direct HLS-core + AXI-DMA overlay."""
        core = self._binarize_core
        dma = self._binarize_dma
        core.register_map.img_w = w
        core.register_map.img_h = h
        core.register_map.threshold = threshold

        dma.recvchannel.transfer(self._bin_buf[:n])
        core.write(0x00, 0x01)
        dma.sendchannel.transfer(self._gray_buf[:n])

        dma.sendchannel.wait()
        dma.recvchannel.wait()

    def _run_pipeline_binarize(self, w: int, h: int, threshold: int, n: int) -> None:
        """Run the full-pipeline register-block overlay."""
        if self._ctrl is None or self._dma_gray is None:
            raise RuntimeError("Full-pipeline binarize control/DMA IP is not available.")

        self._ctrl.write(_REG_IMG_W,      w)
        self._ctrl.write(_REG_IMG_H,      h)
        self._ctrl.write(_REG_THRESHOLD,  threshold)
        self._ctrl.write(_REG_GRAY_ADDR,  self._gray_buf.physical_address)
        self._ctrl.write(_REG_BIN_ADDR,   self._bin_buf.physical_address)

        self._dma_gray.sendchannel.transfer(self._gray_buf[:n])
        self._dma_gray.sendchannel.wait()

        self._ctrl.write(_REG_CTRL, _CTRL_START)
        self._wait_status(_STATUS_ALL_DONE)

    def _align_binarize_buffer(self, h: int, w: int, n: int) -> None:
        """Align raw PL binarize output to cv2 image coordinates in place."""
        raw = np.frombuffer(self._bin_buf, dtype=np.uint8, count=n).reshape(h, w)
        aligned = np.roll(raw, shift=(-1, -1), axis=(0, 1))
        aligned[-1, :] = 0   # bottom border (was wrapped from top row)
        aligned[:,  -1] = 0  # right border (was wrapped from left col)
        raw[:] = aligned     # write back to the shared DMA buffer in place
        try:
            self._bin_buf.flush()
        except AttributeError:
            pass

    def binary_image(self, copy: bool = True) -> np.ndarray:
        """Return the latest cv2-aligned binary image produced by binarize_page."""
        h, w = self._img_h, self._img_w
        if w == 0 or h == 0:
            raise RuntimeError("No binary image is available; call binarize_page first.")
        view = np.frombuffer(self._bin_buf, dtype=np.uint8, count=h * w).reshape(h, w)
        return view.copy() if copy else view

    # ------------------------------------------------------------------
    def suppress_text(self, words: Sequence[dict]) -> None:
        """Zero text bboxes in the shared DDR3 binary image buffer.

        Replicates build_text_suppressed_binary() (line 229) using direct
        numpy writes into the PYNQ DMA buffer (which is mmap'd into
        userspace) — no DMA transfer needed.
        """
        expand = 3
        h, w = self._img_h, self._img_w
        if w == 0:
            return

        # View the binary buffer as a 2D array (zero-copy)
        bin_view = np.frombuffer(self._bin_buf, dtype=np.uint8,
                                 count=h * w).reshape(h, w)
        for word in words:
            x0 = max(0, int(word["x0"] - expand))
            y0 = max(0, int(word["y0"] - expand))
            x1 = min(w, int(word["x1"] + expand))
            y1 = min(h, int(word["y1"] + expand))
            bin_view[y0:y1, x0:x1] = 0
        try:
            self._bin_buf.flush()
        except AttributeError:
            pass

    # ------------------------------------------------------------------
    def run_candidates(
        self,
        candidates: Sequence[dict],
        side_templates: dict,
        score_thresh: float = 0.33,
        ferrule_score_thresh: float = 0.30,
        score_margin: float = 0.05,
    ) -> list[dict]:
        """Dispatch all candidates to the PL pipeline and return results.

        `candidates` is the list from collect_endpoint_candidates().
        `side_templates` is the build_side_templates() dict.

        Returns a list of result dicts with keys:
            kind, score, endpoint, side, box, male_score, female_score,
            ferrule_score, id (id is set by postprocess_ps)
        """
        if self._ctrl is None or self._dma_results is None:
            raise RuntimeError("Candidate/result PL pipeline IP is not available in this overlay.")

        if not candidates:
            return []

        n = min(len(candidates), _MAX_CANDIDATES)

        # ---- Pack candidate structs into buffer ----
        from terminal_counter_endpoint_first import MATCH_SCALES
        max_scale = max(MATCH_SCALES)

        offset = 0
        for i, cand in enumerate(candidates[:n]):
            ep_x = int(round(cand["x"]))
            ep_y = int(round(cand["y"]))
            side_code = 0 if cand.get("side", "left") == "left" else 1

            # max_tw/max_th: worst case across all templates at largest scale
            max_tw = 0
            max_th = 0
            for templ_list in side_templates.get(cand.get("side", "left"), {}).values():
                for t in templ_list:
                    max_tw = max(max_tw, int(t.shape[1] * max_scale))
                    max_th = max(max_th, int(t.shape[0] * max_scale))
            max_tw = max(max_tw, 4)
            max_th = max(max_th, 4)

            packed = struct.pack(_CAND_STRUCT_FMT,
                                 ep_x, ep_y, side_code, 0, max_tw, max_th)
            self._cand_buf[offset:offset + _CAND_STRUCT_SIZE] = np.frombuffer(packed, dtype=np.uint8)
            offset += _CAND_STRUCT_SIZE

        # ---- Configure and start PL ----
        self._ctrl.write(_REG_CAND_ADDR,   self._cand_buf.physical_address)
        self._ctrl.write(_REG_RESULT_ADDR, self._result_buf.physical_address)
        self._ctrl.write(_REG_NUM_CANDS,   n)
        self._ctrl.write(_REG_BIN_ADDR,    self._bin_buf.physical_address)

        # Thresholds (Q8.8) — passed in as arguments, not module constants
        self._ctrl.write(_REG_SCORE_THRESH,   _float_to_q88(score_thresh))
        self._ctrl.write(_REG_FERRULE_THRESH, _float_to_q88(ferrule_score_thresh))
        self._ctrl.write(_REG_SCORE_MARGIN,   _float_to_q88(score_margin))

        self._ctrl.write(_REG_CTRL, _CTRL_START)
        self._wait_status(_STATUS_ALL_DONE)

        # ---- Read results ----
        result_bytes = n * _RESULT_STRUCT_SIZE
        self._dma_results.recvchannel.transfer(self._result_buf[:result_bytes])
        self._dma_results.recvchannel.wait()

        results = []
        for i in range(n):
            off = i * _RESULT_STRUCT_SIZE
            chunk = bytes(self._result_buf[off:off + _RESULT_STRUCT_SIZE])
            score, kind_byte, cand_id, bx, by, bw, bh = struct.unpack(_RESULT_STRUCT_FMT, chunk)
            cand = candidates[cand_id] if cand_id < len(candidates) else candidates[i]
            results.append({
                "kind":         _KIND_NAMES.get(kind_byte, "unknown"),
                "score":        float(score),
                "endpoint":     (cand.get("x", 0.0), cand.get("y", 0.0)),
                "side":         cand.get("side", "left"),
                "box":          (int(bx), int(by), int(bw), int(bh)),
                "male_score":   -1.0,   # individual scores not returned by PL in this mode
                "female_score": -1.0,
                "ferrule_score":-1.0,
            })
        return results

    # ------------------------------------------------------------------
    def _wait_status(self, mask: int, timeout_ms: int = 5000) -> None:
        """Poll STATUS register until all bits in mask are set."""
        import time
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            s = self._ctrl.read(_REG_STATUS)
            if (s & mask) == mask:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"PL pipeline timeout waiting for STATUS & 0x{mask:02X}")
            time.sleep(0.0001)

    def close(self) -> None:
        """Release DMA buffers."""
        for buf in (self._gray_buf, self._bin_buf,
                    self._cand_buf, self._result_buf):
            try:
                buf.freebuffer()
            except Exception:
                pass


# -----------------------------------------------------------------------
# Module-level singleton — imported by terminal_counter_endpoint_first.py
# -----------------------------------------------------------------------
_pipeline: Optional[PLPipeline] = None

def get_pipeline(bitfile: str = "/home/xilinx/terminal_counter.bit") -> Optional[PLPipeline]:
    """Return the singleton PLPipeline, or None if PYNQ is unavailable."""
    global _pipeline
    if _pipeline is None:
        try:
            _pipeline = PLPipeline(bitfile)
            print(f"[tme_driver] PL accelerator loaded from {bitfile}")
        except Exception as e:
            print(f"[tme_driver] PYNQ not available ({e}), using CPU fallback")
            _pipeline = None
    return _pipeline
