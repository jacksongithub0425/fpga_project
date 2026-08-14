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

import shutil
import signal
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_gate_extract as G
import safe_teardown as ST
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


# -- fixture verification -------------------------------------------------

def _staged_fixtures(tmp: Path) -> Path:
    """A flat copy of all eight vectors plus the hash record, as on the board."""
    here = Path(__file__).resolve().parent
    tmp.mkdir(parents=True, exist_ok=True)
    shutil.copy(here / G._HASH_RECORD, tmp / G._HASH_RECORD)
    for names, sub in ((G._BPE_FILES, "hls/integration"),
                       (G._HW_FILES, "hls/template_match")):
        src = G.resolve_dir(here, names, sub, "staging")
        for n in names:
            shutil.copy(src / n, tmp / n)
    return tmp


def test_fixtures_match_the_committed_record():
    """The vectors in the repo are the ones GATE4_VECTORS.sha256 names."""
    G.verify_fixtures(Path(__file__).resolve().parent, quiet=True)


def test_a_mismatched_fixture_is_fatal():
    """One flipped byte must stop the gate — in every vector, not just some.

    Checked per file rather than once: a verification loop that skipped the
    524 KB blob, or stopped at the first `.txt`, would still pass a single
    tampering test aimed at whichever file it did read.
    """
    with tempfile.TemporaryDirectory() as td:
        base = _staged_fixtures(Path(td) / "vec")
        for name in G._BPE_FILES + G._HW_FILES:
            target = base / name
            original = target.read_bytes()
            target.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])
            try:
                G.verify_fixtures(base, quiet=True)
            except G.SetupError as exc:
                assert name in str(exc), f"{name}: wrong file blamed: {exc}"
            else:
                raise AssertionError(f"a corrupted {name} was accepted")
            finally:
                target.write_bytes(original)
        G.verify_fixtures(base, quiet=True)      # restored: passes again


def test_a_missing_fixture_is_fatal():
    """A vector that is not there must stop the gate, naming it."""
    with tempfile.TemporaryDirectory() as td:
        base = _staged_fixtures(Path(td) / "vec")
        (base / "tb_tme_patches_hw.bin").unlink()
        try:
            G.verify_fixtures(base, quiet=True)
        except G.SetupError as exc:
            assert "tb_tme_patches_hw.bin" in str(exc), exc
        else:
            raise AssertionError("a missing vector was accepted")


def test_a_missing_hash_record_is_fatal():
    """Without the record there is no payload identity, so no run.

    In a checkout `read_hash_record` legitimately falls back to the copy
    beside the script, so this drives the board case directly: a directory
    with no record, and a script directory that has been made to look like it
    has none either.
    """
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "nothing"
        empty.mkdir()
        saved = G.__file__
        try:
            G.__file__ = str(empty / "board_gate_extract.py")
            try:
                G.read_hash_record(empty)
            except G.SetupError as exc:
                assert G._HASH_RECORD in str(exc), exc
            else:
                raise AssertionError("ran without a hash record")
        finally:
            G.__file__ = saved
    # And the real record is still found from the checkout.
    assert G.read_hash_record(Path(__file__).resolve().parent)


def test_partially_staged_directory_is_fatal():
    """Seven of eight staged must not silently read the eighth from the repo.

    That would run the gate against a mixture of two payloads — exactly the
    ambiguity the hash record exists to remove — and on a board where the
    repo happens to be checked out it would look like a clean pass.
    """
    with tempfile.TemporaryDirectory() as td:
        base = _staged_fixtures(Path(td) / "vec")
        (base / "tb_bpe_tme_patch.bin").unlink()
        try:
            G.verify_fixtures(base, quiet=True)
        except G.SetupError as exc:
            assert "mixture of two payloads" in str(exc), exc
            assert "tb_bpe_tme_patch.bin" in str(exc), exc
        else:
            raise AssertionError("a partially staged directory was accepted")


# -- the teardown scheme, against the REAL pipeline -----------------------
#
# safe_teardown's own suite (test_safe_teardown.py) covers the scheme against
# a fake pipeline: dead stdout, interrupted snapshots, signals, the hold loop.
# What can only be checked here is that it fits the pipeline the gate actually
# uses — `_BUFFER_ATTRS` is the contract between them, and a rename on either
# side would silently reduce the snapshot to nothing.

