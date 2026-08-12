"""
PLPipeline — PYNQ driver for the three_stage_combined overlay.

Drives the three-core PL image (contract §7.1: one AXI4-Lite window per core,
sequenced from software — there is no register-mirroring wrapper, and the
unified 0x00–0x4C map this file used to carry never existed in hardware; it
is preserved as a deferred option in §7.1.3 only).

Overlay contents this driver is written against (three_stage_combined.hwh,
2026-08-11):

    binarize_core_0        CTRL: img_w, img_h, threshold
    patch_extract_core_0   CTRL: bin_image_1/2, img_w, img_h, stride_bytes,
                           buffer_bytes, num_cands, sts_* (+ COR ap_vld)
    tme_top_0              CTRL: patch_w/h, templ_w/h, result_score/x/y
                           (+ COR ap_vld companions)
    axi_dma_binarize       MM2S gray in (8b) + S2MM binary out (8b), 26-bit len
    dma_pe_data            MM2S candidates (64b) + S2MM patch pixels (8b)
    dma_pe_meta            S2MM §6.2 metadata records (128b)
    axi_dma_patch          MM2S patch pixels → tme_top (8b)
    axi_dma_templ          MM2S template pixels → tme_top (8b)

Register access goes through PYNQ's `register_map` (generated from the .hwh),
never hand-transcribed offsets — §7.1.2: adding or reordering a port moves
every offset after it.  The only raw offset used is 0x00 (ap_ctrl), which the
ap_ctrl_hs protocol fixes for every HLS core.

Stage methods, validated separately and in this order on the board:

    pl.binarize_page(gray_np, threshold)      -> binary page view (DDR-backed)
    pl.suppress_text(words)                   -> in-place on the binary page
    pl.extract_candidates(cands, tmpls, s)    -> §6.2 records + patch arrays
    pl.match_template(patch, templ)           -> one trial: (score, x, y, s)
    pl.match_candidate(patch, x0, y0, trials) -> strict-> argmax + boxes

Classification is PS-side by decision (2026-08-11, contract §10 items 4–5):
`class_score_core` is out of the MVP, there is no result record and no result
DMA.  `match_candidate` holds the running per-kind argmax with STRICTLY
GREATER comparison over the caller's frozen trial order — first trial to
reach the maximum wins ties, same as the CPU baseline — and constructs boxes
as (patch_x0 + match_x, patch_y0 + match_y, templ_w, templ_h) in absolute
logical-page coordinates (§1).

NO SILENT FALLBACK.  Constructing PLPipeline raises if PYNQ or the overlay is
missing or malformed.  Backend selection (cpu / pl-binarize / pl-extract /
pl-all) belongs to the detector's command line, stated explicitly per run;
a PL failure during validation must fail the run, not quietly hand the work
to the CPU (the old get_pipeline() that returned None on any exception is
gone on purpose).
"""

from __future__ import annotations
from typing import Optional, Sequence

import struct
import time

import numpy as np

# Pure validation helpers, proven on silicon by the standalone bring-up.
# Importable without PYNQ (their module only imports pynq lazily).
from tme_standalone_bringup import (DMA_MAX_BYTES_DEFAULT, MAX_PATCH_H,
                                    MAX_PATCH_W, MAX_TEMPL_H, MAX_TEMPL_W,
                                    validate_geometry,
                                    validate_template_content)

# ap_ctrl_hs bits at CTRL offset 0x00 — fixed by the protocol, not by any
# per-core map, so a raw offset is safe here and only here.
_AP_CTRL_OFF = 0x00
_AP_START = 1 << 0
_AP_DONE = 1 << 1
_AP_IDLE = 1 << 2

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

# Envelope constants re-exported under the names this module has always used;
# the values live in tme_standalone_bringup.py and track the HLS headers.
_MAX_TEMPL_W = MAX_TEMPL_W          # 216
_MAX_TEMPL_H = MAX_TEMPL_H          # 96
_MAX_PATCH_W = MAX_PATCH_W          # 820
_MAX_PATCH_H = MAX_PATCH_H          # 307
_MAX_PATCH_BYTES = _MAX_PATCH_W * _MAX_PATCH_H    # 251,740 (§3.1: fits 18-bit DMA)


