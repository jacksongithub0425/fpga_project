"""PYNQ helpers for verifying the A3.3 binarize_core DMA path.

Upload this file next to the notebook, bitstream, HWH, and optional
``tme_driver.py`` on the PYNQ board, then import the functions you need.
Nothing runs at import time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np


SENTINEL = np.uint8(0xAA)
BINARIZE_MAX_W = 9856
BINARIZE_MAX_H = 6400
DEFAULT_TIMEOUT_S = 30.0
_PRESTART_TIMEOUT_S = 1.0

_DMA_STATUS_HALTED = 0x001
_DMA_STATUS_IDLE = 0x002
_DMA_STATUS_ERROR_MASK = 0x770
_DMA_CTRL_RUN = 0x001
_DMA_CTRL_RESET = 0x004
_OUTPUT_GUARD_BYTES = 64

# If a failed DMA cannot be proven quiescent, freeing its CMA buffers would
# turn a recoverable PL hang into memory corruption. Keep strong references
# until the user reloads the overlay and explicitly releases them.
_RETAINED_DMA_BUFFERS: list[dict] = []


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
    """Bit-exact v2.0 HLS reference in compact logical coordinates.

    This intentionally models the integer kernel and truncating ``sum >> 4``;
    OpenCV GaussianBlur rounds and is not a bit-exact oracle for this core.
    """
    gray_u8 = np.ascontiguousarray(gray, dtype=np.uint8)
    if gray_u8.ndim != 2:
        raise ValueError("gray must be a 2-D uint8 image")
    h, w = gray_u8.shape
    if not 3 <= w <= BINARIZE_MAX_W or not 3 <= h <= BINARIZE_MAX_H:
        raise ValueError(
            f"image {w}x{h} outside binarize_core limits "
            f"3..{BINARIZE_MAX_W} by 3..{BINARIZE_MAX_H}")
    if not 0 <= int(threshold) <= 255:
        raise ValueError("threshold must fit the core's unsigned 8-bit register")

    gray_i = gray_u8.astype(np.int32)
    blur_valid = (
        gray_i[:-2, :-2] + 2 * gray_i[:-2, 1:-1] + gray_i[:-2, 2:]
        + 2 * gray_i[1:-1, :-2] + 4 * gray_i[1:-1, 1:-1]
        + 2 * gray_i[1:-1, 2:]
        + gray_i[2:, :-2] + 2 * gray_i[2:, 1:-1] + gray_i[2:, 2:]
    ) >> 4

    logical = np.zeros((h, w), dtype=np.uint8)
    logical[1:-1, 1:-1] = np.where(
        blur_valid <= int(threshold), np.uint8(255), np.uint8(0))
    return logical


def otsu_threshold_downsampled(gray: np.ndarray, downsample: int = 4) -> int:
    """Match the project strategy: compute Otsu threshold on a downsampled page."""
    import cv2

    h, w = gray.shape
    small = cv2.resize(gray, (max(1, w // downsample), max(1, h // downsample)))
    threshold, _ = cv2.threshold(small, 0, 255, cv2.THRESH_OTSU)
    return int(threshold)


def align_pl_output(raw: np.ndarray, *, legacy_v1_0: bool = False) -> np.ndarray:
    """Convert legacy v1.0 raw output to logical layout.

    Do not call this for the regenerated v2.0 IP: v2.0 already emits compact
    logical row-major order and applying this conversion would shift it twice.
    The explicit flag prevents an old notebook call from silently corrupting
    a v2.0 signoff result.
    """
    if not legacy_v1_0:
        raise RuntimeError(
            "align_pl_output() is only for legacy v1.0 raw output; "
            "v2.0 output is already logical and must be compared directly")
    raw_u8 = np.asarray(raw, dtype=np.uint8)
    if raw_u8.ndim != 2:
        raise ValueError("raw must be a 2-D uint8 image")
    aligned = np.zeros_like(raw_u8)
    aligned[:-1, :-1] = raw_u8[1:, 1:]
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


def _read_limit(obj: Any, label: str) -> int:
    """Read one fail-closed DMA length limit across PYNQ API variants."""
    values = []
    for attr in ("buffer_max_size", "_max_size", "_size"):
        try:
            value = getattr(obj, attr)
        except (AttributeError, RuntimeError):
            continue
        try:
            value_i = int(value)
        except (TypeError, ValueError):
            continue
        if value_i > 0:
            values.append((attr, value_i))
    if not values:
        raise RuntimeError(
            f"cannot determine {label} transfer limit: neither "
            "buffer_max_size, _max_size, nor legacy _size is a positive integer")
    return min(value for _attr, value in values)


def _require_dma_capacity(dma: Any, nbytes: int) -> dict[str, int]:
    """Require the DMA object and both channels to advertise enough length."""
    limits = {
        "dma": _read_limit(dma, "DMA"),
        "MM2S": _read_limit(dma.sendchannel, "MM2S channel"),
        "S2MM": _read_limit(dma.recvchannel, "S2MM channel"),
    }
    too_small = {name: limit for name, limit in limits.items()
                 if nbytes > limit}
    if too_small:
        detail = ", ".join(f"{name}={limit}" for name, limit in too_small.items())
        raise ValueError(
            f"{nbytes}-byte raster exceeds DMA transfer limit(s): {detail}")
    return limits


def _channel_state(channel: Any, label: str) -> dict[str, int | bool | str]:
    """Read AXI DMA control/status without entering PYNQ's blocking wait."""
    mmio = getattr(channel, "_mmio", None)
    offset = getattr(channel, "_offset", None)
    if mmio is None or offset is None or not hasattr(mmio, "read"):
        raise RuntimeError(
            f"cannot inspect {label}: PYNQ channel has no _mmio/_offset register view")
    control = int(mmio.read(int(offset)))
    status = int(mmio.read(int(offset) + 4))
    return {
        "label": label,
        "control": control,
        "status": status,
        "run_enabled": bool(control & _DMA_CTRL_RUN),
        "running": not bool(status & _DMA_STATUS_HALTED),
        "halted": bool(status & _DMA_STATUS_HALTED),
        "idle": bool(status & _DMA_STATUS_IDLE),
        "errors": status & _DMA_STATUS_ERROR_MASK,
    }


