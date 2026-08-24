"""Drive board_gate_recovery's phases off the board, against fake silicon.

    python test_board_gate_recovery.py       # from sw/
    pytest test_board_gate_recovery.py

No PYNQ, no board, and — as in `test_board_gate_clock.py` — no real seconds:
`tme_driver.time` and `board_gate_recovery.time` are both replaced by one
virtual clock, so a 0.5-second deadline expiring on a 2.57-second transaction
costs this suite nothing.

WHY THIS SUITE MATTERS MORE THAN MOST.  Gate 7 is the only gate that
deliberately breaks the board, and it is the only one whose failure mode
includes leaving CMA pages in the pool while a DMA still has a command against
them.  Its first execution must not be on hardware.

THE FAKE MODELS THE ONE PROPERTY THE GATE TURNS ON.  `RecoveryCore` completes
an invocation only if the patch MM2S was armed since it was started; started
with nothing feeding it, it stays busy forever.  That is `tme_top`'s actual
behaviour — it reads patch_w*patch_h beats by construction and blocks on the
first one — and it means the wedge in phase R5 is produced by the same
mechanism here as on silicon, not scripted.  It also means the follow-up
matters: R5's small probe arms 3,072 beats at a core waiting for 251,740, so
the core stays wedged and `_start`'s idle guard is what has to catch it.

Nine injected defects follow the clean run, each required to fail the gate.
Four of them are the ones with no other test anywhere:

    no_timeout        the driver never enforces its deadline
    latch_open        the poison latch is open, so a second set of transfers
                      is armed alongside the stale ones
    close_frees       close() hands the CMA pages back while a transfer is
                      outstanding — the exact corruption _RETAINED_BUFFERS
                      exists to prevent
    starts_on_busy    _start's ap_ctrl_hs idle check is skipped, so a new
                      invocation is written to a busy core and a plausible
                      result comes back computed from the wrong pixels
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_gate_clock as C
import board_gate_extract as G
import board_gate_recovery as R
import safe_teardown
import tme_driver as d
from test_board_gate_clock import VClock
from test_board_gate_extract import FakeCore
import test_board_gate_extract as TG


HERE = Path(__file__).resolve().parent


# -- fake silicon ----------------------------------------------------------

class RecoveryCore(FakeCore):
    """A matcher that completes only when something is actually feeding it.

    `write` is ap_start.  If the patch MM2S has been armed since the last
    start, the invocation is scheduled to finish `cycles/f` virtual seconds
    later; if it has NOT — which is exactly what phase R5 does on purpose —
    the core goes busy and never comes back, because the beats it is blocked
    on will never arrive.

    `read` is the ap_ctrl poll, and it models the clear-on-read `ap_done`: the
    poll that observes completion consumes it.
    """

    def __init__(self, regmap, vc: VClock, pipe, f_mhz=100.0,
                 overhead_s=0.002, law="B2", read_cost_s=0.0,
                 unfed_completes=False):
        super().__init__(regmap)
        self.vc = vc
        self._pipe = pipe
        self.f_mhz = f_mhz
        self.overhead_s = overhead_s
        self.law = law
        self.read_cost_s = read_cost_s
        # The `wedge_completes` defect: a core that reports done without ever
        # consuming its input stream.  It has to finish AT ONCE, not merely
        # eventually — a core that took its full modelled time would still
        # read busy across R5's observation window and the check would be
        # right to pass it.
        self.unfed_completes = unfed_completes
        self._seen_armed = 0
        self._finish_at = None          # None = idle, inf = wedged
        self.starts = 0

    def _fed(self) -> bool:
        ch = self._pipe._dma_patch.sendchannel
        if ch.armed > self._seen_armed:
            self._seen_armed = ch.armed
            return True
        return False

    def write(self, off, val):
        rm = self.register_map
        n = C.cycles(int(rm.patch_w), int(rm.patch_h),
                     int(rm.templ_w), int(rm.templ_h), self.law)
        self.vc.advance(self.overhead_s)
        self.starts += 1
        if self._fed():
            self._finish_at = self.vc.t + n / (self.f_mhz * 1e6)
        elif self.unfed_completes:
            self._finish_at = self.vc.t
        else:
            self._finish_at = math.inf

    def read(self, off):
        if self.read_cost_s:
            self.vc.advance(self.read_cost_s)
        if self._finish_at is None:
            return R.AP_IDLE
        if self.vc.t >= self._finish_at:
            self._finish_at = None
            return R.AP_DONE | R.AP_IDLE
        return 0                         # busy: neither idle nor done


class HeldForever(Exception):
    """What the injected hold_fn raises instead of never returning.

    `safe_teardown.fail_stop_holding` sleeps forever on purpose, so it
    cannot be called from a test suite.  Raising in its place keeps the
    one property under test -- control does NOT come back to the gate --
    while letting the suite finish.
    """


class Scenario:
    """One run of the whole gate, with at most one deliberate defect."""

    def __init__(self, vectors, mutate=None, f_mhz=100.0):
        self.g, self.cases, self.patches, self.templs = vectors
        self.mutate = mutate
        self.f_mhz = f_mhz
        self.vc = VClock()
        self.resets = 0            # reprogram ATTEMPTS
        self.reprograms = 0        # reprograms that actually SUCCEEDED
        self.pipelines = []
        self.holds = []
        self._corrupt = False

    # -- the two things run_all needs ------------------------------------
    def make_pipeline(self, timeout_s: float):
        p = TG.make_fake_pipeline(self.g, self.cases, self.patches,
                                  self.templs)
        p.timeout_s = float(timeout_s)
        core = RecoveryCore(
            p._tme.register_map, self.vc, p, self.f_mhz,
            read_cost_s=1.0 if self.mutate == "late_timeout" else 0.0,
            unfed_completes=self.mutate == "wedge_completes")
        p._tme = core

        if self.mutate == "latch_open" and timeout_s < 1.0:
            p._require_usable = lambda what: None
        if self.mutate == "close_frees" and timeout_s < 1.0:
            p.close = lambda: True
        if self.mutate == "retain_open_after_wedge" and self.resets >= 1:
            p.close = lambda: True
        if self.mutate == "stale_after_reset" and self._corrupt:
            self._break_table(p)
        if self.mutate == "r4_timeout" and len(self.pipelines) == 2:
            # The third pipeline is R4's (R0, R1-R3, R4, R5, R5-after).  A
            # deadline this short expires inside the very first case, so R4
            # aborts with a TimeoutError and a pipeline whose transfers are
            # still marked outstanding -- the state `_discard` cannot make
            # safe on its own.
            p.timeout_s = 1e-9
        self._watch_close(p)
        self.pipelines.append(p)
        return p

    def _watch_close(self, p):
        """Record every close()'s verdict and when it happened.

        Wrapped AFTER the defect injections above, so a mutated `close` is
        watched too -- the mutations that matter here are the ones that make
        close() lie about freeing.

        `_dirty_after` is the reprogram count at the last moment this pipeline
        was known to be holding pages: its creation, or its most recent
        refusing close().  A later successful reprogram is what makes those
        pages safe to drop, so the invariant compares against this.
        """
        real_close = p.close
        p._close_verdict = None            # None = close() was never called
        p._dirty_after = self.reprograms

        def close(_p=p, _real=real_close):
            freed = _real()
            _p._close_verdict = bool(freed)
            if not freed:
                _p._dirty_after = self.reprograms
            return freed

        p.close = close

    def unprotected(self):
        """Pipelines left holding CMA pages that nothing protects.

        A pipeline is safe if its `close()` proved the DMAs halted and freed
        the pages (verdict True).  Otherwise -- a refusing close(), or no
        close() at all -- the pages are retained, and only two things make
        dropping the reference safe: a PL reprogram that SUCCEEDED after the
        pipeline was last dirty, or a fail-stop hold that keeps them
        reachable and never returns.

        Attempts do not count; `reset_fails` calls reset_fn and gets False,
        which leaves the fabric exactly as unsafe as before it was asked.
        """
        held = {id(o) for objs, _bit in self.holds for o in objs}
        bad = []
        for pl in self.pipelines:
            if pl._close_verdict is True:
                continue
            if id(pl) in held:
                continue
            if self.reprograms > pl._dirty_after:
                continue
            bad.append(pl)
        return bad

    def reset_fn(self, bitfile: str) -> bool:
        self.resets += 1
        if self.mutate == "stale_after_reset":
            self._corrupt = True
        if self.mutate == "reset_fails_late":
            ok = self.resets < 2         # R3 recovers, R5 does not
        else:
            ok = self.mutate != "reset_fails"
        if ok:
            self.reprograms += 1
        return ok

    def hold_fn(self, held, bitfile: str):
        self.holds.append((list(held), bitfile))
        raise HeldForever(f"holding {len(held)} object(s)")

    # -- defect helper ----------------------------------------------------
    def _break_table(self, p):
        """A fabric that came back with stale state: one wrong location."""
        tbl = p._tme.register_map._table
        c = C.pick_probe(self.cases, C.SHORT_GEOM, "short")
        key = (self.templs[c.templ_off:c.templ_off + c.templ_bytes],
               c.pw, c.ph, c.tw, c.th)
        score, x, y = tbl[key]
        tbl[key] = (score, x, y - 1)


class _Swapped:
    """One virtual clock, standing in for `time` in both modules."""

    def __init__(self, vc):
        self.vc = vc

    def __enter__(self):
        self.old_d, self.old_r = d.time, R.time
        d.time = self.vc
        R.time = self.vc
        return self.vc

    def __exit__(self, *exc):
        d.time = self.old_d
        R.time = self.old_r
        return False


_REAL_START = d.PLPipeline._start
_REAL_WAIT_DONE = d.PLPipeline._wait_done


def _unbounded_wait_done(self, core, deadline, label, channels=()):
    """`_wait_done` with the deadline check DELETED — the `no_timeout` defect.

    Bounded by an iteration cap rather than by time, so a defect in the fake
    cannot hang the suite; the cap is far beyond any transaction here.
    """
    seen_busy = False
    for _ in range(10_000_000):
        ctrl = core.read(d._AP_CTRL_OFF)
        if ctrl & d._AP_DONE:
            return
        if not ctrl & d._AP_IDLE:
            seen_busy = True
        elif seen_busy:
            return
        d.time.sleep(0.0005)
    raise AssertionError("the unbounded wait never finished")


def run_gate(mutate=None, vectors=None, f_mhz=100.0):
    """Run every phase against fake silicon.  Returns (ok, report, error)."""
    sc = Scenario(vectors or load_vectors(), mutate, f_mhz)
    rep = G.Report()
    err = None

    if mutate == "starts_on_busy":
        def _no_guard(self, core, label):
            core.write(R.AP_CTRL_OFF, R.AP_START)
        d.PLPipeline._start = _no_guard
    if mutate == "r5_wrong_error":
        def _reworded(self, core, label):
            # The guard still fires -- only the message changes, so R5's
            # `"not idle" in msg` check fails and the phase ABORTS with the
            # pipeline poisoned and the core still wedged.
            if not self._transfers_outstanding:
                raise AssertionError(f"{label}: no _begin_stage()")
            ctrl = core.read(d._AP_CTRL_OFF)
            if not ctrl & d._AP_IDLE:
                raise RuntimeError(f"{label}: core busy (0x{ctrl:08X})")
            core.write(d._AP_CTRL_OFF, d._AP_START)
        d.PLPipeline._start = _reworded
    if mutate == "no_timeout":
        d.PLPipeline._wait_done = _unbounded_wait_done
    try:
        with _Swapped(sc.vc):
            try:
                R.run_all(sc.make_pipeline, "fake.bit", sc.cases, sc.patches,
                          sc.templs, rep, reset_fn=sc.reset_fn,
                          hold_fn=sc.hold_fn,
                          deadline_s=R.DEADLINE_S, natural_s=2.5715)
            except Exception as exc:                         # noqa: BLE001
                err = exc
    finally:
        d.PLPipeline._start = _REAL_START
        d.PLPipeline._wait_done = _REAL_WAIT_DONE
    return (not rep.failures and err is None), rep, err, sc


def load_vectors():
    g = G.load_bpe_golden(HERE)
    cases, patches, templs = G.load_hw_manifest(HERE)
    return g, cases, patches, templs


# -- the clean run ---------------------------------------------------------

def test_the_whole_gate_passes_against_healthy_fake_silicon():
    ok, rep, err, sc = run_gate()
    assert err is None, err
    assert ok, rep.failures
    # R0 + R4 + R5-after are three nine-case suites, plus R1/R2/R3/R5's own
    # checks.  A gate that silently stopped running phases would show here.
    assert rep.checks >= 27 + 12, rep.checks
    # R3 and R5 each recover exactly once.  main()'s closing reprogram is
    # outside run_all and so is not counted here.
    assert sc.resets == 2, f"expected two PL reprograms, got {sc.resets}"


def test_it_reprograms_the_pl_after_each_deliberate_break():
    _, _, _, sc = run_gate()
    # R3, R5, and nothing else: two means each recovered exactly once and no
    # phase quietly reset a second time to make itself pass.
    assert sc.resets == 2, sc.resets


def test_the_deadline_cuts_a_running_transaction_short():
    """R1's timing claim, checked directly rather than through the report."""
    sc = Scenario(load_vectors())
    rep = G.Report()
    stall = R.pick_case(sc.cases, R.STALL_GEOM, "stall")
    with _Swapped(sc.vc):
        pl = sc.make_pipeline(R.DEADLINE_S)
        R.phase_r1_deadline(pl, stall, sc.patches, sc.templs, rep,
                            R.DEADLINE_S, 2.5715)
    assert not rep.failures, rep.failures


