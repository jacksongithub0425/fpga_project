"""Drive board_gate_protocol's phases off the board, against fake silicon.

    python test_board_gate_protocol.py       # from sw/
    pytest test_board_gate_protocol.py

No PYNQ and no board.  Same construction as `test_board_gate_extract.py` — a
`PLPipeline` built with `object.__new__`, given fake cores and fake DMA
channels — and it reuses that file's `FakeBuf`, `FakeCore`, `FakeRegMap` and
`MatcherRegMap` rather than growing a second set.

**Every line of driver code still runs for real**, including the new metadata
framing check and the whole `match_candidate` reduction.  Only the cores and
the DMAs are simulated.

THE FAKE EXTRACTOR IS A MODEL, NOT A SCRIPT.  Gate 5 dispatches five different
batches (4, 4, 1, 2 and 2 descriptors, one of them permuted and one of them a
repeat of a single descriptor), so a fake that replayed a fixed answer would
prove nothing about any of them.  Instead `FakeExtractor` DECODES the packed
descriptor words the driver actually wrote into `_cand_buf`, looks each one up
in the golden manifest, and emits the records and patches those descriptors
call for — in that order, at those lengths.  A driver that packed the wrong
descriptor, ordered the batch wrongly or set `num_cands` inconsistently gets a
KeyError from the model instead of a plausible answer.

WHY.  Gate 5's first execution would otherwise be on the board, where a
mis-decoded record field or an off-by-one in the batch bookkeeping costs a
session and looks exactly like a hardware failure.  So the gate runs here
first — and then, because a suite whose cases all pass proves only that it can
pass, every assertion it makes is deliberately broken in turn and required to
fail it.  The mutations are listed in MUTATIONS, and four of them target
behaviour that gate 4 could not reach at n = 1:

    meta_short / meta_long      the batch TLAST lands early or late, so the
                                metadata S2MM's received count disagrees with
                                n x 16 — the check that did not exist before
    ignore_num_cands            the core keeps the previous batch's count and
                                emits four records for a one-descriptor batch
    stale_after_first           the extractor serves the FIRST batch's patches
                                to every batch after it
    patch_stale_length          the patch receive re-arms at the previous
                                candidate's length
    record_order                records come back permuted
    bykind_first                the per-kind reduction keeps the first trial of
                                a kind instead of its argmax — the exact defect
                                gate 4's phase D documented as untestable
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_gate_protocol as P
import tme_driver as d
import tme_standalone_bringup as B
from test_board_gate_extract import FakeBuf, FakeCore, FakeRegMap, MatcherRegMap


# -- fake hardware ---------------------------------------------------------

class BatchChannel:
    """A DMA channel whose payload may vary per arm within a batch.

    `payload(channel, buf)` receives the channel itself so it can set `count`
    — the reported `transferred` — before `transfer` latches it.  That is what
    lets one channel serve four patches of four different lengths, which is the
    property gate 5 exists to test and the reason gate 4's simpler
    `FakeChannel` could not be reused here.
    """

    def __init__(self, payload=None, offset=0):
        # Same register model as gate 4's FakeChannel, and it has to be: the
        # driver's teardown verifies quiescence by driving DMACR and reading
        # DMASR back, so a channel without registers is a channel `close()`
        # cannot prove safe — and it would retain every buffer rather than
        # free it.
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
            self.payload(self, buf)
        self.transferred = len(buf) if self.count is None else self.count
        self._mmio.regs[self._offset + B.DMA_DMASR] |= 0x02

    def wait(self):
        pass


class BatchDma:
    def __init__(self, send=None, recv=None):
        self.sendchannel = BatchChannel(send)
        self.recvchannel = BatchChannel(recv, offset=0x30)


def unpack_candidate(word: int) -> tuple:
    """(ep_x, ep_y, side_code, max_tw, max_th) — the inverse of pack_candidate.

    Written out here rather than imported so the model decodes independently:
    a fake that used the packer to read the packer would agree with itself.
    """
    return (word & 0xFFFF, (word >> 16) & 0xFFFF, (word >> 32) & 0x3,
            (word >> 34) & 0x3FFF, (word >> 48) & 0xFFFF)


class FakeExtractor:
    """patch_extract_core, modelled from the descriptors the driver wrote."""

    def __init__(self, pipe, g, mutate=None):
        self.pipe = pipe
        self.g = g
        self.mutate = mutate
        self.batches = 0
        self.queue = []
        self.first_batch = None

    def _lookup(self, packed):
        ep_x, ep_y, side, tw, th = unpack_candidate(packed)
        for c in self.g["cands"]:
            if (c["ep_x"], c["ep_y"], c["side_code"]) == (ep_x, ep_y, side):
                if (tw, th) != (P.MAX_TW, P.MAX_TH):
                    raise KeyError(
                        f"descriptor for ({ep_x},{ep_y}) carries envelope "
                        f"{tw}x{th}, the manifest says "
                        f"{P.MAX_TW}x{P.MAX_TH}")
                return c
        raise KeyError(
            f"the driver dispatched a descriptor the manifest does not "
            f"describe: ep({ep_x},{ep_y}) side {side}")

    def meta_payload(self, chan, buf):
        """One record per descriptor, batch TLAST at the end.

        Runs before any patch receive is armed (the driver arms the metadata
        S2MM first), which is what makes this the right place to decode the
        batch and stage the patch queue.
        """
        n = int(self.pipe._extract.register_map.num_cands)
        words = np.frombuffer(bytes(self.pipe._cand_buf[:n * 8]),
                              dtype="<u8")
        cands = [self._lookup(int(w)) for w in words]

        if self.mutate == "ignore_num_cands":
            cands = list(self.g["cands"])          # the PREVIOUS batch's count
        if self.mutate == "record_order" and len(cands) >= 3:
            cands = cands[:1] + [cands[2], cands[1]] + cands[3:]
        if self.mutate == "stale_after_first" and self.first_batch is not None:
            cands = list(self.first_batch)

        self.batches += 1
        if self.first_batch is None:
            self.first_batch = list(cands)

        blob = bytearray()
        for c in cands:
            # §6.2: valid, then geometry.  Layout mirrors the record the
            # driver's unpack_patch_metadata reads.
            blob += struct.pack("<HHHHHHI", 0, 1, c["x0"], c["y0"],
                                c["pw"], c["ph"], 0)
        buf[:len(blob)] = np.frombuffer(bytes(blob), dtype=np.uint8)

        got = len(blob)
        if self.mutate == "meta_short":
            got -= P.META_STRUCT_SIZE
        elif self.mutate == "meta_long":
            got += P.META_STRUCT_SIZE
        chan.count = got

        self.queue = [(c["patch"].ravel(), c["pw"] * c["ph"]) for c in cands]
        if self.mutate == "sts_processed":
            self.pipe._extract.register_map.sts_processed = P.N_CANDS
        else:
            self.pipe._extract.register_map.sts_processed = n

    def patch_payload(self, chan, buf):
        """One patch per arm, ended by that patch's own TLAST."""
        if not self.queue:
            raise KeyError("the driver armed a patch receive with no patch "
                           "left in the batch")
        px, nbytes = self.queue.pop(0)
        if self.mutate == "patch_byte" and len(self.queue) == 1:
            px = px.copy()
            px[17] ^= 0xFF
        buf[:len(px)] = px
        if self.mutate == "patch_stale_length" and getattr(self, "_prev", None):
            chan.count = self._prev                 # the PREVIOUS length
        else:
            chan.count = nbytes
        self._prev = nbytes


