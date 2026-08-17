"""Gate 3's teardown, driven through its own `main()`.

    python test_board_gate_full_dma.py       # from sw/
    pytest test_board_gate_full_dma.py

No PYNQ and no board.  `main()` runs for real — argument parsing, the overlay
construction, the `finally` — with `_run` stubbed to a verdict and the 63 MB
procedural page stubbed to a small one.  The gate proper is covered by
`--selftest`; what is covered HERE is the thing `--selftest` cannot reach: what
happens to ~120 MiB of CMA buffers when the run is over.

Gate 3 matters at least as much as gate 4 for this. It allocates the two
biggest buffers of the whole session, and its old teardown was a bare
`pl.close()` in a `finally` with `freed` initialised to **True** — so a close()
that raised printed a note, left `freed` True, reported nothing, and let the
process exit with the pages going back while a DMA could still have been
writing them. A retained-buffer close() could not fail the gate either: the
verdict was already returned from inside the `try`.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_gate_full_dma as G3
import safe_teardown as ST
import tme_driver as d


class FakeBuf:
    def __init__(self, name):
        self.name = name
        self.freed = False

    def freebuffer(self):
        self.freed = True


class FakePipe:
    """Stands in for PLPipeline: seven buffers and a close() under control."""

    _BUFFER_ATTRS = d.PLPipeline._BUFFER_ATTRS

    def __init__(self, close_result=True, close_exc=None):
        self._close_result = close_result
        self._close_exc = close_exc
        self.close_calls = 0
        for n in self._BUFFER_ATTRS:
            setattr(self, n, FakeBuf(n))

    def close(self):
        """Mirrors the real contract: True only if every buffer was freed.

        False means retained — the pages are still allocated and still
        targeted, which is why the caller must not let the process exit.
        Either way `_forget_buffers()` nulls the attributes, so the pipeline
        stops being what holds them.
        """
        self.close_calls += 1
        if self._close_exc is not None:
            raise self._close_exc
        for n in self._BUFFER_ATTRS:
            if self._close_result:
                getattr(self, n).freebuffer()
            setattr(self, n, None)              # as _forget_buffers does
        return self._close_result


class _FailStopReached(BaseException):
    """The stub for `fail_stop_holding`, which never returns.

    BaseException so a stub that merely returned could not be mistaken for the
    real thing, and so no `except Exception` in the gate absorbs it.
    """


def run_main(pipe, run_status=0, run_exc=None, reset_ok=True,
             overlay="fake.bit"):
    """Drive `G3.main()` to its teardown.  Returns `(rc, rec)`.

    `rc` is None when main() never returned — which is one of the required
    outcomes, not an error.
    """
    rec = {"reset": [], "fail_stop": [], "held": None, "bufs_at_close": None,
           "run_args": None}
    attrs = FakePipe._BUFFER_ATTRS

    real_close = pipe.close

    def wrapped_close():
        rec["bufs_at_close"] = [getattr(pipe, a) for a in attrs]
        return real_close()
    pipe.close = wrapped_close

    def fake_run(pl, gray, n, cpu_golden):
        # Same arity as the real `_run`, deliberately. A stub that took fewer
        # arguments would keep passing while main() called the real thing
        # wrongly — which is exactly how the missing `cpu_golden` argument
        # survived every test here and failed on the board instead.
        rec["run_args"] = (n, cpu_golden)
        if run_exc is not None:
            raise run_exc
        return run_status

    def fake_reset(bitfile):
        rec["reset"].append(bitfile)
        return reset_ok

    def fake_fail_stop(objs, bitfile):
        rec["fail_stop"].append(bitfile)
        rec["held"] = list(objs)
        raise _FailStopReached(bitfile)

    present = [n for n in ST._TERMINATION_SIGNALS
               if getattr(signal, n, None) is not None]
    saved_sig = {n: signal.getsignal(getattr(signal, n)) for n in present}
    saved = (d.PLPipeline, G3._run, G3.build_page, ST.reset_pl,
             ST.fail_stop_holding, sys.argv)
    d.PLPipeline = lambda bitfile, timeout_s=None: pipe
    G3._run = fake_run
    G3.build_page = lambda: np.zeros((4, 4), dtype=np.uint8)
    ST.reset_pl = fake_reset
    ST.fail_stop_holding = fake_fail_stop
    sys.argv = ["board_gate_full_dma.py", "--overlay", overlay]
    try:
        rc = G3.main()
    except _FailStopReached:
        rc = None
    finally:
        (d.PLPipeline, G3._run, G3.build_page, ST.reset_pl,
         ST.fail_stop_holding, sys.argv) = saved
        # teardown() ignores SIGINT/SIGTERM for the rest of the process; right
        # in a gate, wrong in a test runner.
        for n, h in saved_sig.items():
            try:
                signal.signal(getattr(signal, n), h)
            except (ValueError, OSError):
                pass
    return rc, rec


def test_a_clean_run_exits_zero_and_frees_every_buffer():
    pipe = FakePipe(close_result=True)
    rc, rec = run_main(pipe, run_status=0)
    assert rc == 0, f"a clean gate 3 run exited {rc}"
    assert rec["reset"] == [], "a clean close() must not reprogram the PL"
    assert pipe.close_calls == 1
    bufs = rec["bufs_at_close"]
    assert len(bufs) == 7 and all(b.freed for b in bufs)


def test_a_failing_run_still_tears_down_cleanly():
    """A FAIL is not an unsafe teardown; the exit code must stay 1."""
    rc, rec = run_main(FakePipe(close_result=True), run_status=1)
    assert rc == 1
    assert rec["reset"] == []


def test_a_retained_close_fails_the_gate_and_resets_the_pl():
    """The hole the old code had: a PASS could survive retained buffers.

    The verdict used to be returned from inside the `try`, so the `finally`
    could only print a note — gate 3 exited 0 with ~120 MiB of CMA pages
    retained and the DMAs never proved stopped.
    """
    rc, rec = run_main(FakePipe(close_result=False), run_status=0)
    assert rec["reset"] == ["fake.bit"], (
        f"expected exactly one PL reset, got {rec['reset']}")
    assert rc == 1, (
        f"gate 3 exited {rc} after a close() that could not free — an unsafe "
        f"teardown is a failure even when the transfer verified")


def test_a_raising_close_fails_the_gate_and_resets_the_pl():
    """`freed` used to be initialised True, so this path reported success.

    close() raised, the note was printed, `freed` stayed True, no reset was
    attempted, and the process exited — releasing the pages.
    """
    pipe = FakePipe(close_exc=RuntimeError("DMACR readback timed out"))
    rc, rec = run_main(pipe, run_status=0)
    assert rec["reset"] == ["fake.bit"], (
        f"close() raised and the PL was never reset: {rec['reset']}")
    assert rc == 1, f"a close() that raised exited {rc}, not 1"
    assert rec["fail_stop"] == []


def test_a_failed_reset_does_not_return_and_holds_everything():
    """close() False AND the reset failed: gate 3 must not exit."""
    pipe = FakePipe(close_result=False)
    rc, rec = run_main(pipe, run_status=0, reset_ok=False)
    assert rc is None, (
        f"main() returned {rc} after a failed PL reset — the process would "
        f"exit and hand back ~120 MiB with the fabric unknown")
    assert rec["fail_stop"] == ["fake.bit"]
    held = rec["held"]
    assert held[0] is pipe, "the pipeline itself must be held"
    assert len(held) == 8, f"pipeline + 7 buffers expected, got {len(held)}"
    assert not any(b.freed for b in held[1:])


def test_an_exception_in_the_run_still_reaches_the_teardown():
    """A crash mid-transfer is exactly when a DMA is most likely still live."""
    pipe = FakePipe(close_result=False)
    rc, rec = run_main(pipe, run_exc=RuntimeError("S2MM timed out"))
    assert rec["reset"] == ["fake.bit"], rec["reset"]
    assert rc == 1


def test_the_status_is_taken_from_the_teardown_not_the_run():
    """`return status` must sit BELOW the finally, or the reassignment is lost.

    A `return` inside the `try` is evaluated before the `finally` runs, so a
    teardown that worsened the status would be silently discarded — the gate
    would print its failure and exit 0 anyway.
    """
    rc, _ = run_main(FakePipe(close_result=False), run_status=0)
    assert rc == 1, (
        "the teardown's verdict did not reach the exit code; check that "
        "main() returns after the finally, not from inside the try")


# =========================================================================
# The real _run(), not a stub.
#
# Everything above stubs `_run` so that main()'s teardown can be driven
# through every outcome.  That is the right shape for those tests and it is
# also how a NameError inside `_run` reached the board: `cpu_golden` is
# imported into main()'s LOCAL scope, `_run` read it as a global, and no test
# ever executed the line.  The gate ran, moved 63,078,400 B each way, asserted
# the envelope — and then died in the comparison it exists to perform.
#
# So these execute the real function end to end on a small page, with fake
# silicon that returns the true oracle output.
# =========================================================================

class OraclePipe:
    """A pipeline whose binarize_page returns the CPU oracle's own answer.

    `corrupt` flips one output byte, so the comparison the gate performs has
    something to find — a test where the DUT is the oracle proves the plumbing
    but not that a mismatch is detected.
    """

    def __init__(self, gray, threshold, cpu_golden, corrupt=False,
                 stats=None):
        self._out = cpu_golden(gray, threshold)
        if corrupt:
            r, c = 5, 7
            self._out[r, c] = 255 - self._out[r, c]
            self.corrupted_at = (r, c)
        self.last_transfer_stats = stats

    def binarize_page(self, gray, threshold):
        return self._out


def _small_page(h=40, w=64):
    """A page with structure in it, built with the gate's own generator."""
    page = np.empty((h, w), dtype=np.uint8)
    saved_w, saved_h = G3.IMG_W, G3.IMG_H
    G3.IMG_W, G3.IMG_H = w, h
    try:
        G3.fill_page_strip(page, 0)
    finally:
        G3.IMG_W, G3.IMG_H = saved_w, saved_h
    return page


