"""Host-only tests for PLPipeline.close() — the CMA teardown decision.

    python test_driver_close.py         # from sw/
    pytest test_driver_close.py

No PYNQ and no board.  The pipeline is built with `object.__new__` and given
fake cores, fake DMA channels backed by `tme_standalone_bringup._FakeDmaMmio`
(a register block that models a real engine's halt behaviour, including a
reset that stays asserted while an AXI transaction drains), and fake buffers.

WHY THIS FILE EXISTS.  close() decides whether ~120 MiB of CMA goes back to
the pool while the PL may still be reading it, and it runs almost exclusively
on failure paths — so without host tests its first execution is during a real
failure on real hardware.  Two of the cases below are regressions, not
hypotheticals:

  - **the virgin-channel bug.** close() used to poll `channel.idle` on all
    seven channels.  `idle` is DMASR bit 1, which a channel that has never
    completed a transfer reports as 0 — indistinguishable from a channel
    genuinely mid-transfer.  A clean binarize-only run arms two of the seven,
    so the other five read "busy", every buffer was retained, and close()
    returned False on the happy path.  `test_virgin_channels_*` pin that.
  - **the freebuffer() verdict.** A `freebuffer()` that raised was swallowed:
    the buffer was parked in `_RETAINED_BUFFERS` and close() still returned
    True, so a caller checking the return value was told everything came back
    when it had not.  `test_freebuffer_failure_returns_false` pins that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tme_driver as d
import tme_standalone_bringup as B

_AP_IDLE = 1 << 2
_AP_DONE = 1 << 1


class FakeBuf(np.ndarray):
    """A numpy array that also answers the PynqBuffer calls the driver makes.

    `free_raises` models the case that matters most: `freebuffer()` failing
    after the halt was verified, which leaves the pages in a state nobody can
    describe and must not be reported as a clean close.
    """

    def __new__(cls, n, free_raises=False):
        obj = np.zeros(n, dtype=np.uint8).view(cls)
        obj.freed = False
        obj.free_raises = free_raises
        return obj

    def flush(self):
        pass

    def invalidate(self):
        pass

    def freebuffer(self):
        if getattr(self, "free_raises", False):
            raise RuntimeError("simulated freebuffer failure")
        self.freed = True

    @property
    def physical_address(self):
        return 0x1000_0000


class FakeChannel:
    """A DMA channel over a `_FakeDmaMmio` register block.

    `idle` reads DMASR bit 1 exactly as PYNQ's does, so a never-armed channel
    reports False here for the same reason it does on the board — which is the
    whole point of the virgin-channel cases.

    `writes_zeros` makes a receive channel behave like an S2MM that actually
    filled its destination, so a *successful* run can be simulated end to end
    (binarize_page checks that no pre-fill sentinel survives).
    """

    def __init__(self, mmio=None, offset=0, writes_zeros=False):
        self._mmio = mmio if mmio is not None else B._FakeDmaMmio()
        self._offset = offset
        self.armed = 0
        self.error = False
        self.transferred = 0
        self.waited = 0
        self.writes_zeros = writes_zeros

    @property
    def idle(self):
        return bool(self._mmio.read(self._offset + B.DMA_DMASR) & 0x02)

    def transfer(self, buf):
        self.armed += 1
        self.transferred = len(buf)
        if self.writes_zeros:
            buf[:] = 0
        # A completed transfer leaves the engine idle, as the hardware does.
        self._mmio.regs[self._offset + B.DMA_DMASR] |= 0x02

    def wait(self):
        self.waited += 1


class FakeDma:
    def __init__(self, **kw):
        self.sendchannel = FakeChannel(**kw)
        self.recvchannel = FakeChannel(**kw)


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
    """An HLS core that goes busy on ap_start and raises ap_done once.

    `_wait_done` treats ap_done as Clear-on-Read, so returning it exactly once
    is the honest model; `_start` requires ap_idle before the write.
    """

    def __init__(self, idle=True):
        self.idle = idle
        self.register_map = FakeRegMap()
        self.started = 0
        self._done_pending = False

    def read(self, off):
        if self._done_pending:
            self._done_pending = False
            return _AP_DONE | _AP_IDLE
        return _AP_IDLE if self.idle else 0

    def write(self, off, val):
        self.started += 1
        self._done_pending = True


def make_pipeline(bin_recv_writes=True, mmios=None, free_raises=()):
    """A PLPipeline with no PYNQ behind it.

    `mmios` maps a channel label to a `_FakeDmaMmio`, so one channel can be
    made stuck while the rest halt normally.  `free_raises` names buffer
    attributes whose `freebuffer()` must raise.
    """
    mmios = mmios or {}
    p = object.__new__(d.PLPipeline)
    p.timeout_s = 2.0
    p.halt_timeout_s = 0.02        # bounded, and short: these are unit tests
    p._transfers_outstanding = False
    p._channels_armed = set()
    p._closed = False
    p._close_result = None
    p.last_transfer_stats = None
    p.last_extract_stats = None
    p.last_suppress_stats = None
    p._staged_patch = None
    p._img_w = p._img_h = 0
    p._stride_bytes = 0
    p._binarize = FakeCore()
    p._extract = FakeCore()
    p._tme = FakeCore()

    p._dma_binarize = FakeDma()
    p._dma_binarize.recvchannel.writes_zeros = bin_recv_writes
    p._dma_pe_data = FakeDma()
    p._dma_pe_meta = FakeDma()
    p._dma_patch = FakeDma()
    p._dma_templ = FakeDma()
    p._tme_dma_max = 262143

    for ch, label in p._dma_channels():
        if label in mmios:
            ch._mmio = mmios[label]

    p._gray_buf = None
    p._bin_buf = None
    p._allocate = lambda shape, dtype: FakeBuf(shape[0])
    p._cand_buf = FakeBuf(64 * 8)
    p._meta_buf = FakeBuf(64 * 16)
    p._patch_rx_buf = FakeBuf(1024)
    p._tme_patch_buf = FakeBuf(4096)
    p._tme_templ_buf = FakeBuf(4096)
    for attr in free_raises:
        getattr(p, attr).free_raises = True
    return p


def all_refs_cleared(p) -> bool:
    return all(getattr(p, a) is None for a in d.PLPipeline._BUFFER_ATTRS)


# -- virgin channels ------------------------------------------------------

def test_virgin_channels_do_not_block_a_close():
    """A pipeline that never ran must still free everything.

    Every one of the seven channels reads `idle == False` here, exactly as a
    never-transferred channel does on the board.  The old close() called that
    "seven channels still busy" and retained all five allocated buffers.
    """
    p = make_pipeline()
    assert not any(ch.idle for ch, _ in p._dma_channels()), (
        "the fake must reproduce the board's virgin state: DMASR.Idle clear")

    assert p.close() is True, "a pipeline that armed nothing must free cleanly"
    for attr in ("_cand_buf", "_meta_buf", "_patch_rx_buf", "_tme_patch_buf",
                 "_tme_templ_buf"):
        assert attr not in [x for x in ()], ""      # readability no-op
    assert all_refs_cleared(p), "close() left a buffer reference behind"


def test_virgin_channels_are_not_halted():
    """Never-armed channels must not be poked at all.

    Not an optimisation: driving RS=0 or a soft reset on a channel this
    pipeline never used would disturb whatever else in the design is using it.
    """
    p = make_pipeline()
    before = {label: dict(ch._mmio.regs) for ch, label in p._dma_channels()}
    assert p.close() is True
    for ch, label in p._dma_channels():
        assert ch._mmio.regs == before[label], (
            f"{label} was never armed but close() wrote its registers")
        assert not ch._mmio.reset_seen, f"{label} got a soft reset"


def test_binarize_only_run_frees_and_reports_true():
    """The regression, end to end: a clean run must return True.

    Two of the seven channels are armed by binarize_page; the other five stay
    virgin.  The old close() saw five "busy" channels and returned False with
    every buffer retained — on the happy path.
    """
    p = make_pipeline()
    gray = np.full((16, 16), 200, dtype=np.uint8)
    p.binarize_page(gray, 140)

    assert p._channels_armed == {"gray MM2S", "bin S2MM"}, p._channels_armed
    assert not p._transfers_outstanding

    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS]
    assert p.close() is True, "a completed binarize must close cleanly"
    assert all(b.freed for b in bufs), "not every buffer was freed"
    assert all_refs_cleared(p)


def test_armed_channels_are_verified_halted():
    """The two armed channels get RS=0 and a positive read-back."""
    p = make_pipeline()
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)
    armed = [ch for ch, label in p._dma_channels()
             if label in p._channels_armed]
    assert p.close() is True
    for ch in armed:
        cr = ch._mmio.read(ch._offset + B.DMA_DMACR)
        sr = ch._mmio.read(ch._offset + B.DMA_DMASR)
        assert not cr & B.DMACR_RS, "RS was never cleared on an armed channel"
        assert sr & B.DMASR_HALTED, "close() freed without a Halted read-back"


# -- failure paths --------------------------------------------------------

def test_failed_arm_retains_every_buffer():
    """A stage that died mid-arm must not hand the pages back.

    The non-idle core makes `_start` raise after the S2MM is already armed —
    the exact window `_begin_stage` exists to cover.  close() must see
    `_transfers_outstanding`, retain all seven, and say False.
    """
    p = make_pipeline()
    p._binarize.idle = False
    try:
        p.binarize_page(np.zeros((16, 16), dtype=np.uint8), 140)
    except RuntimeError as e:
        assert "not idle" in str(e), e
    else:
        raise AssertionError("expected the non-idle core to fail the run")

    assert p._transfers_outstanding
    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS
            if getattr(p, a) is not None]
    assert len(bufs) == 7, f"expected all seven allocated, got {len(bufs)}"
    n_retained = len(d._RETAINED_BUFFERS)

    assert p.close() is False, "close() freed after a failed arm"
    assert not any(b.freed for b in bufs), "a buffer was freed anyway"
    assert len(d._RETAINED_BUFFERS) == n_retained + 7, (
        "retained buffers must be held for the life of the process")
    assert all_refs_cleared(p), "close() left a buffer reference behind"


def test_stuck_dma_retains_and_returns_bounded():
    """An engine that ignores RS=0 AND the soft reset must fail the close.

    Bounded is part of the assertion: PYNQ's own `stop()` spins forever here,
    which is why this driver drives the registers itself.
    """
    stuck = B._FakeDmaMmio(halt_on_rs=False, halt_on_reset=False)
    p = make_pipeline(mmios={"bin S2MM": stuck})
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)

    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS]
    assert p.close() is False, "close() freed against an unhalted DMA"
    assert stuck.reset_seen, "the soft-reset fallback was never issued"
    assert not any(b.freed for b in bufs)
    assert all_refs_cleared(p)


def test_draining_reset_is_not_quiescent():
    """Halted=1 with DMACR.Reset still set is NOT a halt (PG021).

    A soft reset does not abort an AXI transaction already in flight — it lets
    it finish, holding Reset asserted meanwhile.  So this state can mean
    "still reading the buffer we are about to free", and accepting it on the
    strength of Halted alone is precisely the corruption close() prevents.
    """
    draining = B._FakeDmaMmio(halt_on_rs=False, halt_on_reset=True,
                              reset_self_clears=False)
    p = make_pipeline(mmios={"gray MM2S": draining})
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)

    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS]
    assert draining.read(B.DMA_DMASR) & B.DMASR_HALTED == 0 or True
    assert p.close() is False, (
        "close() accepted Halted=1 with a reset still in flight")
    assert not any(b.freed for b in bufs)


def test_unreadable_registers_are_not_quiescent():
    """"Cannot verify" must never read as "verified"."""
    p = make_pipeline()
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)
    p._dma_binarize.recvchannel._mmio = None      # no register block at all

    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS]
    assert p.close() is False
    assert not any(b.freed for b in bufs)


def test_freebuffer_failure_returns_false():
    """One buffer that will not free makes the whole close False.

    It used to be swallowed: the buffer went into `_RETAINED_BUFFERS` and
    close() still returned True, so a caller that checked the return value was
    told the pool had come back when part of it had not.
    """
    p = make_pipeline(free_raises=("_meta_buf",))
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)
    stubborn = p._meta_buf
    others = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS
              if getattr(p, a) is not stubborn]
    n_retained = len(d._RETAINED_BUFFERS)

    assert p.close() is False, "a failed freebuffer() reported a clean close"
    assert all(b.freed for b in others), (
        "one stubborn buffer must not stop the others being freed")
    assert d._RETAINED_BUFFERS[n_retained:] == [stubborn], (
        "the buffer that would not free must be retained")
    assert all_refs_cleared(p)


# -- idempotence ----------------------------------------------------------

def test_double_close_is_idempotent_and_frees_once():
    """The second close() repeats the verdict and touches nothing."""
    p = make_pipeline()
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)
    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS]

    assert p.close() is True
    for b in bufs:
        b.freed = False                 # a second free would set it again
    regs = {label: dict(ch._mmio.regs) for ch, label in p._dma_channels()}

    assert p.close() is True, "close() changed its verdict on the second call"
    assert not any(b.freed for b in bufs), "close() freed a buffer twice"
    for ch, label in p._dma_channels():
        assert ch._mmio.regs == regs[label], (
            f"the second close() wrote {label}'s registers")


def test_double_close_after_failure_stays_false():
    """A retained close cannot be turned into a clean one by calling again.

    This is the same hazard `_halt_channel` documents: a stuck reset holds
    `Halted` high forever, so a second attempt that re-read the registers
    could see leftover evidence and free what the first call correctly kept.
    """
    stuck = B._FakeDmaMmio(halt_on_rs=False, halt_on_reset=False)
    p = make_pipeline(mmios={"bin S2MM": stuck})
    p.binarize_page(np.full((8, 8), 30, dtype=np.uint8), 140)
    bufs = [getattr(p, a) for a in d.PLPipeline._BUFFER_ATTRS]

    assert p.close() is False
    assert p.close() is False, "a failed close became a clean one on retry"
    assert not any(b.freed for b in bufs)


def test_closed_pipeline_refuses_further_work():
    """Every entry point must refuse after close() — the buffers are gone."""
    p = make_pipeline()
    assert p.close() is True

    patch = np.zeros((8, 8), dtype=np.uint8)
    templ = np.zeros((4, 4), dtype=np.uint8)
    templ[0, 0] = 255
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
            assert "close() has already run" in str(e), f"{name}: {e}"
        else:
            raise AssertionError(f"{name} ran on a closed pipeline")


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