def test_the_snapshot_matches_the_real_pipeline_and_outlives_its_close():
    """Seven buffers, read from a real PLPipeline, still held after close().

    close() calls `_forget_buffers()` and nulls every attribute; from that
    moment the only thing between the CMA pages and `PynqBuffer.__del__` is
    the snapshot taken beforehand. If `_BUFFER_ATTRS` ever stopped naming what
    the driver allocates, this is where it shows up.
    """
    v = load_vectors()
    g, cases, patches, templs = v
    pl = make_fake_pipeline(g, cases, patches, templs)
    rep = G.Report()
    G.phase_a_binarize(pl, g, rep)
    G.phase_b_extract(pl, g, rep)
    assert not rep.failures

    held, complete = ST.snapshot_buffers(pl)
    assert complete is True, "the snapshot was incomplete on a healthy run"
    assert len(held) == 7, (
        f"{len(held)} of 7 DMA buffers were referenced before close(); the "
        f"rest could be collected while the fabric is still unknown")

    pl.close()
    assert all(getattr(pl, a) is None for a in d.PLPipeline._BUFFER_ATTRS), (
        "close() no longer forgets the buffers — this test's premise is stale")
    assert len(held) == 7 and all(b is not None for b in held)


# -- main(): the teardown paths end to end --------------------------------

class _FailStopReached(BaseException):
    """Raised by the stub standing in for `_fail_stop_holding`.

    Derived from BaseException, not Exception, for two reasons: the real
    function never returns, so a stub that returned would let `main()` carry on
    to `return status` and the test would assert the opposite of the property
    it exists to check; and it must not be absorbed by any `except Exception`
    on the way out.
    """


def run_main(vectors, close=None, reset_ok=True, overlay="fake.bit"):
    """Run `G.main()` end to end against fake silicon, recording teardown.

    Returns `(rc, rec, pipe)`.  `rc` is None when main() never returned, which
    is itself one of the required outcomes.  `rec` records what teardown saw:
    the overlay each `reset_pl`/`_fail_stop_holding` call was given, the seven
    buffers and the armed-channel set as of the moment close() was entered, and
    the buffers still held at the fail-stop.

    `close` replaces `PLPipeline.close` (to return False, or to raise) without
    disturbing the phases above it, which run for real either way.
    """
    g, cases, patches, templs = vectors
    pipe = make_fake_pipeline(g, cases, patches, templs)
    rec = {"reset": [], "fail_stop": [], "held": None,
           "bufs_at_close": None, "armed_at_close": None}

    real_close = pipe.close
    # Read off the real class now: `d.PLPipeline` is a factory below, and
    # wrapped_close runs while that substitution is in place.
    attrs = d.PLPipeline._BUFFER_ATTRS

    def wrapped_close():
        rec["bufs_at_close"] = [getattr(pipe, a) for a in attrs]
        rec["armed_at_close"] = set(pipe._channels_armed)
        return real_close() if close is None else close(pipe)
    pipe.close = wrapped_close

    def fake_reset(bitfile):
        rec["reset"].append(bitfile)
        return reset_ok

    def fake_fail_stop(bufs, bitfile):
        rec["fail_stop"].append(bitfile)
        rec["held"] = list(bufs)
        raise _FailStopReached(bitfile)

    here = str(Path(__file__).resolve().parent)
    present = [n for n in ST._TERMINATION_SIGNALS
               if getattr(signal, n, None) is not None]
    saved_sig = {n: signal.getsignal(getattr(signal, n)) for n in present}
    saved = (d.PLPipeline, ST.reset_pl, ST.fail_stop_holding, sys.argv)
    d.PLPipeline = lambda bitfile, timeout_s=None: pipe
    ST.reset_pl = fake_reset
    ST.fail_stop_holding = fake_fail_stop
    sys.argv = ["board_gate_extract.py", "--overlay", overlay,
                "--data-dir", here]
    try:
        rc = G.main()
    except _FailStopReached:
        rc = None
    finally:
        d.PLPipeline, ST.reset_pl, ST.fail_stop_holding, sys.argv = saved
        # teardown() ignores SIGINT/SIGTERM for the rest of the process:
        # correct in a gate, unwelcome in a test runner.
        for n, h in saved_sig.items():
            try:
                signal.signal(getattr(signal, n), h)
            except (ValueError, OSError):
                pass
    return rc, rec, pipe