def make_fake_pipeline(g, mutate=None):
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

    page = g["bin"].ravel().copy()
    if mutate == "binary_byte":
        page[3271] ^= 0xFF
    p._dma_binarize = BatchDma(
        recv=lambda chan, buf: buf.__setitem__(slice(None), page[:len(buf)]))

    ext = FakeExtractor(p, g, mutate)
    p._dma_pe_data = BatchDma(recv=ext.patch_payload)
    p._dma_pe_meta = BatchDma(recv=ext.meta_payload)
    p._dma_patch = BatchDma()
    p._dma_templ = BatchDma()

    p._binarize = FakeCore()
    extract_rm = FakeRegMap()
    extract_rm.sts_flags = 0
    extract_rm.sts_rejected = 0
    extract_rm.sts_processed = 0
    for n in ("sts_flags", "sts_rejected", "sts_processed"):
        setattr(extract_rm, n + "_ctrl", 1)
    p._extract = FakeCore(extract_rm)
    p._extractor_model = ext

    # ---- matcher: golden results keyed by what was actually staged --------
    c0 = g["cands"][0]
    table = {}
    for t in g["templs"]:
        loc = (t["ux"], t["uy"])
        score = t["score"]
        if mutate == "match_location" and t["index"] == P.BY_KIND["alpha"]:
            loc = (loc[0] + 1, loc[1])
        if mutate == "tie_to_second" and t["index"] == P.BY_KIND["beta"]:
            # beta by a hair: enough to take `best` from the first trial, far
            # inside SCORE_TOL so every per-kind score check still passes.
            score = t["score"] + 0.0001
        table[(t["pixels"].tobytes(), c0["pw"], c0["ph"], t["tw"], t["th"])] = (
            score, loc[0], loc[1])
    p._tme = FakeCore(MatcherRegMap(p, table))
    return p