def test_a_poisoned_pipeline_refuses_every_entry_point():
    sc = Scenario(load_vectors())
    rep = G.Report()
    stall = R.pick_case(sc.cases, R.STALL_GEOM, "stall")
    with _Swapped(sc.vc):
        pl = sc.make_pipeline(R.DEADLINE_S)
        try:
            R.phase_r1_deadline(pl, stall, sc.patches, sc.templs, rep,
                                R.DEADLINE_S, 2.5715)
        except G.GateError:
            pass
        R.phase_r2_latch(pl, sc.cases, sc.patches, sc.templs, rep)
    assert not rep.failures, rep.failures
    assert rep.checks >= 4


# -- injected defects, each required to FAIL the gate ----------------------

MUTATIONS = {
    "no_timeout":     "the driver never enforces its deadline",
    "late_timeout":   "the raise lands a second past the deadline",
    "latch_open":     "the poison latch is open after a failed run",
    "close_frees":    "close() frees the buffers with a transfer outstanding",
    "reset_fails":    "reprogramming the PL fails",
    "reset_fails_late": "the reprogram fails only at the wedge",
    "stale_after_reset": "the fabric comes back with a stale result",
    "wedge_completes":   "a core started with nothing armed completes anyway",
    "starts_on_busy": "_start's ap_ctrl_hs idle guard is skipped",
    "retain_open_after_wedge": "close() frees after the wedge",
    "r5_wrong_error": "the busy-core guard refuses with unrecognised wording",
    "r4_timeout":     "R4 times out, aborting with the pipeline poisoned",
}


