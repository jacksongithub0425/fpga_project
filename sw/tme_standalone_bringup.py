#!/usr/bin/env python3
"""Standalone hardware bring-up for template_match_core (tme_top).

Runs on the Zynq board under PYNQ, against a PL image containing ONLY
`tme_top` plus two MM2S-only AXI DMAs:

    axi_dma_patch.sendchannel  ->  tme_top.patch_stream
    axi_dma_templ.sendchannel  ->  tme_top.templ_stream

and one AXI4-Lite slave (`CTRL`) carrying start/done, the four geometry
scalars and the three result registers.  This mirrors what the extractor's
standalone image did for `patch_extract_core` (contract §8) — one core, its
DMAs, and nothing else, so a failure has one place to be.

    sudo -E python3 tme_standalone_bringup.py \
        --overlay  /home/xilinx/jupyter_notebooks/tme_test/tme_standalone.bit \
        --data-dir /home/xilinx/jupyter_notebooks/tme_test

Exit status: 0 = every case passed, 1 = a case failed, 2 = could not run.

-----------------------------------------------------------------------------
WHY THIS SUITE AND NOT THE COSIM ONE

The vectors come from `tb_tme_cases_hw.txt`, written by
`hls/template_match/tme_generate_golden.py`.  That suite is the cosim cases
plus two 820 x 307 stress cases, and those two are the reason it exists.  The
small cases are all under 15 KB; a board run limited to them verifies the
matcher's arithmetic against golden and says *nothing* about the two bounds
that only silicon can test:

  stress-max-envelope  contract §3.1 — one patch is one AXI DMA transfer, and
                       the platform caps that at 262,143 bytes (2^18 - 1, from
                       the DMA's 18-bit `c_sg_length_width`).  This case is a
                       single 251,740-byte transfer, 10,403 bytes under the
                       ceiling.  The bound lives in the block design, not in
                       any source file.
  stress-max-result    the maximum 817 x 304 result map, peak at its final
                       cell.  The envelope case only reaches 605 x 212, so
                       without this the top 212 entries of the column
                       accumulators are never written.

TWO cases also exist to make `result_score` worth reading: it crosses
AXI4-Lite as raw IEEE-754 bits that this script reinterprets, and before
`equality-negative` (-0.73) and `equality-different` (0.0096) were added every
score in the suite was exactly 0.0 or 1.0 — no sign bit, one mantissa bit.
Those two are still the only ones: the other seven hw scores are 0.0 or 1.0.

The same bytes run through csim (`csim_design -argv "hw"`), so a failure here
is a hardware finding rather than a bad golden.

-----------------------------------------------------------------------------
WHAT THIS DOES NOT COVER

- **Framing.** `tme_top` takes scalar `patch_w`/`patch_h` and reads exactly
  `patch_w * patch_h` beats.  It ignores `TLAST` entirely.  This script writes
  the geometry the manifest specifies, so the DMA length and the core's
  expectation agree by construction — which is exactly the agreement the
  extractor seam does NOT get for free (contract §7.1 / the OPEN item 3 in
  `package_provisional.tcl`).  A pass here says the matcher works when told
  the truth about its input; it says nothing about how it learns that truth.
- **Timing.** A bitstream is not timing closure and neither is a passing run.
  Read post-route WNS off the implementation report — see
  `vivado/tme_standalone/build_tme_standalone.tcl`, which writes it to
  `post_route_wns.txt`.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# CTRL register map.
#
# Transcribed from the GENERATED header, which is the authority:
#   hls/template_match/template_match_provisional/solution1/
#       .autopilot/db/driver/src/xtme_top_hw.h
#
# Contract §7.1.2: adding or reordering a port moves every offset after it, so
# these constants are a snapshot, not a specification.  `preflight()` below
# round-trips all four geometry registers before anything is started — that is
# what actually proves the offsets, and it is the check that would have caught
# the extractor's three-way interface split.
#
# All four scalars sit on 8-byte strides with a reserved word between them;
# that is HLS's own layout for 16-bit ports, not padding this script invented.
REG_AP_CTRL      = 0x00   # bit0 ap_start (COH), bit1 ap_done (COR),
                          # bit2 ap_idle, bit3 ap_ready (COR)
REG_PATCH_W      = 0x10
REG_PATCH_H      = 0x18
REG_TEMPL_W      = 0x20
REG_TEMPL_H      = 0x28
REG_RESULT_SCORE = 0x30   # IEEE-754 float32, read as a raw 32-bit word
REG_SCORE_VLD    = 0x34   # bit0 ap_vld (Clear-on-Read)
REG_RESULT_X     = 0x40
REG_X_VLD        = 0x44   # bit0 ap_vld (COR)
REG_RESULT_Y     = 0x50
REG_Y_VLD        = 0x54   # bit0 ap_vld (COR)

AP_START = 1 << 0
AP_DONE  = 1 << 1
AP_IDLE  = 1 << 2
AP_READY = 1 << 3

# AXI DMA register offsets within one channel's block (PG021 Table 2-1), used
# ONLY on the teardown path — see `close()`.  PYNQ's channel object exposes the
# `_mmio` and `_offset` these are applied to; going under the driver is
# deliberate there, because `channel.stop()` busy-spins without a deadline.
DMA_DMACR    = 0x00       # bit0 RS (run/stop), bit2 Reset (self-clearing)
DMA_DMASR    = 0x04       # bit0 Halted, bit1 Idle
DMACR_RS     = 1 << 0
DMACR_RESET  = 1 << 2
DMASR_HALTED = 1 << 0

# Buffers whose DMA could not be proved halted. Holding a reference keeps
# `PynqBuffer.__del__` from calling `freebuffer()` while this process lives.
# That is a DELAY, not a quarantine — see `close()`.
_UNSAFE_TO_FREE: list = []

# Contract §4.1 / §3 envelope.  Must track MAX_* in hls/template_match/tme_top.h
# and PE_MAX_PATCH_* in hls/patch_extract/patch_extract_core.h.
MAX_PATCH_W, MAX_PATCH_H = 820, 307
MAX_TEMPL_W, MAX_TEMPL_H = 216, 96
MIN_TEMPL_DIM = 4

# Contract §3.1.  A fallback only: the DMA's own reported buffer_max_size wins
# when PYNQ exposes it, because this number is a block-design parameter and a
# BD edit can raise it to 26 bits without touching anything in this repo.
DMA_MAX_BYTES_DEFAULT = 262143

# stress-max-envelope takes 13.362 s on silicon at 31.25 MHz — MEASURED
# 2026-08-07 on the standalone image, not derived.  (stress-max-result: 0.676 s.)
# The pre-silicon derivation said 372-411M cycles / 11.9-13.2 s; the real figure
# is ~1.6% above the top of that bracket, which is about what the per-case DMA
# setup and the 1 ms poll granularity account for.  Keep quoting the measured
# number.
# A 5 s timeout borrowed from the extractor's driver would report a hang on a
# perfectly healthy run; 120 s clears the measured worst case 9x.
DEFAULT_TIMEOUT_S = 120.0

SCORE_TOL = 0.005        # matches MAX_SCORE_ERR in tme_tb.cpp


# ---------------------------------------------------------------------------
# Geometry validation — pure, importable without PYNQ, and MANDATORY.
# ---------------------------------------------------------------------------

def validate_geometry(patch_w: int, patch_h: int, templ_w: int, templ_h: int,
                      dma_max_bytes: int = DMA_MAX_BYTES_DEFAULT) -> list[str]:
    """Return a list of reasons this geometry must not be sent to the matcher.

    Empty list means legal.  **`tme_top` does not reject anything** — it has no
    validation path, no reason bitmask and no status register; it takes the
    four scalars at face value and indexes its BRAMs with them.  So every one
    of these is a silent-corruption or hang mode on real hardware, not an error
    the core will report:

      - `patch_w > 820` / `patch_h > 307` overrun `patch_buf[307][820]`.  The
        write wraps into the neighbouring row (or past the array), so the
        matcher correlates against pixels from the wrong place and returns a
        confident, wrong answer.
      - `templ_w > 216` / `templ_h > 96` overrun `templ_buf` and the
        fully-partitioned `t_row[MAX_TEMPL_W]` register file the same way.
      - `patch_w < templ_w` (or the height equivalent) makes `rw = pw-tw+1`
        non-positive, so the search space is empty: every loop falls through
        and `result_score` comes back as the core's initialiser, -2.0, at
        (0,0).  `check_result()` below names that sentinel rather than
        reporting a score of -2.0 as data.
      - `patch_w * patch_h > dma_max_bytes` is §3.1: the DMA truncates the
        transfer at its 18-bit length register, the core blocks in
        `patch_stream.read()` waiting for beats that will never arrive, and
        `ap_done` never rises.  That one at least fails loudly — as a timeout.

    Equality is legal in both dimensions: §4.4 option 1 is adopted, so
    `patch_w == templ_w` yields a 1-wide result map, not an empty one.
    """
    errs: list[str] = []

    if not MIN_TEMPL_DIM <= templ_w <= MAX_TEMPL_W:
        errs.append(f"templ_w {templ_w} outside [{MIN_TEMPL_DIM}, "
                    f"{MAX_TEMPL_W}] (§4.1)")
    if not MIN_TEMPL_DIM <= templ_h <= MAX_TEMPL_H:
        errs.append(f"templ_h {templ_h} outside [{MIN_TEMPL_DIM}, "
                    f"{MAX_TEMPL_H}] (§4.1)")
    if not 1 <= patch_w <= MAX_PATCH_W:
        errs.append(f"patch_w {patch_w} outside [1, {MAX_PATCH_W}] "
                    f"(§3 envelope)")
    if not 1 <= patch_h <= MAX_PATCH_H:
        errs.append(f"patch_h {patch_h} outside [1, {MAX_PATCH_H}] "
                    f"(§3 envelope)")
    if patch_w < templ_w:
        errs.append(f"patch_w {patch_w} < templ_w {templ_w}: empty result map "
                    f"(§4.4 — equality is legal, less-than is not)")
    if patch_h < templ_h:
        errs.append(f"patch_h {patch_h} < templ_h {templ_h}: empty result map "
                    f"(§4.4 — equality is legal, less-than is not)")

    patch_bytes = patch_w * patch_h
    if patch_bytes > dma_max_bytes:
        errs.append(f"patch {patch_w}x{patch_h} = {patch_bytes:,} B exceeds "
                    f"the single-transfer bound of {dma_max_bytes:,} B "
                    f"(§3.1) — the DMA would truncate and the core would hang")
    templ_bytes = templ_w * templ_h
    if templ_bytes > dma_max_bytes:
        errs.append(f"template {templ_w}x{templ_h} = {templ_bytes:,} B "
                    f"exceeds the single-transfer bound of {dma_max_bytes:,} B "
                    f"(§3.1)")
    return errs


def validate_template_content(templ, templ_w: int, templ_h: int) -> list[str]:
    """Return reasons this template's PIXELS must not be sent to the matcher.

    Geometry is `validate_geometry`'s job.  This is about content, and there is
    exactly one content rule — contract §4.6:

        dt = N·ΣT² − (ΣT)²   must be > 0,

    i.e. the template must not be flat.  `min == max` is the *exact* test, not
    an approximation of one: `dt = ½ ΣᵢΣⱼ (Tᵢ − Tⱼ)²`, which is zero if and
    only if every pixel is equal.  No threshold and no tolerance is involved,
    and none should be introduced — the smallest legal nonzero `dt` is 15 (a
    4×4 template with one pixel one grey level off), and that template is
    perfectly legal input.

    A flat template is illegal input, not a degenerate case with a defined
    answer:

      - `tme_top` returns 0.0 for it — a defensive fallback that only keeps
        0/0 out of `result_score`, explicitly NOT a contract value.
      - OpenCV may return **ones**, or a patch-dependent numerical result
        **including zero**; no contractual agreement exists on this illegal
        domain.  Its `templNorm < DBL_EPSILON` early return fills the ENTIRE
        result map with ones (historical behaviour: OpenCV issue #5688), but
        `templNorm` is a double-scaled variance, so a mathematically flat
        template does not always reach that branch: a 7x7 filled with 2
        computes `templNorm = 4.44e-16 > DBL_EPSILON` and gets correlated like
        any other template.  What that then scores is **patch-dependent** —
        §4.6 pins one patch and gets ~5.5e-08 from an all-2 template and
        exactly 0.0 from an all-127 one — so no score is quoted here without
        the patch that produced it.  Measured on the OpenCV installed in
        `hls/.venv` (5.0.0); see §4.6.

    So a DUT 0.0 that happens to equal a cv2 0.0 here is a coincidence, not
    agreement, and nothing may be built on it.  Note which side of this is
    exact: `min == max` decides on the integers and is right every time, while
    OpenCV's epsilon test is the one that can be fooled.  That is an argument
    for doing the rejection here, not for deferring to cv2.

    **Call this AFTER the final resize / binarisation / cropping**, on the
    exact contiguous byte buffer that will be handed to the DMA.  Validating a
    pre-resize template proves nothing: downscaling a two-pixel stroke can
    produce a flat result from a perfectly non-flat source, and a crop can cut
    a window that contains only background.

    The buffer must be **`bytes`**, and nothing else is accepted:

      - a `bytearray` can change between this check and the transfer, which
        would make this function decorative;
      - a *read-only* `memoryview` proves nothing either.  `readonly` says this
        view cannot write, not that the memory is immutable —
        `memoryview(bytearray(...)).toreadonly()` passes every flag test while
        the underlying `bytearray` stays writable through its own name;
      - a multi-dimensional `memoryview` breaks the checks themselves rather
        than failing them: `len()` returns the first dimension instead of the
        byte count, and `min()`/`max()` raise `NotImplementedError`.

    The production path (`load_manifest` -> slice of a `bytes` blob ->
    `run_case`) is `bytes` end to end, so this costs the caller nothing.
    """
    errs: list[str] = []

    if not isinstance(templ, bytes):
        errs.append(
            f"template buffer is {type(templ).__name__}, not bytes — only an "
            f"immutable bytes object can be validated and then DMA'd with the "
            f"guarantee that the two saw the same pixels. A read-only "
            f"memoryview does not qualify: it can alias a mutable bytearray, "
            f"and a multi-dimensional one would break len()/min()/max() here "
            f"(§4.6)")
        return errs

    n = len(templ)
    expect = templ_w * templ_h
    if n == 0:
        errs.append("template buffer is empty — there is nothing to match "
                    "against, and the core would read beats that never arrive")
    elif n != expect:
        errs.append(
            f"template buffer is {n} B but geometry {templ_w}x{templ_h} says "
            f"{expect} B — the core reads exactly templ_w*templ_h beats and "
            f"ignores TLAST, so a padded or short buffer either desynchronises "
            f"the stream or silently matches against the wrong pixels")
    if n and min(templ) == max(templ):
        errs.append(
            f"flat template: all {n} pixels are {min(templ)}, so "
            f"dt = N·ΣT² − (ΣT)² = 0. Illegal input (§4.6) — tme_top would "
            f"return 0.0 and cv2 may return ones or a patch-dependent value, "
            f"including zero; there is no agreed answer on this domain. "
            f"Reject before the first DMA.")
    return errs


def validate_template_bank(cases, templs) -> list[tuple[int, str, list[str]]]:
    """Content-validate every template in a manifest bank.

    Returns `[(index, tag, reasons), ...]` for each offending case; an empty
    list means the whole bank is safe to launch.  Whole-bank, not per-case, and
    called before the first DMA or `ap_start`: discovering a flat entry after
    five cases have already run leaves the streams in a state nothing here can
    reason about, and the run has already been meaningless for one candidate.

    Keyed by index rather than tag so a duplicated tag cannot hide a rejection.
    """
    bad: list[tuple[int, str, list[str]]] = []
    for c in cases:
        templ = templs[c.templ_off:c.templ_off + c.templ_bytes]
        errs = validate_template_content(templ, c.tw, c.th)
        if errs:
            bad.append((c.index, c.tag, errs))
    return bad


def check_result(score: float, x: int, y: int,
                 patch_w: int, patch_h: int,
                 templ_w: int, templ_h: int) -> list[str]:
    """Sanity-check a result against the contract, independent of any golden.

    Separate from the golden comparison on purpose: these are the checks that
    would still catch a broken run if the golden itself were wrong.
    """
    errs: list[str] = []
    rw = patch_w - templ_w + 1
    rh = patch_h - templ_h + 1

    if score == -2.0:
        errs.append("score is exactly -2.0 — that is tme_top's best_score "
                    "initialiser, i.e. norm_cols never executed and no window "
                    "was ever scored")
    elif not -1.0 <= score <= 1.0:
        errs.append(f"score {score!r} outside [-1, 1]; TM_CCOEFF_NORMED is "
                    f"clamped to that range in tme_top, so this is not a "
                    f"rounding artefact")
    if not 0 <= x < rw:
        errs.append(f"result_x {x} outside the result map width {rw}")
    if not 0 <= y < rh:
        errs.append(f"result_y {y} outside the result map height {rh}")
    return errs


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class Case:
    __slots__ = ("index", "pw", "ph", "tw", "th", "patch_off", "templ_off",
                 "score", "x", "y", "margin", "category", "tag")

    def __init__(self, fields: list[str]):
        (idx, pw, ph, tw, th, poff, toff,
         score, gx, gy, margin, category, tag) = fields
        self.index     = int(idx)
        self.pw        = int(pw)
        self.ph        = int(ph)
        self.tw        = int(tw)
        self.th        = int(th)
        self.patch_off = int(poff)
        self.templ_off = int(toff)
        self.score     = float(score)
        self.x         = int(gx)
        self.y         = int(gy)
        self.margin    = float(margin)
        self.category  = category
        self.tag       = tag

    @property
    def patch_bytes(self) -> int:
        return self.pw * self.ph

    @property
    def templ_bytes(self) -> int:
        return self.tw * self.th


def load_manifest(data_dir: Path, suite: str = "hw"):
    """Parse tb_tme_cases_<suite>.txt and its two blobs.

    The blob sizes in the header are part of the contract, exactly as in
    `tme_tb.cpp:read_blob` — a short or long blob means the generator and the
    manifest disagree, and running anyway would correlate against whatever
    followed in the file.
    """
    cases_path = data_dir / f"tb_tme_cases_{suite}.txt"
    patch_path = data_dir / f"tb_tme_patches_{suite}.bin"
    templ_path = data_dir / f"tb_tme_templs_{suite}.bin"

    for p in (cases_path, patch_path, templ_path):
        if not p.is_file():
            raise FileNotFoundError(
                f"{p} not found — copy the tb_tme_*_{suite}.* files from "
                f"hls/template_match/ to the board, or run "
                f"tme_generate_golden.py to create them")

    lines = [ln for ln in cases_path.read_text().splitlines() if ln.strip()]
    n_cases, patch_bytes, templ_bytes = (int(v) for v in lines[0].split())

    cases = []
    for i, line in enumerate(lines[1:1 + n_cases]):
        fields = line.split()
        if len(fields) != 13:
            raise ValueError(f"{cases_path}: row {i} has {len(fields)} fields, "
                             f"expected 13")
        c = Case(fields)
        if c.index != i:
            raise ValueError(f"{cases_path}: row {i} is indexed {c.index}")
        cases.append(c)
    if len(cases) != n_cases:
        raise ValueError(f"{cases_path}: header says {n_cases} cases, found "
                         f"{len(cases)}")

    patches = patch_path.read_bytes()
    templs  = templ_path.read_bytes()
    if len(patches) != patch_bytes:
        raise ValueError(f"{patch_path}: {len(patches)} B, manifest says "
                         f"{patch_bytes} B")
    if len(templs) != templ_bytes:
        raise ValueError(f"{templ_path}: {len(templs)} B, manifest says "
                         f"{templ_bytes} B")

    for c in cases:
        if c.patch_off + c.patch_bytes > patch_bytes:
            raise ValueError(f"case {c.tag}: patch slice runs past the blob")
        if c.templ_off + c.templ_bytes > templ_bytes:
            raise ValueError(f"case {c.tag}: template slice runs past the blob")

    return cases, patches, templs


# ---------------------------------------------------------------------------
# Board driver
# ---------------------------------------------------------------------------

def _find_ip(overlay, *substrings):
    """First overlay IP whose name contains any of `substrings`, or (None, None)."""
    for want in substrings:
        for name in overlay.ip_dict:
            if want.lower() in name.lower():
                return getattr(overlay, name), name
    return None, None


class TmeStandalone:
    """PS-side sequencer for the standalone template_match_core image."""

    def __init__(self, overlay_path: str, patch_dma: str | None = None,
                 templ_dma: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 expect_fclk_mhz: float | None = None,
                 fclk_tol_mhz: float = 0.01):
        from pynq import Overlay, allocate

        # A non-finite timeout makes every `time.monotonic() > deadline` test
        # false, turning every wait loop into an unkillable spin; a
        # non-positive one fires before the core can possibly have finished.
        # Both are silent, so check rather than trust.
        if not (timeout_s > 0) or timeout_s in (float("inf"),):
            raise ValueError(
                f"timeout {timeout_s!r} must be finite and positive; "
                f"stress-max-envelope needs 12-13 s at 31.25 MHz, so the "
                f"default is {DEFAULT_TIMEOUT_S:g}")
        self.timeout_s = float(timeout_s)
        self.ol = Overlay(overlay_path)

        # Measure the PL clock rather than assuming it.  Contract §8 records
        # 31.25 MHz and explains it as Vivado's 50 MHz request (computed
        # against an assumed 1600 MHz source) meeting a 1000 MHz PLL through
        # the same 8x4 divisors — but that explanation has been an INFERENCE.
        # The overlay is loaded; the number is one attribute away, and every
        # elapsed-time figure this script prints is only interpretable with
        # it.
        # FAIL-CLOSED when --expect-fclk-mhz is given.  Warning was the wrong
        # policy for a timing measurement: every elapsed time this script
        # prints is only interpretable against the clock, so a wrong OR
        # UNREADABLE clock must stop the run, not annotate it.  An unreadable
        # clock is the more dangerous of the two — it produces a run that looks
        # clean and whose numbers mean nothing.
        try:
            from pynq import Clocks
            self.fclk0_mhz = float(Clocks.fclk0_mhz)
            period_ns = 1000.0 / self.fclk0_mhz
            print(f"\nPL clock (measured): {self.fclk0_mhz:.4f} MHz "
                  f"({period_ns:.3f} ns)")
        except Exception as exc:                       # noqa: BLE001
            self.fclk0_mhz = None
            print(f"\nPL clock: could not read Clocks.fclk0_mhz ({exc}); "
                  f"elapsed times below cannot be converted to cycles.")
            if expect_fclk_mhz is not None:
                raise RuntimeError(
                    f"--expect-fclk-mhz {expect_fclk_mhz:g} was requested but "
                    f"the PL clock could not be read ({exc}). Refusing to run: "
                    f"an unverifiable clock makes every elapsed time below "
                    f"uninterpretable.") from exc

        if expect_fclk_mhz is not None:
            delta = abs(self.fclk0_mhz - expect_fclk_mhz)
            if delta > fclk_tol_mhz:
                raise RuntimeError(
                    f"PL clock is {self.fclk0_mhz:.4f} MHz, expected "
                    f"{expect_fclk_mhz:g} +/- {fclk_tol_mhz:g} MHz "
                    f"(off by {delta:.4f}). Refusing to run: the wrong overlay "
                    f"is loaded, or the PS7 divisors did not land on the "
                    f"requested frequency.")
            print(f"  FCLK0 gate: PASS — {self.fclk0_mhz:.4f} MHz is within "
                  f"{fclk_tol_mhz:g} MHz of the required {expect_fclk_mhz:g}")
        elif self.fclk0_mhz is not None:
            # No gate requested: state the clock and stop there.  This script
            # used to print 31.25 MHz / 20 ns guidance here; that was written
            # for the 50 MHz-request shipping image and is stale for any other
            # build, so it is not reproduced.  Pass --expect-fclk-mhz to make
            # the clock a gate instead of a note.
            print("  (no --expect-fclk-mhz given: the clock is reported, not "
                  "gated. Elapsed times below scale with it.)")

        print("\nIPs in overlay:")
        for name in sorted(self.ol.ip_dict):
            entry = self.ol.ip_dict[name]
            print(f"  {name:<28} {entry.get('type', '?'):<44} "
                  f"base=0x{entry.get('phys_addr', 0):08X}")

        self.tme, tme_name = _find_ip(self.ol, "tme_top", "template_match")
        if self.tme is None:
            raise RuntimeError("no tme_top / template_match IP in the overlay")

        if patch_dma:
            dma_p, p_name = getattr(self.ol, patch_dma), patch_dma
        else:
            dma_p, p_name = _find_ip(self.ol, "dma_patch", "axi_dma_0")
        if templ_dma:
            dma_t, t_name = getattr(self.ol, templ_dma), templ_dma
        else:
            dma_t, t_name = _find_ip(self.ol, "dma_templ", "axi_dma_1")
        if dma_p is None or dma_t is None:
            raise RuntimeError(
                "need two AXI DMAs; found patch=%r templ=%r. Pass --patch-dma "
                "/ --templ-dma if the block design names them differently."
                % (p_name, t_name))
        if dma_p is dma_t:
            raise RuntimeError(
                f"both streams resolved to the same DMA ({p_name}). The patch "
                f"and template are two independent MM2S transfers that overlap "
                f"in time — one DMA cannot serve both.")

        print(f"\nUsing: matcher={tme_name}  patch DMA={p_name}  "
              f"templ DMA={t_name}")

        # MM2S only.  A missing sendchannel is a block-design error, and it is
        # worth failing on by name here rather than as an AttributeError deep
        # inside the first transfer.
        self.ch_patch = self._send_channel(dma_p, p_name)
        self.ch_templ = self._send_channel(dma_t, t_name)

        self.dma_max = min(self._max_size(dma_p, self.ch_patch),
                           self._max_size(dma_t, self.ch_templ))
        print(f"DMA single-transfer bound: {self.dma_max:,} B "
              f"(contract §3.1 assumes {DMA_MAX_BYTES_DEFAULT:,})")
        if self.dma_max != DMA_MAX_BYTES_DEFAULT:
            print(f"  NOTE: this differs from §3.1's recorded value. The "
                  f"bound is a block-design parameter (c_sg_length_width); "
                  f"update §3.1 rather than this script.")

        # One allocation per stream, sized to the envelope, reused by every
        # case.  Allocating per case would make the stress case's 251,740 B
        # contiguous request depend on CMA fragmentation late in the run.
        self.patch_buf = allocate(shape=(MAX_PATCH_W * MAX_PATCH_H,),
                                  dtype="u1")
        self.templ_buf = allocate(shape=(MAX_TEMPL_W * MAX_TEMPL_H,),
                                  dtype="u1")
        print(f"Buffers: patch {len(self.patch_buf):,} B @ "
              f"0x{self.patch_buf.physical_address:X}, template "
              f"{len(self.templ_buf):,} B @ "
              f"0x{self.templ_buf.physical_address:X}")

    @staticmethod
    def _send_channel(dma, name):
        try:
            ch = dma.sendchannel
        except Exception as exc:                      # noqa: BLE001
            raise RuntimeError(
                f"{name} has no MM2S sendchannel ({exc}) — both matcher "
                f"streams are PL inputs, so both DMAs must be "
                f"read-from-memory (MM2S) enabled") from exc
        if ch is None:
            raise RuntimeError(f"{name} has no MM2S sendchannel")
        return ch

    @staticmethod
    def _max_size(dma, channel) -> int:
        """The DMA's own single-transfer ceiling, however PYNQ exposes it."""
        for obj, attr in ((dma, "buffer_max_size"), (channel, "_max_size"),
                          (channel, "buffer_max_size")):
            val = getattr(obj, attr, None)
            if isinstance(val, int) and val > 0:
                return val
        print("  WARNING: PYNQ did not report a DMA transfer bound; falling "
              f"back to §3.1's {DMA_MAX_BYTES_DEFAULT:,} B. If the block "
              "design widened c_sg_length_width this is now wrong in the "
              "safe direction.")
        return DMA_MAX_BYTES_DEFAULT

    # -- register helpers ---------------------------------------------------

    def _w(self, off: int, val: int) -> None:
        self.tme.write(off, val)

    def _r(self, off: int) -> int:
        return self.tme.read(off)

    def preflight(self) -> bool:
        """Prove the CTRL offsets and the reset state before running anything.

        Contract §7.1.2: the generated header is the authority and adding a
        port moves every offset after it.  A write/readback on all four
        geometry registers is what actually catches that — a name check
        against `register_map` would not, since the names survive a
        re-layout.
        """
        print("\n--- preflight ---")
        ok = True

        ctrl = self._r(REG_AP_CTRL)
        idle = bool(ctrl & AP_IDLE)
        print(f"  ap_idle after reset: {idle}  (AP_CTRL=0x{ctrl:08X})")
        ok &= idle
        if not idle:
            print("  FAIL: the core is not idle before we have started it")

        rmap = getattr(self.tme, "register_map", None)
        if rmap is not None:
            print(f"  register_map:\n{rmap}")

        # Walking patterns within each field's 16 bits, plus the real values
        # the suite will use.  0xFFFF proves the field is 16 bits wide and not
        # aliased onto a neighbour.
        patterns = [0x0000, 0xFFFF, 0x5555, 0xAAAA, 0x0001, 0x8000,
                    MAX_PATCH_W, MAX_TEMPL_W]
        for name, off in (("patch_w", REG_PATCH_W), ("patch_h", REG_PATCH_H),
                          ("templ_w", REG_TEMPL_W), ("templ_h", REG_TEMPL_H)):
            bad = []
            for pat in patterns:
                self._w(off, pat)
                got = self._r(off) & 0xFFFF
                if got != pat:
                    bad.append((pat, got))
            if bad:
                ok = False
                print(f"  FAIL: {name} @0x{off:02X} round-trip: {bad}")
            else:
                print(f"  PASS: {name} @0x{off:02X} round-trips 16 bits")

        # Independence: the four registers must not alias each other.  Writing
        # distinct values and reading them all back afterwards is the check;
        # doing it per-register above would pass even if all four were one
        # register.
        distinct = {REG_PATCH_W: 0x1234, REG_PATCH_H: 0x2345,
                    REG_TEMPL_W: 0x3456, REG_TEMPL_H: 0x0456}
        for off, val in distinct.items():
            self._w(off, val)
        aliased = [f"0x{off:02X}: wrote 0x{val:04X}, read 0x{self._r(off) & 0xFFFF:04X}"
                   for off, val in distinct.items()
                   if (self._r(off) & 0xFFFF) != val]
        if aliased:
            ok = False
            print("  FAIL: geometry registers alias each other — " +
                  "; ".join(aliased))
        else:
            print("  PASS: the four geometry registers are independent")

        ctrl = self._r(REG_AP_CTRL)
        if not ctrl & AP_IDLE:
            ok = False
            print("  FAIL: writing scalars disturbed ap_idle — spurious start")
        else:
            print("  PASS: still idle after the register writes")

        return bool(ok)

    # -- one invocation -----------------------------------------------------

    def run_case(self, patch: bytes, templ: bytes,
                 pw: int, ph: int, tw: int, th: int):
        """Configure, start, wait, read back.  Returns (score, x, y, seconds).

        Raises ValueError on illegal geometry or illegal template content
        BEFORE touching `ap_start` — that is the whole point of
        `validate_geometry` and `validate_template_content`, since the core has
        no rejection path of its own.

        `main()` validates the entire bank before the first transfer, so on
        that path these checks can only pass.  They are repeated here for
        direct callers (a notebook, a bisect script) that never went through
        the manifest at all, and because the object validated here is the exact
        one copied into the DMA buffer a few lines below.
        """
        errs = validate_geometry(pw, ph, tw, th, self.dma_max)
        if errs:
            raise ValueError(
                f"refusing to start the matcher on {pw}x{ph} / {tw}x{th}:\n"
                + "\n".join(f"    - {e}" for e in errs))
        # Content check on the same immutable buffer that is DMA'd below; this
        # subsumes the template length check (§4.6).
        errs = validate_template_content(templ, tw, th)
        if errs:
            raise ValueError(
                f"refusing to start the matcher on this {tw}x{th} template:\n"
                + "\n".join(f"    - {e}" for e in errs))
        if len(patch) != pw * ph:
            raise ValueError(f"patch slice is {len(patch)} B, geometry says "
                             f"{pw * ph} B — the core reads exactly "
                             f"patch_w*patch_h beats and ignores TLAST, so a "
                             f"mismatch desynchronises every later case")

        ctrl = self._r(REG_AP_CTRL)
        if not ctrl & AP_IDLE:
            raise RuntimeError(f"core not idle before start "
                               f"(AP_CTRL=0x{ctrl:08X}); a previous case "
                               f"probably left beats in a stream")

        # np.frombuffer gives a bulk memcpy into the DMA buffer; assigning a
        # bytearray goes element-wise, which is slow on a Zynq PS at the
        # stress case's 251,740 bytes.  It sits outside the timed region
        # below either way — `t0` starts at the transfer.
        import numpy as np

        n_p, n_t = len(patch), len(templ)
        self.patch_buf[:n_p] = np.frombuffer(patch, dtype=np.uint8)
        self.templ_buf[:n_t] = np.frombuffer(templ, dtype=np.uint8)
        # The PL reads these through the DMA, which does not see the CPU's
        # dirty cache lines.  PYNQ's transfer() flushes for us on a PynqBuffer;
        # doing it explicitly costs nothing and does not depend on that
        # staying true.
        self.patch_buf.flush()
        self.templ_buf.flush()

        self._w(REG_PATCH_W, pw)
        self._w(REG_PATCH_H, ph)
        self._w(REG_TEMPL_W, tw)
        self._w(REG_TEMPL_H, th)

        # Arm both streams before starting.  tme_top drains the patch fully
        # and only then reads the template, so the template DMA sits
        # backpressured for most of the run — that is normal, and arming it
        # first means there is no window where the core is waiting on the PS.
        t0 = time.monotonic()
        self.ch_patch.transfer(self.patch_buf[:n_p])
        self.ch_templ.transfer(self.templ_buf[:n_t])

        self._w(REG_AP_CTRL, AP_START)

        # Wait on the CORE first, then confirm the channels drained.
        #
        # Not the other way round: a DMA channel reports idle both after a
        # transfer and before one has started, so polling a channel
        # immediately after arming it can observe the idle left over from the
        # previous case and conclude the transfer finished before it began.
        # `ap_done` has no such ambiguity, and it cannot rise until the core
        # has consumed every beat of both streams — so by the time it is set,
        # a channel that is still busy is genuinely still busy, which is the
        # "we sent more than the core wanted" case.
        #
        # `ap_idle` has exactly the ambiguity `ap_done` does not, for the same
        # reason the channels do: the core is idle before it starts as well as
        # after it finishes.  `_wait_done` therefore treats idle as completion
        # only after it has seen the core busy.
        deadline = t0 + self.timeout_s
        self._wait_done(deadline)
        self._wait_channel(self.ch_patch, deadline, "patch MM2S", n_p)
        self._wait_channel(self.ch_templ, deadline, "template MM2S", n_t)
        elapsed = time.monotonic() - t0

        # Data registers are plain reads; the *_ap_vld companions are
        # Clear-on-Read (§7.1.1 item 3), so each is read exactly once and
        # latched here.  A clear vld means the value in the data register is
        # left over from a previous run rather than produced by this one.
        score_bits = self._r(REG_RESULT_SCORE)
        x = self._r(REG_RESULT_X) & 0xFFFF
        y = self._r(REG_RESULT_Y) & 0xFFFF
        vlds = (self._r(REG_SCORE_VLD) & 1,
                self._r(REG_X_VLD) & 1,
                self._r(REG_Y_VLD) & 1)
        if not all(vlds):
            raise RuntimeError(
                f"ap_done rose but result ap_vld is "
                f"score={vlds[0]} x={vlds[1]} y={vlds[2]} — at least one "
                f"result register was not written by this invocation")

        score = struct.unpack("<f", struct.pack("<I", score_bits))[0]
        return score, x, y, elapsed

    def _wait_channel(self, channel, deadline: float, label: str,
                      nbytes: int) -> None:
        """Confirm a DMA channel drained, after the core reported done.

        Deliberately not `channel.wait()`: that blocks forever, and a hang is
        one of the failures worth reporting rather than sitting in.  Reaching
        the timeout here means the core finished while this channel still had
        beats queued — i.e. we sent MORE than the geometry told the core to
        read, so the surplus would desynchronise the next case.
        """
        while not channel.idle:
            self._check_channel_error(channel, label)
            if time.monotonic() > deadline:
                ctrl = self._r(REG_AP_CTRL)
                raise TimeoutError(
                    f"{label} still had beats queued after ap_done "
                    f"({nbytes:,} B transfer, AP_CTRL=0x{ctrl:08X}). The core "
                    f"read fewer beats than were sent; the surplus stays in "
                    f"the stream and corrupts the next case.")
            time.sleep(0.001)
        self._check_channel_error(channel, label)
        # Let PYNQ settle its own per-transfer bookkeeping now the channel is
        # idle, rather than leaving a transfer perpetually "outstanding" from
        # the driver's point of view across the whole suite.
        #
        # NOT suppressed.  This call is made only after DMASR has already
        # reported the channel idle, so there is no expected exception left for
        # it to raise: if it raises anyway, PYNQ and the hardware disagree about
        # what just happened, and every number this case is about to report was
        # read under that disagreement.  Swallowing it would turn a driver bug
        # or a half-finished transfer into a silently passing case.
        try:
            channel.wait()
        except Exception as exc:                       # noqa: BLE001
            raise RuntimeError(
                f"{label}: the channel reported idle, but channel.wait() then "
                f"raised {type(exc).__name__}: {exc}. PYNQ's view of the "
                f"transfer and the DMA's status register disagree — treat this "
                f"case's result as unusable rather than as a pass."
            ) from exc

    def _wait_done(self, deadline: float) -> None:
        """Poll AP_CTRL until ap_done, or until an idle the core can be shown
        to have REACHED rather than never left.

        ap_done is Clear-on-Read, so the first poll that observes it also
        consumes it — hence the latch.  Accepting ap_idle as well means a run
        whose done bit was consumed by something else (an interrupt handler,
        an operator poking register_map from a notebook) still terminates.

        But ap_idle ALONE is not completion.  The core is idle both before it
        has started and after it has finished, and this loop begins one
        register write after `ap_start`; taking the first idle at face value
        reports success for a run that never began — a dropped `ap_start`, a
        core held in reset, an overlay whose CTRL slave is not the IP we think
        it is.  Each of those would then be "confirmed" by reading result
        registers left over from the previous case.  So idle ends the wait only
        once some poll has observed the core BUSY.  A run short enough to start
        and finish between two polls is still caught, by the latched ap_done —
        which is why the two conditions are not redundant.

        Both DMAs are checked on every pass.  A channel that has taken a decode
        or slave error stops moving beats without telling anyone, and the core
        then blocks in `patch_stream.read()` for as long as the timeout allows;
        without this the symptom is a timeout here and the cause is two layers
        away in the block design.
        """
        seen_done = False
        seen_busy = False
        while True:
            ctrl = self._r(REG_AP_CTRL)
            if ctrl & AP_DONE:
                seen_done = True
            if not ctrl & AP_IDLE:
                seen_busy = True
            if seen_done or (seen_busy and (ctrl & AP_IDLE)):
                return
            # Before sleeping: a DMA error explains a stall that would
            # otherwise only show up as the timeout below.
            self._check_channel_error(self.ch_patch, "patch MM2S")
            self._check_channel_error(self.ch_templ, "template MM2S")
            if time.monotonic() > deadline:
                if not seen_busy:
                    raise TimeoutError(
                        f"the core never left idle within {self.timeout_s:g} s "
                        f"(AP_CTRL=0x{ctrl:08X}, patch DMA idle="
                        f"{self._channel_idle(self.ch_patch)}, template DMA "
                        f"idle={self._channel_idle(self.ch_templ)}). ap_start "
                        f"was written but the core was never seen out of idle: "
                        f"either it never started (a dropped ap_start, a core "
                        f"in reset, or a CTRL slave that is not this IP), or it "
                        f"ran to completion between two polls AND something "
                        f"else consumed the Clear-on-Read ap_done first. The "
                        f"result registers still hold the previous case either "
                        f"way, so no value read now would mean anything.")
                raise TimeoutError(
                    f"ap_done never rose within {self.timeout_s:g} s "
                    f"(AP_CTRL=0x{ctrl:08X}, patch DMA idle="
                    f"{self._channel_idle(self.ch_patch)}, template DMA idle="
                    f"{self._channel_idle(self.ch_templ)}). The core started "
                    f"and is now blocked in a stream read, wanting beats that "
                    f"never arrived. Either a transfer was truncated (what "
                    f"§3.1's bound looks like from the PS), or a DMA stopped "
                    f"early — a channel still reporting busy above is the one "
                    f"to look at.")
            time.sleep(0.001)

    @staticmethod
    def _channel_idle(channel) -> object:
        """`channel.idle` for a diagnostic message, never raising.

        Only used while building an error string: a driver that cannot read a
        DMA status register must still be able to report the failure that got
        it there.
        """
        try:
            return bool(channel.idle)
        except Exception as exc:                       # noqa: BLE001
            return f"unreadable ({type(exc).__name__})"

    @staticmethod
    def _check_channel_error(channel, label: str) -> None:
        """Raise if the DMA reported an internal error.

        A DMA that has taken a decode or slave error stops moving data but
        does not tell anyone; without this the symptom is a timeout somewhere
        else, several layers from the cause.
        """
        try:
            err = channel.error
        except Exception:                              # noqa: BLE001
            return          # older PYNQ without the property — not fatal
        if err:
            raise RuntimeError(
                f"{label} DMA reported error 0x{int(err):X} (see DMASR: bit 4 "
                f"internal, 5 slave, 6 decode). The transfer did not complete "
                f"and the core's stream is now short.")

    @staticmethod
    def _halt_channel(channel, label: str, timeout_s: float = 0.5) -> bool:
        """Bounded low-level halt.  True ONLY on a positive register read-back.

        Deliberately not `channel.stop()`: PYNQ clears RS and then spins on
        `while self.running: pass` with no deadline, so a DMA that will not halt
        hangs teardown instead of reporting it — and teardown is reached exactly
        when something has already gone wrong.

        Clearing RS is a *request*; this waits for the acknowledgement. If it
        does not come, it issues a soft reset (`DMACR.Reset`) and waits again.
        On **neither** path is `DMASR.Halted` alone sufficient evidence. Per
        PG021 a soft reset does not abort an AXI transaction the engine already
        has in flight: it lets that transaction complete gracefully, and
        `DMACR.Reset` stays asserted until it does. So a read of `Halted == 1`
        with `Reset` still set can mean "still draining a read that targets the
        very buffer we are about to free". Quiescence therefore requires
        **both** `DMACR.Reset == 0` (the bit self-cleared) **and**
        `DMASR.Halted == 1`, on every path that can return True.

        Applying that to the RS=0 path too is not symmetry for its own sake.
        The engine this runs against is not necessarily in its power-on state:
        an earlier `close()` — or an earlier call on the same channel — may
        have left a reset in flight, and a stuck reset holds `Halted` high
        forever. A second call that tested `Halted` alone would read that
        leftover 1 and report a verified halt, so the *first* call correctly
        refusing to free the buffers would be undone by the next one. Pinned by
        the repeated-call case in `_selftest_halt_path`.
        """
        mmio = getattr(channel, "_mmio", None)
        base = getattr(channel, "_offset", None)
        if mmio is None or base is None:
            return False        # cannot verify => must not claim quiescence

        def quiescent() -> bool:
            if not mmio.read(base + DMA_DMASR) & DMASR_HALTED:
                return False
            if mmio.read(base + DMA_DMACR) & DMACR_RESET:
                return False    # reset still in progress; not yet quiescent
            return True

        def wait_quiescent() -> bool:
            end = time.monotonic() + timeout_s
            while time.monotonic() < end:
                if quiescent():
                    return True
                time.sleep(0.001)
            return quiescent()

        try:
            cr = mmio.read(base + DMA_DMACR)
            mmio.write(base + DMA_DMACR, cr & ~DMACR_RS)
            if wait_quiescent():
                return True
            print(f"  {label} DMA did not halt on RS=0 within {timeout_s:g} s; "
                  f"issuing DMACR.Reset")
            mmio.write(base + DMA_DMACR, DMACR_RESET)
            if wait_quiescent():
                return True
            print(f"  {label} DMA not quiescent {timeout_s:g} s after reset: "
                  f"DMACR=0x{mmio.read(base + DMA_DMACR):08X} "
                  f"DMASR=0x{mmio.read(base + DMA_DMASR):08X} "
                  f"(need Reset=0 and Halted=1)")
            return False
        except Exception as exc:                       # noqa: BLE001
            print(f"  {label} DMA halt could not be driven: "
                  f"{type(exc).__name__}: {exc}")
            return False

    def _quarantine_buffers(self) -> None:
        """Hold references so `PynqBuffer.__del__` cannot free the pages yet.

        A DELAY, not a quarantine — see `close()` for why nothing stronger is
        available from inside this process.
        """
        for buf in (getattr(self, "patch_buf", None),
                    getattr(self, "templ_buf", None)):
            if buf is not None:
                _UNSAFE_TO_FREE.append(buf)

    def close(self) -> bool:
        """Bring both channels to a VERIFIED halt, then release their buffers.

        Returns True only if both halts were verified.

        `freebuffer()` hands CMA pages back to the pool. A channel still
        mid-transfer keeps reading those physical addresses, and mid-transfer is
        exactly the state a timeout leaves it in — the path where `close()`
        matters most.

        What this can and cannot promise, spelled out because an earlier version
        of this docstring promised something it did not deliver:

          - It CAN establish quiescence positively: RS cleared, then
            `DMACR.Reset == 0` and `DMASR.Halted == 1` read back together,
            bounded, with a soft reset as the fallback. When that read-back
            succeeds, freeing is safe and the pages go back.
          - It CANNOT protect the memory when quiescence is NOT established.
            Skipping `freebuffer()` quarantines nothing: `PynqBuffer.__del__`
            calls it when the object is collected, and process exit releases the
            CMA pages regardless. **The old "deliberately leaked until reboot"
            claim was false** — there is no leak to rely on, and code that
            counted on one was counting on nothing.

        So when the halt cannot be verified, the only honest actions are to hold
        the buffers for as long as this process lives (a delay, which at least
        does not hand them back early), to say plainly that the PL may still be
        reading them, and to name the remedy the operator has to apply:
        **reset the PL — reload the overlay — before anything else allocates
        that memory.** This script will not do that itself: it does not know
        what else in the design is live, and a blind PL reset is its own hazard.
        """
        halted_all = True
        for ch, label in ((getattr(self, "ch_patch", None), "patch"),
                          (getattr(self, "ch_templ", None), "template")):
            if ch is None:
                continue
            # Settle PYNQ's per-channel bookkeeping for a transfer that
            # completed.  Raises if nothing was started, which is fine and
            # expected on an early failure — but say so, so a teardown-time
            # disagreement is not invisible the way it was in _wait_channel.
            try:
                if ch.idle:
                    try:
                        ch.wait()
                    except Exception as exc:           # noqa: BLE001
                        print(f"  note: {label} channel.wait() at close raised "
                              f"{type(exc).__name__}: {exc} (expected if no "
                              f"transfer was ever started on it)")
            except Exception:                          # noqa: BLE001
                pass        # `idle` unreadable; the verified halt below decides

            if not self._halt_channel(ch, label):
                halted_all = False

        if halted_all:
            for buf in (getattr(self, "patch_buf", None),
                        getattr(self, "templ_buf", None)):
                if buf is None:
                    continue
                try:
                    buf.freebuffer()
                except Exception:                      # noqa: BLE001
                    pass
            return True

        self._quarantine_buffers()
        print("\n" + "!" * 72)
        print("WARNING: a DMA channel could not be proved halted "
              "(DMACR.Reset=0 and DMASR.Halted=1 never read back together).")
        print("The PL may still be issuing AXI reads against the DMA buffers. "
              "This script is")
        print("holding those buffers so they are not handed back early, but "
              "that is a DELAY, not")
        print("a quarantine: PynqBuffer.__del__ and process exit both release "
              "the CMA pages,")
        print("and nothing in this process can prevent it.")
        print("REMEDY: reset the PL (reload the overlay) before anything else "
              "allocates CMA.")
        print("!" * 72)
        return False


# ---------------------------------------------------------------------------
# Self-test: the validator, off the board
# ---------------------------------------------------------------------------

def selftest() -> int:
    """Exercise validate_geometry and validate_template_content without PYNQ.

    The validators are the only thing standing between a bad descriptor and
    silent BRAM corruption, or between a flat template and a result nobody can
    interpret (§4.6) — and both are pure Python, so neither should be a
    function only the board can test.
    """
    print("validate_geometry self-test")
    legal = [(820, 307, 216, 96), (64, 48, 64, 48), (40, 30, 4, 4),
             (820, 307, 4, 4), (216, 96, 216, 96)]
    illegal = [
        ((821, 307, 216, 96), "patch_w"),
        ((820, 308, 216, 96), "patch_h"),
        ((820, 307, 217, 96), "templ_w"),
        ((820, 307, 216, 97), "templ_h"),
        ((820, 307, 216, 3),  "templ_h"),
        ((820, 307, 3, 96),   "templ_w"),
        ((100, 307, 216, 96), "patch_w"),      # pw < tw
        ((820, 50, 216, 96),  "patch_h"),      # ph < th
    ]
    failures = 0
    for geom in legal:
        errs = validate_geometry(*geom)
        ok = not errs
        print(f"  [{'PASS' if ok else 'FAIL'}] legal   {geom}"
              + ("" if ok else f" -> {errs}"))
        failures += not ok
    for geom, expect in illegal:
        errs = validate_geometry(*geom)
        ok = any(expect in e for e in errs)
        print(f"  [{'PASS' if ok else 'FAIL'}] illegal {geom} -> "
              f"{errs if errs else 'ACCEPTED (should not be)'}")
        failures += not ok

    # §3.1 specifically: the envelope must be legal and one byte more must not
    # be, at the recorded bound.  This is the check that fails if anyone
    # "rounds up the envelope for safety".
    #
    # An explicit check, not `assert`: this file is run with `python -O` as
    # part of its acceptance, and an assert would make that run test less than
    # the unoptimised one while still printing PASS.
    envelope_errs = validate_geometry(820, 307, 216, 96)
    ok = not envelope_errs
    print(f"  [{'PASS' if ok else 'FAIL'}] 820x307 / 216x96 is legal"
          + ("" if ok else f" -> {envelope_errs}"))
    failures += not ok
    over = validate_geometry(820, 320, 216, 96)
    ok = any("§3.1" in e or "patch_h" in e for e in over)
    print(f"  [{'PASS' if ok else 'FAIL'}] §3.1 bound rejects an oversized "
          f"patch")
    failures += not ok

    # The envelope is exactly 251,740 B, so a bound one byte below it must
    # reject the very geometry the default bound accepts.  This is what makes
    # the run-time `dma_max` (read off the DMA itself) meaningful rather than
    # decorative: narrow the bound and the same patch becomes illegal.
    edge = validate_geometry(820, 307, 4, 4, dma_max_bytes=251739)
    ok = any("§3.1" in e for e in edge)
    print(f"  [{'PASS' if ok else 'FAIL'}] a DMA bound one byte under the "
          f"envelope rejects 820x307 (251,740 B)")
    failures += not ok

    failures += _selftest_template_content()
    failures += _selftest_halt_path()

    print(f"\n{'SELF-TEST PASSED' if not failures else f'SELF-TEST FAILED: {failures}'}")
    return 0 if not failures else 1


class _FakeDmaMmio:
    """A DMA channel's register block, for testing the teardown halt off-board.

    Models only what `_halt_channel` touches: DMACR at `base`, DMASR at
    `base + 4`, and the ways a real engine can and cannot come to rest.

    `reset_self_clears=False` is the important one: it models an engine that
    raises `Halted` while `DMACR.Reset` stays asserted — a reset still draining
    an in-flight AXI transaction, per PG021 — which must NOT be accepted as
    quiescent, because that transaction can still be reading the buffer.

    `DMACR.Reset` is modelled as genuinely self-clearing hardware: once a reset
    is in flight it reads 1 until the reset completes, and writing 0 to that bit
    does not clear it. That detail is what keeps the repeated-call case honest —
    a fake where the halt path could scrub the bit itself would let a stuck
    reset look resolved on the second call for reasons no real engine offers.
    """

    def __init__(self, base: int = 0, halt_on_rs: bool = True,
                 halt_on_reset: bool = True, reset_self_clears: bool = True):
        self.base = base
        self.halt_on_rs = halt_on_rs
        self.halt_on_reset = halt_on_reset
        self.reset_self_clears = reset_self_clears
        self.regs = {base + DMA_DMACR: DMACR_RS, base + DMA_DMASR: 0}
        self.reset_seen = False
        self.reset_in_flight = False

    def read(self, off: int) -> int:
        return self.regs.get(off, 0)

    def write(self, off: int, val: int) -> None:
        if off != self.base + DMA_DMACR:
            self.regs[off] = val
            return
        if val & DMACR_RESET:
            self.reset_seen = True
            if self.halt_on_reset:
                self.regs[self.base + DMA_DMASR] |= DMASR_HALTED
            # A reset that never completes stays in flight forever.
            self.reset_in_flight = not self.reset_self_clears
        elif not val & DMACR_RS and self.halt_on_rs:
            self.regs[self.base + DMA_DMASR] |= DMASR_HALTED
        # Self-clearing bit: readable as 1 only while the reset is in flight,
        # and not writable to 0 by software.
        val = val | DMACR_RESET if self.reset_in_flight else val & ~DMACR_RESET
        self.regs[off] = val


class _FakeChannel:
    """Enough of a PYNQ send channel for `_halt_channel` and `close()`."""

    def __init__(self, mmio, offset: int = 0):
        self._mmio = mmio
        self._offset = offset
        self.waited = False

    @property
    def idle(self) -> bool:
        return bool(self._mmio.read(self._offset + DMA_DMASR) & 0x02)

    def wait(self) -> None:
        self.waited = True


class _FakeBuffer:
    def __init__(self):
        self.freed = False

    def freebuffer(self) -> None:
        self.freed = True


def _exit_status(cases_ok: bool, halt_ok: bool) -> int:
    """0 only if the cases passed AND teardown proved the DMAs halted.

    A separate function so the second half of that rule is testable off the
    board: a run that leaves a DMA in an unknown state is not a clean run, and
    the way that stops being true is someone dropping `halt_ok` from the
    expression while the printed warning still appears to cover it.
    """
    return 0 if (cases_ok and halt_ok) else 1


def _selftest_halt_path() -> int:
    """Exercise `_halt_channel` off the board.

    This is the path that decides whether the CMA buffers may be handed back,
    and it only ever runs when something has already gone wrong — so without
    this it would be written once and first executed during a failure on real
    hardware.  What each case pins:

      - a halt is claimed ONLY on a `DMACR.Reset == 0` **and**
        `DMASR.Halted == 1` read-back, never on the absence of an exception;
      - an engine that ignores RS=0 still gets the soft reset, and the reset is
        actually issued;
      - an engine that ignores both is reported as unhalted **and the call
        returns**, bounded — the failure that PYNQ's own `stop()` cannot report
        because it spins forever;
      - an engine whose reset never self-clears is unhalted on the first call
        AND on every call after it, so a leftover `Halted == 1` cannot be
        mistaken for fresh evidence;
      - a channel whose registers are not reachable is unhalted by default,
        because "cannot verify" must never read as "verified".

    Non-zero `_offset` is used in one case: the S2MM block sits at 0x30, and an
    offset bug would otherwise poke DMACR of the wrong channel.
    """
    print("\nDMA halt path self-test (close() / §CMA teardown)")
    failures = 0
    t = 0.05        # short deadline; the point is boundedness, not duration

    def report(ok: bool, label: str, detail: str = "") -> int:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{detail}")
        return 0 if ok else 1

    mm = _FakeDmaMmio()
    ok = TmeStandalone._halt_channel(_FakeChannel(mm), "fake", t) is True
    failures += report(ok, "halts on RS=0, verified by DMASR.Halted read-back")

    mm = _FakeDmaMmio(base=0x30, halt_on_rs=False)
    got = TmeStandalone._halt_channel(_FakeChannel(mm, 0x30), "fake", t)
    ok = (got is True and mm.reset_seen)
    failures += report(ok, "ignores RS=0 -> soft reset issued and verified, at "
                           "offset 0x30", "" if ok else f" -> {got}, "
                           f"reset_seen={mm.reset_seen}")

    mm = _FakeDmaMmio(halt_on_rs=False, halt_on_reset=False)
    start = time.monotonic()
    got = TmeStandalone._halt_channel(_FakeChannel(mm), "fake", t)
    elapsed = time.monotonic() - start
    ok = (got is False and mm.reset_seen and elapsed < 5.0)
    failures += report(ok, f"never halts -> returns False in {elapsed:.2f}s "
                           f"(bounded, unlike PYNQ's stop())",
                       "" if ok else f" -> {got}")

    # Halted goes high but DMACR.Reset never self-clears: a reset still
    # draining an in-flight AXI read. Accepting this would free a buffer the
    # engine is reading, which is the exact failure close() exists to prevent.
    mm = _FakeDmaMmio(halt_on_rs=False, reset_self_clears=False)
    got = TmeStandalone._halt_channel(_FakeChannel(mm), "fake", t)
    halted_high = bool(mm.read(DMA_DMASR) & DMASR_HALTED)
    reset_high = bool(mm.read(DMA_DMACR) & DMACR_RESET)
    ok = (got is False and halted_high and reset_high)
    failures += report(ok, "reset stuck (Halted=1 but Reset=1) -> False, not "
                           "accepted on Halted alone",
                       "" if ok else f" -> {got}, Halted={halted_high}, "
                                     f"Reset={reset_high}")

    # ...and calling it AGAIN on that same stuck engine must still say False.
    # This is the case that fails if the Reset==0 requirement is applied only
    # on the post-reset path: the second call enters with Halted already high
    # from the first call's reset, satisfies a Halted-only test immediately,
    # and returns True — quietly reversing the first call's correct refusal to
    # free the buffers.  close() calls this once per channel, but a caller that
    # retries teardown, or a second TmeStandalone against a channel a previous
    # run left mid-reset, hits exactly this entry state.
    got2 = TmeStandalone._halt_channel(_FakeChannel(mm), "fake", t)
    still_reset = bool(mm.read(DMA_DMACR) & DMACR_RESET)
    ok = (got2 is False and still_reset)
    failures += report(ok, "reset stuck, called a SECOND time -> still False "
                           "(a leftover Halted=1 is not evidence)",
                       "" if ok else f" -> {got2}, Reset={still_reset}")

    class _NoRegs:
        pass

    got = TmeStandalone._halt_channel(_NoRegs(), "fake", t)
    ok = got is False
    failures += report(ok, "unreachable registers -> False ('cannot verify' is "
                           "not 'verified')", "" if ok else f" -> {got}")

    # --- close() itself: the buffers must follow the halt, all or nothing ---
    def make_dut(ok_patch: bool, ok_templ: bool):
        dut = TmeStandalone.__new__(TmeStandalone)     # no board, no __init__
        dut.ch_patch = _FakeChannel(_FakeDmaMmio(halt_on_rs=ok_patch,
                                                 halt_on_reset=ok_patch))
        dut.ch_templ = _FakeChannel(_FakeDmaMmio(halt_on_rs=ok_templ,
                                                 halt_on_reset=ok_templ))
        dut.patch_buf, dut.templ_buf = _FakeBuffer(), _FakeBuffer()
        return dut

    held = len(_UNSAFE_TO_FREE)
    dut = make_dut(True, True)
    got = dut.close()
    ok = (got is True and dut.patch_buf.freed and dut.templ_buf.freed
          and len(_UNSAFE_TO_FREE) == held)
    failures += report(ok, "close(): both halts verified -> both buffers freed",
                       "" if ok else f" -> {got}, freed="
                                     f"{dut.patch_buf.freed}/{dut.templ_buf.freed}")

    for label, (p, t_) in (("patch fails", (False, True)),
                           ("template fails", (True, False))):
        held = len(_UNSAFE_TO_FREE)
        dut = make_dut(p, t_)
        got = dut.close()
        ok = (got is False and not dut.patch_buf.freed
              and not dut.templ_buf.freed
              and len(_UNSAFE_TO_FREE) == held + 2)
        failures += report(ok, f"close(): {label} -> NEITHER buffer freed, "
                               f"both held",
                           "" if ok else f" -> {got}, freed="
                                         f"{dut.patch_buf.freed}/{dut.templ_buf.freed}")

    # ...and that a failed halt reaches the exit status, not just stdout.
    cases = ((True, True, 0), (True, False, 1), (False, True, 1),
             (False, False, 1))
    bad = [(a, b, _exit_status(a, b), want) for a, b, want in cases
           if _exit_status(a, b) != want]
    ok = not bad
    failures += report(ok, "exit status: 0 only when the cases passed AND the "
                           "halt was verified", "" if ok else f" -> {bad}")

    return failures


def _selftest_template_content() -> int:
    """Exercise the §4.6 content rejection, off the board.

    Written with explicit checks rather than `assert` so it still tests
    something under `python -O` — the same reason the golden generator raises
    ValueError instead of asserting.
    """
    print("\nvalidate_template_content self-test (§4.6)")
    failures = 0

    def report(ok: bool, label: str, detail: str = "") -> int:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{detail}")
        return 0 if ok else 1

    # Flat 4x4 templates at three fills.  0 and 255 are the values the
    # binarized pipeline actually produces (all background / all ink, i.e. a
    # crop that caught nothing or everything); 127 is there so the rejection
    # cannot be a special case for the extremes.
    for fill in (0, 127, 255):
        templ = bytes([fill]) * 16
        errs = validate_template_content(templ, 4, 4)
        ok = any("flat template" in e for e in errs)
        failures += report(ok, f"flat 4x4 all-{fill:<3} rejected",
                           "" if ok else f" -> {errs or 'ACCEPTED'}")

    # Minimally non-flat: fifteen 127s and one 128.  dt = 16*258319 - 2033^2
    # = 15, the global legal minimum (§4.6).  This must be ACCEPTED — it is
    # the guard against anyone turning `min == max` into a variance threshold.
    minimal = bytes([127] * 15 + [128])
    errs = validate_template_content(minimal, 4, 4)
    n, st = 16, sum(minimal)
    dt = n * sum(v * v for v in minimal) - st * st
    ok = not errs and dt == 15
    failures += report(ok, f"minimally non-flat 4x4 accepted (dt={dt})",
                       "" if ok else f" -> {errs}")

    # Padded / short buffers.  The core reads exactly templ_w*templ_h beats and
    # ignores TLAST, so a length mismatch is silent corruption, not an error
    # the hardware reports.
    for label, buf in (("padded (+1 B)", bytes(range(16)) + b"\x00"),
                       ("short  (-1 B)", bytes(range(15))),
                       ("empty", b"")):
        errs = validate_template_content(buf, 4, 4)
        ok = bool(errs)
        failures += report(ok, f"wrong-length template {label} rejected",
                           "" if ok else " -> ACCEPTED")

    # Only `bytes` is accepted.  Each of these is a buffer this function cannot
    # honestly validate, and the last two are the reason a "read-only
    # memoryview" exemption was removed rather than tightened:
    #
    #   bytearray               mutable outright
    #   readonly memoryview     `readonly` is a property of the VIEW; the
    #                           bytearray it aliases is still writable, as the
    #                           mutation below demonstrates
    #   2-D memoryview          len() counts rows, not bytes, and min()/max()
    #                           raise NotImplementedError — the checks would
    #                           crash rather than reject
    backing = bytearray(b"\x00\x01" * 8)
    ro_view = memoryview(backing).toreadonly()
    two_d = memoryview(bytes(range(16))).cast("B", (4, 4))
    for label, buf in (("mutable bytearray", bytearray(range(16))),
                       ("read-only memoryview over a bytearray", ro_view),
                       ("2-D memoryview", two_d)):
        try:
            errs = validate_template_content(buf, 4, 4)
            ok = any("not bytes" in e for e in errs)
            detail = "" if ok else f" -> {errs or 'ACCEPTED'}"
        except Exception as exc:                       # noqa: BLE001
            ok, detail = False, f" -> raised {type(exc).__name__}: {exc}"
        failures += report(ok, f"{label} rejected", detail)

    # ...and the reason the read-only view is not good enough, made explicit:
    # it passed every flag check while its backing store stayed writable.
    before = bytes(ro_view)
    backing[0] = 0xFF
    ok = (ro_view.readonly and bytes(ro_view) != before)
    failures += report(ok, "read-only memoryview aliased a buffer that changed "
                           "under it (readonly=True throughout)")

    # A mixed bank: two legal templates around one flat one.  The whole bank
    # must be rejected, by index, BEFORE any launch — main() calls exactly this
    # function before the first DMA descriptor is written.
    def mk(index, toff, tag):
        return Case([str(index), "40", "30", "4", "4", "0", str(toff),
                     "0.500000", "0", "0", "999.000000", "edge", tag])

    bank = (bytes(range(0, 64, 4))[:16]      # varied
            + bytes([255]) * 16              # FLAT — the one that must fire
            + bytes(range(16, 32)))          # varied
    cases = [mk(0, 0, "bank-good-a"), mk(1, 16, "bank-flat"),
             mk(2, 32, "bank-good-b")]
    bad = validate_template_bank(cases, bank)
    ok = ([(i, t) for i, t, _ in bad] == [(1, "bank-flat")])
    failures += report(ok, "mixed bank: only the flat entry is reported",
                       "" if ok else f" -> {[(i, t) for i, t, _ in bad]}")

    # ...and the abort CONDITION is non-empty.  Label this for what it is: a
    # check on the return value, nothing more.  Whether the run actually stops
    # before the first DMA descriptor or `ap_start` is a property of main()'s
    # statement ORDER, and nothing here instruments either — no fake DUT, no
    # register writes, no PYNQ at all.  The ordering itself is enforced (and
    # commented) at the `content_bad` / `bad` gate in main(), and only a board
    # run or a mocked driver could test it.
    ok = bool(bad)
    failures += report(ok, "mixed bank: abort CONDITION is non-empty "
                           "(main()'s ordering is not instrumented here)")

    return failures


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay", help="path to tme_standalone.bit")
    ap.add_argument("--data-dir", default=".", type=Path,
                    help="directory holding tb_tme_*_<suite>.{txt,bin}")
    ap.add_argument("--suite", default="hw",
                    choices=("hw", "cosim", "csim", "phase_s"),
                    help="manifest to run (default hw — the only one that "
                         "carries the 251,740 B §3.1 case).  phase_s is the "
                         "Priority 3 suite: every case has a 96x64 result map, "
                         "so it measures Phase-S cycles on the UNCHANGED core")
    ap.add_argument("--expect-fclk-mhz", type=float, default=None,
                    help="REQUIRE this measured PL clock and abort otherwise. "
                         "Fail-closed: an unreadable clock also aborts. Use "
                         "125 for the Phase-S / 8 ns probe image.")
    ap.add_argument("--fclk-tol-mhz", type=float, default=0.01,
                    help="tolerance for --expect-fclk-mhz (default 0.01)")
    ap.add_argument("--patch-dma", help="overlay attribute for the patch MM2S")
    ap.add_argument("--templ-dma", help="overlay attribute for the template MM2S")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"per-case timeout in seconds (default "
                         f"{DEFAULT_TIMEOUT_S:g}; stress-max-envelope needs "
                         f"12-13 s at 31.25 MHz)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the geometry and §4.6 template-content validator "
                         "self-tests and exit; needs no board")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.overlay:
        ap.error("--overlay is required (or use --selftest)")

    print("=" * 72)
    print("template_match_core standalone bring-up")
    print("=" * 72)

    try:
        cases, patches, templs = load_manifest(args.data_dir, args.suite)
    except Exception as exc:                           # noqa: BLE001
        print(f"CANNOT RUN: {exc}")
        return 2
    biggest = max(c.patch_bytes for c in cases)
    print(f"\nSuite '{args.suite}': {len(cases)} cases, largest single patch "
          f"transfer {biggest:,} B")
    if biggest < 200_000 and args.suite != "phase_s":
        print("  WARNING: no case here comes near the §3.1 single-transfer "
              "bound. This run will verify arithmetic only. Use --suite hw.")
    elif args.suite == "phase_s":
        # Not a shortfall to warn about: Phase S crops the patch on the PS, so
        # the largest possible trial is 311x159 = 49,449 B.  Leaving §3.1 slack
        # is the POINT of the suite, not a gap in it.  §3.1 stays covered by
        # --suite hw, which is unchanged and still carries the 251,740 B case.
        print(f"  Phase-S geometry: every case has a 96x64 result map, and the "
              f"largest patch uses {100.0 * biggest / 262143:.1f}% of the §3.1 "
              f"bound. This run measures CYCLES at Phase-S geometry on the "
              f"unchanged core; §3.1 is covered by --suite hw.")
        # STALE BANNER, PRESERVED DELIBERATELY.  "the unchanged core" was true
        # when this suite was written for Priority 3 and stopped being true on
        # 2026-08-19, when the byte-identical runner drove the B1 bitstream
        # (logs/b1_board_20260818/).  The runner cannot know: it talks to
        # whatever overlay it is pointed at and has no way to read the RTL back
        # out of it.  The line above is left exactly as it was because it is
        # quoted verbatim in retained transcripts -- rewording it would make
        # those transcripts unquotable -- and this notice is printed under it.
        print("  NOTE: 'the unchanged core' above is STALE. This runner is "
              "overlay-agnostic; what it measures is whatever --overlay names.")
        print("        Read the bitstream hash, not this banner, to know which "
              "RTL produced a number.")

    try:
        import pynq                                    # noqa: F401
    except ImportError as exc:
        print(f"\nCANNOT RUN: pynq is not importable ({exc}). This script "
              f"drives real hardware; use --selftest off the board.")
        return 2

    try:
        dut = TmeStandalone(args.overlay, args.patch_dma, args.templ_dma,
                            args.timeout, args.expect_fclk_mhz,
                            args.fclk_tol_mhz)
    except Exception as exc:                           # noqa: BLE001
        print(f"\nCANNOT RUN: {exc}")
        return 2

    try:
        if not dut.preflight():
            print("\nPREFLIGHT FAILED — not starting the core. Fix the "
                  "address map or the reset before trusting any result.")
            return 1

        # Validate the whole suite before running any of it, exactly as
        # tme_tb.cpp validates its manifest before touching the DUT: a bad row
        # halfway through should not be discovered after five good cases have
        # already run.
        # Nothing below this point may run if ANY case is rejected: the whole
        # bank is judged before the first DMA descriptor is written and before
        # ap_start is ever asserted (§4.6).
        print("\n--- validation (§4.1 / §3.1 geometry, §4.6 template content;"
              " before any DMA or ap_start) ---")
        bad = False
        content_bad = {idx: errs
                       for idx, _tag, errs in validate_template_bank(cases,
                                                                     templs)}
        for c in cases:
            errs = validate_geometry(c.pw, c.ph, c.tw, c.th, dut.dma_max)
            errs = errs + content_bad.get(c.index, [])
            status = "OK" if not errs else "REJECT"
            print(f"  [{status:6}] {c.tag:<24} patch {c.pw}x{c.ph} "
                  f"({c.patch_bytes:,} B)  templ {c.tw}x{c.th}")
            for e in errs:
                print(f"             - {e}")
            bad |= bool(errs)
        if bad:
            print("\nA manifest case is outside the envelope or carries an "
                  "illegal template. Not starting — the whole bank is "
                  "rejected, not just the offending case.")
            return 1

        print("\n--- cases ---")
        # Counted explicitly rather than derived from len(results): a case that
        # raises never reaches `results`, so `len(results) - failures` would
        # silently under-report the passes on exactly the runs where the
        # numbers matter most.
        passed = 0
        aborted = False
        results = []
        for c in cases:
            patch = patches[c.patch_off:c.patch_off + c.patch_bytes]
            templ = templs[c.templ_off:c.templ_off + c.templ_bytes]
            try:
                score, x, y, secs = dut.run_case(patch, templ,
                                                 c.pw, c.ph, c.tw, c.th)
            except Exception as exc:                   # noqa: BLE001
                # Stop rather than continue: every failure mode here (a hung
                # core, a channel with beats left over) leaves the streams in
                # an unknown state, so later cases would report noise.
                aborted = True
                print(f"[{c.index}] {c.tag:<24} ERROR: {exc}")
                break

            sane = check_result(score, x, y, c.pw, c.ph, c.tw, c.th)
            score_ok = abs(score - c.score) <= SCORE_TOL
            loc_ok = (x == c.x and y == c.y)
            ok = score_ok and loc_ok and not sane
            passed += ok
            results.append((c, score, x, y, secs))

            mbs = c.patch_bytes / secs / 1e6 if secs > 0 else 0.0
            print(f"[{c.index}] {c.tag:<24} {c.category:<11} "
                  f"patch {c.pw:4}x{c.ph:<3} templ {c.tw:3}x{c.th:<2}  "
                  f"gold {c.score:+.4f} @({c.x:3},{c.y:3})  "
                  f"dut {score:+.4f} @({x:3},{y:3})  "
                  f"{secs:6.3f} s ({mbs:.2f} MB/s in)  "
                  f"{'PASS' if ok else 'FAIL'}"
                  f"{'' if score_ok else ' [score]'}"
                  f"{'' if loc_ok else ' [loc]'}")
            for s in sane:
                print(f"      - {s}")

        # Re-invocation with a SMALLER case after the largest one.  tme_top's
        # patch/template BRAMs and column accumulators are `static`, so a
        # 820x307 run leaves 251,740 bytes of residue that a later 64x48 run
        # must not read.  The suite order puts the stress case last, so
        # without this the board never tests the shrink direction that
        # tme_tb.cpp's back-to-back ordering does cover.
        reinvoke_ok = None
        if not aborted and passed == len(cases) and len(cases) > 1:
            print("\n--- re-invocation after the largest case ---")
            c = cases[0]
            patch = patches[c.patch_off:c.patch_off + c.patch_bytes]
            templ = templs[c.templ_off:c.templ_off + c.templ_bytes]
            try:
                score, x, y, secs = dut.run_case(patch, templ,
                                                 c.pw, c.ph, c.tw, c.th)
                reinvoke_ok = (abs(score - c.score) <= SCORE_TOL
                               and x == c.x and y == c.y)
                print(f"  {c.tag} re-run after "
                      f"{max(cc.patch_bytes for cc in cases):,} B: "
                      f"dut {score:+.4f} @({x},{y}) vs gold {c.score:+.4f} "
                      f"@({c.x},{c.y})  "
                      f"{'PASS' if reinvoke_ok else 'FAIL — stale BRAM'}")
            except Exception as exc:                   # noqa: BLE001
                reinvoke_ok = False
                print(f"  ERROR: {exc}")

        print("\n" + "=" * 72)
        stress = [r for r in results if r[0].patch_bytes > 200_000]
        if stress:
            c, _, _, _, secs = stress[0]
            print(f"§3.1 EXERCISED: {c.tag} moved {c.patch_bytes:,} B in one "
                  f"transfer ({dut.dma_max - c.patch_bytes:,} B under the "
                  f"{dut.dma_max:,} B bound) in {secs:.3f} s")
        else:
            print("§3.1 NOT exercised — no case moved a large enough transfer")

        print(f"{passed}/{len(cases)} cases passed"
              + (" (run ABORTED early — the cases after the error did not "
                 "run at all, they did not pass)" if aborted else ""))
        if reinvoke_ok is None:
            print("re-invocation check: not run")
        else:
            print(f"re-invocation check: {'PASS' if reinvoke_ok else 'FAIL'}")

        print("Reminder: this says nothing about timing. Post-route WNS comes "
              "from the implementation report — see §8 and "
              "vivado/tme_standalone/post_route_wns.txt.")
        ok_overall = (not aborted and passed == len(cases)
                      and reinvoke_ok is not False)
    finally:
        # A teardown that cannot prove the DMAs halted leaves the board in an
        # unknown state, so it must not be reportable as a clean run — the
        # warning close() prints scrolls past, an exit status does not.
        halt_ok = dut.close()
    if not halt_ok:
        print("EXIT 1: the cases may have passed, but teardown could not prove "
              "the DMAs were halted (see the warning above).")
    return _exit_status(ok_overall, halt_ok)


if __name__ == "__main__":
    sys.exit(main())
