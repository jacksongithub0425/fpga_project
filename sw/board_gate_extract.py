#!/usr/bin/env python3
"""Board gate 4: the extractor and the matcher, through the combined overlay.

RUN THIS ON THE BOARD, after board_gate_full_dma.py PASSES:

    sudo python3 board_gate_extract.py --overlay three_stage_combined.bit

Gate 3 validated `binarize_page()` at full size.  This is the gate for
everything after it: `extract_candidates()` has never run on silicon in any
form, and `tme_top_0` has only ever run in a DIFFERENT image — the standalone
template_match bring-up (9/9, 2026-08-07, contract §8).  Neither result says
anything about the two of them in `three_stage_combined`, sharing HP ports and
sequenced by one driver.

WHY A PINNED 24x20 GOLDEN AND NOT A REAL PDF.  A page from the corpus gives a
detection count, and a detection count is a number that can be right for the
wrong reasons — the extractor could clip a patch one pixel short, or the
matcher could report a location in patch coordinates instead of page
coordinates, and the totals would very likely survive it.  What is needed
first is a case whose every intermediate byte is known in advance, and that
already exists: the three-stage C/golden in `hls/integration/`, which passed
Vitis HLS CSim on 2026-08-09 and is composed from the binarizer, extractor and
matcher oracles that were each already proved on their own.  This gate feeds
the SAME vectors to real hardware and demands the SAME bytes:

    480 gray bytes -> 480 binary bytes -> one 168-byte 14x12 patch at (3,4)
    -> matcher score +1.000000 at local (4,1), page (7,5)

Every one of those is asserted byte-for-byte or exactly, never by tolerance,
except the two matcher scores where the contract defines a tolerance (§4.6:
agreement with the float oracle is tolerance-based, SCORE_TOL = 0.005).

WHAT EACH PHASE ADDS, and why it is a separate phase:

  A  binarize the 24x20 page.  Small, but not redundant with gate 3: the
     compact-stride path at a size where every one of the 480 bytes is
     compared individually, and the input the rest of the gate depends on.
  B  extract one candidate.  The first execution of `patch_extract_core` on
     silicon.  Metadata geometry, the §7.1.1 status registers, the patch
     length as MEASURED BY THE DMA, and all 168 pixels.
  C  the matcher's own 9-case `hw` manifest, through THIS overlay's
     `axi_dma_patch`/`axi_dma_templ` and through `PLPipeline.match_template`
     rather than the standalone script — including the 251,740-byte
     maximum-envelope case, which is the only test of §3.1's single-transfer
     bound, and a re-invocation after it to catch stale `static` BRAM.
  D  `match_candidate()`: the PS-side reduction that has no hardware at all
     since class_score_core left the MVP (§10 items 4-5).  Absolute box
     construction, the strict-`>` tie rule over the frozen trial order, and
     the per-kind argmax — each with a control that would fail if the rule
     were the other way round.
  E  the chain.  binarize -> extract -> match with the patch that came out of
     the PL in phase B, not the one read from the golden file, required to
     give bit-identical results to phase D.  Phases A-D each check one core
     against a file; only this one checks that the bytes handed BETWEEN them
     are the same bytes.

This gate is CMA-light on purpose: the page is 480 bytes, so the two image
buffers are trivial and the ~120.8 MiB driver-order allocation of gate 1 is
not repeated.  A failure here is therefore about the cores, not the pool.

Needs on the board, same directory:

    three_stage_combined.bit / .hwh / BUILD_INFO.txt
    tme_driver.py, tme_standalone_bringup.py
    tb_bpe_tme_{gray,bin,patch,templs}.bin, tb_bpe_tme_cases.txt
        -- from hls/integration/, run pe_tme_generate_golden.py
    tb_tme_cases_hw.txt, tb_tme_patches_hw.bin, tb_tme_templs_hw.bin
        -- from hls/template_match/, run tme_generate_golden.py

None of those vectors are committed (they are generated files); regenerate
and copy them.  `--selftest` checks every one of this gate's pinned
expectations against the golden files with no PYNQ and no board, including
that the descriptor it will dispatch is one `patch_extract_core` accepts.
Run it after touching this file.

Exit status: 0 = every phase passed, 1 = a phase FAILED (the hardware or the
driver is wrong), 2 = could not run (missing vectors, missing module, no
PYNQ, unloadable overlay).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Pinned expectations.
#
# These are the numbers the three-stage C/golden produced and Vitis CSim
# confirmed on 2026-08-09; `hls/integration/pe_tme_generate_golden.py` pins the
# same values, plus SHA-256 over every blob, on the generator side.  They are
# repeated here rather than read from the manifest ALONE for one reason: a
# regenerated manifest that drifted would otherwise move the goalposts, and
# this gate would happily "pass" against whatever the new file said.  The
# manifest is read as well, and the two must agree — see check_manifest().
# ---------------------------------------------------------------------------

IMG_W, IMG_H = 24, 20
THRESHOLD = 140
PAGE_BYTES = IMG_W * IMG_H              # 480

EP_X, EP_Y, SIDE = 12, 10, "left"
MAX_TW, MAX_TH = 4, 4
PACKED = 0x00040010000A000C             # the cand_in AXIS word for the above

PATCH_X0, PATCH_Y0 = 3, 4
PATCH_W, PATCH_H = 14, 12
PATCH_BYTES = PATCH_W * PATCH_H         # 168

# The literal 4x4 template ("alpha"), which is also the exact golden crop at
# local (4,1) — the peak is 1.0 there and unique with margin 0.622036.
ALPHA_LOCAL = (4, 1)
ALPHA_PAGE = (PATCH_X0 + ALPHA_LOCAL[0], PATCH_Y0 + ALPHA_LOCAL[1])      # (7,5)

# A second exact crop, at local (3,8), used only by phases D and E.  Also a
# unique 1.0 peak (margin 0.512050, derived with the same exact-integer oracle
# in tme_generate_golden.golden and re-derived by --selftest).  Two templates
# that BOTH score exactly 1.0 are what makes the tie-break testable without a
# float coincidence: the winner is then decided purely by trial order.
BETA_CROP = (3, 8)                      # (ux, uy) into the patch
BETA_PAGE = (PATCH_X0 + BETA_CROP[0], PATCH_Y0 + BETA_CROP[1])           # (6,12)

TEMPL_W = TEMPL_H = 4

# §4.6: agreement with the float oracle is tolerance-based, not bit-exact.
# Same value as MAX_SCORE_ERR in tme_tb.cpp and SCORE_TOL in the bring-up.
SCORE_TOL = 0.005

_BPE_FILES = ("tb_bpe_tme_cases.txt", "tb_bpe_tme_gray.bin",
              "tb_bpe_tme_bin.bin", "tb_bpe_tme_patch.bin",
              "tb_bpe_tme_templs.bin")


class GateError(Exception):
    """A phase failed.  Distinct from an environment problem (exit 2)."""


class SetupError(Exception):
    """The gate could not be run at all — exit 2, never exit 1."""


# ---------------------------------------------------------------------------
# Golden vectors
# ---------------------------------------------------------------------------

def unpack_candidate(word: int) -> tuple:
    """Inverse of tme_driver.pack_candidate — (ep_x, ep_y, side, tw, th).

    Written out here rather than imported so the manifest cross-check has an
    independent decoder: if the packer's field placement ever drifts, a check
    that used the packer to decode the packer would agree with itself.
    """
    return (word & 0xFFFF, (word >> 16) & 0xFFFF, (word >> 32) & 0x3,
            (word >> 34) & 0x3FFF, (word >> 48) & 0xFFFF)


def resolve_dir(data_dir: Path, files, fallback: str, what: str) -> Path:
    """`data_dir` if it holds `files`, else the in-repo directory they live in.

    On the board everything is copied flat into one directory, which is what
    `--data-dir` names.  In a checkout the vectors stay where their generators
    put them, and making `--selftest` runnable there without copying anything
    is worth the six lines: a self-test that needs a manual staging step is a
    self-test that gets skipped.
    """
    if all((data_dir / f).is_file() for f in files):
        return data_dir
    alt = Path(__file__).resolve().parent.parent / fallback
    if all((alt / f).is_file() for f in files):
        return alt
    missing = [f for f in files if not (data_dir / f).is_file()]
    raise SetupError(
        f"{what}: missing {', '.join(missing)} in {data_dir} (and not in "
        f"{alt} either). These are generated files and are not committed — "
        f"regenerate them and copy them to the board.")


def load_bpe_golden(data_dir: Path) -> dict:
    """Read the three-stage golden: manifest, gray, binary, patch, template."""
    data_dir = resolve_dir(data_dir, _BPE_FILES, "hls/integration",
                           "three-stage golden (pe_tme_generate_golden.py)")

    lines = [ln for ln in (data_dir / "tb_bpe_tme_cases.txt")
             .read_text().splitlines() if ln.strip()]
    if len(lines) != 2:
        raise SetupError(f"tb_bpe_tme_cases.txt has {len(lines)} lines, "
                         f"expected a header and exactly one descriptor")
    head = lines[0].split()
    if len(head) != 11:
        raise SetupError(f"tb_bpe_tme_cases.txt header has {len(head)} "
                         f"fields, expected 11")
    row = lines[1].split()
    if len(row) != 21:
        raise SetupError(f"tb_bpe_tme_cases.txt row has {len(row)} fields, "
                         f"expected 21")

    g = {
        "n_cands": int(head[0]), "img_w": int(head[1]), "img_h": int(head[2]),
        "stride": int(head[3]), "buffer_bytes": int(head[4]),
        "n_templ": int(head[5]), "templ_bytes": int(head[6]),
        "threshold": int(head[7]), "gray_bytes": int(head[8]),
        "bin_bytes": int(head[9]), "patch_bytes": int(head[10]),
        "packed": int(row[1], 16), "last": int(row[2]), "valid": int(row[3]),
        "reason": int(row[4], 16),
        "x0": int(row[5]), "y0": int(row[6]),
        "pw": int(row[7]), "ph": int(row[8]),
        "tw": int(row[11]), "th": int(row[12]),
        "score": float(row[13]),
        "page_x": int(row[14]), "page_y": int(row[15]),
        "ux": int(row[16]), "uy": int(row[17]),
        "margin": float(row[18]), "tag": row[20],
    }

    def blob(name, count, shape):
        raw = (data_dir / name).read_bytes()
        if len(raw) != count:
            raise SetupError(f"{name}: {len(raw)} B, expected {count} B — "
                             f"the vectors and the manifest disagree")
        return np.frombuffer(raw, dtype=np.uint8).reshape(shape).copy()

    g["gray"] = blob("tb_bpe_tme_gray.bin", g["gray_bytes"],
                     (g["img_h"], g["img_w"]))
    g["bin"] = blob("tb_bpe_tme_bin.bin", g["bin_bytes"],
                    (g["img_h"], g["img_w"]))
    g["patch"] = blob("tb_bpe_tme_patch.bin", g["patch_bytes"],
                      (g["ph"], g["pw"]))
    g["alpha"] = blob("tb_bpe_tme_templs.bin", g["templ_bytes"],
                      (g["th"], g["tw"]))
    g["beta"] = np.ascontiguousarray(
        g["patch"][BETA_CROP[1]:BETA_CROP[1] + TEMPL_H,
                   BETA_CROP[0]:BETA_CROP[0] + TEMPL_W])
    return g


def check_manifest(g: dict) -> list:
    """Every pinned constant, against the golden files.  [] means agreement.

    Runs before the overlay is touched, on the board and in `--selftest`. A
    disagreement means the vectors were regenerated from a changed oracle, and
    the right response is to look at what changed — not to run the gate
    against expectations it no longer shares.
    """
    e = []
    ep_x, ep_y, side, tw, th = unpack_candidate(g["packed"])

    def want(label, got, exp):
        if got != exp:
            e.append(f"{label}: golden says {got}, this gate expects {exp}")

    want("img_w", g["img_w"], IMG_W)
    want("img_h", g["img_h"], IMG_H)
    want("stride", g["stride"], IMG_W)          # compact, as the S2MM emits
    want("threshold", g["threshold"], THRESHOLD)
    want("gray bytes", g["gray_bytes"], PAGE_BYTES)
    want("binary bytes", g["bin_bytes"], PAGE_BYTES)
    want("patch bytes", g["patch_bytes"], PATCH_BYTES)
    want("descriptors", g["n_cands"], 1)
    want("packed descriptor", f"0x{g['packed']:016X}", f"0x{PACKED:016X}")
    want("endpoint", (ep_x, ep_y), (EP_X, EP_Y))
    want("side code", side, 0)
    want("max_tw/max_th", (tw, th), (MAX_TW, MAX_TH))
    want("valid", g["valid"], 1)
    want("reason", g["reason"], 0)
    want("patch origin", (g["x0"], g["y0"]), (PATCH_X0, PATCH_Y0))
    want("patch size", (g["pw"], g["ph"]), (PATCH_W, PATCH_H))
    want("template size", (g["tw"], g["th"]), (TEMPL_W, TEMPL_H))
    want("alpha local peak", (g["ux"], g["uy"]), ALPHA_LOCAL)
    want("alpha page peak", (g["page_x"], g["page_y"]), ALPHA_PAGE)
    if abs(g["score"] - 1.0) > 1e-9:
        e.append(f"alpha golden score {g['score']} is not 1.0")

    # Structural facts the gate's own arithmetic rests on.
    if not np.array_equal(g["patch"],
                          g["bin"][PATCH_Y0:PATCH_Y0 + PATCH_H,
                                   PATCH_X0:PATCH_X0 + PATCH_W]):
        e.append("the golden patch is not the golden page cropped at "
                 f"({PATCH_X0},{PATCH_Y0}) {PATCH_W}x{PATCH_H} — phase E's "
                 "chain assertion would be comparing unrelated things")
    if not np.array_equal(g["alpha"],
                          g["patch"][ALPHA_LOCAL[1]:ALPHA_LOCAL[1] + TEMPL_H,
                                     ALPHA_LOCAL[0]:ALPHA_LOCAL[0] + TEMPL_W]):
        e.append("the alpha template is no longer the exact patch crop at "
                 f"local {ALPHA_LOCAL}")
    for name in ("alpha", "beta"):
        t = g[name]
        if int(t.min()) == int(t.max()):
            e.append(f"the {name} template is flat — illegal input (§4.6)")
    if np.array_equal(g["alpha"], g["beta"]):
        e.append("alpha and beta are the same template; phase D's per-kind "
                 "reduction would have nothing to distinguish")
    if set(np.unique(g["bin"]).tolist()) - {0, 255}:
        e.append("the golden binary page holds values other than 0 and 255")
    return e


def check_dispatchable(driver) -> list:
    """Prove the descriptor this gate dispatches is one the core accepts.

    `extract_candidates` refuses to dispatch a descriptor `patch_extract_core`
    would reject, because a rejected candidate emits no pixels and would
    strand the patch receive armed for it.  So a gate whose descriptor is
    illegal fails as a driver ValueError with nothing having reached the PL —
    which is a correct outcome but a useless gate.  Checked off-board instead.

    `buffer_bytes` here is the real one the driver programs: the binary buffer
    carries `_OUTPUT_GUARD_BYTES` past the visible page, so it is larger than
    the manifest's 480 and the §4.3 footprint test is satisfied a fortiori.
    """
    e = []
    buffer_bytes = PAGE_BYTES + driver._OUTPUT_GUARD_BYTES
    box = driver.predict_patch_box(EP_X, EP_Y, 0, MAX_TW, MAX_TH, IMG_W, IMG_H)
    if box != (PATCH_X0, PATCH_Y0, PATCH_W, PATCH_H):
        e.append(f"predict_patch_box gives {box}, golden says "
                 f"{(PATCH_X0, PATCH_Y0, PATCH_W, PATCH_H)}")
    reasons = driver.predict_reject_reasons(
        EP_X, EP_Y, 0, MAX_TW, MAX_TH, IMG_W, IMG_H, IMG_W, buffer_bytes)
    if reasons:
        e.append("the host-side predictor would reject this descriptor: "
                 + "; ".join(driver._REASON_NAMES_BY_BIT[b] for b in reasons))
    if driver.pack_candidate(EP_X, EP_Y, 0, MAX_TW, MAX_TH) != \
            PACKED.to_bytes(8, "little"):
        e.append("pack_candidate does not reproduce the golden descriptor "
                 f"word 0x{PACKED:016X}")
    envelope = driver.compute_cand_envelope(
        {SIDE: {"alpha": [np.zeros((TEMPL_H, TEMPL_W), dtype=np.uint8)]}},
        SIDE, (1.0,))
    if envelope != (MAX_TW, MAX_TH):
        e.append(f"compute_cand_envelope gives {envelope}, the golden "
                 f"descriptor carries {(MAX_TW, MAX_TH)}")
    return e


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
    """Accumulates PASS/FAIL lines so one failure does not hide the rest.

    A phase still stops at its first failure — every failure mode in here
    leaves the PL in a state that makes later results noise — but the phases
    that did run report themselves.
    """

    def __init__(self):
        self.failures: list = []
        self.checks = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
              + (f": {detail}" if detail else ""))
        if not ok:
            self.failures.append(label)
        return ok

    def require(self, ok: bool, label: str, detail: str = "") -> None:
        if not self.check(ok, label, detail):
            raise GateError(label)


def first_mismatch(got: np.ndarray, want: np.ndarray) -> str:
    """Locate the first differing byte without allocating an index array.

    `np.argmax` on the bool array, never `np.argwhere`: the same rule the
    full-page gate follows, and the reason is the same — the diagnostic must
    not be the thing that runs out of memory.
    """
    diff = got.ravel() != want.ravel()
    n = int(np.count_nonzero(diff))
    if not n:
        return ""
    i = int(np.argmax(diff))
    r, c = divmod(i, got.shape[1])
    return (f"{n}/{got.size} bytes differ; first at byte {i} = row {r} "
            f"col {c}: got {int(got.ravel()[i])}, want {int(want.ravel()[i])}")


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

_HW_FILES = ("tb_tme_cases_hw.txt", "tb_tme_patches_hw.bin",
             "tb_tme_templs_hw.bin")


def load_hw_manifest(data_dir: Path):
    """The matcher's 9-case silicon manifest, with the same directory rule."""
    import tme_standalone_bringup as B
    d = resolve_dir(data_dir, _HW_FILES, "hls/template_match",
                    "matcher hw manifest (tme_generate_golden.py)")
    try:
        return B.load_manifest(d, "hw")
    except (FileNotFoundError, ValueError) as exc:
        raise SetupError(f"{d}: {exc}") from exc