def _state_text(state: dict[str, int | bool | str]) -> str:
    return (
        f"{state['label']}(ctrl=0x{int(state['control']):08x}, "
        f"status=0x{int(state['status']):08x}, idle={state['idle']}, "
        f"run_enabled={state['run_enabled']}, halted={state['halted']}, "
        f"errors=0x{int(state['errors']):03x})"
    )


def _assert_dma_ready(core: Any, dma: Any, timeout_s: float) -> None:
    """Require a clean channel state without rejecting PYNQ's first transfer.

    In AXI DMA direct-register mode, DMASR.Idle can remain zero until the
    first transfer has actually completed.  PYNQ models that architected
    state with ``_first_transfer=True`` and deliberately permits the initial
    ``transfer()`` while the channel is running but not yet idle.  Accept only
    that narrow virgin state; every later transfer must begin from Idle.
    """
    deadline = time.monotonic() + timeout_s
    missing = object()
    while True:
        ctrl = int(core.read(0x00))
        if ctrl & 0x81:
            raise RuntimeError(
                "binarize_core unexpectedly has AP_START or AUTO_RESTART set "
                f"before transfer (AP_CTRL=0x{ctrl:08x})")

        all_channels_ready = True
        details = []
        for channel, label in (
            (dma.sendchannel, "MM2S"),
            (dma.recvchannel, "S2MM"),
        ):
            state = _channel_state(channel, label)
            if int(state["errors"]):
                raise RuntimeError(
                    f"DMA error before start: {_state_text(state)}")

            first_transfer = getattr(channel, "_first_transfer", missing)
            active_buffer = getattr(channel, "_active_buffer", missing)
            virgin = first_transfer is True and active_buffer is None
            reset_busy = bool(int(state["control"]) & _DMA_CTRL_RESET)
            ready = (
                bool(state["run_enabled"])
                and bool(state["running"])
                and not reset_busy
                and (bool(state["idle"]) or virgin)
            )
            all_channels_ready = all_channels_ready and ready

            first_text = ("<missing>" if first_transfer is missing
                          else repr(first_transfer))
            active_text = ("<missing>" if active_buffer is missing
                           else "None" if active_buffer is None else "set")
            details.append(
                f"{_state_text(state)}, first_transfer={first_text}, "
                f"active_buffer={active_text}")

        if ctrl & 0x4 and all_channels_ready:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"pre-start readiness timeout after {timeout_s:.3f}s: "
                f"AP_CTRL=0x{ctrl:08x}; " + "; ".join(details))
        time.sleep(0.001)


