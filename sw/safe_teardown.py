#!/usr/bin/env python3
"""The teardown both board gates use: never release CMA pages unsafely.

A gate runs as its own `sudo` process, and `PLPipeline.close()` protects a
buffer it cannot prove idle by RETAINING it — a strong reference in
`tme_driver._RETAINED_BUFFERS`.  That retention lasts exactly as long as the
process.  When it exits, the references go with it, `PynqBuffer.__del__` runs,
and the CMA pages return to the pool while an S2MM may still have an open
command against them.  The next allocation is then written into by an engine
nobody stopped.

The notebook cannot help: by the time it reads an exit code, the pages it was
going to protect are already back.  So the whole decision is made HERE, inside
the process that owns the references, in this order:

    1. ignore the catchable termination signals, so nothing below can be
       interrupted into an early exit;
    2. take a COMPLETE snapshot of the buffers — or refuse to proceed;
    3. `close()`, which is the halt proof;
    4. if that could not free them, reload the overlay, which resets the DMA
       engines so no command targets those pages any more;
    5. if even THAT fails, do not exit at all.

Four rules make it hold, and each of them is a hole that was open once:

**Hold `pl` first, and snapshot completely or not at all.**  While `pl` is
alive it owns all seven buffers, so nothing can be collected.  The moment
`close()` runs it calls `_forget_buffers()` and nulls every attribute — from
then on only a caller's own references keep the pages out of the pool.  A
snapshot that silently skipped an unreadable attribute would leave exactly
that one buffer unheld, so `snapshot_buffers` reports incompleteness instead,
and the caller must then NOT call `close()`: `pl` still owns everything, and
the recovery runs with it intact.

**Recovery output can never change control flow.**  These messages go to a
notebook's stdout, which can vanish mid-run — a closed kernel, a dropped
websocket, a `BrokenPipeError` on the next write.  A print that raised inside
the recovery would abort the recovery and exit the process, releasing the
pages BECAUSE the log went away.  Every recovery message goes through `say()`,
which cannot raise.

**Termination signals are ignored for the rest of the process.**  SIGINT,
SIGTERM, SIGHUP and SIGQUIT all default to killing the process, and killing it
is precisely the unsafe release.  A Ctrl-C, a closed notebook (SIGHUP), or a
shutdown sequence's SIGTERM must not be able to do it.  SIGKILL cannot be
caught by anything, which is why the fail-stop banner asks for a power cycle
rather than a `kill`.

**A failed reset means power-cycle, NOT `reboot`.**  A software reboot runs a
shutdown sequence that terminates userspace first — it would kill the holder,
freeing the pages, and only then reset the hardware.  That is the same window
under a friendlier name.  Cutting power resets the fabric and the DDR
controller together, with the holder still alive until the instant it dies.

Used by `board_gate_full_dma.py` (gate 3) and `board_gate_extract.py`
(gate 4).  Both copy this file to the board alongside `tme_driver.py`.
"""

from __future__ import annotations

import signal
import sys
import time

# Once stdout has failed there is no reason to believe the next write will
# work, and every attempt costs an exception.  Latched, never reset.
_OUTPUT_DEAD = False

# The catchable signals that would otherwise end this process.  SIGHUP and
# SIGQUIT do not exist on Windows, where these gates do not run but their
# tests do, so every one is looked up by name.
_TERMINATION_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")


def say(*lines: str) -> None:
    """Print recovery output.  Cannot raise, and cannot alter control flow.

    `BaseException`, and everything swallowed: this is called from inside the
    teardown decision, where an escaping exception would unwind `main()` and
    release the very pages being protected.  Losing a log line is a cosmetic
    failure; losing the recovery because of a lost log line is a corruption.
    """
    global _OUTPUT_DEAD
    for line in lines:
        if _OUTPUT_DEAD:
            return
        try:
            print(line)
        except BaseException:                              # noqa: BLE001
            _OUTPUT_DEAD = True
            return
    try:
        sys.stdout.flush()
    except BaseException:                                  # noqa: BLE001
        _OUTPUT_DEAD = True


def block_termination_signals() -> list:
    """Ignore SIGINT/SIGTERM/SIGHUP/SIGQUIT.  Returns the names installed.

    Not restored afterwards, on purpose: the only thing that follows a
    teardown is the process exiting a few statements later, and a window where
    the signals work again is a window where the pages can be released.

    Best-effort by necessity — `signal.signal` raises off the main thread, and
    a platform may not have a given signal at all.  A signal that could not be
    blocked is one more reason the fail-stop banner asks for a power cycle
    instead of promising the process is unkillable.
    """
    installed = []
    for name in _TERMINATION_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue                       # not on this platform
        try:
            signal.signal(sig, signal.SIG_IGN)
        except BaseException:                              # noqa: BLE001
            continue                       # not the main thread, or not settable
        installed.append(name)
    return installed


