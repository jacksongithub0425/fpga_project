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
    rec = {"reset": [], "fail_stop": [], "held": None, "bufs_at_close": None}
    attrs = FakePipe._BUFFER_ATTRS

    real_close = pipe.close

    def wrapped_close():
        rec["bufs_at_close"] = [getattr(pipe, a) for a in attrs]
        return real_close()
    pipe.close = wrapped_close

    def fake_run(pl, gray, n):
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