def _poll_completion(core: Any, dma: Any, timeout_s: float) -> None:
    """Poll core and both DMA directions to one shared monotonic deadline."""
    deadline = time.monotonic() + timeout_s
    done_seen = False
    busy_seen = False
    while True:
        ctrl = int(core.read(0x00))
        # AP_DONE is Clear-on-Read; latch it. AP_IDLE alone is not sufficient:
        # immediately after the start write there is a small window where the
        # core can still read idle, so accepting initial idle could call wait()
        # before either transaction actually ran.
        done_seen = done_seen or bool(ctrl & 0x2)
        busy_seen = busy_seen or not bool(ctrl & 0x4)
        send = _channel_state(dma.sendchannel, "MM2S")
        recv = _channel_state(dma.recvchannel, "S2MM")
        for state in (send, recv):
            if int(state["errors"]):
                raise RuntimeError(
                    f"DMA error while waiting: {_state_text(state)}")
        core_complete = done_seen or (busy_seen and bool(ctrl & 0x4))
        if core_complete and send["idle"] and recv["idle"]:
            return
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError(
                f"binarize timeout after {timeout_s:.3f}s: "
                f"AP_CTRL=0x{ctrl:08x}, done_seen={done_seen}, "
                f"busy_seen={busy_seen}; {_state_text(send)}; "
                f"{_state_text(recv)}")
        time.sleep(min(0.001, max(0.0, deadline - now)))


def _halt_reset_channel(channel: Any, label: str, timeout_s: float) -> bool:
    """Boundedly halt/reset one AXI DMA channel and verify no bus activity."""
    mmio = getattr(channel, "_mmio", None)
    offset = getattr(channel, "_offset", None)
    if (mmio is None or offset is None or not hasattr(mmio, "read")
            or not hasattr(mmio, "write")):
        return False
    offset = int(offset)
    deadline = time.monotonic() + timeout_s
    try:
        # Clear RS first (graceful halt), then force the AXI DMA reset bit.
        mmio.write(offset, 0)
        halt_deadline = min(deadline, time.monotonic() + min(0.250, timeout_s / 2))
        while time.monotonic() < halt_deadline:
            state = _channel_state(channel, label)
            if (not state["run_enabled"]
                    and (state["halted"] or state["idle"])):
                break
            time.sleep(0.001)

        mmio.write(offset, _DMA_CTRL_RESET)
        while time.monotonic() < deadline:
            state = _channel_state(channel, label)
            reset_busy = bool(int(state["control"]) & _DMA_CTRL_RESET)
            # A reset is proven complete only when the self-clearing Reset bit
            # is zero and DMASR.Halted is one. Idle without Halted is not a
            # sufficient guarantee that memory traffic cannot resume.
            quiescent = not state["run_enabled"] and bool(state["halted"])
            if not reset_busy and quiescent and not int(state["errors"]):
                # PYNQ's object still remembers the buffer because wait() was
                # deliberately never entered. Hardware is now quiescent, so
                # detach that stale reference before the caller frees it.
                if hasattr(channel, "_active_buffer"):
                    channel._active_buffer = None
                return True
            time.sleep(0.001)
    except Exception:
        return False
    return False


def _recover_dma(dma: Any, timeout_s: float) -> bool:
    """Reset both directions; return true only if both verify quiescent."""
    per_channel = min(5.0, max(0.5, timeout_s / 6.0))
    send_ok = _halt_reset_channel(dma.sendchannel, "MM2S", per_channel)
    recv_ok = _halt_reset_channel(dma.recvchannel, "S2MM", per_channel)
    if send_ok and recv_ok:
        print(
            "[binarize_dma_checks] DMA failure recovered by halting/resetting "
            "both channels. Reload the overlay before another transfer.",
            flush=True,
        )
        return True
    return False