class TeardownUnprotected(RuntimeError):
    """The termination signals could not be blocked, so no DMA may start."""


def arm_teardown_protection() -> list:
    """Block the termination signals BEFORE any DMA is armed.  Raises if not.

    **This must happen before the first transfer, not at teardown.**  Blocking
    them inside `teardown()` protects only the teardown itself; a SIGTERM
    arriving during the hardware work lands while the handlers are still at
    their defaults, and the process dies *before* the `finally` runs at all —
    measured, not theorised:

        SIGTERM -> exit -15, close() never called
        SIGHUP  -> exit  -1, close() never called
        SIGQUIT -> exit  -3, close() never called
        SIGINT  -> KeyboardInterrupt, so the finally DOES run

    Only SIGINT is a Python-level exception; the other three are process
    death, and process death with a DMA in flight is exactly the release this
    module exists to prevent.  So the gates arm this as soon as they have a
    pipeline and before they touch the fabric.

    It RAISES rather than warning when a signal it asked for could not be
    installed.  A gate that started a transfer it could not protect would be
    gambling with the CMA pool on behalf of the next gate, and the cost of
    refusing is one re-run.  Signals absent from the platform (SIGHUP and
    SIGQUIT on Windows) are not "not installed" — they cannot arrive either.
    """
    wanted = [n for n in _TERMINATION_SIGNALS
              if getattr(signal, n, None) is not None]
    installed = block_termination_signals()
    missing = [n for n in wanted if n not in installed]
    if missing:
        raise TeardownUnprotected(
            f"could not ignore {', '.join(missing)} (of {', '.join(wanted)}); "
            f"one of those signals would kill this process mid-transfer and "
            f"release its CMA pages with a DMA still running. Refusing to "
            f"start a transfer that cannot be protected — run the gate from "
            f"the main thread of a plain `sudo python3` process")
    return installed


def snapshot_buffers(pl) -> tuple:
    """`(buffers, complete)` — every DMA buffer, or an honest partial.

    `complete` is True only if EVERY attribute in `_BUFFER_ATTRS` was read.
    It is not a detail: the caller may only call `close()` when the snapshot
    is complete, because `close()` nulls all seven attributes and a buffer
    missing from the snapshot would then be referenced by nothing at all.

    An interrupt landing mid-loop is caught and reported as incomplete rather
    than re-raised.  Re-raising would unwind `main()` and exit — releasing
    every buffer, including the six that were read successfully.  Nothing is
    skipped silently: a partial snapshot changes what the caller does.
    """
    bufs = []
    try:
        # Inside the try, including this lookup: an interrupt can land on it
        # as easily as on any of the seven that follow, and an exception
        # escaping from here would leave teardown unmade rather than produce
        # the incomplete snapshot the caller knows how to handle.
        attrs = getattr(pl, "_BUFFER_ATTRS", None)
        if not attrs:
            # An unknown layout cannot be claimed complete.  Treated as the
            # incomplete case, which is the conservative one.
            return [], False
        for attr in attrs:
            buf = getattr(pl, attr, None)
            if buf is not None:
                bufs.append(buf)
    except BaseException as exc:                           # noqa: BLE001
        say(f"\n[gate] the buffer snapshot was interrupted after "
            f"{len(bufs)} buffer(s): {type(exc).__name__}: {exc}")
        return bufs, False
    return bufs, True


def close_safely(pl) -> tuple:
    """`(freed, exc)`.  A close() that RAISES is a close() that did not free.

    close() drives DMA registers and waits on them, which is exactly where an
    interrupt lands; letting one through would skip the recovery below, unwind
    `main()`, and release the pages.  Whatever came out is reported and
    returned, never re-raised.
    """
    try:
        return bool(pl.close()), None
    except BaseException as exc:                           # noqa: BLE001
        say(f"\n[gate] close() itself raised: {type(exc).__name__}: {exc}",
            "[gate] The halt checks did not complete, so no buffer is "
            "provably free: treating this as an unsafe teardown.")
        return False, exc