def phase_a_binarize(pl, g: dict, rep: Report) -> None:
    print("\n--- phase A: binarize the 24x20 page ---")
    t0 = time.monotonic()
    binary = pl.binarize_page(g["gray"], THRESHOLD)
    secs = time.monotonic() - t0

    st = pl.last_transfer_stats
    rep.require(st is not None, "binarize_page published transfer stats")
    rep.require(st["mm2s_bytes"] == PAGE_BYTES,
                f"MM2S moved {PAGE_BYTES} B", f"{st['mm2s_bytes']} B")
    rep.require(st["s2mm_bytes"] == PAGE_BYTES,
                f"S2MM received {PAGE_BYTES} B", f"{st['s2mm_bytes']} B")
    rep.require(st["sentinel_bytes_remaining"] == 0,
                "no 0xAA sentinel survives in the page",
                f"{st['sentinel_bytes_remaining']} unwritten bytes")
    rep.require(st["guard_bytes_clobbered"] == 0,
                "the 64-byte guard tail is intact",
                f"{st['guard_bytes_clobbered']} clobbered")
    rep.require(binary.shape == (IMG_H, IMG_W),
                f"binary page is {IMG_W}x{IMG_H}", str(binary.shape))

    mism = first_mismatch(binary, g["bin"])
    rep.require(not mism, f"all {PAGE_BYTES} binary bytes match the golden",
                mism)
    print(f"  {PAGE_BYTES} B in {secs:.3f} s")


