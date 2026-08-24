"""Prove the teardown scheme both gates depend on.

    python test_safe_teardown.py       # from sw/
    pytest test_safe_teardown.py

No PYNQ and no board.  Every test here is about one property: **no path
through teardown may release a CMA buffer while the fabric could still be
writing it.**  The interesting cases are all failures-during-failure — a
close() that raises while stdout is already gone, an interrupt landing inside
the buffer snapshot, a SIGTERM arriving while the process is holding pages it
must not release — because that is where the release window actually opened.

The hold loop is tested in a daemon THREAD rather than by giving
`fail_stop_holding` a bounded mode.  A test-only escape hatch in the loop
would be a way out of it, and "there is no way out" is the property.  The
thread parks inside the loop forever and the test asserts it never returned.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_teardown as ST


# -- fakes ----------------------------------------------------------------

class FakeBuf:
    def __init__(self, name):
        self.name = name
        self.freed = False

    def freebuffer(self):
        self.freed = True


class FakePipe:
    """A pipeline whose close() can be told how to misbehave.

    `forget` mirrors the real `_forget_buffers()`: close() nulls all seven
    attributes, which is what makes a caller's own references the only thing
    standing between the pages and `__del__`.
    """

    _BUFFER_ATTRS = ("a", "b", "c", "d", "e", "f", "g")

    def __init__(self, close_result=True, close_exc=None, forget=True):
        self._close_result = close_result
        self._close_exc = close_exc
        self._forget = forget
        self.close_calls = 0
        for n in self._BUFFER_ATTRS:
            setattr(self, n, FakeBuf(n))

    def close(self):
        self.close_calls += 1
        if self._close_exc is not None:
            raise self._close_exc
        if self._forget:
            for n in self._BUFFER_ATTRS:
                setattr(self, n, None)
        return self._close_result


class InterruptingPipe(FakePipe):
    """Reading the fifth buffer attribute is interrupted.

    A property that raises stands in for the real hazard: a Ctrl-C landing
    between two `getattr` calls in the snapshot loop.
    """

    @property
    def e(self):
        raise KeyboardInterrupt

    @e.setter
    def e(self, _v):
        pass            # FakePipe.__init__ populates all seven; this one is
                        # write-only, so reading it is what fails


class DeadStdout:
    """stdout after the notebook went away."""

    def __init__(self):
        self.writes = 0

    def write(self, _s):
        self.writes += 1
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def _fake_pynq(overlay):
    """Install a fake `pynq` module; returns (restore_fn, calls list)."""
    calls = []

    class Mod:
        def Overlay(self, bitfile):                       # noqa: N802
            calls.append(bitfile)
            return overlay(bitfile)

    saved = sys.modules.get("pynq")
    sys.modules["pynq"] = Mod()

    def restore():
        if saved is None:
            del sys.modules["pynq"]
        else:
            sys.modules["pynq"] = saved
    return restore, calls


def _teardown(pipe, bitfile, status):
    """`ST.teardown`, with this process's signal handlers put back after.

    teardown() ignores SIGINT/SIGTERM/SIGHUP/SIGQUIT for the REST OF THE
    PROCESS, which is correct in a gate — the process must not be killable
    while it holds CMA pages — and wrong in a test runner, where it would
    leave the terminal unable to Ctrl-C out of the suite.  Restoring here
    keeps that property under test (see the signal tests) without inflicting
    it on whoever is running the tests.
    """
    present = [n for n in ST._TERMINATION_SIGNALS
               if getattr(signal, n, None) is not None]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in present}
    try:
        return ST.teardown(pipe, bitfile, status)
    finally:
        for n, h in saved.items():
            try:
                signal.signal(getattr(signal, n), h)
            except (ValueError, OSError):
                pass


def _with_dead_stdout(fn):
    """Run `fn()` with stdout raising BrokenPipeError on every write."""
    dead = DeadStdout()
    saved_out, saved_dead = sys.stdout, ST._OUTPUT_DEAD
    sys.stdout = dead
    ST._OUTPUT_DEAD = False
    try:
        return fn(), dead
    finally:
        sys.stdout = saved_out
        ST._OUTPUT_DEAD = saved_dead


# -- say(): output can never change control flow --------------------------

def test_say_survives_a_stdout_that_raises():
    """A dead notebook must not be able to abort the recovery.

    This is not hypothetical: the gate's stdout is a pipe to a Jupyter kernel,
    and a closed browser tab or a restarted kernel makes the next write raise
    BrokenPipeError. If that escaped, the recovery would abort and the process
    would exit — releasing the pages BECAUSE the log went away.
    """
    (ret, dead) = _with_dead_stdout(
        lambda: ST.say("first line", "second line", "third line"))
    assert ret is None
    assert dead.writes >= 1, "say() never even tried to write"
    # Latched after the first failure: the remaining lines are not re-attempted.
    assert dead.writes <= 2, (
        f"say() kept writing to a broken stdout ({dead.writes} attempts); "
        f"each one costs an exception")


def test_teardown_completes_with_no_stdout_at_all():
    """The whole decision, start to finish, with every print raising.

    The control flow must be identical to the working-stdout case: close()
    tried, reset attempted, status worsened to 1.
    """
    pipe = FakePipe(close_result=False)
    restore, calls = _fake_pynq(lambda b: object())
    try:
        (status, dead) = _with_dead_stdout(
            lambda: _teardown(pipe, "combined.bit", 0))
    finally:
        restore()
    assert status == 1, f"status {status} with a dead stdout, expected 1"
    assert calls == ["combined.bit"], (
        f"the PL reset did not happen when stdout was broken: {calls}")
    assert dead.writes >= 1


def _park_at_first_sleep():
    """(fake_sleep, reached) — a sleep that parks forever the first time.

    Parks on an Event rather than sleeping: `ST.time` IS the global time
    module, so a fake that called `time.sleep` would call itself.
    """
    reached = threading.Event()
    forever = threading.Event()          # never set

    def fake_sleep(_secs):
        reached.set()
        forever.wait()
    return fake_sleep, reached


def test_fail_stop_banner_cannot_raise_through_a_dead_stdout():
    """Even the fail-stop banner is best-effort; it must reach the hold loop.

    stdout is swapped in THIS thread and restored here too: the holding thread
    never returns, so it can never restore anything itself.
    """
    fake_sleep, reached = _park_at_first_sleep()
    dead = DeadStdout()
    saved_sleep, saved_out, saved_dead = (ST.time.sleep, sys.stdout,
                                          ST._OUTPUT_DEAD)
    ST.time.sleep = fake_sleep
    sys.stdout = dead
    ST._OUTPUT_DEAD = False
    try:
        t = threading.Thread(
            target=lambda: ST.fail_stop_holding([FakeBuf("x")],
                                                "combined.bit"),
            daemon=True)
        t.start()
        ok = reached.wait(5.0)
    finally:
        sys.stdout = saved_out
        ST._OUTPUT_DEAD = saved_dead
        ST.time.sleep = saved_sleep
    assert ok, ("fail_stop_holding never reached its hold loop — the banner "
                "raised on the dead stdout instead")
    assert dead.writes >= 1, "the banner never tried to print"


# -- the buffer snapshot --------------------------------------------------

def test_snapshot_is_complete_on_a_healthy_pipeline():
    bufs, complete = ST.snapshot_buffers(FakePipe())
    assert complete is True
    assert len(bufs) == 7


def test_an_interrupted_snapshot_is_reported_incomplete():
    """A snapshot cut short must say so — never silently skip a buffer.

    Skipping would leave exactly that buffer referenced by nothing once
    close() nulled the attributes, which is the release this whole module
    prevents. The interrupt is not re-raised either: unwinding out of teardown
    would exit the process and release all seven.
    """
    bufs, complete = ST.snapshot_buffers(InterruptingPipe())
    assert complete is False, "an interrupted snapshot was reported complete"
    assert len(bufs) == 4, (
        f"expected the four buffers read before the interrupt, got "
        f"{len(bufs)}")


def test_an_unknown_layout_is_not_claimed_complete():
    class Opaque:
        pass
    bufs, complete = ST.snapshot_buffers(Opaque())
    assert (bufs, complete) == ([], False)


def test_an_interrupt_on_the_attribute_list_is_incomplete_not_an_escape():
    """The `_BUFFER_ATTRS` lookup is inside the try, like the seven after it.

    An interrupt can land on the first getattr as easily as on the fifth, and
    if it escaped from there teardown would be abandoned entirely — no close,
    no reset, no fail-stop, just an unwind to process exit with every buffer
    released. It has to come back as an incomplete snapshot like any other.
    """
    class HostilePipe:
        @property
        def _BUFFER_ATTRS(self):
            raise KeyboardInterrupt

    bufs, complete = ST.snapshot_buffers(HostilePipe())
    assert (bufs, complete) == ([], False)


def test_an_incomplete_snapshot_never_calls_close():
    """The rule that makes a partial snapshot safe.

    close() nulls all seven attributes. If it ran after a partial snapshot,
    the unread buffer would be held by nobody. Not closing leaves `pl` owning
    every buffer, so the recovery runs with all of them still reachable.
    """
    pipe = InterruptingPipe()
    restore, calls = _fake_pynq(lambda b: object())
    try:
        status = _teardown(pipe, "combined.bit", 0)
    finally:
        restore()
    assert pipe.close_calls == 0, (
        "close() was called after an incomplete snapshot — the buffer that "
        "could not be read would now be referenced by nothing")
    assert calls == ["combined.bit"], "the PL was not reset"
    assert status == 1
    # pl still owns them: nothing was forgotten, nothing was freed.
    assert pipe.a is not None and pipe.g is not None
    assert not pipe.a.freed


def test_an_incomplete_snapshot_with_a_failed_reset_holds_the_pipeline():
    """The worst case: partial snapshot AND no reset. Hold `pl` itself.

    The buffers that could not be read are still reachable through the
    pipeline, so the pipeline is what has to be held.
    """
    pipe = InterruptingPipe()
    held = {}

    def fake_fail_stop(objs, bitfile):
        held["objs"] = list(objs)
        raise _Escape

    restore, _ = _fake_pynq(lambda b: (_ for _ in ()).throw(
        RuntimeError("no bitstream")))
    saved = ST.fail_stop_holding
    ST.fail_stop_holding = fake_fail_stop
    try:
        _teardown(pipe, "combined.bit", 0)
    except _Escape:
        pass
    else:
        raise AssertionError("teardown returned after a failed reset")
    finally:
        ST.fail_stop_holding = saved
        restore()
    assert held["objs"][0] is pipe, (
        "the pipeline itself was not held; the buffers the snapshot could "
        "not read are reachable only through it")


class _Escape(BaseException):
    """Stands in for "the operator power-cycled the board"."""


# -- close_safely ---------------------------------------------------------

def test_close_safely_reports_a_raising_close_as_not_freed():
    for exc in (RuntimeError("DMASR readback timed out"), KeyboardInterrupt()):
        freed, got = ST.close_safely(FakePipe(close_exc=exc))
        assert freed is False, f"{type(exc).__name__} read as a clean close"
        assert got is exc, f"{type(exc).__name__} was swallowed, not reported"


def test_close_safely_passes_a_clean_close_through():
    freed, exc = ST.close_safely(FakePipe(close_result=True))
    assert (freed, exc) == (True, None)


# -- signals --------------------------------------------------------------

def test_termination_signals_are_ignored():
    """SIGINT/SIGTERM/SIGHUP/SIGQUIT must not be able to end the process.

    Each of them defaults to killing it, and killing it IS the unsafe release:
    the retained buffers are strong references in this process and go with it.
    Whichever of the four this platform has must end up as SIG_IGN.
    """
    present = [n for n in ST._TERMINATION_SIGNALS
               if getattr(signal, n, None) is not None]
    assert present, "no termination signals on this platform at all?"
    saved = {n: signal.getsignal(getattr(signal, n)) for n in present}
    try:
        installed = ST.block_termination_signals()
        assert set(installed) == set(present), (
            f"installed {installed}, expected {present}")
        for n in present:
            assert signal.getsignal(getattr(signal, n)) is signal.SIG_IGN, (
                f"{n} is still deliverable; a {n} would kill the holder")
    finally:
        for n, h in saved.items():
            signal.signal(getattr(signal, n), h)


def test_blocking_signals_off_the_main_thread_does_not_raise():
    """`signal.signal` raises off the main thread — best-effort, never fatal.

    teardown() calls this first. If it could raise, the whole teardown would
    be skipped by the very call that exists to protect it.
    """
    out = {}
    t = threading.Thread(
        target=lambda: out.update(r=ST.block_termination_signals()))
    t.start()
    t.join(5.0)
    assert not t.is_alive()
    assert out.get("r") == [], (
        f"expected no signals installed off the main thread, got {out.get('r')}")


def test_arming_installs_every_signal_this_platform_has():
    """The pre-transfer arm returns exactly what it installed."""
    present = [n for n in ST._TERMINATION_SIGNALS
               if getattr(signal, n, None) is not None]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in present}
    try:
        assert set(ST.arm_teardown_protection()) == set(present)
    finally:
        for n, h in saved.items():
            signal.signal(getattr(signal, n), h)


def test_arming_refuses_when_a_signal_could_not_be_installed():
    """A partial arm must stop the gate, not warn and carry on.

    Starting a transfer that cannot be protected gambles the CMA pool on the
    next gate's behalf. The cost of refusing is one re-run; the cost of
    proceeding is a corrupted pool nobody can attribute.
    """
    saved = ST.block_termination_signals
    ST.block_termination_signals = lambda: ["SIGINT"]     # SIGTERM missing
    try:
        ST.arm_teardown_protection()
    except ST.TeardownUnprotected as exc:
        assert "SIGTERM" in str(exc), exc
    else:
        raise AssertionError(
            "arming reported success while SIGTERM was still deliverable")
    finally:
        ST.block_termination_signals = saved


def test_teardown_blocks_signals_before_touching_the_pipeline():
    """Order matters: signals first, then the snapshot, then close().

    A SIGTERM arriving during the snapshot or inside close() would otherwise
    kill the process mid-decision — with the buffers half-referenced and the
    fabric unproved.
    """
    order = []

    class Watcher(FakePipe):
        def close(self):
            order.append("close")
            return super().close()

    real_block = ST.block_termination_signals
    ST.block_termination_signals = lambda: order.append("signals") or []
    try:
        ST.teardown(Watcher(), "combined.bit", 0)
    finally:
        ST.block_termination_signals = real_block
    assert order == ["signals", "close"], order


# -- the hold loop --------------------------------------------------------

def test_fail_stop_never_returns_through_any_exception():
    """Injected KeyboardInterrupt, SystemExit and BrokenPipeError, in a row.

    All three are BaseExceptions that would normally end a loop, and any exit
    from this one frees the pages. The thread is expected to still be inside
    the loop at the end — that is what "never returns" means, and the test
    would fail if the function returned or the exception escaped.
    """
    injected = [KeyboardInterrupt(), SystemExit(1), BrokenPipeError()]
    ticks = []
    parked = threading.Event()
    entered_park = threading.Event()
    returned = []

    def fake_sleep(_secs):
        ticks.append(1)
        if len(ticks) <= len(injected):
            raise injected[len(ticks) - 1]
        entered_park.set()
        parked.wait()             # never set: the thread stays in the loop

    def run():
        ST.fail_stop_holding([FakeBuf("held")], "combined.bit")
        returned.append(True)     # only reachable if the loop was left

    saved = ST.time.sleep
    ST.time.sleep = fake_sleep
    try:
        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert entered_park.wait(10.0), (
            f"the hold loop stopped after {len(ticks)} tick(s): an injected "
            f"exception ended it instead of being refused")
    finally:
        ST.time.sleep = saved

    assert len(ticks) == len(injected) + 1
    assert not returned, "fail_stop_holding RETURNED — the pages would go back"
    assert t.is_alive(), "the holding thread died; nothing is holding the pages"


def test_fail_stop_holds_the_objects_it_was_given():
    """The reference the whole scheme rests on is the one in this frame."""
    buf = FakeBuf("held")
    fake_sleep, entered = _park_at_first_sleep()

    saved = ST.time.sleep
    ST.time.sleep = fake_sleep
    try:
        t = threading.Thread(
            target=lambda: ST.fail_stop_holding([buf], "combined.bit"),
            daemon=True)
        t.start()
        assert entered.wait(5.0)
        assert not buf.freed
        assert t.is_alive()
    finally:
        ST.time.sleep = saved


# -- teardown, end to end -------------------------------------------------

def test_a_clean_teardown_does_not_reset_the_pl():
    pipe = FakePipe(close_result=True)
    restore, calls = _fake_pynq(lambda b: object())
    try:
        status = _teardown(pipe, "combined.bit", 0)
    finally:
        restore()
    assert status == 0
    assert calls == [], "a clean close() must not reprogram the PL"
    assert pipe.close_calls == 1


def test_a_retained_close_resets_once_and_fails_the_gate():
    pipe = FakePipe(close_result=False)
    restore, calls = _fake_pynq(lambda b: object())
    try:
        status = _teardown(pipe, "combined.bit", 0)
    finally:
        restore()
    assert calls == ["combined.bit"], calls
    assert status == 1, "an unsafe teardown is a failure even if phases passed"


def test_a_setup_failure_status_is_not_downgraded():
    """status 2 ("could not run") must survive a teardown that also failed."""
    pipe = FakePipe(close_result=False)
    restore, _ = _fake_pynq(lambda b: object())
    try:
        status = _teardown(pipe, "combined.bit", 2)
    finally:
        restore()
    assert status == 2, f"exit 2 was rewritten to {status}"


def test_a_failed_reset_fail_stops_holding_pipeline_and_buffers():
    pipe = FakePipe(close_result=False)
    held = {}

    def fake_fail_stop(objs, bitfile):
        held["objs"] = list(objs)
        raise _Escape

    restore, _ = _fake_pynq(lambda b: (_ for _ in ()).throw(
        RuntimeError("no bitstream")))
    saved = ST.fail_stop_holding
    ST.fail_stop_holding = fake_fail_stop
    try:
        _teardown(pipe, "combined.bit", 0)
    except _Escape:
        pass
    else:
        raise AssertionError(
            "teardown RETURNED after a failed reset — the caller would exit "
            "and release the pages")
    finally:
        ST.fail_stop_holding = saved
        restore()
    objs = held["objs"]
    assert objs[0] is pipe
    assert len(objs) == 8, f"pipeline + 7 buffers expected, got {len(objs)}"
    assert not any(b.freed for b in objs[1:])


def test_an_interrupted_reset_is_a_failed_reset():
    """A Ctrl-C inside the seconds-long Overlay() call must not escape."""
    def interrupted(_bitfile):
        raise KeyboardInterrupt

    restore, _ = _fake_pynq(interrupted)
    try:
        assert ST.reset_pl("combined.bit") is False
    finally:
        restore()


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