def _validate_batch_size(n: int) -> None:
    """Raise if a batch of `n` candidates will not fit the DMA buffers.

    Module level rather than inline in extract_candidates() so it is reachable
    without PYNQ: extract_candidates() cannot be called off the board, and a
    boundary this easy to get wrong should not be a rule that only hardware
    can check.  See test_cand_packing.test_batch_size_boundary.
    """
    if n > _MAX_CANDIDATES:
        raise ValueError(
            f"{n} candidates exceeds the driver buffer limit of "
            f"{_MAX_CANDIDATES}; split the page into batches or raise "
            f"_MAX_CANDIDATES and the _cand_buf/_meta_buf allocations "
            f"with it.  This is a host-side allocation bound, not a PL "
            f"one — patch_extract_core takes num_cands as a 16-bit "
            f"register and has no per-candidate storage.")


def compute_cand_envelope(side_templates: dict, side: str,
                          scales: Sequence[float]) -> tuple[int, int]:
    """max_tw/max_th for one candidate: worst case across the bank at the
    largest scale.

    int(round(...)) — NOT int(...) — because the actual template transmitted
    to the PL is resized with int(round(base * scale)); truncating here
    underestimates the real template by one pixel at half-integer products
    and feeds every §4 bound a value one short (contract §4.5: the descriptor
    must describe the real template, not a re-derivation of it).
    """
    max_scale = max(scales)
    max_tw = 0
    max_th = 0
    for templ_list in side_templates.get(side, {}).values():
        for t in templ_list:
            max_tw = max(max_tw, int(round(t.shape[1] * max_scale)))
            max_th = max(max_th, int(round(t.shape[0] * max_scale)))
    return max(max_tw, 4), max(max_th, 4)


def build_trials(templates_by_kind: dict, scales: Sequence[float]) -> list[dict]:
    """Flatten a template bank into the FROZEN trial order.

    Order is (kind in dict insertion order) × (template in list order) ×
    (scale in the given order), and `match_candidate` iterates it exactly as
    built.  This order is part of the classification result: the argmax uses
    strictly-greater comparison, so the first trial reaching the maximum wins
    ties, and reordering trials silently changes which template's box is
    reported for tied scores (contract §6.4 option 1).  It matches the CPU
    baseline's loop nesting in classify_endpoint/best_template_match_local.

    Each trial dict carries: kind, templ_id (ordinal in this frozen order),
    base_index, scale, pixels (the actual resized uint8 array — §4.5: the
    descriptor and the stream must describe THIS array, not a re-derivation),
    and legal/illegal_reasons from §4.6's flat-template test.  Illegal trials
    are kept in place (so templ_id stays stable) and skipped at match time.
    """
    import cv2  # lazy: PYNQ images ship it, but importing here keeps
    # module-level imports PYNQ-image-friendly and test-importable.

    trials: list[dict] = []
    for kind, templ_list in templates_by_kind.items():
        for base_index, base in enumerate(templ_list):
            for scale in scales:
                tw = max(4, int(round(base.shape[1] * scale)))
                th = max(4, int(round(base.shape[0] * scale)))
                pixels = cv2.resize(base, (tw, th),
                                    interpolation=cv2.INTER_NEAREST)
                # §4.6: a flat template is illegal ABI input, rejected
                # host-side after the final resize and before any DMA.  The
                # trial stays in the list so templ_id is stable; it is
                # skipped, not run.
                reasons = validate_template_content(pixels.tobytes(), tw, th)
                trials.append({
                    "kind": kind,
                    "templ_id": len(trials),
                    "base_index": base_index,
                    "scale": scale,
                    "pixels": pixels,
                    "legal": not reasons,
                    "illegal_reasons": reasons,
                })
    return trials