def phase_b_extract(pl, g: dict, rep: Report) -> dict:
    print("\n--- phase B: extract one candidate ---")
    side_templates = {SIDE: {"alpha": [g["alpha"]]}}
    scales = (1.0,)
    cands = [{"endpoint": (EP_X, EP_Y), "side": SIDE}]

    t0 = time.monotonic()
    recs = pl.extract_candidates(cands, side_templates, scales)
    secs = time.monotonic() - t0

    rep.require(len(recs) == 1, "one record per descriptor", str(len(recs)))
    rec = recs[0]

    st = pl.last_extract_stats
    rep.require(st is not None, "extract_candidates published status stats")
    rep.require(st["sts_flags"] == 0, "sts_flags == 0",
                f"0x{st['sts_flags']:X}")
    rep.require(st["sts_rejected"] == 0, "sts_rejected == 0",
                str(st["sts_rejected"]))
    rep.require(st["sts_processed"] == 1, "sts_processed == 1",
                str(st["sts_processed"]))

    rep.require(bool(rec["valid"]), "§6.2 record: valid == 1",
                str(rec["valid"]))
    rep.require((rec["x0"], rec["y0"]) == (PATCH_X0, PATCH_Y0),
                f"§6.2 record: origin ({PATCH_X0},{PATCH_Y0})",
                f"({rec['x0']},{rec['y0']})")
    rep.require((rec["patch_w"], rec["patch_h"]) == (PATCH_W, PATCH_H),
                f"§6.2 record: patch {PATCH_W}x{PATCH_H}",
                f"{rec['patch_w']}x{rec['patch_h']}")

    # The DMA's own count, not the record's arithmetic: TLAST must land
    # exactly on beat patch_w*patch_h.  tme_top ignores TLAST and reads the
    # count it is told, so a framing disagreement is silent in the matcher and
    # corrupts the NEXT patch.
    rep.require(st["patch_bytes"] == [PATCH_BYTES],
                f"patch S2MM moved exactly {PATCH_BYTES} B",
                str(st["patch_bytes"]))

    patch = rec["patch"]
    rep.require(patch.shape == (PATCH_H, PATCH_W),
                f"patch array is {PATCH_W}x{PATCH_H}", str(patch.shape))
    mism = first_mismatch(patch, g["patch"])
    rep.require(not mism, f"all {PATCH_BYTES} patch bytes match the golden",
                mism)
    print(f"  {PATCH_BYTES} B in {secs:.3f} s")
    return rec


