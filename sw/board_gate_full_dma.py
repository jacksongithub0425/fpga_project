#!/usr/bin/env python3
"""Board gate: the real 63,078,400-byte DMA transfer, end to end.

RUN THIS ON THE BOARD, after probe_cma_budget.py PASSES and
inspect_overlay.py PASSES:

    sudo -E python3 board_gate_full_dma.py --overlay /home/xilinx/three_stage_combined.bit

probe_cma_budget.py proves the two ~60.2 MiB buffers can be ALLOCATED (§2.2).
This script is the separate gate the allocation probe deliberately is not: it
MOVES a full 9856 x 6400 page through the PL — one 63,078,400-byte MM2S
transfer into binarize_core_0 and one 63,078,400-byte S2MM transfer out —
and verifies the output bit-exactly against the truncating-Gaussian CPU
oracle (cpu_golden in binarize_dma_checks.py, the v2.0 logical-layout
reference).  A 26-bit DMA length register (67,108,863 B max) is what makes
the single transfer legal; this is its first exercise at full size.

It drives the transfer through tme_driver.PLPipeline.binarize_page(), so a
PASS also validates the driver's binarize path — the first of the three
per-stage validations (contract §9, sw/tme_driver.py row).

WHAT "FULL-SIZE" MEANS HERE, PRECISELY.  A bit-exact page does not prove the
envelope: a short S2MM leaves the tail of the destination holding whatever
was there before, and a compare can pass over bytes the PL never wrote.  So
the gate asserts, explicitly and in its own output:

  - S2MM transferred == 63,078,400 B.  S2MM_LENGTH is written by the engine
    with the bytes actually received, so this one is a measurement of the
    return path;
  - MM2S transferred == 63,078,400 B.  Weaker by nature: MM2S_LENGTH is
    principally the length the driver programmed, so this confirms the
    request, not the movement.  What supports the outbound direction is the
    combination of the channel going idle with no error, the core raising
    ap_done, and binarize_core consuming exactly img_w*img_h beats by
    construction — a short feed leaves it blocked in a stream read and the
    gate times out instead of passing;
  - no pre-fill sentinel byte survives anywhere in the visible page — 0xAA
    cannot be a legitimate output, since binarize_core emits only 0 or 255;
  - a 64-byte guard tail past the page is untouched, catching the opposite
    error of an S2MM that wrote too much.

Together with the bit-exact compare those support "the full 63,078,400-byte
envelope moved". Quote them that way round: the S2MM count, the sentinel
scan and the guard are the direct evidence; the MM2S count is corroboration.

EVERYTHING HERE WORKS IN ROW STRIPS, AND THAT IS NOT AN OPTIMISATION.  The
board has 512 MB of DDR with no swap, of which the CMA pool is already carved
out and two 60.2 MiB CMA buffers are spoken for.  The pool is a required
`cma=192M` (the 128 MiB default was tried twice and failed both times — see
BOARD_RUNBOOK.md), so the userspace left over is smaller still, ~290 MB.
Whole-page numpy would not fit and would not fail gracefully:
`gray.astype(int32)` alone is 240.6 MiB, and cpu_golden's nine-term blur
holds several such temporaries at once — measured 1022 MiB peak, roughly 3x
the available userspace, i.e. an OOM kill (SIGKILL, exit 137, no traceback)
during "generating the page" rather than any verdict about the DMA.  The
same applies to generating the page (541 MiB whole-page) and to reporting a
mismatch (np.argwhere over ~31 M mismatching pixels is ~480 MiB of indices,
which would blow up precisely in the stuck-data-lane case the diagnostic
exists for).  Keep every array in this file bounded by STRIP_ROWS.

Measured peak for the strip version, tracemalloc at the full 9856 x 6400:
89.1 MiB to build the page, 98.7 MiB with a comparison strip in flight and
the 60.2 MiB page resident.  Against the ~290 MiB of userspace a 192 MiB
CMA pool leaves, that is roughly 3x headroom, where the whole-page version
needed 3x more than exists.

Needs on the board, same directory: tme_driver.py, tme_standalone_bringup.py,
binarize_dma_checks.py, safe_teardown.py, and the overlay .bit + .hwh pair.

Exit status: 0 = full-size transfer moved and verified bit-exact,
1 = the gate FAILS (the transfer or the data is wrong),
2 = could not run (missing module, missing or unloadable overlay, no PYNQ).

And one outcome that is NOT an exit status: if a DMA cannot be proved halted
AND the PL cannot then be reprogrammed, this gate does not exit at all.  It
holds its ~120 MiB of CMA buffers and blocks, because exiting would hand them
back while an engine may still be writing them.  See `safe_teardown`; the
answer is a POWER CYCLE, not `reboot` and not `kill -9`.

`--selftest` proves the strip decomposition against whole-page cpu_golden on
a small image, with no PYNQ and no board.  Run it after touching anything in
here: a strip boundary that silently drops or duplicates a row would make
the gate pass on a broken transfer.

The gray page is procedural: a coprime-stride ramp, a 64-pixel checkerboard,
and a low-bit parity term.  The first two give the binary result structure in
every region, so a stuck data lane cannot hide the way it could behind an
all-flat page.  The parity term is what makes the result sensitive to
TRUNCATION specifically — see `fill_page_strip`, and `--selftest`, which
fails if a rounding oracle would produce the same page.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

import safe_teardown

IMG_W = 9856      # contract §2 maxima; keep in sync with PE_MAX_IMG_W/H
IMG_H = 6400
THRESHOLD = 140   # same value the three-stage C/golden case pinned

# Rows per strip.  256 x 9856 x 4 B = ~10 MB per int32 temporary, so the
# nine-term blur peaks around 40 MB — comfortably inside userspace with the
# 60.2 MiB page also resident.
STRIP_ROWS = 256


def fill_page_strip(out: np.ndarray, y0: int) -> None:
    """Write rows [y0, y0+len(out)) of the procedural page into `out`.

    Same arithmetic a whole-page expression would produce, evaluated one
    strip at a time.

    The steps are chosen so that NO two adjacent pixels are ever equal, in
    either direction: 251 and 199 are coprime with 256, so a horizontal step
    changes the value by -5 (mod 256) and a vertical step by -57, and the
    64-pixel checkerboard offsets its cells by +128 (mod 256), so a step that
    also crosses a cell boundary changes the value by -5+-128 = 123 or
    -57+-128 = 71.  The parity term below shifts each of those by +-1, and
    none of the results is 0.  (The first draft halved the checkerboard cells
    instead; `--selftest` caught that floor(v/2) can collide with an
    unhalved neighbour, leaving flat pixel pairs on the one page the gate
    uses to catch a stuck data lane.)

    THE PARITY TERM IS WHAT MAKES THIS A TEST OF *TRUNCATION*.  Without it
    every 3x3 Gaussian weighted sum over this page is divisible by 16, so
    `sum >> 4` is exact and a core that ROUNDED (`(sum + 8) >> 4`) would
    produce byte-identical output — the gate would have verified arithmetic
    it cannot distinguish from the arithmetic it claims to check.  That is
    not an accident of the constants: for a linear ramp the symmetric kernel
    gives `16 * centre`, the mod-256 wrap subtracts multiples of 256, and
    the checkerboard adds multiples of 128 — all divisible by 16.

    Adding `(x + y) & 1` fixes it exactly.  Over the 3x3 window the parity
    term contributes weight 8 whichever way the centre parity falls (the
    four even-offset taps sum to 8 and the four odd-offset taps sum to 8),
    so every weighted sum becomes `16k + 8`: the truncating and rounding
    forms now differ by one grey level at EVERY pixel, and differ in the
    binarised output wherever that level straddles the threshold.
    `--selftest` asserts the two oracles really do disagree on this page.
    """
    h = out.shape[0]
    x = np.arange(IMG_W, dtype=np.uint32)
    y = np.arange(y0, y0 + h, dtype=np.uint32)
    # (h, IMG_W) uint32 temporary, ~10 MB at STRIP_ROWS=256.
    strip = x[None, :] * np.uint32(251) + y[:, None] * np.uint32(199)
    mask = ((y[:, None] // 64) + (x[None, :] // 64)) % 2 == 0
    strip[mask] += np.uint32(128)
    strip += (x[None, :] + y[:, None]) & np.uint32(1)     # breaks 16 | sum
    out[:] = (strip % 256).astype(np.uint8)


def build_page() -> np.ndarray:
    """The full 60.2 MiB uint8 page, filled strip by strip."""
    page = np.empty((IMG_H, IMG_W), dtype=np.uint8)
    for y0 in range(0, IMG_H, STRIP_ROWS):
        y1 = min(y0 + STRIP_ROWS, IMG_H)
        fill_page_strip(page[y0:y1], y0)
    return page


def compare_strips(binary: np.ndarray, gray: np.ndarray, cpu_golden,
                   threshold: int = THRESHOLD,
                   strip_rows: int = STRIP_ROWS) -> tuple:
    """Compare the PL output against the CPU oracle, strip by strip.

    Returns (total_mismatches, first_mismatch) where first_mismatch is
    (row, col, pl_value, cpu_value) or None.

    The oracle zeroes the outer border of whatever array it is given, so a
    strip is computed with one row of context on each side and the border
    rows of the sub-result are discarded: for logical rows [a, b),
    cpu_golden(gray[a-1:b+1])[1:-1] is exactly the whole-page result for
    those rows.  Logical row 0 and the final row are all-zero by §1's border
    rule (the oracle's own convention), so they are compared directly
    against zero.  `--selftest` proves this decomposition equals the
    whole-page oracle.
    """
    img_h = gray.shape[0]
    total = 0
    first = None

    def note(diff: np.ndarray, row_base: int, golden) -> None:
        nonlocal total, first
        cnt = int(np.count_nonzero(diff))
        if not cnt:
            return
        total += cnt
        if first is None:
            # argmax on the flattened bool, NOT argwhere: argwhere
            # materialises one index pair per mismatch, which is hundreds of
            # MB exactly when a data lane is stuck and half the page is
            # wrong — losing the diagnostic in its primary use case.
            r, c = divmod(int(np.argmax(diff)), diff.shape[1])
            cpu_val = 0 if golden is None else int(golden[r, c])
            first = (row_base + r, c, int(binary[row_base + r, c]), cpu_val)

    # Border rows: all zero.
    for r in (0, img_h - 1):
        note(binary[r:r + 1] != 0, r, None)

    for a in range(1, img_h - 1, strip_rows):
        b = min(a + strip_rows, img_h - 1)
        golden = cpu_golden(gray[a - 1:b + 1], threshold)[1:-1]
        note(binary[a:b] != golden, a, golden)
    return total, first


def selftest() -> int:
    """Prove strip-wise comparison == whole-page oracle, without a board."""
    from binarize_dma_checks import cpu_golden

    rng = np.random.default_rng(20260811)
    failures = 0
    # Sizes chosen to exercise: an exact multiple of the strip, a partial
    # final strip, a page shorter than one strip, and the 3-row minimum.
    for (h, w), strip in (((64, 40), 16), ((70, 40), 16), ((9, 33), 16),
                          ((3, 5), 4), ((65, 17), 1)):
        gray = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
        whole = cpu_golden(gray, THRESHOLD)

        # A correct PL: strip comparison must find nothing.
        n, first = compare_strips(whole, gray, cpu_golden, THRESHOLD, strip)
        if n or first:
            failures += 1
            print(f"  FAIL {h}x{w} strip={strip}: clean page reported "
                  f"{n} mismatches at {first}")

        # A corrupted PL: every single-pixel corruption must be found, and
        # the reported location and values must be right.  This is what
        # catches a strip boundary that skips a row.
        for (r, c) in ((0, 0), (1, 1), (h // 2, w // 2), (h - 2, w - 2),
                       (h - 1, w - 1), (strip, 0), (strip + 1, w - 1)):
            if not (0 <= r < h and 0 <= c < w):
                continue
            bad = whole.copy()
            bad[r, c] ^= 0xFF
            n, first = compare_strips(bad, gray, cpu_golden, THRESHOLD, strip)
            want = (r, c, int(bad[r, c]), int(whole[r, c]))
            if n != 1 or first != want:
                failures += 1
                print(f"  FAIL {h}x{w} strip={strip}: corruption at "
                      f"({r},{c}) reported as n={n} first={first}, "
                      f"expected n=1 first={want}")

    # The procedural page must be deterministic and structured: no 3x3
    # neighbourhood flat, so a stuck data lane cannot hide.
    page = np.empty((STRIP_ROWS + 7, IMG_W), dtype=np.uint8)
    fill_page_strip(page, 0)
    again = np.empty_like(page)
    fill_page_strip(again, 0)
    if not np.array_equal(page, again):
        failures += 1
        print("  FAIL: fill_page_strip is not deterministic")
    # Strip-wise generation must equal generation at a row offset.
    mid = np.empty((8, IMG_W), dtype=np.uint8)
    fill_page_strip(mid, 100)
    if not np.array_equal(mid, page[100:108]):
        failures += 1
        print("  FAIL: fill_page_strip disagrees with itself at a row offset")
    sample = page[:, :1024].astype(np.int16)
    if int(np.min(np.abs(np.diff(sample, axis=1)))) == 0:
        failures += 1
        print("  FAIL: procedural page has horizontally-equal neighbours")
    if int(np.min(np.abs(np.diff(sample, axis=0)))) == 0:
        failures += 1
        print("  FAIL: procedural page has vertically-equal neighbours")

    # The page must actually be able to TELL TRUNCATION FROM ROUNDING.
    # Without the parity term every weighted sum is a multiple of 16, the
    # shift is exact, and a rounding core produces byte-identical output —
    # so the gate would verify arithmetic it cannot distinguish. Assert the
    # distinguisher exists rather than assuming the constants provide it.
    gi = page[:, :2048].astype(np.int32)
    wsum = (gi[:-2, :-2] + 2 * gi[:-2, 1:-1] + gi[:-2, 2:]
            + 2 * gi[1:-1, :-2] + 4 * gi[1:-1, 1:-1] + 2 * gi[1:-1, 2:]
            + gi[2:, :-2] + 2 * gi[2:, 1:-1] + gi[2:, 2:])
    nondivisible = int(np.count_nonzero(wsum % 16))
    if nondivisible != wsum.size:
        failures += 1
        print(f"  FAIL: {wsum.size - nondivisible}/{wsum.size} Gaussian "
              f"weighted sums are divisible by 16 — truncation is exact "
              f"there, so rounding is indistinguishable")
    trunc = np.where((wsum >> 4) <= THRESHOLD, 255, 0)
    rounded = np.where(((wsum + 8) >> 4) <= THRESHOLD, 255, 0)
    differing = int(np.count_nonzero(trunc != rounded))
    if differing == 0:
        failures += 1
        print("  FAIL: truncating and rounding oracles agree everywhere on "
              "this page — the gate cannot detect a rounding core")
    else:
        print(f"  truncation is testable: the rounding oracle differs at "
              f"{differing:,} of {wsum.size:,} sampled pixels")

    print(f"selftest: {'FAILED' if failures else 'PASSED'} "
          f"({failures} failure(s))")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", metavar="BITFILE")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds before the transfer is declared hung")
    ap.add_argument("--selftest", action="store_true",
                    help="check the strip decomposition offline; no board")
    args = ap.parse_args()

    if args.selftest:
        try:
            return selftest()
        except ImportError as exc:
            print(f"CANNOT RUN: {exc}")
            return 2
    if not args.overlay:
        ap.error("--overlay is required (or pass --selftest)")

    try:
        from tme_driver import PLPipeline
        from binarize_dma_checks import cpu_golden
    except ImportError as exc:
        print(f"CANNOT RUN: {exc} — copy tme_driver.py, "
              f"tme_standalone_bringup.py and binarize_dma_checks.py next to "
              f"this script")
        return 2

    n = IMG_W * IMG_H
    print(f"full-size DMA gate: {IMG_W} x {IMG_H} = {n:,} B each way")
    print(f"  strip size {STRIP_ROWS} rows "
          f"({STRIP_ROWS * IMG_W * 4 / 2**20:.0f} MB per int32 temporary)")

    t0 = time.monotonic()
    gray = build_page()
    print(f"  procedural page built in {time.monotonic() - t0:.1f} s "
          f"({gray.nbytes / 2**20:.1f} MiB resident)")

    # PLPipeline raises on any missing IP — no silent fallback, ever.  An
    # environment failure (no pynq, missing .bit/.hwh) is "could not run",
    # NOT a gate failure: reporting a forgotten file copy as a DMA fault
    # sends the next hour into hardware debugging.
    try:
        pl = PLPipeline(args.overlay, timeout_s=args.timeout)
    except (ImportError, FileNotFoundError, OSError) as exc:
        print(f"CANNOT RUN: could not load the overlay ({type(exc).__name__}: "
              f"{exc}) — check that {args.overlay} and its matching .hwh are "
              f"both present, and that PYNQ is installed")
        return 2
    except Exception as exc:                           # noqa: BLE001
        print(f"FAIL: overlay loaded but the driver rejected it: "
              f"{type(exc).__name__}: {exc}")
        return 1

    # BEFORE the transfer, not at teardown: SIGTERM/SIGHUP/SIGQUIT kill the
    # process outright, without running a `finally`, and this gate spends
    # minutes with 63 MB in flight each way.  No channel is armed yet, so
    # refusing here costs nothing but a re-run.
    try:
        armed = safe_teardown.arm_teardown_protection()
    except safe_teardown.TeardownUnprotected as exc:
        print(f"CANNOT RUN: {exc}")
        return safe_teardown.teardown(pl, args.overlay, 2)
    print(f"  teardown protection: ignoring {', '.join(armed)}")

    # NOT a bare `pl.close()` in a finally.  This gate allocates the two
    # biggest CMA buffers of the whole run (~120 MiB), and every hazard
    # board_gate_extract documents applies here first: the retained references
    # live only as long as this process, so an exit releases them.  One shared
    # implementation, in safe_teardown, for both gates.
    #
    # `status` starts at 1 and is only ever lowered by a completed run: an
    # exception before `_run` returns must not read as a pass.
    status = 1
    try:
        # cpu_golden is passed EXPLICITLY, not read as a global.  It is
        # imported into main()'s local scope above, so when `_run` was split
        # out of main it kept referencing a name that only ever existed here —
        # and the tests stub `_run` wholesale, so nothing caught it until the
        # board did: the transfer completed, the envelope was asserted, and
        # then the comparison died with `NameError: name 'cpu_golden' is not
        # defined`.  Threading it through the signature is what makes that
        # unrepresentable rather than merely fixed.
        status = _run(pl, gray, n, cpu_golden)
    except Exception as exc:                               # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}")
        status = 1
    finally:
        # May not return at all — see safe_teardown.fail_stop_holding.  The
        # reassignment is picked up by the `return status` BELOW the finally;
        # a `return` inside the try would have been evaluated first and would
        # discard it.
        status = safe_teardown.teardown(pl, args.overlay, status)
    return status


def _run(pl, gray, n: int, cpu_golden) -> int:
    """The gate proper.  Teardown is the caller's job, not this function's.

    `cpu_golden` is a parameter rather than a module global on purpose: it is
    imported inside `main()` (so that a missing binarize_dma_checks.py is
    "could not run", exit 2, rather than an import error at load time), which
    means it is NOT in scope here.  Reading it as a global cost a board run —
    see the note at the call site.
    """
    try:
        t0 = time.monotonic()
        binary = pl.binarize_page(gray, THRESHOLD)
        elapsed = time.monotonic() - t0
        print(f"  PL round trip (copy-in + 2 x {n:,} B DMA + core): "
              f"{elapsed:.2f} s")

        # ---- Assert the DMA ENVELOPE, not just the pixels ----
        #
        # This is the gate's actual claim and it does not follow from a
        # bit-exact compare.  A short S2MM leaves the tail of the buffer
        # holding whatever was there before; if that happened to match the
        # golden — or if the compare only ever looked at what was written —
        # the page would verify while the full 63,078,400-byte transfer had
        # never occurred.  binarize_page() checks these too; the gate
        # re-asserts them from the reported measurements so the claim is
        # made here, visibly, in the log that gets kept.
        stats = pl.last_transfer_stats
        if not stats:
            print("FAIL: the driver reported no transfer statistics, so the "
                  "full-envelope claim cannot be made")
            return 1
        print(f"  DMA envelope: S2MM {stats['s2mm_bytes']:,} B received "
              f"(measured), MM2S {stats['mm2s_bytes']:,} B programmed, "
              f"guard {stats['guard_bytes_checked']} B intact, "
              f"{stats['sentinel_bytes_remaining']} sentinel bytes left")
        envelope_errs = []
        if stats["mm2s_bytes"] != n:
            envelope_errs.append(
                f"MM2S length register reads {stats['mm2s_bytes']:,} B, "
                f"expected {n:,} (the programmed length is wrong)")
        if stats["s2mm_bytes"] != n:
            envelope_errs.append(
                f"S2MM received {stats['s2mm_bytes']:,} B, expected {n:,}")
        if stats["guard_bytes_clobbered"]:
            envelope_errs.append(
                f"{stats['guard_bytes_clobbered']} guard bytes past the page "
                f"were overwritten — the S2MM wrote beyond its bound")
        if stats["sentinel_bytes_remaining"]:
            envelope_errs.append(
                f"{stats['sentinel_bytes_remaining']:,} output bytes still "
                f"hold the pre-fill sentinel — never written by the PL")
        if envelope_errs:
            print("FAIL: the full-size DMA envelope was not met:")
            for e in envelope_errs:
                print(f"  - {e}")
            return 1

        t0 = time.monotonic()
        mism, first = compare_strips(binary, gray, cpu_golden)
        print(f"  verified against the CPU oracle in "
              f"{time.monotonic() - t0:.1f} s")

        if mism:
            r, c, pl_val, cpu_val = first
            print(f"FAIL: {mism:,} of {n:,} bytes mismatch the exact CPU "
                  f"oracle; first at logical ({c},{r}): PL={pl_val} "
                  f"CPU={cpu_val}")
            return 1

        print(f"PASS: {n:,} B received on S2MM, MM2S programmed to the "
              f"same, core reported done with both channels idle and no "
              f"error, guard intact, no unwritten bytes, and the output is "
              f"bit-exact against the TRUNCATING Gaussian oracle "
              f"({elapsed:.2f} s wall for the PL round trip).")
        return 0
    except Exception as exc:                           # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