def test_every_injected_defect_fails_the_gate():
    vectors = load_vectors()
    for name, what in MUTATIONS.items():
        ok, rep, err, sc = run_gate(mutate=name, vectors=vectors)
        assert not ok, f"{name}: the gate PASSED with {what}"
        detail = (f"{len(rep.failures)} check(s) failed"
                  + (f", {type(err).__name__}" if err else ""))
        print(f"  [detected] {name:<24} {detail}")


def test_a_defect_that_only_appears_after_recovery_is_still_caught():
    """The R4 phase is what separates 'survived' from 'recovered'."""
    ok, rep, _, _ = run_gate(mutate="stale_after_reset")
    assert not ok
    assert any(f.startswith("R4") or f.startswith("R5-after")
               for f in rep.failures), rep.failures


def test_a_gate_failure_still_leaves_the_board_reprogrammed():
    """A phase that aborts must not leave a poisoned pipeline behind."""
    ok, _, err, sc = run_gate(mutate="close_frees")
    assert not ok
    assert sc.resets >= 1, "the aborted run left the PL unprogrammed"


def test_a_failed_reprogram_fail_stops_instead_of_exiting():
    """The one state with no in-process recovery must not return a verdict.

    R3 reaches its reprogram holding CMA pages that close() refused to
    free.  If the reprogram fails, recording the failure and running on
    ends main(), drops the references and hands those pages back while
    the fabric is in an unknown state -- the release the retention
    exists to refuse.  The gate must hold instead.
    """
    ok, rep, err, sc = run_gate(mutate="reset_fails")
    assert not ok
    assert isinstance(err, HeldForever), (
        f"the gate returned a verdict instead of holding: {err!r}")
    assert len(sc.holds) == 1, sc.holds
    held, bitfile = sc.holds[0]
    assert bitfile == "fake.bit", bitfile
    assert held, (
        "the hold was handed an EMPTY list: nothing keeps the pages "
        "reachable, so they are released the moment this frame goes")
    # Nothing after R3 ran: a third pipeline would mean R4 proceeded on
    # a board whose fabric was never put back.
    assert len(sc.pipelines) == 2, len(sc.pipelines)
    assert sc.resets == 1, sc.resets