def phase_c_matcher_suite(pl, data_dir: Path, rep: Report) -> None:
    """The 9-case `hw` manifest, through this overlay and this driver."""
    print("\n--- phase C: the 9-case matcher manifest through tme_top_0 ---")
    cases, patches, templs = load_hw_manifest(data_dir)
    rep.require(len(cases) == 9, "the hw manifest carries 9 cases",
                str(len(cases)))
    biggest = max(c.patch_bytes for c in cases)
    rep.require(biggest == 251_740,
                "the maximum-envelope case moves 251,740 B",
                f"{biggest:,} B")

    def run(c):
        patch = np.frombuffer(patches, dtype=np.uint8, count=c.patch_bytes,
                              offset=c.patch_off).reshape(c.ph, c.pw)
        templ = np.frombuffer(templs, dtype=np.uint8, count=c.templ_bytes,
                              offset=c.templ_off).reshape(c.th, c.tw)
        return pl.match_template(patch, templ)

    for c in cases:
        score, x, y, secs = run(c)
        score_ok = abs(score - c.score) <= SCORE_TOL
        loc_ok = (x, y) == (c.x, c.y)
        rep.require(score_ok and loc_ok,
                    f"[{c.index}] {c.tag} ({c.pw}x{c.ph} / {c.tw}x{c.th})",
                    f"dut {score:+.6f} @({x},{y}) vs gold {c.score:+.6f} "
                    f"@({c.x},{c.y}), {secs:.3f} s")

    # tme_top's patch/template BRAMs and column accumulators are `static`, so
    # the 820x307 case leaves 251,740 B of residue a later small case must not
    # read.  The manifest puts the stress cases last, so without this the
    # shrink direction is never exercised on hardware.
    c = cases[0]
    score, x, y, _ = run(c)
    rep.require(abs(score - c.score) <= SCORE_TOL and (x, y) == (c.x, c.y),
                f"re-invocation of {c.tag} after {biggest:,} B",
                f"dut {score:+.6f} @({x},{y}) — stale BRAM would show here")