class PLPipeline:
    """PYNQ interface to the three_stage_combined overlay.

    Raises on construction if anything is missing — never falls back.
    """

    # Exact instance names from three_stage_combined.hwh.  Resolved by name,
    # with the overlay's actual contents in the error when one is absent —
    # a renamed block-design instance should fail here, loudly, not as an
    # AttributeError deep inside a transfer.
    _CORE_NAMES = ("binarize_core_0", "patch_extract_core_0", "tme_top_0")
    _DMA_NAMES = ("axi_dma_binarize", "dma_pe_data", "dma_pe_meta",
                  "axi_dma_patch", "axi_dma_templ")

    def __init__(self, bitfile: str = "/home/xilinx/three_stage_combined.bit",
                 timeout_s: float = 120.0):
        from pynq import Overlay, allocate

        if not (timeout_s > 0) or timeout_s == float("inf"):
            raise ValueError(f"timeout {timeout_s!r} must be finite and "
                             f"positive")
        self.timeout_s = float(timeout_s)

        self._ol = Overlay(bitfile)
        self._allocate = allocate

        missing = [n for n in self._CORE_NAMES + self._DMA_NAMES
                   if n not in self._ol.ip_dict]
        if missing:
            raise RuntimeError(
                f"overlay {bitfile} lacks {missing}; it has "
                f"{sorted(self._ol.ip_dict)}.  This driver is written "
                f"against three_stage_combined.hwh (2026-08-11) — if the "
                f"block design was renamed, update _CORE_NAMES/_DMA_NAMES.")

        self._binarize = self._ol.binarize_core_0
        self._extract  = self._ol.patch_extract_core_0
        self._tme      = self._ol.tme_top_0

        self._dma_binarize = self._ol.axi_dma_binarize   # gray in / binary out
        self._dma_pe_data  = self._ol.dma_pe_data        # cands in / patches out
        self._dma_pe_meta  = self._ol.dma_pe_meta        # §6.2 records out
        self._dma_patch    = self._ol.axi_dma_patch      # patch → matcher
        self._dma_templ    = self._ol.axi_dma_templ      # template → matcher

        # The matcher DMAs' own single-transfer ceiling (§3.1).  The block
        # design parameter wins over the compiled-in default.
        self._tme_dma_max = min(
            self._channel_max(self._dma_patch.sendchannel),
            self._channel_max(self._dma_templ.sendchannel))

        # Small fixed buffers.  Image buffers are lazy (see _ensure_image_bufs)
        # so that loading the driver does not itself decide the §2.2 CMA gate.
        self._cand_buf = allocate(
            shape=(_MAX_CANDIDATES * _CAND_STRUCT_SIZE,), dtype=np.uint8)
        self._meta_buf = allocate(
            shape=(_MAX_CANDIDATES * _META_STRUCT_SIZE,), dtype=np.uint8)
        self._patch_rx_buf = allocate(shape=(_MAX_PATCH_BYTES,), dtype=np.uint8)
        self._tme_patch_buf = allocate(shape=(_MAX_PATCH_BYTES,), dtype=np.uint8)
        self._tme_templ_buf = allocate(
            shape=(_MAX_TEMPL_W * _MAX_TEMPL_H,), dtype=np.uint8)

        self._gray_buf = None
        self._bin_buf = None
        self._img_w: int = 0
        self._img_h: int = 0
        # Row stride of the binary image buffer (contract §2).  Compact —
        # binarize_core emits exactly img_w * img_h compact logical beats —
        # but everything downstream reads this attribute rather than assuming
        # stride == img_w, so a padded layout is a one-line change here.
        self._stride_bytes: int = 0

        # Staged-patch state for match_candidate's one-copy-per-candidate path.
        self._staged_patch: Optional[tuple[int, int]] = None

    # -- generic helpers ----------------------------------------------------

    @staticmethod
    def _channel_max(channel) -> int:
        for obj, attr in ((channel, "_max_size"), (channel, "buffer_max_size")):
            val = getattr(obj, attr, None)
            if isinstance(val, int) and val > 0:
                return val
        return DMA_MAX_BYTES_DEFAULT

    def _start(self, core) -> None:
        core.write(_AP_CTRL_OFF, _AP_START)

    def _wait_done(self, core, deadline: float, label: str,
                   channels: Sequence[tuple] = ()) -> None:
        """Poll ap_ctrl until done, with the bring-up script's latches.

        ap_done is Clear-on-Read: the poll that observes it also consumes it,
        so it is latched.  ap_idle alone is NOT completion — the core is idle
        before starting too — so idle only ends the wait after some poll has
        seen the core busy.  `channels` are (channel, name) pairs checked for
        DMA errors on every pass, because a dead channel otherwise surfaces
        two layers away as this timeout.
        """
        seen_busy = False
        while True:
            ctrl = core.read(_AP_CTRL_OFF)
            if ctrl & _AP_DONE:
                return
            if not ctrl & _AP_IDLE:
                seen_busy = True
            elif seen_busy:
                return
            for ch, name in channels:
                err = getattr(ch, "error", False)
                if err:
                    raise RuntimeError(f"{label}: DMA channel {name} reports "
                                       f"an error while the core is running")
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"{label}: ap_done never rose within {self.timeout_s:g} s "
                    f"(AP_CTRL=0x{ctrl:08X}, seen_busy={seen_busy})")
            time.sleep(0.0005)

    def _wait_channel(self, channel, deadline: float, label: str) -> None:
        """Wait for a DMA channel to drain, bounded — never bare wait().

        channel.wait() blocks forever, and a hang is a failure worth
        reporting rather than sitting in.  After idle, wait() is called once
        to let PYNQ settle its per-transfer bookkeeping; if it raises then,
        PYNQ and the hardware disagree and the run's data is unusable.
        """
        while not channel.idle:
            err = getattr(channel, "error", False)
            if err:
                raise RuntimeError(f"{label}: DMA error")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{label}: transfer still outstanding at "
                                   f"the {self.timeout_s:g} s deadline")
            time.sleep(0.0005)
        try:
            channel.wait()
        except Exception as exc:                       # noqa: BLE001
            raise RuntimeError(
                f"{label}: channel reported idle but wait() raised "
                f"{type(exc).__name__}: {exc} — PYNQ's view of the transfer "
                f"and the DMA's status register disagree; treat this run's "
                f"data as unusable.") from exc

    @staticmethod
    def _read_scalar(core, name: str) -> int:
        return int(getattr(core.register_map, name))

    @staticmethod
    def _read_vld(core, name: str) -> int:
        """Read a Clear-on-Read ap_vld companion — once, caller latches."""
        return int(getattr(core.register_map, name)) & 1

    # -- stage 1: binarize ---------------------------------------------------

    def _ensure_image_bufs(self, h: int, w: int) -> None:
        n = h * w
        if self._gray_buf is None or len(self._gray_buf) < n:
            for buf in (self._gray_buf, self._bin_buf):
                if buf is not None:
                    buf.freebuffer()
            # Two SEPARATELY contiguous CMA allocations — this pair is what
            # the §2.2 gate is about; at the 9856×6400 maximum it is the
            # 2 × 60.2 MiB probe_cma_budget.py exists to test.
            self._gray_buf = self._allocate(shape=(n,), dtype=np.uint8)
            self._bin_buf = self._allocate(shape=(n,), dtype=np.uint8)

    def binarize_page(self, gray_np: np.ndarray, threshold: int) -> np.ndarray:
        """Run binarize_core over a full page.  Returns a (h, w) uint8 view
        backed by the DDR buffer that patch extraction will read.

        Sequence (§7.1): write scalars, arm the binary S2MM, ap_start, send
        the gray page on MM2S, wait core + both channels, invalidate.
        """
        h, w = gray_np.shape
        n = h * w
        self._ensure_image_bufs(h, w)
        self._img_h, self._img_w = h, w
        self._stride_bytes = w   # compact: exactly img_w*img_h logical beats

        self._gray_buf[:n] = gray_np.ravel()
        self._gray_buf.flush()

        rm = self._binarize.register_map
        rm.img_w = w
        rm.img_h = h
        rm.threshold = threshold

        deadline = time.monotonic() + self.timeout_s
        # Arm the receive first: the core produces output beats as input
        # arrives, and an unarmed S2MM backpressures into the core mid-page.
        self._dma_binarize.recvchannel.transfer(self._bin_buf[:n])
        self._start(self._binarize)
        self._dma_binarize.sendchannel.transfer(self._gray_buf[:n])

        self._wait_done(self._binarize, deadline, "binarize_core",
                        channels=((self._dma_binarize.sendchannel, "gray MM2S"),
                                  (self._dma_binarize.recvchannel, "bin S2MM")))
        self._wait_channel(self._dma_binarize.sendchannel, deadline,
                           "gray MM2S")
        self._wait_channel(self._dma_binarize.recvchannel, deadline,
                           "bin S2MM")

        # The S2MM went through PYNQ's DMA driver, which invalidates for us;
        # doing it explicitly costs nothing and does not depend on that
        # staying true.
        self._bin_buf.invalidate()
        return self.binary_view()

    def binary_view(self) -> np.ndarray:
        """Zero-copy (h, w) view of the binary page in DDR.

        Sliced from a (h, stride) view so it stays correct if a padded
        layout ever appears (contract §2.1): a compact reshape over a padded
        buffer skews every row silently.
        """
        if self._img_w == 0:
            raise RuntimeError("no page has been binarized yet")
        h, w, stride = self._img_h, self._img_w, self._stride_bytes
        return np.frombuffer(self._bin_buf, dtype=np.uint8,
                             count=h * stride).reshape(h, stride)[:, :w]

    def suppress_text(self, words: Sequence[dict]) -> None:
        """Zero text bboxes in the shared DDR3 binary image buffer.

        Replicates build_text_suppressed_binary() using direct numpy writes
        into the PYNQ DMA buffer (mmap'd into userspace) — no DMA transfer.

        Cache ownership of _bin_buf alternates and both halves are explicit:
        binarize_page() invalidates after the PL writes it, and this method
        flushes after the CPU writes it.  patch_extract_core reads the buffer
        by physical address via its bin_image pointer, which bypasses PYNQ's
        DMA driver and so gets no cache maintenance for free.
        """
        if self._img_w == 0:
            return
        expand = 3
        h, w = self._img_h, self._img_w
        bin_view = self.binary_view()
        for word in words:
            x0 = max(0, int(word["x0"] - expand))
            y0 = max(0, int(word["y0"] - expand))
            x1 = min(w, int(word["x1"] + expand))
            y1 = min(h, int(word["y1"] + expand))
            bin_view[y0:y1, x0:x1] = 0
        self._bin_buf.flush()

    # -- stage 2: patch extraction -------------------------------------------

    def extract_candidates(self, candidates: Sequence[dict],
                           side_templates: dict,
                           scales: Sequence[float]) -> list[dict]:
        """Dispatch one batch of descriptors and collect §6.2 records + patches.

        Returns one dict per candidate, in input order: the unpacked metadata
        record plus "patch" — a (patch_h, patch_w) uint8 array, or None for
        an invalid candidate (which the §4.1 pre-checks below should make
        unreachable; a valid=0 record coming back anyway is model drift and
        raises).

        Framing (§5/§6.2): the extractor emits one metadata record per
        descriptor with TLAST at batch end — armed once — and the patch pixel
        stream carries per-patch TLAST, so each patch is its own S2MM
        transfer, armed per candidate at the §3 envelope bound.  A rejected
        descriptor produces no pixels and no TLAST, which is why rejects must
        be caught host-side before dispatch: an armed receive for a patch
        that never comes fails as a timeout here, not silently.
        """
        if not candidates:
            return []

        # Reject, do not truncate: a page with 65 endpoints must fail loudly,
        # not return 64 plausible results (nothing downstream compares result
        # count to input count).
        n = len(candidates)
        _validate_batch_size(n)
        if self._img_w == 0:
            raise RuntimeError("binarize_page() must run before "
                               "extract_candidates() — the extractor reads "
                               "the binary page buffer")

        # ---- Pack candidate descriptors (§6.1) ----
        offset = 0
        for i, cand in enumerate(candidates):
            ep_xf, ep_yf = cand["endpoint"]
            ep_x = int(round(ep_xf))
            ep_y = int(round(ep_yf))

            side = cand.get("side", "left")
            if side not in _SIDE_CODE:
                raise ValueError(
                    f"candidate {i}: side {side!r} is neither 'left' nor "
                    f"'right' — a typo here used to silently become 'right', "
                    f"mirroring the patch about the endpoint")
            side_code = _SIDE_CODE[side]

            max_tw, max_th = compute_cand_envelope(side_templates, side, scales)

            # Enforce §4.1 before dispatch: the PL would reject these with a
            # reason code, but software generating an illegal descriptor is a
            # configuration bug worth an exception — and (framing note above)
            # a rejected descriptor also strands its armed patch receive.
            if not (4 <= max_tw <= _MAX_TEMPL_W):
                raise ValueError(
                    f"candidate {i}: max_tw {max_tw} outside "
                    f"[4, {_MAX_TEMPL_W}] — template bank exceeds the frozen "
                    f"envelope (§4.1)")
            if not (4 <= max_th <= _MAX_TEMPL_H):
                raise ValueError(
                    f"candidate {i}: max_th {max_th} outside "
                    f"[4, {_MAX_TEMPL_H}] — template bank exceeds the frozen "
                    f"envelope (§4.1)")
            if not (0 <= ep_x < self._img_w and 0 <= ep_y < self._img_h):
                raise ValueError(
                    f"candidate {i}: endpoint ({ep_x},{ep_y}) outside "
                    f"{self._img_w}x{self._img_h} (§4.1)")

            packed = pack_candidate(ep_x, ep_y, side_code, max_tw, max_th)
            self._cand_buf[offset:offset + _CAND_STRUCT_SIZE] = \
                np.frombuffer(packed, dtype=np.uint8)
            offset += _CAND_STRUCT_SIZE

        # Zero the tail so a shorter batch cannot leave a previous run's
        # descriptors where the PL might fetch them.
        self._cand_buf[offset:] = 0
        self._cand_buf.flush()

        # ---- Configure the extractor (per-core map, §7.1.2) ----
        rm = self._extract.register_map
        bin_addr = self._bin_buf.physical_address
        rm.bin_image_1 = bin_addr & 0xFFFFFFFF
        rm.bin_image_2 = (bin_addr >> 32) & 0xFFFFFFFF
        rm.img_w = self._img_w
        rm.img_h = self._img_h
        rm.stride_bytes = self._stride_bytes
        rm.buffer_bytes = len(self._bin_buf)
        rm.num_cands = n

        deadline = time.monotonic() + self.timeout_s

        # Metadata S2MM: one arm for the whole batch (TLAST at batch end).
        meta_bytes = n * _META_STRUCT_SIZE
        self._dma_pe_meta.recvchannel.transfer(self._meta_buf[:meta_bytes])

        self._start(self._extract)
        self._dma_pe_data.sendchannel.transfer(
            self._cand_buf[:n * _CAND_STRUCT_SIZE])

        # Patch pixels: one S2MM transfer per candidate, ended by the per-patch
        # TLAST.  Armed at the envelope bound; the actual length is
        # cross-checked against the §6.2 record afterwards.
        raw_patches: list[np.ndarray] = []
        transferred: list[Optional[int]] = []
        for i in range(n):
            self._dma_pe_data.recvchannel.transfer(
                self._patch_rx_buf[:_MAX_PATCH_BYTES])
            self._wait_channel(self._dma_pe_data.recvchannel, deadline,
                               f"patch S2MM (candidate {i})")
            self._patch_rx_buf.invalidate()
            raw_patches.append(np.array(self._patch_rx_buf))  # full-bound copy
            got = getattr(self._dma_pe_data.recvchannel, "transferred", None)
            transferred.append(int(got) if got else None)

        self._wait_done(self._extract, deadline, "patch_extract_core",
                        channels=((self._dma_pe_data.sendchannel, "cand MM2S"),
                                  (self._dma_pe_meta.recvchannel, "meta S2MM")))
        self._wait_channel(self._dma_pe_data.sendchannel, deadline, "cand MM2S")
        self._wait_channel(self._dma_pe_meta.recvchannel, deadline, "meta S2MM")
        self._meta_buf.invalidate()

        # ---- Status registers: read once, latch (§7.1.1 item 3) ----
        sts = {}
        for name in ("sts_flags", "sts_rejected", "sts_processed"):
            sts[name] = self._read_scalar(self._extract, name)
            if not self._read_vld(self._extract, name + "_ctrl"):
                raise RuntimeError(
                    f"patch_extract_core: {name} ap_vld is clear — the value "
                    f"read is left over from a previous run, not this one")
        if sts["sts_flags"] & 0x1:
            raise RuntimeError(
                f"patch_extract_core: global image configuration invalid "
                f"(img {self._img_w}x{self._img_h}, stride "
                f"{self._stride_bytes}, buffer {len(self._bin_buf)})")
        if sts["sts_flags"] & 0x2:
            raise RuntimeError(
                f"patch_extract_core: TLAST/num_cands mismatch — processed "
                f"{sts['sts_processed']} of {n} descriptors")
        if sts["sts_rejected"]:
            raise RuntimeError(
                f"patch_extract_core rejected {sts['sts_rejected']}/{n} "
                f"candidates that passed the PS-side §4.1 checks — the "
                f"validation models have drifted, and the per-candidate "
                f"patch receives above are no longer aligned to candidates")

        # ---- Unpack §6.2 records and slice patches to their REAL geometry.
        # The record is authoritative (the seam test's clipped-candidate
        # lesson: 106 px where the §4.5 formula re-derives 152) — the PS
        # formula is only a cross-check, never the slice length.
        records = unpack_patch_metadata(bytes(self._meta_buf[:meta_bytes]))
        out = []
        for i, rec in enumerate(records):
            if not rec["valid"]:
                raise RuntimeError(
                    f"candidate {i}: valid=0 ({rec['reasons']}) survived the "
                    f"§4.1 pre-checks — model drift")
            nbytes = rec["patch_w"] * rec["patch_h"]
            if transferred[i] is not None and transferred[i] != nbytes:
                raise RuntimeError(
                    f"candidate {i}: DMA moved {transferred[i]} B but the "
                    f"§6.2 record says {rec['patch_w']}x{rec['patch_h']} = "
                    f"{nbytes} B — framing disagreement, do not trust this "
                    f"batch")
            rec = dict(rec)
            rec["patch"] = raw_patches[i][:nbytes].reshape(
                rec["patch_h"], rec["patch_w"])
            out.append(rec)
        return out

    # -- stage 3: template match ----------------------------------------------

    def _stage_patch(self, patch: np.ndarray) -> None:
        ph, pw = patch.shape
        self._tme_patch_buf[:pw * ph] = patch.ravel()
        self._tme_patch_buf.flush()
        self._staged_patch = (pw, ph)

    def _run_trial(self, templ: np.ndarray) -> tuple[float, int, int, float]:
        """One matcher invocation against the staged patch.

        The sequencing and the result-register discipline mirror
        tme_standalone_bringup.run_case, which is what passed 9/9 on silicon:
        validate before ap_start (the core has no rejection path), arm both
        MM2S before starting, wait on the CORE first and the channels after,
        read each Clear-on-Read ap_vld exactly once.
        """
        if self._staged_patch is None:
            raise RuntimeError("no patch staged")
        pw, ph = self._staged_patch
        th_, tw_ = templ.shape

        errs = validate_geometry(pw, ph, tw_, th_, self._tme_dma_max)
        if errs:
            raise ValueError(
                f"refusing to start the matcher on {pw}x{ph} / {tw_}x{th_}:\n"
                + "\n".join(f"    - {e}" for e in errs))
        errs = validate_template_content(templ.tobytes(), tw_, th_)
        if errs:
            raise ValueError(
                f"refusing to start the matcher on this {tw_}x{th_} "
                f"template:\n" + "\n".join(f"    - {e}" for e in errs))

        n_t = tw_ * th_
        self._tme_templ_buf[:n_t] = templ.ravel()
        self._tme_templ_buf.flush()

        rm = self._tme.register_map
        rm.patch_w = pw
        rm.patch_h = ph
        rm.templ_w = tw_
        rm.templ_h = th_

        t0 = time.monotonic()
        deadline = t0 + self.timeout_s
        # tme_top drains the patch fully and only then reads the template, so
        # the template DMA sits backpressured for most of the run — normal.
        self._dma_patch.sendchannel.transfer(self._tme_patch_buf[:pw * ph])
        self._dma_templ.sendchannel.transfer(self._tme_templ_buf[:n_t])
        self._start(self._tme)

        self._wait_done(self._tme, deadline, "tme_top",
                        channels=((self._dma_patch.sendchannel, "patch MM2S"),
                                  (self._dma_templ.sendchannel, "templ MM2S")))
        self._wait_channel(self._dma_patch.sendchannel, deadline, "patch MM2S")
        self._wait_channel(self._dma_templ.sendchannel, deadline, "templ MM2S")
        elapsed = time.monotonic() - t0

        score_bits = self._read_scalar(self._tme, "result_score")
        x = self._read_scalar(self._tme, "result_x") & 0xFFFF
        y = self._read_scalar(self._tme, "result_y") & 0xFFFF
        vlds = (self._read_vld(self._tme, "result_score_ctrl"),
                self._read_vld(self._tme, "result_x_ctrl"),
                self._read_vld(self._tme, "result_y_ctrl"))
        if not all(vlds):
            raise RuntimeError(
                f"ap_done rose but result ap_vld is score={vlds[0]} "
                f"x={vlds[1]} y={vlds[2]} — at least one result register was "
                f"not written by this invocation")

        score = struct.unpack("<f", struct.pack("<I", score_bits))[0]
        return score, x, y, elapsed

    def match_template(self, patch: np.ndarray,
                       templ: np.ndarray) -> tuple[float, int, int, float]:
        """One (patch, template) trial.  Returns (score, match_x, match_y,
        seconds) — score/x/y read from tme_top_0's scalar result registers.
        """
        self._stage_patch(patch)
        return self._run_trial(templ)

    def match_candidate(self, patch: np.ndarray, patch_x0: int, patch_y0: int,
                        trials: Sequence[dict], score_fn=None) -> dict:
        """Run every legal trial against one patch and reduce PS-side.

        `trials` comes from build_trials() and its ORDER IS THE TIE-BREAK:
        the running argmax uses `>` — strictly greater, never `>=` — so the
        FIRST trial reaching the maximum wins, per candidate and per kind
        (contract §6.4 option 1; identical to the CPU baseline's loops).

        `score_fn(raw_score, match_x, match_y, trial)` optionally maps the raw
        register score to the value the argmax runs on (the detector's
        anchor-distance adjustment); the raw score is retained alongside.

        A trial whose template does not fit the patch is skipped with the CPU
        baseline's rule (`tw >= patch_w or th >= patch_h` — the baseline skips
        equality even though §4.4 makes it legal on the PL; parity wins here).

        Returns {"best": hit-or-None, "by_kind": {kind: hit}} where each hit
        carries score, raw_score, match_x/y, kind, templ_id, base_index,
        scale, and box = (patch_x0 + match_x, patch_y0 + match_y, templ_w,
        templ_h) in absolute logical-page coordinates (§1/§6.3 decision).
        """
        ph, pw = patch.shape
        self._stage_patch(patch)

        best: Optional[dict] = None
        by_kind: dict[str, dict] = {}
        for trial in trials:
            if not trial["legal"]:
                continue
            t = trial["pixels"]
            th_, tw_ = t.shape
            if tw_ >= pw or th_ >= ph:
                continue

            raw, x, y, elapsed = self._run_trial(t)
            score = score_fn(raw, x, y, trial) if score_fn else raw
            hit = {
                "score": score,
                "raw_score": raw,
                "match_x": x,
                "match_y": y,
                "kind": trial["kind"],
                "templ_id": trial["templ_id"],
                "base_index": trial["base_index"],
                "scale": trial["scale"],
                "box": (patch_x0 + x, patch_y0 + y, tw_, th_),
                "elapsed_s": elapsed,
            }
            # Strictly greater on both reductions: first trial wins ties.
            if best is None or hit["score"] > best["score"]:
                best = hit
            k = trial["kind"]
            if k not in by_kind or hit["score"] > by_kind[k]["score"]:
                by_kind[k] = hit

        return {"best": best, "by_kind": by_kind}

    # -- teardown --------------------------------------------------------------

    def close(self) -> None:
        """Release DMA buffers."""
        for buf in (self._gray_buf, self._bin_buf, self._cand_buf,
                    self._meta_buf, self._patch_rx_buf, self._tme_patch_buf,
                    self._tme_templ_buf):
            if buf is None:
                continue
            try:
                buf.freebuffer()
            except Exception:
                pass