MUTATIONS = {
    "binary_byte":        "one binary page byte differs from the golden",
    "patch_byte":         "one byte of a mid-batch patch differs",
    "record_order":       "records come back permuted, not in descriptor order",
    "meta_short":         "the batch TLAST lands one record early",
    "meta_long":          "the metadata S2MM receives one record too many",
    "ignore_num_cands":   "the core emits 4 records whatever num_cands says",
    "stale_after_first":  "every batch after the first replays the first",
    "patch_stale_length": "the patch receive re-arms at the previous length",
    "sts_processed":      "sts_processed reports 4 for every batch",
    "match_location":     "the winning alpha peak is one column off",
    "box_local":          "boxes are in patch, not page, coordinates",
    "tie_to_second":      "the global tie goes to the second trial",
    "bykind_first":       "by_kind keeps the first trial of a kind, not the "
                          "argmax",
}


def _degrade_bykind(pl):
    """Replace the per-kind argmax with 'keep the first trial of each kind'.

    A defect in the DRIVER's reduction rather than in the fake hardware, so it
    is injected here.  This is the one gate 4 named and could not test: with
    one trial per kind, keeping the first and taking the argmax are the same
    operation.
    """
    real = pl.match_candidate

    def first_wins(patch, x0, y0, trials, score_fn=None):
        out = real(patch, x0, y0, trials, score_fn)
        by = {}
        for t in trials:
            if not t["legal"] or t["kind"] in by:
                continue
            one = real(patch, x0, y0, [t], score_fn)
            if one["best"] is not None:
                by[t["kind"]] = one["best"]
        out["by_kind"] = by
        return out
    pl.match_candidate = first_wins


def run_gate(mutate=None, g=None):
    """Run all five phases against fake silicon.

    Returns (ok, report, error).  `ok` is False if any check failed OR if
    anything raised: several injected defects are caught by the DRIVER before
    the gate can assert on them — a short metadata transfer now raises inside
    `extract_candidates`, and a permuted record disagrees with
    `predict_patch_box` and is refused as model drift — and a run that dies is
    a run that did not pass.  The exception is returned rather than swallowed
    so the golden run can require that nothing raised at all.
    """
    g = g or P.load_golden(Path(__file__).resolve().parent)
    pl = make_fake_pipeline(g, mutate)
    rep = P.Report()

    if mutate == "box_local":
        real = pl.match_candidate

        def local_boxes(patch, x0, y0, trials, score_fn=None):
            return real(patch, 0, 0, trials, score_fn)
        pl.match_candidate = local_boxes
    elif mutate == "bykind_first":
        _degrade_bykind(pl)

    err = None
    try:
        P.phase_a_binarize(pl, g, rep)
        recs = P.phase_b_batch(pl, g, rep)
        P.phase_c_reentry(pl, g, rep, recs)
        out = P.phase_d_reduction(pl, g, rep, g["cands"][0]["patch"], "golden")
        P.phase_e_chain(pl, g, rep, recs[0]["patch"], out)
    except Exception as exc:                            # noqa: BLE001
        err = exc
    finally:
        pl.close()
    return (err is None and not rep.failures), rep, err


# -- tests -----------------------------------------------------------------

