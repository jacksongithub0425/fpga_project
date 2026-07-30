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

# AXI4-Lite register offsets (must match axi_lite_regs in block design).
#
# This is a SYSTEM map, not any single core's HLS map, and it cannot become
# one: it spans binarize_core, patch_extract_core, class_score_core, the
# candidate feeder and the template streamer, and four of its registers fan
# out to more than one core.  It is specified in docs/pl_interface_contract.md
# §7.1, which also lists what the wrapper owes each core.  The wrapper does
# not exist yet — until it does, every offset below is provisional.
#
# Do not "fix" these against a generated xpatch_*_hw.h; that header describes
# one core's CTRL bundle (patch_extract_core's now starts at 0x10 for
# bin_image and runs to 0x68) and reconciling the two by hand is how the
# 14-vs-16-byte result-record mismatch in §6.3 happened.
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
# Contract §2/§7 additions for the final patch_extract_core interface
# (m_axi + explicit stride + status) — provisional, per the §7.1 note above.
_REG_STRIDE_BYTES  = 0x3C   # row stride of the binary image buffer, >= img_w
_REG_BUFFER_BYTES  = 0x40   # 32-bit (§2.1); must be >= stride * img_h
_REG_PE_FLAGS      = 0x44   # PE status: bit0 global-invalid, bit1 TLAST mism.
_REG_PE_REJECTED   = 0x48   # descriptors rejected (valid=0 records)
_REG_PE_PROCESSED  = 0x4C   # descriptors consumed from the stream

_CTRL_START        = 0x01
_CTRL_RESET        = 0x02
_STATUS_ALL_DONE   = 0x08

_KIND_UNKNOWN  = 0
_KIND_MALE     = 1
_KIND_FEMALE   = 2
_KIND_FERRULE  = 3  # tentative — postprocess_ps validates via ferrule_shape_metrics

_KIND_NAMES = {0: "unknown", 1: "male", 2: "female", 3: "ferrule"}

# Candidate descriptor in DDR3 — one 64-bit little-endian word per candidate,
# consumed verbatim by patch_extract_core's cand_in AXI4-Stream port
# (hls/patch_extract/patch_extract_core.h:6-12):
#   bits [15:0] ep_x, [31:16] ep_y, [33:32] side, [47:34] max_tw, [63:48] max_th
#
# Bit-packed rather than byte-aligned because it has to be: byte-aligning all
# five fields costs 2+2+1+1+2+2 = 10 bytes and the AXIS word is 8, so side is
# narrowed to 2 bits and max_tw to 14.
#
# The previous "<HHBBHHxx" here was wrong twice over — 12-byte stride against
# an 8-byte word (desynchronising the stream after candidate 0), and wrong
# field placement even for candidate 0, where the core decoded max_tw as 0 and
# max_th as the driver's max_tw.
_CAND_STRUCT_FMT  = "<Q"
_CAND_STRUCT_SIZE = struct.calcsize(_CAND_STRUCT_FMT)   # 8


def pack_candidate(ep_x: int, ep_y: int, side_code: int,
                   max_tw: int, max_th: int) -> bytes:
    """Build one cand_in AXIS word.

    Field widths are checked rather than masked: a value that does not fit is
    an upstream bug, and truncating it silently would corrupt the neighbouring
    field instead of failing.
    """
    if not 0 <= ep_x <= 0xFFFF:
        raise ValueError(f"ep_x {ep_x} exceeds 16 bits")
    if not 0 <= ep_y <= 0xFFFF:
        raise ValueError(f"ep_y {ep_y} exceeds 16 bits")
    if not 0 <= side_code <= 0x3:
        raise ValueError(f"side {side_code} exceeds 2 bits")
    if not 0 <= max_tw <= 0x3FFF:
        raise ValueError(f"max_tw {max_tw} exceeds 14 bits")
    if not 0 <= max_th <= 0xFFFF:
        raise ValueError(f"max_th {max_th} exceeds 16 bits")

    word = (ep_x
            | (ep_y      << 16)
            | (side_code << 32)
            | (max_tw    << 34)
            | (max_th    << 48))
    return struct.pack(_CAND_STRUCT_FMT, word)