def test_a_failed_reprogram_at_the_wedge_also_fail_stops():
    """R5 holds pages too, and it is a separate call site."""
    ok, rep, err, sc = run_gate(mutate="reset_fails_late")
    assert not ok
    assert isinstance(err, HeldForever), (
        f"the wedge site returned instead of holding: {err!r}")
    assert sc.resets == 2, sc.resets       # R3 recovered, R5 did not
    assert len(sc.holds) == 1, sc.holds
    assert sc.holds[0][0], "empty hold list at the wedge site"


def test_a_hold_that_returns_is_still_refused():
    """fail_stop() must not fall through, even given a broken hold_fn."""
    sc = Scenario(load_vectors(), mutate="reset_fails")
    rep = G.Report()
    err = None
    with _Swapped(sc.vc):
        try:
            R.run_all(sc.make_pipeline, "fake.bit", sc.cases, sc.patches,
                      sc.templs, rep, reset_fn=sc.reset_fn,
                      hold_fn=lambda held, bit: None,
                      deadline_s=R.DEADLINE_S, natural_s=2.5715)
        except Exception as exc:                         # noqa: BLE001
            err = exc
    assert isinstance(err, G.GateError), (
        f"the gate CONTINUED past a hold that returned: {err!r}")
    assert len(sc.pipelines) == 2, len(sc.pipelines)