def reset_pl(bitfile: str) -> bool:
    """Reprogram the PL from inside THIS process.  True if it worked.

    Reloading the overlay resets the DMA engines and the cores, so no engine
    holds a command against the retained pages any more and they become safe
    to release.  It happens here rather than in the operator's notebook
    because the references are this process's, and by the time the notebook
    sees an exit code they are already gone.

    False is the one state with no in-process recovery left, and the caller
    must NOT return on it — see `fail_stop_holding`.
    """
    say("\n" + "!" * 72,
        "UNSAFE TEARDOWN: a DMA could not be proved halted, or a buffer "
        "would not free.",
        "The retained references live only as long as THIS process, so "
        "resetting the PL is done",
        "here rather than left to the caller — by the time the caller sees "
        "the exit code, the",
        "pages are already back in the pool.")
    try:
        from pynq import Overlay
        Overlay(bitfile)
    except BaseException as exc:                           # noqa: BLE001
        # BaseException for the same reason as close_safely: programming a
        # bitstream takes seconds, an interrupt lands in it easily, and an
        # exception escaping HERE would leave the recovery decision unmade.
        say(f"\nPL RESET FAILED: {type(exc).__name__}: {exc}",
            "The fabric is in an UNKNOWN state, so these CMA pages cannot be "
            "handed back at all.",
            "!" * 72)
        return False
    say(f"\nPL reset: reloaded {bitfile}. The DMA engines and cores are back "
        f"in their power-on state,",
        "so the retained pages are no longer targeted by any command and are "
        "safe to release.",
        "The gate still FAILS — an unsafe teardown is a failure — but the "
        "board is recoverable.",
        "!" * 72)
    return True


def fail_stop_holding(held: list, bitfile: str) -> None:
    """NEVER RETURNS.  Holds the CMA pages until the board is power-cycled.

    Reached only when the PL could not be reprogrammed, which is the one state
    with no in-process recovery: the fabric may still have an open command
    against these pages and nothing this process can do will retire it.
    Returning would end `main()`, end the process, drop `held`, and hand those
    pages straight back — the release this gate exists to refuse.  There is no
    exit code that means "do not reap me", so the only protection left is to
    not exit.

    `held` carries the pipeline object as well as the buffers, so the pages
    stay reachable even if the snapshot was partial.

    POWER-CYCLE, not `reboot`: a software reboot terminates userspace before
    it resets the hardware, so it would kill this holder and free the pages
    while the fabric is still live.  No reboot is initiated from here either —
    a gate does not know what else the board is doing.
    """
    block_termination_signals()      # idempotent; also covers direct callers
    say("\n" + "!" * 72,
        f"FAIL-STOP: the PL could not be reset and {len(held)} object(s) "
        f"— the pipeline and its",
        "CMA buffers — are still held. This process is NOT exiting, on "
        "purpose. Exiting would",
        "free those pages while the fabric may still have a DMA command "
        "against them; the next",
        "allocation would then be written into by an engine nobody stopped.",
        "",
        "  DO THIS:   POWER-CYCLE the board.",
        "  NOT THIS:  `reboot`. Shutdown kills this holder first and resets "
        "the hardware after,",
        "             which frees the pages while the fabric is still live — "
        "the same window.",
        "  NOR THIS:  kill -9. Same release, by hand. (SIGINT/SIGTERM/SIGHUP/"
        "SIGQUIT are ignored",
        "             from here on; SIGKILL cannot be caught by anything, so "
        "it is on you.)",
        f"  Then:      reprogram with {bitfile} and re-run the gate from a "
        f"fresh boot.",
        "!" * 72)

    ticks = 0
    while True:
        try:
            time.sleep(60.0)
            ticks += 1
            if ticks % 5 == 0:
                say(f"[gate] still holding {len(held)} object(s) after "
                    f"{ticks} min — power-cycle the board.")
        except BaseException:                              # noqa: BLE001
            # EVERY exception, KeyboardInterrupt and SystemExit included:
            # leaving this loop by any route frees the pages, so there is no
            # exception worth leaving for.  The signals are ignored above, so
            # this is a backstop for the ones that could not be installed.
            say("\n[gate] Refused: releasing these pages is the thing this "
                "state exists to prevent. Power-cycle the board.")


def teardown(pl, bitfile: str, status: int = 0) -> int:
    """The whole teardown decision.  Returns a status — or does not return.

    `status` is the gate's verdict so far; the returned one is never better.
    An unsafe teardown is a failure even when every phase passed, because the
    next gate cannot trust a board whose DMAs were never proved stopped.

    Callers must use the returned value (`status = teardown(...)`) and must
    return it from OUTSIDE the `finally`, or the reassignment is discarded.
    """
    block_termination_signals()

    bufs, complete = snapshot_buffers(pl)
    if not complete:
        # close() is not called at all.  It would null the attributes, and the
        # buffer that could not be read would then be held by nothing; while
        # `pl` is intact it still owns every one of them.
        say("\n[gate] Not calling close(): the buffer snapshot is incomplete, "
            "and close() would",
            "[gate] drop the pipeline's own references to buffers this "
            "process could not take.",
            "[gate] Recovering with the pipeline intact instead.")
        status = status or 1
        if not reset_pl(bitfile):
            fail_stop_holding([pl] + bufs, bitfile)        # does not return
        return status

    freed, exc = close_safely(pl)
    if exc is not None:
        status = status or 1
    if not freed:
        status = status or 1
        if not reset_pl(bitfile):
            fail_stop_holding([pl] + bufs, bitfile)        # does not return
    return status
