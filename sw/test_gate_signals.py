"""Deliver termination signals to a running gate, as a real process.

    python test_gate_signals.py       # from sw/
    pytest test_gate_signals.py

No PYNQ and no board, but a REAL child process: the gate's `main()` runs with
a fake pipeline, blocks where the hardware work would be, and a signal is
delivered while it is blocked.  Nothing in-process can test this — the whole
point is what happens when the interpreter is killed rather than unwound.

WHY THIS EXISTS.  Blocking the termination signals inside `teardown()`
protects only the teardown.  A signal arriving during the transfer lands while
the handlers are still at their defaults, and three of the four do not run
`finally` blocks at all:

    SIGTERM -> exit -15, close() never called
    SIGHUP  -> exit  -1, close() never called
    SIGQUIT -> exit  -3, close() never called
    SIGINT  -> KeyboardInterrupt, so the finally DOES run

That was measured on the pre-fix gates.  Both gates now arm
`safe_teardown.arm_teardown_protection()` immediately after constructing the
pipeline and before any transfer, and these tests are what keeps it there.

DELIVERY DIFFERS BY PLATFORM, and only one of the two is the real thing:

  * POSIX — the parent calls `os.kill(child, sig)`.  This is exactly what a
    shutdown sequence, a closed terminal or an impatient operator does, and it
    is the case the board runs.
  * Windows — SIGHUP and SIGQUIT do not exist, and `os.kill(pid, SIGTERM)`
    maps to TerminateProcess, which no handler can intercept.  So the child
    raises the signal in-process with `signal.raise_signal()` instead.  That
    still proves SIG_IGN was installed and honoured, but it does NOT prove
    anything about external delivery.  The suite says which mode it ran in;
    a Windows PASS is not a substitute for the POSIX one.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
POSIX = os.name == "posix"

# The signals worth delivering, in the order the report prints them.
SIGNALS = [n for n in ("SIGTERM", "SIGHUP", "SIGQUIT", "SIGINT")
           if getattr(signal, n, None) is not None]

BLOCK_TIMEOUT_S = 60.0        # a child that is never released gives up
WAIT_S = 20.0


# =========================================================================
# The child: a gate whose hardware work blocks until the parent says go
# =========================================================================

def _child(gate: str, work: Path, mutate: str = "none") -> int:
    import numpy as np

    sys.path.insert(0, str(HERE))
    import safe_teardown as ST
    import tme_driver as d

    # The parent may itself be ignoring these (another test, or a previous
    # run); handlers are inherited across fork/exec, and an inherited SIG_IGN
    # would make this test pass without the gate doing anything at all.
    for name in SIGNALS:
        try:
            signal.signal(getattr(signal, name), signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    calls = work / "close_calls"
    calls.write_text("0")

    class Buf:
        def __init__(self, n):
            self.name = n
            self.freed = False

        def freebuffer(self):
            self.freed = True

    class Pipe:
        _BUFFER_ATTRS = d.PLPipeline._BUFFER_ATTRS

        def __init__(self):
            for n in self._BUFFER_ATTRS:
                setattr(self, n, Buf(n))

        def close(self):
            calls.write_text(str(int(calls.read_text()) + 1))
            for n in self._BUFFER_ATTRS:
                getattr(self, n).freebuffer()
                setattr(self, n, None)
            return True

    def block():
        """Stand in for the transfer: announce, then wait to be released.

        On Windows the signal is raised here, in-process, because external
        delivery cannot be intercepted there at all.
        """
        (work / "ready").write_text("1")
        deadline = time.monotonic() + BLOCK_TIMEOUT_S
        while time.monotonic() < deadline:
            deliver = work / "deliver"
            if deliver.exists():
                name = deliver.read_text().strip()
                deliver.unlink()
                (work / "delivered").write_text(name)
                signal.raise_signal(getattr(signal, name))
            if (work / "go").exists():
                return
            time.sleep(0.02)
        (work / "block_timeout").write_text("1")

    if mutate == "no_arm":
        # The pre-fix gates: protection armed only inside teardown(), which is
        # too late because three of the four signals never reach it.  This is
        # the control for the tests above — see
        # test_without_the_early_arm_the_signal_kills_the_gate.
        ST.arm_teardown_protection = lambda: []

    pipe = Pipe()
    d.PLPipeline = lambda bitfile, timeout_s=None: pipe

    if gate == "gate3":
        import board_gate_full_dma as G3
        G3.build_page = lambda: np.zeros((4, 4), dtype=np.uint8)

        def fake_run(pl, gray, n):
            block()
            return 0
        G3._run = fake_run
        sys.argv = ["board_gate_full_dma.py", "--overlay", "fake.bit"]
        return G3.main()

    import board_gate_extract as G

    def fake_phase_a(pl, g, rep):
        block()
        # Ends the phase sequence without needing the rest of the fake
        # hardware; main() then reaches its teardown, which is the subject.
        raise G.GateError("phase A stopped by the signal test")
    G.phase_a_binarize = fake_phase_a
    sys.argv = ["board_gate_extract.py", "--overlay", "fake.bit",
                "--data-dir", str(HERE)]
    return G.main()


# =========================================================================
# The parent
# =========================================================================

def _wait_for(path: Path, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _deliver(gate: str, signame: str, work: Path,
             mutate: str = "none") -> dict:
    """Run the gate, deliver `signame` mid-transfer, return what happened."""
    for f in ("ready", "go", "deliver", "delivered", "close_calls",
              "block_timeout"):
        (work / f).unlink(missing_ok=True)

    child = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve()),
         "--child", gate, str(work), mutate],
        cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        if not _wait_for(work / "ready", WAIT_S):
            child.kill()
            raise AssertionError(
                f"{gate}: the child never reached its transfer; output:\n"
                f"{child.communicate()[0][-2000:]}")

        if POSIX:
            os.kill(child.pid, getattr(signal, signame))
            mode = "os.kill"
        else:
            (work / "deliver").write_text(signame)
            _wait_for(work / "delivered", WAIT_S)
            mode = "raise_signal"

        # Give the signal time to do its worst before releasing the block.
        time.sleep(1.0)
        survived = child.poll() is None

        (work / "go").write_text("1")
        try:
            out = child.communicate(timeout=WAIT_S)[0]
        except subprocess.TimeoutExpired:
            child.kill()
            out = child.communicate()[0]
            raise AssertionError(f"{gate}/{signame}: the child hung")
    finally:
        if child.poll() is None:
            child.kill()

    closes = 0
    try:
        closes = int((work / "close_calls").read_text())
    except (OSError, ValueError):
        pass
    return {"rc": child.returncode, "survived": survived, "closes": closes,
            "mode": mode, "timed_out": (work / "block_timeout").exists(),
            "out": out}


def _check(gate: str, signame: str, want_rc: int) -> None:
    work = _scratch(gate, signame)
    try:
        r = _deliver(gate, signame, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert not r["timed_out"], f"{gate}/{signame}: the child was never released"
    assert r["survived"], (
        f"{gate}: {signame} KILLED the process mid-transfer (rc={r['rc']}, "
        f"close() calls {r['closes']}). The CMA buffers went back to the pool "
        f"with a DMA potentially still running — arm_teardown_protection() is "
        f"not being called before the transfer.\n{r['out'][-1500:]}")
    assert r["closes"] == 1, (
        f"{gate}/{signame}: close() ran {r['closes']} times, expected 1 — "
        f"teardown did not happen")
    assert r["rc"] == want_rc, (
        f"{gate}/{signame}: exit {r['rc']}, expected {want_rc}\n"
        f"{r['out'][-1500:]}")


def _scratch(gate: str, signame: str) -> Path:
    """A private working directory, outside the repository."""
    return Path(tempfile.mkdtemp(prefix=f"gatesig_{gate}_{signame}_"))


# -- the tests ------------------------------------------------------------
#
# One per (gate, signal).  Written out rather than parametrised so a failure
# names the signal that got through.

def test_gate3_survives_sigterm_mid_transfer():
    _check("gate3", "SIGTERM", 0)


def test_gate3_survives_sigint_mid_transfer():
    _check("gate3", "SIGINT", 0)


def test_gate4_survives_sigterm_mid_phase_a():
    _check("gate4", "SIGTERM", 1)


def test_gate4_survives_sigint_mid_phase_a():
    _check("gate4", "SIGINT", 1)


if POSIX:
    def test_gate3_survives_sighup_mid_transfer():
        _check("gate3", "SIGHUP", 0)

    def test_gate3_survives_sigquit_mid_transfer():
        _check("gate3", "SIGQUIT", 0)

    def test_gate4_survives_sighup_mid_phase_a():
        _check("gate4", "SIGHUP", 1)

    def test_gate4_survives_sigquit_mid_phase_a():
        _check("gate4", "SIGQUIT", 1)


def test_without_the_early_arm_the_signal_kills_the_gate():
    """The control: with protection armed only at teardown, the signal wins.

    Without this the whole suite could be passing for the wrong reason — a
    platform that never delivers the signal, or a child that exits before it
    arrives, looks exactly like a gate that survived one. Here the gate is run
    with `arm_teardown_protection` stubbed out, which is precisely the pre-fix
    code, and the process is required to DIE with close() never called.
    """
    work = _scratch("gate3", "control")
    try:
        r = _deliver("gate3", "SIGTERM", work, mutate="no_arm")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert not r["survived"], (
        "SIGTERM did not kill the unprotected gate, so these tests are not "
        "testing signal protection at all: whatever makes the protected gate "
        f"survive, it is not the arming. rc={r['rc']}\n{r['out'][-1500:]}")
    assert r["closes"] == 0, (
        f"the unprotected gate somehow ran close() {r['closes']} time(s); the "
        f"control no longer reproduces the failure it stands for")


def test_the_gates_report_what_they_armed():
    """A silent arm is not evidence; the log has to name the signals.

    The operator reads this line to know the run was protected — and on a
    platform missing SIGHUP/SIGQUIT it is the only way to tell "not installed"
    from "cannot arrive here".
    """
    work = _scratch("gate3", "report")
    try:
        r = _deliver("gate3", "SIGINT", work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert "teardown protection: ignoring" in r["out"], r["out"][-1500:]
    for name in SIGNALS:
        assert name in r["out"], (
            f"{name} is available on this platform but the gate did not "
            f"report arming it:\n{r['out'][-1500:]}")


def main() -> int:
    print(f"signal delivery mode: "
          f"{'os.kill (real external delivery)' if POSIX else 'in-process raise_signal'}")
    if not POSIX:
        print("SKIPPING SIGHUP/SIGQUIT: not present on this platform.")
        print("NOTE: on Windows os.kill(SIGTERM) is TerminateProcess, which no")
        print("      handler can catch, so external delivery is NOT covered")
        print("      here. Run this suite on Linux for that.")
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
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(_child(sys.argv[2], Path(sys.argv[3]),
                                sys.argv[4] if len(sys.argv) > 4 else "none"))
    raise SystemExit(main())