def phase_d_match_candidate(pl, g: dict, rep: Report,
                            patch: np.ndarray, label: str) -> dict:
    """The PS-side reduction: boxes, tie order, per-kind argmax.

    Both templates are exact crops of the patch, so BOTH score exactly 1.0 at
    their own location.  That is deliberate: it makes `best` a genuine tie
    that only the trial order can settle, with no float coincidence involved,
    and it gives the per-kind reduction two known, different locations to keep
    apart.
    """
    import tme_driver as d
    print(f"\n--- phase D: match_candidate ({label}) ---")
    banks = {"alpha": [g["alpha"]], "beta": [g["beta"]]}
    trials = d.build_trials(banks, (1.0,))

    rep.require(len(trials) == 2, "two trials in the frozen order",
                str(len(trials)))
    rep.require([t["kind"] for t in trials] == ["alpha", "beta"],
                "trial order is alpha, then beta",
                str([t["kind"] for t in trials]))
    rep.require(all(t["legal"] for t in trials),
                "both templates are legal (§4.6)")

    out = pl.match_candidate(patch, PATCH_X0, PATCH_Y0, trials)
    best, by_kind = out["best"], out["by_kind"]

    rep.require(set(by_kind) == {"alpha", "beta"},
                "per-kind reduction kept both kinds", str(sorted(by_kind)))

    a, b = by_kind["alpha"], by_kind["beta"]
    rep.require(abs(a["score"] - 1.0) <= SCORE_TOL, "alpha score is 1.0",
                f"{a['score']:+.6f}")
    rep.require((a["match_x"], a["match_y"]) == ALPHA_LOCAL,
                f"alpha peak at local {ALPHA_LOCAL}",
                f"({a['match_x']},{a['match_y']})")
    rep.require(a["box"] == (ALPHA_PAGE[0], ALPHA_PAGE[1], TEMPL_W, TEMPL_H),
                f"alpha box is absolute page {ALPHA_PAGE} + {TEMPL_W}x"
                f"{TEMPL_H}", str(a["box"]))

    rep.require(abs(b["score"] - 1.0) <= SCORE_TOL, "beta score is 1.0",
                f"{b['score']:+.6f}")
    rep.require((b["match_x"], b["match_y"]) == BETA_CROP,
                f"beta peak at local {BETA_CROP}",
                f"({b['match_x']},{b['match_y']})")
    rep.require(b["box"] == (BETA_PAGE[0], BETA_PAGE[1], TEMPL_W, TEMPL_H),
                f"beta box is absolute page {BETA_PAGE} + {TEMPL_W}x"
                f"{TEMPL_H}", str(b["box"]))

    # The tie, and the proof that it IS a tie rather than alpha simply being
    # better: the two scores must be identical bit-for-bit, and the first
    # trial must win.  `>=` instead of `>` in the reduction would hand this to
    # beta, silently, on every tied page.
    rep.require(a["score"] == b["score"],
                "the two kinds score identically — a real tie",
                f"{a['score']!r} vs {b['score']!r}")
    rep.require(best is not None and best["kind"] == "alpha"
                and best["templ_id"] == 0,
                "the tie goes to the FIRST trial (strict >, §6.4)",
                f"best kind={best['kind'] if best else None}")

    # The control.  If the tie were being settled by anything except order,
    # reversing the bank would not change the winner — and this suite would be
    # asserting a coincidence.
    rev = d.build_trials({"beta": [g["beta"]], "alpha": [g["alpha"]]}, (1.0,))
    out_rev = pl.match_candidate(patch, PATCH_X0, PATCH_Y0, rev)
    rep.require(out_rev["best"]["kind"] == "beta",
                "control: reversing the trial order moves the tie to beta",
                f"best kind={out_rev['best']['kind']}")

    # An empty selection must not touch the hardware at all.
    empty = pl.match_candidate(patch, PATCH_X0, PATCH_Y0, [])
    rep.require(empty == {"best": None, "by_kind": {}},
                "an empty trial list returns without running anything",
                str(empty))
    return out


