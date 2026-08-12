"""Host-only tests for PLPipeline's in-flight state machine.

    python test_driver_state.py        # from sw/
    pytest test_driver_state.py

No PYNQ and no board: PLPipeline is built with object.__new__ and given fake
cores, channels and buffers, so only the state machine is exercised.

What it pins down: a stage must be marked in-flight BEFORE the first buffer
write or DMA arm, and must stay marked if anything then fails.  The window
that motivated these tests was real — transfers were armed, and only after
that did `_start()` set the flag, so a failure in between (a non-idle core)
left an armed S2MM with the buffers still marked free for `close()` to
release.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tme_driver as d

_AP_IDLE = 1 << 2


class FakeBuf(np.ndarray):
    """A numpy array that also answers the PynqBuffer calls the driver makes."""

    def __new__(cls, n):
        obj = np.zeros(n, dtype=np.uint8).view(cls)
        obj.freed = False
        return obj

    def flush(self):
        pass

    def invalidate(self):
        pass

    def freebuffer(self):
        self.freed = True

    @property
    def physical_address(self):
        return 0x1000_0000


class FakeChannel:
    def __init__(self):
        self.armed = 0
        self.idle = True
        self.error = False
        self.transferred = 0

    def transfer(self, buf):
        self.armed += 1
        self.transferred = len(buf)

    def wait(self):
        pass


class FakeDma:
    def __init__(self):
        self.sendchannel = FakeChannel()
        self.recvchannel = FakeChannel()


class FakeRegMap:
    def __init__(self):
        self._v = {}

    def __setattr__(self, k, v):
        if k == "_v":
            object.__setattr__(self, k, v)
        else:
            self._v[k] = v

    def __getattr__(self, k):
        return self._v.get(k, 0)


class FakeCore:
    """An HLS core whose ap_ctrl can be made to report 'not idle'."""

    def __init__(self, idle=True):
        self.idle = idle
        self.register_map = FakeRegMap()
        self.started = 0

    def read(self, off):
        return _AP_IDLE if self.idle else 0

    def write(self, off, val):
        self.started += 1


def make_pipeline(core_idle=True):
    p = object.__new__(d.PLPipeline)
    p.timeout_s = 1.0
    p._transfers_outstanding = False
    p.last_transfer_stats = None
    p._staged_patch = None
    p._img_w = p._img_h = 0
    p._stride_bytes = 0
    p._binarize = FakeCore(core_idle)
    p._extract = FakeCore(core_idle)
    p._tme = FakeCore(core_idle)
    p._dma_binarize = FakeDma()
    p._dma_pe_data = FakeDma()
    p._dma_pe_meta = FakeDma()
    p._dma_patch = FakeDma()
    p._dma_templ = FakeDma()
    p._tme_dma_max = 262143
    p._gray_buf = None
    p._bin_buf = None
    p._allocate = lambda shape, dtype: FakeBuf(shape[0])
    p._cand_buf = FakeBuf(64 * 8)
    p._meta_buf = FakeBuf(64 * 16)
    p._patch_rx_buf = FakeBuf(1024)
    p._tme_patch_buf = FakeBuf(4096)
    p._tme_templ_buf = FakeBuf(4096)
    return p


def test_start_requires_a_begun_stage():
    """_start() must refuse if the stage was never marked in-flight."""
    p = make_pipeline()
    try:
        p._start(p._binarize, "binarize_core")
    except AssertionError as e:
        assert "_begin_stage" in str(e)
    else:
        raise AssertionError("_start() accepted an unbegun stage")


def test_binarize_marks_inflight_before_arming():
    """A core that is not idle fails the run WITH the flag already set.

    This is the regression: the arm happens before ap_start, so if the flag
    only went up inside _start(), this path would leave an armed S2MM behind
    while close() still believed the buffers were free.
    """
    p = make_pipeline(core_idle=False)
    gray = np.zeros((16, 16), dtype=np.uint8)
    try:
        p.binarize_page(gray, 140)
    except RuntimeError as e:
        assert "not idle" in str(e), e
    else:
        raise AssertionError("expected the non-idle core to fail the run")

    assert p._dma_binarize.recvchannel.armed == 1, (
        "the S2MM was armed — that is the state this test is about")
    assert p._transfers_outstanding, (
        "FLAG NOT SET after arming failed mid-stage: close() would free "
        "buffers an armed DMA still targets")


def test_bad_input_does_not_poison_the_pipeline():
    """An ordinary ValueError must leave the pipeline usable.

    The flag goes up before the first mutation, not before validation — a
    caller passing a float page should not have to reload the overlay.
    """
    p = make_pipeline()
    for bad in (np.zeros((16, 16), dtype=np.float64),   # wrong dtype
                np.zeros((2, 2), dtype=np.uint8),       # below 3x3
                np.zeros((4, 4, 3), dtype=np.uint8)):   # not 2-D
        try:
            p.binarize_page(bad, 140)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad.shape}/{bad.dtype} should be rejected")
        assert not p._transfers_outstanding, (
            "a rejected input marked the pipeline in-flight; it never "
            "touched the hardware")
    # And a bad threshold, same rule.
    try:
        p.binarize_page(np.zeros((16, 16), dtype=np.uint8), 300)
    except ValueError:
        pass
    assert not p._transfers_outstanding
    assert p._dma_binarize.sendchannel.armed == 0, "nothing should be armed"


def test_outstanding_blocks_every_entry_point():
    """Once in-flight, every stage refuses until the overlay is reloaded."""
    p = make_pipeline()
    p._img_w, p._img_h, p._stride_bytes = 16, 16, 16
    p._bin_buf = FakeBuf(256)
    p._transfers_outstanding = True

    patch = np.zeros((8, 8), dtype=np.uint8)
    templ = np.zeros((4, 4), dtype=np.uint8)
    templ[0, 0] = 255                      # not flat, so §4.6 passes
    calls = [
        ("binarize_page", lambda: p.binarize_page(
            np.zeros((16, 16), dtype=np.uint8), 140)),
        ("suppress_text", lambda: p.suppress_text([])),
        ("extract_candidates", lambda: p.extract_candidates(
            [{"endpoint": (4, 4), "side": "left"}], {}, (1.0,))),
        ("match_template", lambda: p.match_template(patch, templ)),
        ("match_candidate", lambda: p.match_candidate(patch, 0, 0, [])),
    ]
    for name, fn in calls:
        try:
            fn()
        except RuntimeError as e:
            assert "reload the overlay" in str(e), f"{name}: {e}"
        else:
            raise AssertionError(f"{name} ran while a transfer was outstanding")


def test_match_paths_guard_before_staging_the_patch():
    """The guard must fire BEFORE _stage_patch writes the matcher buffer."""
    p = make_pipeline()
    p._transfers_outstanding = True
    patch = np.full((8, 8), 7, dtype=np.uint8)
    templ = np.zeros((4, 4), dtype=np.uint8)
    templ[0, 0] = 255

    for name, fn in (("match_template", lambda: p.match_template(patch, templ)),
                     ("match_candidate",
                      lambda: p.match_candidate(patch, 0, 0, []))):
        before = bytes(p._tme_patch_buf[:64])
        try:
            fn()
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"{name} ran while outstanding")
        assert bytes(p._tme_patch_buf[:64]) == before, (
            f"{name} wrote the patch buffer before its guard fired")
        assert p._staged_patch is None, f"{name} staged a patch anyway"


def test_illegal_trial_does_not_poison_the_pipeline():
    """A flat template (§4.6) is an input error, not a hardware failure."""
    p = make_pipeline()
    patch = np.zeros((8, 8), dtype=np.uint8)
    flat = np.zeros((4, 4), dtype=np.uint8)      # min == max: illegal
    try:
        p.match_template(patch, flat)
    except ValueError as e:
        assert "template" in str(e).lower(), e
    else:
        raise AssertionError("a flat template must be rejected (§4.6)")
    assert not p._transfers_outstanding, (
        "an illegal template poisoned the pipeline; nothing was armed")
    assert p._dma_patch.sendchannel.armed == 0


def test_reallocation_refused_while_outstanding():
    """_ensure_image_bufs must never free pages a DMA may still target."""
    p = make_pipeline()
    p._gray_buf = FakeBuf(16)
    p._bin_buf = FakeBuf(16 + d._OUTPUT_GUARD_BYTES)
    p._transfers_outstanding = True
    try:
        p._ensure_image_bufs(100, 100)
    except RuntimeError as e:
        assert "reload the overlay" in str(e), e
    else:
        raise AssertionError("reallocated while a transfer was outstanding")
    assert not p._gray_buf.freed and not p._bin_buf.freed, "buffers were freed"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:                          # noqa: BLE001
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