# -- nothing may be left holding unprotected pages -------------------------

def test_an_unexpected_r5_result_still_closes_and_reprograms():
    """The audit's measured hole: R5 runs OUTSIDE run_all's abort handler.

    `r5_wrong_error` makes R5's third `require` fail on a result it did not
    expect, which raises straight out of the phase -- past the close() that
    would retain the pages and past the reprogram that clears the wedge.
    Before the guard this measured resets=1, holds=0, outstanding=True,
    closed=False: a wedged core, retained pages, and a dying frame as the
    only thing referencing them.
    """
    ok, rep, err, sc = run_gate(mutate="r5_wrong_error")
    assert not ok, "the gate PASSED with an unrecognised guard message"
    pl5 = sc.pipelines[-1]
    assert pl5._close_verdict is False, (
        f"R5's pipeline was not closed on the abort "
        f"({pl5._close_verdict!r}); its retained pages went with the frame")
    assert sc.resets == 2, (
        f"expected R3's reprogram and R5's recovery reprogram, got "
        f"{sc.resets} -- the wedged core was left wedged")
    assert not sc.unprotected(), sc.unprotected()
    # The phases after the abort did not run, and did not claim to pass.
    assert not any(f.startswith("R5-after") for f in rep.failures)


def test_no_run_ever_leaves_a_pipeline_holding_unprotected_pages():
    """The invariant, over the clean run and every injected defect.

    Each pipeline must end in one of exactly three safe states: a `close()`
    that proved the DMAs halted and freed the pages, a SUCCESSFUL reprogram
    after the last time it refused to free them, or a fail-stop hold that
    keeps them reachable for as long as the process lives.  Anything else is
    CMA pages back in the pool with a command possibly still against them.
    """
    vectors = load_vectors()
    for name in [None] + list(MUTATIONS):
        _ok, _rep, _err, sc = run_gate(mutate=name, vectors=vectors)
        bad = sc.unprotected()
        assert not bad, (
            f"{name or 'clean run'}: {len(bad)} pipeline(s) left neither "
            f"closed, nor reprogrammed after, nor held "
            f"(reprograms={sc.reprograms}, holds={len(sc.holds)})")
        print(f"  [protected] {name or 'clean run':<26} "
              f"{len(sc.pipelines)} pipeline(s), {sc.reprograms} reprogram(s),"
              f" {len(sc.holds)} hold(s)")


def test_every_hold_carries_the_pipeline_and_its_retained_buffers():
    """An empty hold protects nothing -- it is a `sleep` with a banner.

    Both fail-stop sites are reached with pages a refusing `close()` moved
    into `tme_driver._RETAINED_BUFFERS`, so the held list must carry the
    pipeline itself AND those buffers; the whole point is that the references
    outlive the frame that raised.
    """
    for name in ("reset_fails", "reset_fails_late"):
        _ok, _rep, err, sc = run_gate(mutate=name)
        assert isinstance(err, HeldForever), (name, err)
        assert len(sc.holds) == 1, (name, sc.holds)
        held, bitfile = sc.holds[0]
        assert bitfile == "fake.bit", bitfile
        assert sc.pipelines[-1] in held, (
            f"{name}: the hold does not contain the pipeline that was "
            f"holding the pages")
        assert len(held) > 1, (
            f"{name}: the hold carries the pipeline but none of its "
            f"retained buffers ({len(held)} object(s))")