def _stats(n, **over):
    s = {"mm2s_bytes": n, "s2mm_bytes": n, "expected_bytes": n,
         "guard_bytes_checked": 64, "guard_bytes_clobbered": 0,
         "sentinel_bytes_remaining": 0}
    s.update(over)
    return s


def test_the_real_run_completes_the_comparison():
    """THE REGRESSION. Executes `_run` for real, comparison included.

    Before the fix this returned 1 with `FAIL: NameError: name 'cpu_golden'
    is not defined` — the gate's own `except Exception` caught it, so it
    failed closed rather than passing wrongly, but the comparison never ran.
    """
    from binarize_dma_checks import cpu_golden
    gray = _small_page()
    n = gray.size
    pipe = OraclePipe(gray, G3.THRESHOLD, cpu_golden, stats=_stats(n))
    assert G3._run(pipe, gray, n, cpu_golden) == 0, (
        "the real _run() did not reach a clean verdict")


def test_the_real_run_detects_a_mismatched_byte():
    """And the comparison it now reaches actually compares.

    The verdict alone is not enough to assert here: `_run` returns 1 for any
    exception too, so under the `cpu_golden` NameError this would have
    "passed" while the comparison never ran. So the OUTPUT is checked — it
    must name the mismatch and its location, not an exception.
    """
    import contextlib
    import io
    from binarize_dma_checks import cpu_golden
    gray = _small_page()
    n = gray.size
    pipe = OraclePipe(gray, G3.THRESHOLD, cpu_golden, corrupt=True,
                      stats=_stats(n))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = G3._run(pipe, gray, n, cpu_golden)
    text = out.getvalue()
    assert rc == 1, "one flipped output byte did not fail the gate"
    assert "mismatch the exact CPU oracle" in text, (
        f"the gate failed for some other reason than the comparison:\n{text}")
    r, c = pipe.corrupted_at
    assert f"({c},{r})" in text, (
        f"the first mismatch was not located at the corrupted byte "
        f"({c},{r}):\n{text}")