def release_retained_dma_buffers(*, overlay_reloaded: bool = False) -> int:
    """Free retained buffers after explicit acknowledgement of overlay reload."""
    if overlay_reloaded is not True:
        raise RuntimeError(
            "refusing to free retained CMA while DMA safety is unknown; "
            "reload the overlay first, then call "
            "release_retained_dma_buffers(overlay_reloaded=True)")
    retained = list(_RETAINED_DMA_BUFFERS)
    _RETAINED_DMA_BUFFERS.clear()
    released = 0
    for entry in retained:
        for buf in (entry["in_buf"], entry["out_buf"]):
            try:
                buf.freebuffer()
                released += 1
            except Exception:
                pass
    return released


def _require_transferred(channel: Any, label: str, nbytes: int) -> None:
    transferred = getattr(channel, "transferred", None)
    if transferred is None:
        raise RuntimeError(
            f"{label} exposes no transferred count after wait; signoff fails closed")
    if int(transferred) != nbytes:
        raise RuntimeError(
            f"{label} transferred {int(transferred)} bytes, expected {nbytes}")


def run_direct_binarize(
    core: Any,
    dma: Any,
    gray: np.ndarray,
    threshold: int,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Tuple[np.ndarray, int]:
    """Run one direct ``AXI DMA -> binarize_core -> AXI DMA`` transfer.

    Returns:
        ``logical_pl_output, remaining_sentinel_count`` for regenerated v2.0.
    """
    from pynq import allocate  # type: ignore

    gray_u8 = np.ascontiguousarray(gray, dtype=np.uint8)
    if gray_u8.ndim != 2:
        raise ValueError("gray must be a 2-D uint8 image")

    timeout_s = float(timeout_s)
    if not np.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be a finite positive number")

    h, w = gray_u8.shape
    n = h * w
    if not 3 <= w <= BINARIZE_MAX_W or not 3 <= h <= BINARIZE_MAX_H:
        raise ValueError(
            f"image {w}x{h} outside binarize_core limits "
            f"3..{BINARIZE_MAX_W} by 3..{BINARIZE_MAX_H}")
    if not 0 <= int(threshold) <= 255:
        raise ValueError("threshold must fit the core's unsigned 8-bit register")

    _require_dma_capacity(dma, n)
    _assert_dma_ready(core, dma, min(timeout_s, _PRESTART_TIMEOUT_S))

    in_buf = allocate(shape=(n,), dtype=np.uint8)
    try:
        out_buf = allocate(shape=(n + _OUTPUT_GUARD_BYTES,), dtype=np.uint8)
    except Exception:
        try:
            in_buf.freebuffer()
        except Exception:
            pass
        raise
    transfer_started = False
    retain_buffers = False

    try:
        in_buf[:] = gray_u8.ravel()
        out_buf[:] = SENTINEL

        in_buf.flush()
        out_buf.flush()

        core.register_map.img_w = int(w)
        core.register_map.img_h = int(h)
        core.register_map.threshold = int(threshold)

        transfer_started = True
        dma.recvchannel.transfer(out_buf[:n])
        core.write(0x00, 0x01)
        dma.sendchannel.transfer(in_buf)

        # PYNQ wait() is unbounded. Poll all three completion surfaces first;
        # once both channels report idle, wait() only performs its normal
        # error/accounting/cache finalisation and cannot strand this caller.
        _poll_completion(core, dma, timeout_s)
        dma.sendchannel.wait()
        dma.recvchannel.wait()
        _require_transferred(dma.sendchannel, "MM2S", n)
        _require_transferred(dma.recvchannel, "S2MM", n)

        ctrl_after = int(core.read(0x00))
        if not ctrl_after & 0x4:
            raise RuntimeError(
                "binarize_core did not return idle after both DMA channels "
                f"completed (AP_CTRL=0x{ctrl_after:08x})")

        out_buf.invalidate()
        logical = np.asarray(out_buf[:n]).reshape(h, w).copy()
        remaining_sentinel = int(np.count_nonzero(logical == SENTINEL))
        guard_corruptions = int(np.count_nonzero(
            np.asarray(out_buf[n:]) != SENTINEL))
        if guard_corruptions:
            raise RuntimeError(
                f"S2MM overwrote {guard_corruptions}/{_OUTPUT_GUARD_BYTES} "
                "post-buffer guard bytes")
        return logical, remaining_sentinel
    except BaseException as exc:
        if transfer_started and not _recover_dma(dma, timeout_s):
            retain_buffers = True
            _RETAINED_DMA_BUFFERS.append({
                "in_buf": in_buf,
                "out_buf": out_buf,
                "reason": repr(exc),
            })
            print(
                "[binarize_dma_checks] WARNING: DMA could not be proven "
                "quiescent. CMA buffers were NOT freed and are retained in "
                "_RETAINED_DMA_BUFFERS. Reload the overlay, then call "
                "release_retained_dma_buffers(overlay_reloaded=True).",
                flush=True,
            )
        raise
    finally:
        if not retain_buffers:
            for buf in (in_buf, out_buf):
                try:
                    buf.freebuffer()
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


def make_pattern_image(h: int, w: int, salt: int) -> np.ndarray:
    """Match the asymmetric deterministic pattern used by the exact C TB."""
    yy, xx = np.indices((h, w), dtype=np.int32)
    return ((yy * 53 + xx * 29 + yy * xx * 7 + salt * 41 + 17)
            & 0xFF).astype(np.uint8)


def run_exact_image_check(
    core: Any,
    dma: Any,
    name: str,
    img: np.ndarray,
    threshold: int,
    verbose: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Run and fail-closed compare one image over the complete logical raster."""
    gold = cpu_golden(img, threshold)
    observed, sentinel_count = run_direct_binarize(
        core, dma, img, threshold, timeout_s=timeout_s)
    logical_match = compare_region(name, observed, gold)

    result = {
        "name": name,
        "shape": observed.shape,
        "threshold": int(threshold),
        "remaining_0xAA": sentinel_count,
        "unique": np.unique(observed, return_counts=True),
        "logical": logical_match,
        "observed": observed,
        "gold": gold,
        "last_row_nonzero": int(np.count_nonzero(observed[-1, :])),
        "last_col_nonzero": int(np.count_nonzero(observed[:, -1])),
    }

    if verbose:
        print(f"[{name}] shape:", result["shape"])
        print(f"[{name}] remaining 0xAA:", sentinel_count)
        print(f"[{name}] PL unique:", result["unique"])
        print(logical_match)
        print(f"[{name}] last row nonzero:", result["last_row_nonzero"])
        print(f"[{name}] last col nonzero:", result["last_col_nonzero"])

    failures = []
    if sentinel_count:
        failures.append(f"{sentinel_count} sentinel byte(s) remain")
    if logical_match.mismatches:
        failures.append(f"{logical_match.mismatches} raster mismatch(es)")
    if result["last_row_nonzero"]:
        failures.append("final logical row is not all zero")
    if result["last_col_nonzero"]:
        failures.append("final logical column is not all zero")
    if failures:
        raise AssertionError(
            f"v2.0 binarize signoff {name!r} failed: " + "; ".join(failures))

    return result


def run_synthetic_check(
    core: Any,
    dma: Any,
    h: int,
    w: int,
    threshold: int = 128,
    verbose: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Compare regenerated v2.0 PL output against the exact HLS model."""
    img = make_synthetic_image(h, w)
    return run_exact_image_check(
        core, dma, f"synthetic-{w}x{h}", img, threshold,
        verbose=verbose, timeout_s=timeout_s)


def run_smoke_suite(
    bitfile: str = "terminal_counter.bit",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """Run the standard A3.3 synthetic checks on the current overlay."""
    _, core, dma = load_direct_overlay(bitfile)
    return [
        run_synthetic_check(core, dma, 64, 64, timeout_s=timeout_s),
        run_synthetic_check(core, dma, 37, 31, timeout_s=timeout_s),
    ]


def run_logical_layout_suite(
    bitfile: str = "terminal_counter.bit",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    verbose: bool = True,
) -> list[dict]:
    """Run the prepared five-case compact-layout v2.0 silicon suite.

    The final two cases deliberately run maximum width followed immediately by
    maximum height at width 3, catching stale line-buffer columns or a width
    register that failed to shrink between starts.
    """
    _, core, dma = load_direct_overlay(bitfile)

    # The cheap vectors below exercise each dimension limit without allocating
    # a 60 MiB page. Still gate the real legal envelope here: otherwise an
    # 18-bit DMA would pass every vector while being unable to transfer a
    # 9856x6400 raster. The frozen maximum requires a 26-bit length register.
    max_raster_bytes = BINARIZE_MAX_W * BINARIZE_MAX_H
    limits = _require_dma_capacity(dma, max_raster_bytes)
    limit_text = ", ".join(
        f"{name}={limit} (headroom {limit - max_raster_bytes})"
        for name, limit in limits.items())
    print(
        f"DMA MAX-RASTER PREFLIGHT PASS: required={max_raster_bytes}; "
        f"{limit_text}")

    truncation = np.full((3, 3), 100, dtype=np.uint8)
    truncation[1, 1] = 102  # weighted sum 1608; 1608 >> 4 == 100
    cases = [
        ("truncation-3x3", truncation, 100),
        ("mixed-5x4", make_pattern_image(4, 5, 1), 150),
        ("mixed-4x5", make_pattern_image(5, 4, 2), 130),
        ("max-width-9856x3", make_pattern_image(3, BINARIZE_MAX_W, 3), 127),
        ("width-shrink-3x6400", make_pattern_image(BINARIZE_MAX_H, 3, 4), 127),
    ]

    results = []
    for name, img, threshold in cases:
        results.append(run_exact_image_check(
            core, dma, name, img, threshold,
            verbose=verbose, timeout_s=timeout_s))
    print("BINARIZE v2.0 LOGICAL-LAYOUT SILICON SUITE PASSED: 5/5")
    return results


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
    use_driver: bool = False,
    threshold: Optional[int] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Run one real PDF/image page through PL binarize and compare to CPU golden.

    The v2.0 standalone signoff uses the direct DMA path. The provisional
    integrated ``PLPipeline`` does not expose a binary-image readback method,
    so ``use_driver=True`` is rejected explicitly rather than failing later or
    accidentally validating a different data path.
    """
    gray = load_grayscale_input(pdf_path, zoom=zoom, page_index=page_index)
    threshold = otsu_threshold_downsampled(gray) if threshold is None else int(threshold)
    gold = cpu_golden(gray, threshold)

    if use_driver:
        raise NotImplementedError(
            "v2.0 signoff requires use_driver=False: the provisional "
            "PLPipeline has no binary-image readback contract")
    else:
        _, core, dma = load_direct_overlay(bitfile)
        observed, sentinel_count = run_direct_binarize(
            core, dma, gray, threshold, timeout_s=timeout_s)

    match = compare_region("REAL_PAGE_LOGICAL_EXACT", observed, gold)
    last_row_nonzero = int(np.count_nonzero(observed[-1, :]))
    last_col_nonzero = int(np.count_nonzero(observed[:, -1]))

    print("gray shape:", gray.shape)
    print("threshold:", threshold)
    print("pl unique:", np.unique(observed, return_counts=True))
    print("remaining 0xAA:", sentinel_count)
    print(match)
    print("last row nonzero:", last_row_nonzero)
    print("last col nonzero:", last_col_nonzero)

    failures = []
    if sentinel_count:
        failures.append(f"{sentinel_count} sentinel byte(s) remain")
    if match.mismatches:
        failures.append(f"{match.mismatches} raster mismatch(es)")
    if last_row_nonzero:
        failures.append("final logical row is not all zero")
    if last_col_nonzero:
        failures.append("final logical column is not all zero")
    if failures:
        raise AssertionError("v2.0 real-page signoff failed: " + "; ".join(failures))

    return {
        "gray": gray,
        "threshold": threshold,
        "gold": gold,
        "observed": observed,
        "match": match,
        "remaining_0xAA": sentinel_count,
        "last_row_nonzero": last_row_nonzero,
        "last_col_nonzero": last_col_nonzero,
    }