# -- output failures must not be able to skip the teardown -----------------

class DeadStdout(io.TextIOBase):
    """A stdout that has gone away: a closed kernel, a dropped websocket."""

    def write(self, s):
        raise BrokenPipeError("stdout is gone")

    def flush(self):
        raise BrokenPipeError("stdout is gone")


class _StdoutGone:
    """Break stdout, and un-latch safe_teardown's dead-output flag after.

    `say()` latches `_OUTPUT_DEAD` for the life of the process on purpose --
    once stdout has failed there is no reason to believe the next write will
    work.  In a test suite that latch would silence every later test, so it
    is saved and restored here.
    """

    def __enter__(self):
        self._out = sys.stdout
        self._dead = safe_teardown._OUTPUT_DEAD
        sys.stdout = DeadStdout()
        return self

    def __exit__(self, *exc):
        sys.stdout = self._out
        safe_teardown._OUTPUT_DEAD = self._dead
        return False


def test_a_broken_stdout_cannot_skip_the_fail_stop_hold():
    """`fail_stop` announced itself with `print` before engaging the hold.

    On the teardown path stdout is a notebook's, and it can already be gone.
    A `print` that raised there propagated out of `fail_stop` before
    `hold_fn` was ever called, unwound `main()` and released the retained
    pages -- the hold skipped BECAUSE the log went away.  Measured before the
    fix: BrokenPipeError out, hold_fn called zero times.
    """
    calls = []

    def hold_fn(held, bitfile):
        calls.append((list(held), bitfile))
        raise HeldForever("held")

    outcome = None
    with _StdoutGone():
        try:
            R.fail_stop(hold_fn, None, "fake.bit", "why this is unrecoverable")
        except HeldForever:
            outcome = "held"
        except BaseException as exc:                         # noqa: BLE001
            outcome = f"{type(exc).__name__}: {exc}"
    assert outcome == "held", (
        f"the hold was skipped because the log went away: {outcome}")
    assert len(calls) == 1, calls


def test_a_broken_stdout_cannot_skip_the_final_reprograms_verdict():
    """`final_reprogram`'s False is what sends `main()` into the hold.

    So its failure message may not be able to raise in place of returning:
    that would carry the exception out of `main()` and past the
    `fail_stop_holding` call the False exists to trigger.
    """
    outcome = None
    with _StdoutGone():
        try:
            # No PYNQ off the board, so the import inside raises and the
            # failure path -- the one that must return False -- is taken.
            outcome = R.final_reprogram("no-such-overlay.bit")
        except BaseException as exc:                         # noqa: BLE001
            outcome = f"{type(exc).__name__}: {exc}"
    assert outcome is False, (
        f"final_reprogram raised instead of returning False: {outcome}")


# -- structural -------------------------------------------------------------

def test_the_ap_ctrl_constants_match_the_driver():
    """Gate 7 mirrors the driver's private ap_ctrl bits; they must agree."""
    assert R.AP_CTRL_OFF == d._AP_CTRL_OFF
    assert R.AP_START == d._AP_START
    assert R.AP_DONE == d._AP_DONE
    assert R.AP_IDLE == d._AP_IDLE


def test_the_stall_geometry_is_the_maximum_envelope():
    _, cases, _, _ = load_vectors()
    c = R.pick_case(cases, R.STALL_GEOM, "stall")
    assert c.patch_bytes == 251_740, c.patch_bytes
    # And the deadline must be well inside the transaction it interrupts, at
    # either clock this build might run at.
    for mhz in (100.0, 125.0):
        assert R.DEADLINE_S < C.cycles(*R.STALL_GEOM, "B2") / (mhz * 1e6) / 2


def test_a_missing_stall_case_is_a_setup_error():
    _, cases, _, _ = load_vectors()
    thinned = [c for c in cases if (c.pw, c.ph, c.tw, c.th) != R.STALL_GEOM]
    try:
        R.pick_case(thinned, R.STALL_GEOM, "stall")
    except G.SetupError:
        return
    raise AssertionError("a manifest without the stall geometry was accepted")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:                               # noqa: BLE001
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