def phase_e_chain(pl, g: dict, rep: Report, hw_patch: np.ndarray,
                  golden_out: dict) -> None:
    """binarize -> extract -> match, on the bytes the PL itself produced."""
    print("\n--- phase E: the chain, on the PL's own patch ---")
    rep.require(np.array_equal(hw_patch, g["patch"]),
                "the extractor's patch equals the golden patch")
    out = phase_d_match_candidate(pl, g, rep, hw_patch, "chained")
    for kind in ("alpha", "beta"):
        a, b = golden_out["by_kind"][kind], out["by_kind"][kind]
        rep.require(
            (a["score"], a["match_x"], a["match_y"], a["box"])
            == (b["score"], b["match_x"], b["match_y"], b["box"]),
            f"chained {kind} result is identical to the golden-fed run",
            f"{b['score']:+.6f} @({b['match_x']},{b['match_y']}) {b['box']}")
    print(f"\n  CHAIN: {PAGE_BYTES} gray bytes -> {PAGE_BYTES} binary bytes "
          f"-> {PATCH_BYTES} patch bytes at ({PATCH_X0},{PATCH_Y0}) -> "
          f"page {ALPHA_PAGE}")


# ---------------------------------------------------------------------------
# Self-test (no PYNQ, no board)
# ---------------------------------------------------------------------------