def test_selftest_passes():
    """The off-board self-test must pass on the committed vectors."""
    assert P.selftest(Path(__file__).resolve().parent) == 0


def test_gate_passes_against_golden_hardware():
    """The whole gate, end to end, on hardware that behaves."""
    ok, rep, err = run_gate(None)
    assert err is None, f"the gate raised on golden data: {err!r}"
    assert ok, f"the gate failed on golden data: {rep.failures}"
    assert rep.checks >= 60, (
        f"only {rep.checks} checks ran; the gate is asserting less than it "
        f"claims to")


def test_five_batches_actually_ran():
    """The batch sequence must be 4, 4, 1, 2, 2 — not five identical runs."""
    g = P.load_golden(Path(__file__).resolve().parent)
    pl = make_fake_pipeline(g)
    rep = P.Report()
    P.phase_a_binarize(pl, g, rep)
    recs = P.phase_b_batch(pl, g, rep)
    P.phase_c_reentry(pl, g, rep, recs)
    assert not rep.failures, rep.failures
    assert pl._extractor_model.batches == 5, (
        f"{pl._extractor_model.batches} batches reached the extractor, "
        f"expected 5")
    # 4+4+1+2+2 = 13 patch receives, each its own S2MM transfer.
    assert pl._dma_pe_data.recvchannel.armed == 13, (
        f"{pl._dma_pe_data.recvchannel.armed} patch receives were armed, "
        f"expected 13 — the per-patch TLAST framing is not being exercised")
    assert pl._dma_pe_meta.recvchannel.armed == 5, (
        f"{pl._dma_pe_meta.recvchannel.armed} metadata receives were armed, "
        f"expected one per batch")


def test_measured_metadata_count_is_checked():
    """The new driver check must be the thing that catches a short batch TLAST.

    Asserted at the driver level as well as through the gate: this is the one
    check added to `tme_driver` for gate 5, and a gate failure alone would not
    distinguish "the driver caught it" from "the gate noticed afterwards".
    """
    g = P.load_golden(Path(__file__).resolve().parent)
    pl = make_fake_pipeline(g, "meta_short")
    pl.binarize_page(g["gray"], P.THRESHOLD)
    cands = [{"endpoint": (c["ep_x"], c["ep_y"]),
              "side": "left" if c["side_code"] == 0 else "right"}
             for c in g["cands"]]
    st = {"left": {"a": [np.zeros((P.MAX_TH, P.MAX_TW), np.uint8)]},
          "right": {"a": [np.zeros((P.MAX_TH, P.MAX_TW), np.uint8)]}}
    try:
        pl.extract_candidates(cands, st, (1.0,))
    except RuntimeError as exc:
        assert "meta S2MM received" in str(exc), str(exc)
    else:
        raise AssertionError(
            "extract_candidates accepted a batch whose metadata S2MM received "
            "one record less than n x 16 — the §5 batch-TLAST framing is "
            "unchecked")
    finally:
        pl.close()


def test_every_assertion_can_fail():
    """Each injected defect must make the gate fail.

    An assertion that cannot fail is decoration, and this is the only place
    where that can be established — on the board every one of these would just
    be a passing run.
    """
    g = P.load_golden(Path(__file__).resolve().parent)
    survived = []
    for name, what in MUTATIONS.items():
        ok, rep, err = run_gate(name, g=g)
        if ok:
            survived.append(f"{name} ({what})")
    assert not survived, (
        "the gate PASSED with these defects injected — it is not testing "
        "them: " + "; ".join(survived))


def test_close_after_a_full_run_frees_everything():
    """A gate run that passed must also leave the CMA pool clean."""
    g = P.load_golden(Path(__file__).resolve().parent)
    pl = make_fake_pipeline(g)
    rep = P.Report()
    P.phase_a_binarize(pl, g, rep)
    P.phase_b_batch(pl, g, rep)
    assert not rep.failures, rep.failures
    bufs = [getattr(pl, a) for a in d.PLPipeline._BUFFER_ATTRS]
    assert pl.close() is True, "a clean gate run must free every buffer"
    assert all(b.freed for b in bufs)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except Exception as exc:                        # noqa: BLE001
            print(f"  [FAIL] {t.__name__}: {type(exc).__name__}: {exc}")
            failed.append(t.__name__)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