def test_main_clean_run_exits_zero_and_leaves_the_pl_alone():
    """The happy path must not reset the PL — that is a failure signal."""
    rc, rec, _ = run_main(load_vectors())
    assert rc == 0, f"a clean gate run exited {rc}"
    assert rec["reset"] == [], (
        "the PL was reprogrammed after a clean close(); a reset is the "
        "recovery for an unsafe teardown, not part of a passing run")
    assert rec["fail_stop"] == []


def test_main_arms_and_closes_all_seven_channels():
    """Five phases touch every channel, and close() must free every buffer.

    Both halves matter.  Seven armed proves the gate exercises the whole
    overlay rather than the binarize corner; seven freed proves close()'s
    never-armed skip has not quietly become a never-free rule — the regression
    that emptied the CMA pool once already.
    """
    rc, rec, _ = run_main(load_vectors())
    assert rc == 0
    assert rec["armed_at_close"] == {
        "gray MM2S", "bin S2MM", "cand MM2S", "patch S2MM", "meta S2MM",
        "tme patch MM2S", "tme templ MM2S"}, rec["armed_at_close"]
    bufs = rec["bufs_at_close"]
    assert len(bufs) == 7 and all(b is not None for b in bufs)
    assert all(b.freed for b in bufs), (
        "a passing run left CMA buffers unfreed: "
        + ", ".join(a for a, b in zip(d.PLPipeline._BUFFER_ATTRS, bufs)
                    if not b.freed))


def test_main_resets_the_pl_once_when_close_cannot_free():
    """close() False -> exactly one reset, and the gate still FAILS.

    The reset makes the board recoverable; it does not make the run a pass.
    An unprovable halt is a failure whatever the phases said.
    """
    rc, rec, _ = run_main(load_vectors(), close=lambda p: False)
    assert rec["reset"] == ["fake.bit"], (
        f"expected exactly one PL reset, got {rec['reset']}")
    assert rec["fail_stop"] == [], "a successful reset must not fail-stop"
    assert rc == 1, f"an unsafe teardown exited {rc}, not 1"


def test_main_resets_the_pl_when_close_itself_raises():
    """A close() that dies is an unsafe teardown, not an unhandled error.

    The old code called `pl.close()` bare in the finally block: an exception
    there propagated out of main() with the buffers referenced by nothing, so
    the pages went back while the fabric was mid-transfer and no reset was
    even attempted.
    """
    def raising_close(pipe):
        raise RuntimeError("DMACR readback timed out")

    rc, rec, _ = run_main(load_vectors(), close=raising_close)
    assert rec["reset"] == ["fake.bit"], (
        "close() raised and the PL was never reset: "
        f"{rec['reset']}")
    assert rc == 1, f"a close() that raised exited {rc}, not 1"


def test_main_does_not_return_when_the_pl_reset_fails():
    """The double failure: close() False AND the reset failed.

    Nothing in-process can retire an outstanding DMA command now, so the pages
    must not go back at all — and `main()` returning is exactly how they would.
    The gate has to stop inside the fail-stop, still holding the pipeline and
    all seven buffers.
    """
    rc, rec, pl = run_main(load_vectors(), close=lambda p: False,
                           reset_ok=False)
    assert rc is None, (
        f"main() returned {rc} after a failed PL reset — the process would "
        f"exit and release its CMA pages with the fabric unknown")
    assert rec["fail_stop"] == ["fake.bit"], rec["fail_stop"]
    held, at_close = rec["held"], rec["bufs_at_close"]
    # The pipeline first, then its seven buffers. `pl` is held as well as the
    # buffers so that a snapshot which could not read every attribute still
    # leaves every page reachable through the pipeline that owns them.
    assert held[0] is pl, "the pipeline itself was not held"
    assert len(held) == 8, (
        f"pipeline + 7 buffers expected, {len(held)} held")
    assert all(h is b for h, b in zip(held[1:], at_close)), (
        "the held references are not the buffers close() was handed")
    assert not any(b.freed for b in held[1:]), (
        "a buffer was freed on the path that must free nothing")


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