def test_the_real_run_fails_on_a_short_envelope():
    """The envelope assertions run before the comparison and stand alone."""
    from binarize_dma_checks import cpu_golden
    gray = _small_page()
    n = gray.size
    for label, over in (("short S2MM", {"s2mm_bytes": n - 1}),
                        ("short MM2S", {"mm2s_bytes": n - 1}),
                        ("clobbered guard", {"guard_bytes_clobbered": 3}),
                        ("unwritten bytes", {"sentinel_bytes_remaining": 9})):
        pipe = OraclePipe(gray, G3.THRESHOLD, cpu_golden,
                          stats=_stats(n, **over))
        assert G3._run(pipe, gray, n, cpu_golden) == 1, (
            f"{label} did not fail the gate")


def test_the_real_run_fails_without_transfer_stats():
    """No measurements means the full-envelope claim cannot be made."""
    from binarize_dma_checks import cpu_golden
    gray = _small_page()
    pipe = OraclePipe(gray, G3.THRESHOLD, cpu_golden, stats=None)
    assert G3._run(pipe, gray, gray.size, cpu_golden) == 1


def test_run_reads_no_name_that_does_not_exist():
    """No function in this gate may read a global that is not there.

    The general form of the bug, checked statically so it cannot come back by
    another route: `_run` referenced `cpu_golden`, which exists only as a
    local of `main()`, and every caller in the tests was a stub.  Any name a
    function loads as a global must resolve in the module or in builtins.
    """
    import builtins
    import dis
    import types

    bad = {}

    def walk(code, where):
        unresolved = sorted({
            ins.argval for ins in dis.get_instructions(code)
            if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME")
            and ins.argval not in vars(G3)
            and not hasattr(builtins, ins.argval)})
        if unresolved:
            bad[where] = unresolved
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const, f"{where}.{const.co_name}")

    for name, obj in vars(G3).items():
        if isinstance(obj, types.FunctionType) and obj.__module__ == G3.__name__:
            walk(obj.__code__, name)
    assert not bad, f"names that would raise NameError if reached: {bad}"


def test_main_passes_the_oracle_through_to_run():
    """main() must hand `_run` the oracle rather than leave it to find one."""
    rc, rec = run_main(FakePipe(close_result=True), run_status=0)
    assert rc == 0
    n, oracle = rec["run_args"]
    assert n == G3.IMG_W * G3.IMG_H, n
    assert callable(oracle), "main() passed no usable oracle to _run"
    assert oracle.__name__ == "cpu_golden", oracle


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