# Result struct layout from PL.  KNOWN BROKEN (contract §6.3, class_score
# D7): this format is 14 bytes while the PL emits 16-byte records, and the
# field placement disagrees with the core even for record 0.  §6.3 is still
# OPEN (per-kind scores and the match location are also missing from the
# record), so the format is left as-is rather than half-fixed — do not
# trust results decoded through it until §6.3 closes.
_RESULT_STRUCT_FMT  = "<fBBHHHH"
_RESULT_STRUCT_SIZE = struct.calcsize(_RESULT_STRUCT_FMT)   # 14, not 16

# Patch metadata record from patch_extract_core (contract §6.2), 16 bytes,
# one per dispatched candidate, in dispatch order:
#   uint16 cand_id, uint16 status, uint16 x0, uint16 y0,
#   uint16 patch_w, uint16 patch_h, uint32 reserved
# status is unpacked whole and masked: bit 0 = valid, bits 9:1 = reason.
_META_STRUCT_FMT  = "<HHHHHHI"
_META_STRUCT_SIZE = struct.calcsize(_META_STRUCT_FMT)       # 16

_META_REASON_NAMES = {
    0: "ep_x >= img_w",
    1: "ep_y >= img_h",
    2: "max_tw outside [4, 216]",
    3: "max_th outside [4, 96]",
    4: "side not in {0, 1}",
    5: "patch_w > 820",
    6: "patch_h > 307",
    7: "patch smaller than template after clipping",
    8: "global image configuration invalid",
}


def unpack_patch_metadata(buf: bytes) -> list[dict]:
    """Decode a block of §6.2 metadata records.

    Returns one dict per record: cand_id, valid, reason (raw bitmask),
    reasons (decoded names), x0, y0, patch_w, patch_h.

    Exactly NUM_CANDS records come back, one per input descriptor in input
    order, and every one describes a descriptor the PL actually read — the
    core consumes the whole batch regardless of where TLAST lands (§5).  So
    valid=False always carries at least one reason bit, and status == 0
    exactly is unreachable.  (It formerly marked a filler ordinal emitted
    after an early TLAST; that path no longer exists.)
    """
    if len(buf) % _META_STRUCT_SIZE:
        raise ValueError(
            f"metadata block of {len(buf)} bytes is not a whole number of "
            f"{_META_STRUCT_SIZE}-byte records")
    records = []
    for off in range(0, len(buf), _META_STRUCT_SIZE):
        cand_id, status, x0, y0, pw, ph, _res = struct.unpack_from(
            _META_STRUCT_FMT, buf, off)
        reason = status >> 1
        records.append({
            "cand_id": cand_id,
            "valid":   bool(status & 1),
            "reason":  reason,
            "reasons": [_META_REASON_NAMES[b] for b in _META_REASON_NAMES
                        if reason & (1 << b)],
            "x0": x0, "y0": y0, "patch_w": pw, "patch_h": ph,
        })
    return records

# Must match SIDE_CODE in patch_extract_generate_golden.py and the side
# encoding in patch_extract_core.cpp:71.  Looked up rather than compared, so a
# misspelled side raises instead of silently becoming "right".
_SIDE_CODE = {"left": 0, "right": 1}

_MAX_CANDIDATES = 64
_MAX_TEMPL_W    = 216
_MAX_TEMPL_H    = 96


def _validate_batch_size(n: int) -> None:
    """Raise if a batch of `n` candidates will not fit the DMA buffers.

    Module level rather than inline in run_candidates() so it is reachable
    without PYNQ: run_candidates() cannot be called off the board, and a
    boundary this easy to get wrong should not be a rule that only hardware
    can check.  See test_cand_packing.test_batch_size_boundary.
    """
    if n > _MAX_CANDIDATES:
        raise ValueError(
            f"{n} candidates exceeds the driver buffer limit of "
            f"{_MAX_CANDIDATES}; split the page into batches or raise "
            f"_MAX_CANDIDATES and the _cand_buf/_result_buf allocations "
            f"with it.  This is a host-side allocation bound, not a PL "
            f"one — patch_extract_core takes num_cands as a 16-bit "
            f"register and has no per-candidate storage.")


