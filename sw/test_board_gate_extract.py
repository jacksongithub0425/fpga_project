"""Drive board_gate_extract's phases off the board, against fake silicon.

    python test_board_gate_extract.py       # from sw/
    pytest test_board_gate_extract.py

No PYNQ and no board.  A `PLPipeline` is built with `object.__new__` and given
fake cores and fake DMA channels whose "hardware" serves the golden vectors:
the binarize S2MM writes the golden binary page, the patch S2MM writes the
golden patch, and `tme_top_0`'s result registers answer from a lookup table
keyed by the template that was actually staged.

**Every line of driver code still runs for real** — `binarize_page`,
`extract_candidates`, `match_template` and the whole `match_candidate`
reduction, including the strict-`>` tie rule.  Only the four cores and the
five DMAs are simulated.

WHY.  `board_gate_extract.py` is the thing that decides whether the extractor
works on silicon, and without this its first execution would be on the board —
where a transposed reshape, a wrong blob offset or a mistyped record field
costs a board session and looks exactly like a hardware failure.  So the gate
runs here first, and then, because a suite whose cases all pass proves only
that it can pass, EVERY assertion the gate makes is deliberately broken in
turn and required to fail it:

    wrong binary byte, wrong patch byte, shifted record origin,
    a rejected candidate, a short patch DMA, an off-by-one match location,
    a box left in patch coordinates instead of page coordinates,
    and the tie going to the second trial instead of the first.

The box control is the sharpest of them: reporting local coordinates as page
coordinates is the exact defect the extractor->matcher seam suite exists for,
and it is invisible in any case whose patch starts at the origin.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_gate_extract as G
import tme_driver as d
import tme_standalone_bringup as B

_AP_IDLE = 1 << 2
_AP_DONE = 1 << 1


# -- fake hardware --------------------------------------------------------

class FakeBuf(np.ndarray):
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
    """A DMA channel with an optional payload writer.

    `payload(buf)` stands in for the S2MM actually receiving beats; without
    one the channel only records the length, which is all an MM2S does from
    this side.  `count` overrides the reported `transferred`, so a short or
    long DMA can be simulated without changing what was written.
    """

    def __init__(self, offset=0, payload=None):
        self._mmio = B._FakeDmaMmio(base=offset)
        self._offset = offset
        self.payload = payload
        self.count = None
        self.armed = 0
        self.error = False
        self.transferred = 0

    @property
    def idle(self):
        return bool(self._mmio.read(self._offset + B.DMA_DMASR) & 0x02)

    def transfer(self, buf):
        self.armed += 1
        if self.payload is not None:
            self.payload(buf)
        self.transferred = len(buf) if self.count is None else self.count
        self._mmio.regs[self._offset + B.DMA_DMASR] |= 0x02

    def wait(self):
        pass


class FakeDma:
    def __init__(self):
        self.sendchannel = FakeChannel()
        self.recvchannel = FakeChannel(offset=0x30)


class FakeRegMap:
    def __init__(self):
        object.__setattr__(self, "_v", {})

    def __setattr__(self, k, v):
        self._v[k] = v

    def __getattr__(self, k):
        return self._v.get(k, 0)


class MatcherRegMap(FakeRegMap):
    """tme_top_0's registers, answering from a template->result table.

    Keyed by the bytes actually staged plus the four geometry scalars, so a
    driver that sent the wrong template, or the right template with the wrong
    dimensions, gets a KeyError here instead of a plausible number — which is
    the only way a fake can avoid rubber-stamping the thing it is testing.
    """

    def __init__(self, pipe, table):
        super().__init__()
        object.__setattr__(self, "_pipe", pipe)
        object.__setattr__(self, "_table", table)

    def __getattr__(self, k):
        if k.startswith("result_"):
            if k.endswith("_ctrl"):
                return 1                       # ap_vld: this run wrote it
            pw = self._v.get("patch_w", 0)
            ph = self._v.get("patch_h", 0)
            tw = self._v.get("templ_w", 0)
            th = self._v.get("templ_h", 0)
            key = (bytes(self._pipe._tme_templ_buf[:tw * th]), pw, ph, tw, th)
            if key not in self._table:
                raise KeyError(
                    f"no golden result for a {tw}x{th} template against a "
                    f"{pw}x{ph} patch — the driver staged something the "
                    f"manifest does not describe")
            score, x, y = self._table[key]
            if k == "result_score":
                return struct.unpack("<I", struct.pack("<f", score))[0]
            return x if k == "result_x" else y
        return self._v.get(k, 0)


class FakeCore:
    def __init__(self, regmap=None):
        self.register_map = regmap if regmap is not None else FakeRegMap()
        self.started = 0
        self._done = False

    def read(self, off):
        if self._done:
            self._done = False
            return _AP_DONE | _AP_IDLE
        return _AP_IDLE

    def write(self, off, val):
        self.started += 1
        self._done = True


# -- the simulated board --------------------------------------------------

def make_fake_pipeline(g, cases, patches, templs, mutate=None):
    """A PLPipeline whose nine DMA channels and three cores are simulated.

    `mutate` names one deliberate defect; see `MUTATIONS`.
    """
    p = object.__new__(d.PLPipeline)
    p.timeout_s = 5.0
    p.halt_timeout_s = 0.02
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
    p._gray_buf = None
    p._bin_buf = None
    p._allocate = lambda shape, dtype: FakeBuf(shape[0])
    p._cand_buf = FakeBuf(64 * 8)
    p._meta_buf = FakeBuf(64 * 16)
    p._patch_rx_buf = FakeBuf(d._MAX_PATCH_BYTES)
    p._tme_patch_buf = FakeBuf(d._MAX_PATCH_BYTES)
    p._tme_templ_buf = FakeBuf(d._MAX_TEMPL_W * d._MAX_TEMPL_H)
    p._tme_dma_max = 262143

    # ---- binarize: the S2MM writes the golden page -----------------------
    page = g["bin"].ravel().copy()
    if mutate == "binary_byte":
        page[271] ^= 0xFF
    p._dma_binarize = FakeDma()
    p._dma_binarize.recvchannel.payload = lambda buf: buf.__setitem__(
        slice(None), page[:len(buf)])

    # ---- extract: metadata record + patch pixels -------------------------
    x0, y0 = (G.PATCH_X0, G.PATCH_Y0)
    if mutate == "record_origin":
        x0 += 1
    status = 0 if mutate == "rejected" else 1
    record = struct.pack("<HHHHHHI", 0, status, x0, y0,
                         G.PATCH_W, G.PATCH_H, 0)
    patch_px = g["patch"].ravel().copy()
    if mutate == "patch_byte":
        patch_px[100] ^= 0xFF

    p._dma_pe_data = FakeDma()
    p._dma_pe_meta = FakeDma()
    p._dma_pe_data.recvchannel.payload = lambda buf: buf.__setitem__(
        slice(0, len(patch_px)), patch_px)
    p._dma_pe_data.recvchannel.count = (
        G.PATCH_BYTES - 2 if mutate == "short_patch" else G.PATCH_BYTES)
    p._dma_pe_meta.recvchannel.payload = lambda buf: buf.__setitem__(
        slice(0, len(record)), np.frombuffer(record, dtype=np.uint8))

    p._dma_patch = FakeDma()
    p._dma_templ = FakeDma()

    p._binarize = FakeCore()
    extract_rm = FakeRegMap()
    extract_rm.sts_flags = 0
    extract_rm.sts_rejected = 1 if mutate == "rejected" else 0
    extract_rm.sts_processed = 1
    for n in ("sts_flags", "sts_rejected", "sts_processed"):
        setattr(extract_rm, n + "_ctrl", 1)
    p._extract = FakeCore(extract_rm)

    # ---- matcher: golden results, keyed by what was staged ---------------
    table = {}
    for c in cases:
        t = templs[c.templ_off:c.templ_off + c.templ_bytes]
        table[(t, c.pw, c.ph, c.tw, c.th)] = (c.score, c.x, c.y)
    alpha_loc, beta_loc = G.ALPHA_LOCAL, G.BETA_CROP
    if mutate == "match_location":
        alpha_loc = (alpha_loc[0] + 1, alpha_loc[1])
    table[(g["alpha"].tobytes(), G.PATCH_W, G.PATCH_H, 4, 4)] = (
        1.0, alpha_loc[0], alpha_loc[1])
    table[(g["beta"].tobytes(), G.PATCH_W, G.PATCH_H, 4, 4)] = (
        1.0, beta_loc[0], beta_loc[1])
    if mutate == "tie_to_second":
        # beta by a hair: enough to take `best` away from the first trial,
        # far inside SCORE_TOL so every per-kind score check still passes.
        table[(g["beta"].tobytes(), G.PATCH_W, G.PATCH_H, 4, 4)] = (
            1.0001, beta_loc[0], beta_loc[1])
    p._tme = FakeCore(MatcherRegMap(p, table))
    return p


MUTATIONS = {
    "binary_byte":    "one binary page byte differs from the golden",
    "patch_byte":     "one patch byte differs from the golden",
    "record_origin":  "the §6.2 record reports x0 one pixel to the right",
    "rejected":       "the core rejected the candidate (sts_rejected=1)",
    "short_patch":    "the patch S2MM moved 166 B, not 168",
    "match_location": "the matcher peak is one column off",
    "box_local":      "boxes are reported in patch, not page, coordinates",
    "tie_to_second":  "the tie is settled by the second trial, not the first",
}


def load_vectors():
    here = Path(__file__).resolve().parent
    g = G.load_bpe_golden(here)
    cases, patches, templs = G.load_hw_manifest(here)
    return g, cases, patches, templs


def run_gate(mutate=None, g=None, vectors=None):
    """Run all five phases against fake silicon.

    Returns (ok, report, error).  `ok` is False if any check failed OR if
    anything raised: some of the injected defects are caught by the DRIVER
    before the gate gets to assert on them — a record whose origin disagrees
    with `predict_patch_box` is model drift and `extract_candidates` refuses
    the whole batch — and a run that dies is a run that did not pass. The
    exception is returned rather than swallowed so the golden run can require
    that nothing raised at all.
    """
    g_, cases, patches, templs = vectors
    g = g or g_
    pl = make_fake_pipeline(g, cases, patches, templs, mutate)
    rep = G.Report()

    # The box control is a defect in the DRIVER's rebasing, not in the fake
    # hardware, so it is injected here rather than in the result table.
    if mutate == "box_local":
        real = pl.match_candidate

        def local_boxes(patch, x0, y0, trials, score_fn=None):
            return real(patch, 0, 0, trials, score_fn)
        pl.match_candidate = local_boxes

    err = None
    try:
        G.phase_a_binarize(pl, g, rep)
        rec = G.phase_b_extract(pl, g, rep)
        G.phase_c_matcher_suite(pl, Path(__file__).resolve().parent, rep)
        out = G.phase_d_match_candidate(pl, g, rep, g["patch"], "golden")
        G.phase_e_chain(pl, g, rep, rec["patch"], out)
    except Exception as exc:                            # noqa: BLE001
        err = exc
    finally:
        pl.close()
    return (err is None and not rep.failures), rep, err


# -- tests ----------------------------------------------------------------

def test_gate_passes_against_golden_hardware():
    """The whole gate, end to end, on hardware that behaves."""
    v = load_vectors()
    ok, rep, err = run_gate(None, vectors=v)
    assert err is None, f"the gate raised on golden data: {err!r}"
    assert ok, f"the gate failed on golden data: {rep.failures}"
    assert rep.checks >= 40, (
        f"only {rep.checks} checks ran; the gate is asserting less than it "
        f"claims to")


def test_every_assertion_can_fail():
    """Each injected defect must make the gate fail.

    Not a formality.  An assertion that cannot fail is decoration, and this is
    the only place where that can be established — on the board, every one of
    these would just be a passing run.
    """
    v = load_vectors()
    survived = []
    for name, what in MUTATIONS.items():
        ok, rep, err = run_gate(name, vectors=v)
        if ok:
            survived.append(f"{name} ({what})")
    assert not survived, (
        "the gate PASSED with these defects injected — it is not testing "
        "them: " + "; ".join(survived))


def test_close_after_a_full_run_frees_everything():
    """A gate run that passed must also leave the CMA pool clean."""
    v = load_vectors()
    g, cases, patches, templs = v
    pl = make_fake_pipeline(g, cases, patches, templs)
    rep = G.Report()
    G.phase_a_binarize(pl, g, rep)
    G.phase_b_extract(pl, g, rep)
    assert not rep.failures

    # binarize + extract arm five of the seven channels; the two matcher
    # MM2S channels stay virgin until phase C.
    assert pl._channels_armed == {"gray MM2S", "bin S2MM", "cand MM2S",
                                  "patch S2MM", "meta S2MM"}, pl._channels_armed
    bufs = [getattr(pl, a) for a in d.PLPipeline._BUFFER_ATTRS]
    assert pl.close() is True, "a clean gate run must free every buffer"
    assert all(b.freed for b in bufs)


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
