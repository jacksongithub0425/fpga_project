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

Three cases also exist to make `result_score` worth reading: it crosses
AXI4-Lite as raw IEEE-754 bits that this script reinterprets, and before
`equality-negative` (-0.73) and `equality-different` (0.0096) were added every
score in the suite was exactly 0.0 or 1.0 — no sign bit, one mantissa bit.

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

# Contract §4.1 / §3 envelope.  Must track MAX_* in hls/template_match/tme_top.h
# and PE_MAX_PATCH_* in hls/patch_extract/patch_extract_core.h.
MAX_PATCH_W, MAX_PATCH_H = 820, 307
MAX_TEMPL_W, MAX_TEMPL_H = 216, 96
MIN_TEMPL_DIM = 4

# Contract §3.1.  A fallback only: the DMA's own reported buffer_max_size wins
# when PYNQ exposes it, because this number is a block-design parameter and a
# BD edit can raise it to 26 bits without touching anything in this repo.
DMA_MAX_BYTES_DEFAULT = 262143

# The stress case runs ~370M cycles at 31.25 MHz — roughly 12 s of wall clock,
# before any PS overhead.  A 5 s timeout borrowed from the extractor's driver
# would report a hang on a perfectly healthy run.
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
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        from pynq import Overlay, allocate

        # A non-finite timeout makes every `time.monotonic() > deadline` test
        # false, turning every wait loop into an unkillable spin; a
        # non-positive one fires before the core can possibly have finished.
        # Both are silent, so check rather than trust.
        if not (timeout_s > 0) or timeout_s in (float("inf"),):
            raise ValueError(
                f"timeout {timeout_s!r} must be finite and positive; the "
                f"stress cases need ~12 s at 31.25 MHz, so the default is "
                f"{DEFAULT_TIMEOUT_S:g}")
        self.timeout_s = float(timeout_s)
        self.ol = Overlay(overlay_path)

        # Measure the PL clock rather than assuming it.  Contract §8 records
        # 31.25 MHz and explains it as Vivado's 50 MHz request (computed
        # against an assumed 1600 MHz source) meeting a 1000 MHz PLL through
        # the same 8x4 divisors — but that explanation has been an INFERENCE.
        # The overlay is loaded; the number is one attribute away, and every
        # elapsed-time figure this script prints is only interpretable with
        # it.
        try:
            from pynq import Clocks
            self.fclk0_mhz = float(Clocks.fclk0_mhz)
            period_ns = 1000.0 / self.fclk0_mhz
            print(f"\nPL clock (measured): {self.fclk0_mhz:.4f} MHz "
                  f"({period_ns:.3f} ns)")
            if abs(self.fclk0_mhz - 31.25) > 0.01:
                print(f"  NOTE: contract §8 records 31.25 MHz. This board "
                      f"reports {self.fclk0_mhz:.4f}. Update §8 and "
                      f"vivado/tme_standalone/README.md — the design is "
                      f"constrained at 20 ns, so anything up to 50 MHz still "
                      f"has post-route margin, but the elapsed times below "
                      f"scale with this.")
        except Exception as exc:                       # noqa: BLE001
            self.fclk0_mhz = None
            print(f"\nPL clock: could not read Clocks.fclk0_mhz ({exc}); "
                  f"elapsed times below cannot be converted to cycles.")

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

        Raises ValueError on illegal geometry BEFORE touching `ap_start` —
        that is the whole point of `validate_geometry`, since the core has no
        rejection path of its own.
        """
        errs = validate_geometry(pw, ph, tw, th, self.dma_max)
        if errs:
            raise ValueError(
                f"refusing to start the matcher on {pw}x{ph} / {tw}x{th}:\n"
                + "\n".join(f"    - {e}" for e in errs))
        if len(patch) != pw * ph:
            raise ValueError(f"patch slice is {len(patch)} B, geometry says "
                             f"{pw * ph} B — the core reads exactly "
                             f"patch_w*patch_h beats and ignores TLAST, so a "
                             f"mismatch desynchronises every later case")
        if len(templ) != tw * th:
            raise ValueError(f"template slice is {len(templ)} B, geometry says "
                             f"{tw * th} B")

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
        try:
            channel.wait()
        except Exception:                              # noqa: BLE001
            pass

    def _wait_done(self, deadline: float) -> None:
        """Poll AP_CTRL until ap_done or ap_idle.

        ap_done is Clear-on-Read, so the first poll that observes it also
        consumes it — hence the latch.  Accepting ap_idle as well means a run
        whose done bit was consumed by something else (an interrupt handler,
        an operator poking register_map from a notebook) still terminates.
        """
        seen_done = False
        while True:
            ctrl = self._r(REG_AP_CTRL)
            if ctrl & AP_DONE:
                seen_done = True
            if seen_done or (ctrl & AP_IDLE):
                return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"ap_done never rose within {self.timeout_s:g} s "
                    f"(AP_CTRL=0x{ctrl:08X}). The core is blocked in a stream "
                    f"read, wanting beats that never arrived. Either a "
                    f"transfer was truncated (what §3.1's bound looks like "
                    f"from the PS), or a DMA never started at all — check "
                    f"that both channels left the idle state.")
            time.sleep(0.001)

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

    def close(self) -> None:
        """Halt both channels BEFORE releasing the buffers they read.

        `freebuffer()` hands CMA pages back to the pool. A channel that is
        still mid-transfer keeps reading those physical addresses — and
        mid-transfer is exactly the state a timeout leaves it in, which is the
        one path where `close()` matters most. Stopping first is not tidiness;
        without it a timeout can turn into memory corruption somewhere
        unrelated, long after this script exits.

        If a channel cannot be stopped, the buffers are deliberately LEAKED.
        Leaking a few hundred KB of CMA until reboot is strictly better than
        returning pages the PL may still be writing through.
        """
        for ch, label in ((getattr(self, "ch_patch", None), "patch"),
                          (getattr(self, "ch_templ", None), "template")):
            if ch is None:
                continue
            try:
                if not ch.idle:
                    ch.stop()
                else:
                    # Settle PYNQ's per-channel bookkeeping for a transfer
                    # that completed. Raises if nothing was started, which is
                    # fine and expected on an early failure.
                    try:
                        ch.wait()
                    except Exception:                  # noqa: BLE001
                        pass
            except Exception as exc:                   # noqa: BLE001
                print(f"WARNING: could not stop the {label} DMA ({exc}). "
                      f"NOT freeing the DMA buffers — they are leaked on "
                      f"purpose, because handing that memory back while the "
                      f"PL may still read it is the worse failure.")
                return

        for buf in (getattr(self, "patch_buf", None),
                    getattr(self, "templ_buf", None)):
            if buf is None:
                continue
            try:
                buf.freebuffer()
            except Exception:                          # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Self-test: the validator, off the board
# ---------------------------------------------------------------------------

def selftest() -> int:
    """Exercise validate_geometry without PYNQ.

    The validator is the only thing standing between a bad descriptor and
    silent BRAM corruption, and it is pure Python — so it should not be a
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
    assert not validate_geometry(820, 307, 216, 96), "820x307 must be legal"
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

    print(f"\n{'SELF-TEST PASSED' if not failures else f'SELF-TEST FAILED: {failures}'}")
    return 0 if not failures else 1


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlay", help="path to tme_standalone.bit")
    ap.add_argument("--data-dir", default=".", type=Path,
                    help="directory holding tb_tme_*_<suite>.{txt,bin}")
    ap.add_argument("--suite", default="hw", choices=("hw", "cosim", "csim"),
                    help="manifest to run (default hw — the only one that "
                         "carries the 251,740 B §3.1 case)")
    ap.add_argument("--patch-dma", help="overlay attribute for the patch MM2S")
    ap.add_argument("--templ-dma", help="overlay attribute for the template MM2S")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"per-case timeout in seconds (default "
                         f"{DEFAULT_TIMEOUT_S:g}; the stress case needs ~12 s "
                         f"at 31.25 MHz)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the geometry-validator self-test and exit; "
                         "needs no board")
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
    if biggest < 200_000:
        print("  WARNING: no case here comes near the §3.1 single-transfer "
              "bound. This run will verify arithmetic only. Use --suite hw.")

    try:
        import pynq                                    # noqa: F401
    except ImportError as exc:
        print(f"\nCANNOT RUN: pynq is not importable ({exc}). This script "
              f"drives real hardware; use --selftest off the board.")
        return 2

    try:
        dut = TmeStandalone(args.overlay, args.patch_dma, args.templ_dma,
                            args.timeout)
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
        print("\n--- geometry validation (§4.1 / §3.1, before any start) ---")
        bad = False
        for c in cases:
            errs = validate_geometry(c.pw, c.ph, c.tw, c.th, dut.dma_max)
            status = "OK" if not errs else "REJECT"
            print(f"  [{status:6}] {c.tag:<24} patch {c.pw}x{c.ph} "
                  f"({c.patch_bytes:,} B)  templ {c.tw}x{c.th}")
            for e in errs:
                print(f"             - {e}")
            bad |= bool(errs)
        if bad:
            print("\nA manifest case is outside the envelope. Not starting.")
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
        return 0 if ok_overall else 1
    finally:
        dut.close()


if __name__ == "__main__":
    sys.exit(main())