def selftest(data_dir: Path) -> int:
    """Check every pinned expectation against the golden files, off-board.

    Not a substitute for the gate — it proves nothing about hardware.  What it
    proves is that the gate is asking the right questions: that its constants
    still match the vectors, that the descriptor it dispatches is one the core
    accepts (otherwise the gate fails as a driver ValueError with nothing
    having reached the PL), and that the two templates really do both peak at
    1.0 at the two distinct locations the tie-break case depends on.
    """
    print("board_gate_extract self-test (no PYNQ, no board)")
    rep = Report()
    try:
        g = load_bpe_golden(data_dir)
    except SetupError as exc:
        print(f"CANNOT RUN: {exc}")
        return 2

    for err in check_manifest(g):
        rep.check(False, "manifest agreement", err)
    rep.check(not rep.failures, "pinned constants agree with the golden files")

    try:
        import tme_driver as d
    except Exception as exc:                               # noqa: BLE001
        print(f"CANNOT RUN: tme_driver.py did not import "
              f"({type(exc).__name__}: {exc})")
        return 2
    errs = check_dispatchable(d)
    for err in errs:
        rep.check(False, "descriptor dispatchability", err)
    rep.check(not errs, "the descriptor is one patch_extract_core accepts")

    # The matcher manifest is checked for shape only — the cases themselves
    # need hardware — but the two facts phase C asserts before running
    # anything are checkable here, and a missing manifest should be found
    # before the board is booked rather than in phase C.
    try:
        cases, _, _ = load_hw_manifest(data_dir)
        rep.check(len(cases) == 9, "the hw manifest carries 9 cases",
                  str(len(cases)))
        biggest = max(c.patch_bytes for c in cases)
        rep.check(biggest == 251_740, "the maximum-envelope case is present",
                  f"{biggest:,} B")
    except SetupError as exc:
        rep.check(False, "matcher hw manifest is available", str(exc))

    # Re-derive the matcher expectations from the exact-integer oracle if it
    # is reachable.  Loudly optional: the oracle lives in the HLS tree, which
    # is not copied to the board, and a silent skip here would let the two
    # template locations drift without anything noticing.
    oracle = Path(__file__).resolve().parent.parent / "hls" / "template_match"
    if (oracle / "tme_generate_golden.py").is_file():
        sys.path.insert(0, str(oracle))
        try:
            import tme_generate_golden as TME
            for name, loc in (("alpha", ALPHA_LOCAL), ("beta", BETA_CROP)):
                score, ux, uy, margin, _ = TME.golden(g["patch"], g[name])
                rep.check(abs(score - 1.0) <= 1e-12 and (ux, uy) == loc,
                          f"oracle: {name} peaks 1.0 at local {loc}",
                          f"{score:.6f} @({ux},{uy}) margin {margin:.6f}")
                rep.check(margin > 0.05,
                          f"oracle: {name}'s peak is unique enough to assert "
                          f"an exact location", f"margin {margin:.6f}")
        except Exception as exc:                           # noqa: BLE001
            rep.check(False, "exact-integer oracle re-derivation",
                      f"{type(exc).__name__}: {exc}")
    else:
        print(f"  [SKIP] exact-integer oracle not reachable at {oracle} — "
              f"the two template locations were NOT re-derived; run this "
              f"self-test from a checkout that has hls/template_match/")

    print(f"\n{rep.checks - len(rep.failures)}/{rep.checks} checks passed")
    if rep.failures:
        print("FAIL: this gate's expectations no longer match its vectors. "
              "Do not run it on the board until they do.")
        return 1
    print("PASS: constants, descriptor and matcher expectations all agree "
          "with the golden files.")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--overlay", default="three_stage_combined.bit",
                    help="path to the combined overlay bitstream")
    ap.add_argument("--data-dir", default=".",
                    help="directory holding the tb_* vectors")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="per-stage timeout in seconds")
    ap.add_argument("--selftest", action="store_true",
                    help="check the pinned expectations off-board and exit")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)

    if args.selftest:
        return selftest(data_dir)

    try:
        g = load_bpe_golden(data_dir)
    except SetupError as exc:
        print(f"CANNOT RUN: {exc}")
        return 2

    drift = check_manifest(g)
    if drift:
        print("CANNOT RUN: the golden vectors no longer match this gate's "
              "pinned expectations:")
        for d_ in drift:
            print(f"  - {d_}")
        print("Regenerating the vectors moved the goalposts; reconcile them "
              "before running on hardware.")
        return 2

    try:
        import tme_driver as driver
    except Exception as exc:                               # noqa: BLE001
        print(f"CANNOT RUN: tme_driver.py did not import "
              f"({type(exc).__name__}: {exc}) — copy it and "
              f"tme_standalone_bringup.py next to this script.")
        return 2

    bad = check_dispatchable(driver)
    if bad:
        print("CANNOT RUN: the descriptor this gate dispatches is not one "
              "patch_extract_core accepts:")
        for b in bad:
            print(f"  - {b}")
        return 2

    print(f"board_gate_extract — overlay {args.overlay}")
    print(f"page {IMG_W}x{IMG_H} threshold {THRESHOLD}, descriptor "
          f"ep({EP_X},{EP_Y}) {SIDE} {MAX_TW}x{MAX_TH}, "
          f"patch {PATCH_W}x{PATCH_H} @({PATCH_X0},{PATCH_Y0})")

    try:
        pl = driver.PLPipeline(args.overlay, timeout_s=args.timeout)
    except Exception as exc:                               # noqa: BLE001
        print(f"CANNOT RUN: the overlay would not load "
              f"({type(exc).__name__}: {exc}). Check that the .hwh sits next "
              f"to the .bit with the same basename, and that PYNQ is "
              f"installed. This is an environment problem, not a gate "
              f"failure.")
        return 2

    rep = Report()
    status = 0
    try:
        phase_a_binarize(pl, g, rep)
        rec = phase_b_extract(pl, g, rep)
        phase_c_matcher_suite(pl, data_dir, rep)
        golden_out = phase_d_match_candidate(pl, g, rep, g["patch"], "golden")
        phase_e_chain(pl, g, rep, rec["patch"], golden_out)
    except GateError as exc:
        print(f"\nPHASE ABORTED at: {exc}")
        print("The phases after this one did not run — they did not pass.")
        status = 1
    except SetupError as exc:
        print(f"\nCANNOT RUN: {exc}")
        status = 2
    except Exception as exc:                               # noqa: BLE001
        print(f"\nERROR {type(exc).__name__}: {exc}")
        status = 1
    finally:
        # close() is the teardown decision, not a formality: it verifies each
        # ARMED channel is halted (DMACR.Reset==0 and DMASR.Halted==1) before
        # any CMA page goes back, and returns False if it could not.  A gate
        # that passed its cases but could not prove the DMAs stopped has not
        # left the board in a state the next gate can trust.
        freed = pl.close()
        if not freed:
            print("\nTEARDOWN: buffers were RETAINED — a DMA could not be "
                  "proved halted, or a free failed. Reload the overlay "
                  "before running anything else that allocates CMA.")
            status = status or 1

    print("\n" + "=" * 72)
    if status == 0 and not rep.failures:
        print(f"EXTRACTOR GATE PASSED (0 failures, {rep.checks} checks): "
              f"{PAGE_BYTES} gray -> {PAGE_BYTES} binary -> "
              f"{PATCH_BYTES} patch bytes at ({PATCH_X0},{PATCH_Y0}) -> "
              f"matcher +1.000000 at page {ALPHA_PAGE}; 9/9 hw cases through "
              f"the combined overlay including the 251,740 B envelope")
        return 0
    print(f"EXTRACTOR GATE FAILED ({len(rep.failures)} of {rep.checks} "
          f"checks): " + "; ".join(rep.failures[:6])
          + (" ..." if len(rep.failures) > 6 else ""))
    return status or 1


if __name__ == "__main__":
    raise SystemExit(main())