# Post-clip patch bound (contract §3/§4.1).  This is the EXACT envelope the
# 216x96 template cap implies, not a round number — it is what lets the
# matcher's patch_buf fit the xc7z020 (224 BRAM18K vs 352 at the former
# 1024x320).  Must track PE_MAX_PATCH_W/H in patch_extract_core.h and
# MAX_PATCH_W/H in tme_top.h; see hls/template_match/ab_bram/.
_MAX_PATCH_W    = 820
_MAX_PATCH_H    = 307


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
        self._ctrl = self._ol.axi_lite_regs_0

        # AXI DMA instances
        self._dma_gray    = self._ol.dma_gray      # S2MM: gray → PL binarize
        self._dma_results = self._ol.dma_results   # MM2S: PL results → PS

        # Contiguous DMA-coherent buffers
        max_img = 2560 * 3600
        self._gray_buf    = allocate(shape=(max_img,),                     dtype=np.uint8)
        self._bin_buf     = allocate(shape=(max_img,),                     dtype=np.uint8)
        self._cand_buf    = allocate(shape=(_MAX_CANDIDATES * _CAND_STRUCT_SIZE,), dtype=np.uint8)
        self._result_buf  = allocate(shape=(_MAX_CANDIDATES * _RESULT_STRUCT_SIZE,), dtype=np.uint8)

        self._img_w: int = 0
        self._img_h: int = 0
        # Row stride of the binary image buffer (contract §2).  Compact for
        # now — the binarizer's stream-to-DDR writer that would introduce
        # padding does not exist yet — but everything downstream reads this
        # attribute rather than assuming stride == img_w, so a padded layout
        # is a one-line change here instead of a hunt.
        self._stride_bytes: int = 0

        # Apply reset on init
        self._ctrl.write(_REG_CTRL, _CTRL_RESET)
        self._ctrl.write(_REG_CTRL, 0)

    # ------------------------------------------------------------------
    def binarize_page(self, gray_np: np.ndarray, threshold: int) -> None:
        """DMA grayscale image to PL, run binarize_core, result stays in DDR3."""
        h, w = gray_np.shape
        self._img_h = h
        self._img_w = w
        self._stride_bytes = w   # compact until the §1 DDR writer lands
        n = h * w

        # Copy into DMA buffer
        self._gray_buf[:n] = gray_np.ravel()

        # Configure PL
        self._ctrl.write(_REG_IMG_W,      w)
        self._ctrl.write(_REG_IMG_H,      h)
        self._ctrl.write(_REG_THRESHOLD,  threshold)
        self._ctrl.write(_REG_GRAY_ADDR,  self._gray_buf.physical_address)
        self._ctrl.write(_REG_BIN_ADDR,   self._bin_buf.physical_address)

        # Kick DMA (S2MM: PS → PL binarize_core input)
        self._dma_gray.sendchannel.transfer(self._gray_buf[:n])
        self._dma_gray.sendchannel.wait()

        # Start binarize FSM
        self._ctrl.write(_REG_CTRL, _CTRL_START)
        self._wait_status(_STATUS_ALL_DONE)

        # binarize_core wrote _bin_buf by physical address, behind the CPU's
        # back.  Drop any cache lines the CPU still holds for it: without this,
        # suppress_text()'s read-modify-write below reads stale bytes and
        # writes them back over pixels the PL just produced.  No-op on a
        # coherent platform.
        self._bin_buf.invalidate()

    # ------------------------------------------------------------------
    def suppress_text(self, words: Sequence[dict]) -> None:
        """Zero text bboxes in the shared DDR3 binary image buffer.

        Replicates build_text_suppressed_binary() (line 229) using direct
        numpy writes into the PYNQ DMA buffer (which is mmap'd into
        userspace) — no DMA transfer needed.

        Cache ownership of _bin_buf alternates and both halves are explicit:
        binarize_page() invalidates after the PL writes it, and this method
        flushes after the CPU writes it.  The PL reads the buffer by physical
        address via _REG_BIN_ADDR, which bypasses PYNQ's DMA driver and so
        gets no cache maintenance for free.
        """
        expand = 3
        h, w = self._img_h, self._img_w
        stride = self._stride_bytes
        if w == 0:
            return

        # Strided 2D view of the binary buffer (zero-copy).  The previous
        # count=h*w reshape assumed a compact buffer; with stride > img_w it
        # silently produced a skewed view — every row shifted progressively
        # left, so suppression zeroed the wrong pixels with no error
        # (contract §2.1).  Slicing a (h, stride) view down to w columns is
        # correct for any stride >= w, compact included.
        bin_view = np.frombuffer(self._bin_buf, dtype=np.uint8,
                                 count=h * stride).reshape(h, stride)[:, :w]
        for word in words:
            x0 = max(0, int(word["x0"] - expand))
            y0 = max(0, int(word["y0"] - expand))
            x1 = min(w, int(word["x1"] + expand))
            y1 = min(h, int(word["y1"] + expand))
            bin_view[y0:y1, x0:x1] = 0

        # Push the suppressed boxes out to DDR before the PL reads the page.
        self._bin_buf.flush()

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
        if not candidates:
            return []

        # Reject, do not truncate.  This was `min(len(candidates),
        # _MAX_CANDIDATES)`, which silently dropped every candidate past the
        # 64th — a page with 65 endpoints returned 64 results and looked
        # entirely healthy, because nothing downstream compares the result
        # count against the input count.  The bound is a driver/ABI limit, not
        # a PL one: _cand_buf and _result_buf are allocated at
        # _MAX_CANDIDATES * struct size, and patch_extract_core itself has no
        # per-candidate array and takes num_cands as a 16-bit register.  So
        # raising here is the whole fix; there is no matching hardware check.
        n = len(candidates)
        _validate_batch_size(n)

        # ---- Pack candidate structs into buffer ----
        from terminal_counter_endpoint_first import MATCH_SCALES
        max_scale = max(MATCH_SCALES)

        offset = 0
        for i, cand in enumerate(candidates[:n]):
            # collect_endpoint_candidates() stores coords as "endpoint": (x, y)
            ep_xf, ep_yf = cand["endpoint"]
            ep_x = int(round(ep_xf))
            ep_y = int(round(ep_yf))

            side = cand.get("side", "left")
            if side not in _SIDE_CODE:
                raise ValueError(
                    f"candidate {i}: side {side!r} is neither 'left' nor "
                    f"'right' — a typo here used to silently become 'right', "
                    f"mirroring the patch about the endpoint"
                )
            side_code = _SIDE_CODE[side]

            # max_tw/max_th: worst case across all templates at largest
            # scale.  int(round(...)) — NOT int(...) — because the actual
            # template transmitted to the PL is resized with
            # int(round(base * scale)); truncating here underestimates the
            # real template by one pixel at half-integer products and feeds
            # every §4 bound a value one short (contract §4.5: the
            # descriptor must describe the real template, not a
            # re-derivation of it).
            max_tw = 0
            max_th = 0
            for templ_list in side_templates.get(side, {}).values():
                for t in templ_list:
                    max_tw = max(max_tw, int(round(t.shape[1] * max_scale)))
                    max_th = max(max_th, int(round(t.shape[0] * max_scale)))
            max_tw = max(max_tw, 4)
            max_th = max(max_th, 4)

            # Enforce §4.1 before dispatch: the PL will reject these with a
            # reason code, but software generating an illegal descriptor is
            # a configuration bug worth an exception, not a silent valid=0
            # record downstream.
            if not (4 <= max_tw <= _MAX_TEMPL_W):
                raise ValueError(
                    f"candidate {i}: max_tw {max_tw} outside [4, {_MAX_TEMPL_W}]"
                    f" — template bank exceeds the frozen envelope (§4.1)")
            if not (4 <= max_th <= _MAX_TEMPL_H):
                raise ValueError(
                    f"candidate {i}: max_th {max_th} outside [4, {_MAX_TEMPL_H}]"
                    f" — template bank exceeds the frozen envelope (§4.1)")
            if not (0 <= ep_x < self._img_w and 0 <= ep_y < self._img_h):
                raise ValueError(
                    f"candidate {i}: endpoint ({ep_x},{ep_y}) outside "
                    f"{self._img_w}x{self._img_h} (§4.1)")

            packed = pack_candidate(ep_x, ep_y, side_code, max_tw, max_th)
            self._cand_buf[offset:offset + _CAND_STRUCT_SIZE] = np.frombuffer(packed, dtype=np.uint8)
            offset += _CAND_STRUCT_SIZE

        # Zero the tail so a shorter batch cannot leave a previous run's
        # descriptors where the PL might fetch them, then push the writes out
        # of the CPU cache.  The PL reads this buffer by physical address via
        # _REG_CAND_ADDR below, which bypasses PYNQ's DMA driver and so gets no
        # cache maintenance for free.  (_bin_buf is fed the same way and needs
        # the same treatment — see the note in suppress_text.)
        self._cand_buf[offset:] = 0
        self._cand_buf.flush()

        # ---- Configure and start PL ----
        self._ctrl.write(_REG_CAND_ADDR,   self._cand_buf.physical_address)
        self._ctrl.write(_REG_RESULT_ADDR, self._result_buf.physical_address)
        self._ctrl.write(_REG_NUM_CANDS,   n)
        self._ctrl.write(_REG_BIN_ADDR,    self._bin_buf.physical_address)
        # Image geometry for patch_extract_core's §4 validation.  The stride
        # is explicit (§2) and buffer_bytes bounds every DDR read; a
        # misconfiguration here comes back as PE_FLAGS bit 0 plus reason
        # bit 8 in every metadata record, not as silent wrong pixels.
        self._ctrl.write(_REG_STRIDE_BYTES, self._stride_bytes)
        self._ctrl.write(_REG_BUFFER_BYTES, len(self._bin_buf))

        # Thresholds (Q8.8) — passed in as arguments, not module constants
        self._ctrl.write(_REG_SCORE_THRESH,   _float_to_q88(score_thresh))
        self._ctrl.write(_REG_FERRULE_THRESH, _float_to_q88(ferrule_score_thresh))
        self._ctrl.write(_REG_SCORE_MARGIN,   _float_to_q88(score_margin))

        self._ctrl.write(_REG_CTRL, _CTRL_START)
        self._wait_status(_STATUS_ALL_DONE)

        # ---- Check extractor status (§7) ----
        # Without this, the §4.3 rejected-batch path is indistinguishable
        # from a clean run.  Flags are fatal (configuration bugs); a nonzero
        # rejected count is surfaced but not fatal — the §4.1 pre-checks
        # above should make it unreachable, so it indicates model drift.
        pe_flags     = self._ctrl.read(_REG_PE_FLAGS)
        pe_rejected  = self._ctrl.read(_REG_PE_REJECTED)
        pe_processed = self._ctrl.read(_REG_PE_PROCESSED)
        if pe_flags & 0x1:
            raise RuntimeError(
                "patch_extract_core: global image configuration invalid "
                f"(img {self._img_w}x{self._img_h}, stride "
                f"{self._stride_bytes}, buffer {len(self._bin_buf)})")
        if pe_flags & 0x2:
            raise RuntimeError(
                f"patch_extract_core: TLAST/num_cands mismatch — "
                f"processed {pe_processed} of {n} descriptors")
        if pe_rejected:
            print(f"[tme_driver] WARNING: PL rejected {pe_rejected}/{n} "
                  f"candidates that passed the PS-side §4.1 checks — "
                  f"validation models have drifted")

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
                "endpoint":     cand["endpoint"],
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
