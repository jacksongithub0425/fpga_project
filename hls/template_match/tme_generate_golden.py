"""
Generate testbench golden data for tme_tb.cpp.

Run from the template_match/ directory (hls/.venv python):
    python tme_generate_golden.py

Self-contained: every case is procedural (seeded numpy), no PDF render, no
sw/ imports.  Two oracles per case:

  1. EXACT integer window sums (int64 integral images + FFT cross-correlation
     rounded back to integers, spot-verified against direct dot products),
     normalised in float64:
         score = (N*STI - ST*SI) / sqrt((N*STT - ST^2) * (N*SII - SI^2))
     This is algebraically cv2's TM_CCOEFF_NORMED scaled by N/N and is the
     same arithmetic tme_top.cpp performs (integer sums, one float
     sqrt/divide), so it is the golden that gets WRITTEN.
  2. cv2.matchTemplate(TM_CCOEFF_NORMED) as the independent reference —
     asserted against oracle 1 per case (score and argmax), never shipped.

The suite exists to discharge the verification gap named in contract §9 /
run_hls.tcl item 2: location assertions, a unique nonzero match, the final
result row/column (the §4.4 +1 fix), patch==template equality, and the
maximum-storage 820x307/216x96 case with near-maximum window energies.
Peak uniqueness is enforced by construction: each randomised case is
seed-searched until the best score clears the runner-up by MIN_MARGIN, so
the TB may assert the DUT's (x, y) EXACTLY, not approximately.

Writes (manifest header: "n_cases patch_blob_bytes templ_blob_bytes";
row: "index pw ph tw th patch_off templ_off score x y margin category tag"):

    tb_tme_cases_csim.txt  / tb_tme_patches_csim.bin  / tb_tme_templs_csim.bin
    tb_tme_cases_cosim.txt / tb_tme_patches_cosim.bin / tb_tme_templs_cosim.bin
    tb_tme_cases_hw.txt    / tb_tme_patches_hw.bin    / tb_tme_templs_hw.bin

The cosim manifest is the small subset run_hls.tcl selects with -argv
"cosim"; anything only in the csim manifest never reaches RTL.

The hw manifest is what sw/tme_standalone_bringup.py sends to the board: the
cosim cases plus the two 820 x 307 stress cases, which stress different things.

`stress-max-envelope` is the point of the suite's existence — its patch is
251,740 bytes, the single AXI DMA transfer contract §3.1 bounds at 262,143
(2^18 - 1, from the DMA's 18-bit c_sg_length_width).  The small cases are all
under 15 KB, so a run limited to them verifies arithmetic and says nothing
about that bound.  Putting it in *cosim* instead would not help: it is
372-411M cycles (below), against the 2,269,854 cycles the whole cosim suite
measured — hours of xsim for one case.  And an RTL simulation containing no DMA
cannot speak to a DMA length bound anyway.  It is a block-design property, so
silicon is the only place it can be tested.

That bracket is arithmetic off `solution1/syn/report`, not a fitted curve.  The
case is rh=212 output rows x th=96 template rows = 20,352 `correlation_core`
calls, each ceil(605/16) = 38 tiles, and a tile is `load_seg` (236 cycles,
min == max) then `mac_loop` (tw + 6 = 222 at tw=216) — so 458 cycles per tile
at best and the report's own 509 max at worst, giving 354-394M for correlation
alone.  The incremental window loops add 20,352 x (tw+1 + rw) = 16.7M and
everything else under 1M.  Do NOT try to pin it tighter by fitting the four
measured cosim latencies: four points against four free parameters interpolates
exactly and then extrapolates to nonsense (it wants a -56,109-cycle constant
and a NEGATIVE per-tile cost).

**Superseded 2026-08-07 by a board measurement: 13.362 s, i.e. ~418M cycles at
31.25 MHz** (and 0.676 s for stress-max-result).  The bracket's top was 1.6%
low — close for the case it was built for, and 11% low on the smaller stress
case, so do not reuse it as a general model.  It is kept here as the record of
what was claimed before hardware existed, and because the reasoning about why
NOT to fit the cosim latencies is still the right reasoning.  Quote the
measured figure.

`stress-max-result` covers the other axis.  The envelope case maximises
storage but its result map is only 605 x 212; MAX_RESULT_W/H are 817/304,
sized for the smallest legal 4 x 4 template.  Until it was added nothing
wrote the top 212 accumulator entries or ran norm_cols past u = 604.

TWO cases exist purely to make the *score* worth transporting: before them
every score in cosim and hw was exactly 0.0 or 1.0 — 0x00000000 and
0x3F800000 — which between them exercise no sign bit and one mantissa bit,
on a value that crosses AXI4-Lite as raw IEEE-754 for software to
reinterpret.  `equality-negative` (-0.73) and `equality-different` (0.0096)
fix that, and they are the whole of it: every other score in
tb_tme_cases_{cosim,hw}.txt is still exactly 0.0 or 1.0.
"""

import sys
from pathlib import Path

import numpy as np
import cv2

# Contract §4.1 / tme_top.h limits — keep in sync.
MAX_PATCH_W, MAX_PATCH_H = 820, 307
MAX_TEMPL_W, MAX_TEMPL_H = 216, 96
MIN_TEMPL_DIM = 4
MAX_CASES = 64          # TB manifest bound

MIN_MARGIN = 0.02       # best-vs-runner-up separation for exact-loc asserts
SCORE_XCHECK = 2e-3     # oracle-1 vs cv2 agreement bound
N_SPOT = 12             # direct dot-product spot checks on the FFT STI

# Contract §3.1: one patch is one AXI DMA transfer, and the bring-up platform's
# DMA reports buffer_max_size = 262,143 (2^18 - 1, from an 18-bit
# c_sg_length_width).  This is a BLOCK-DESIGN parameter, invisible from any
# source file here — it is repeated in sw/tme_standalone_bringup.py, which
# checks it against the DMA's own reported value at run time rather than
# trusting this constant.
DMA_MAX_BYTES = 262143


def require(cond, msg: str = "") -> None:
    """`assert`, except it still exists under `python -O`.

    Every check in this file gates either an ORACLE (does the exact-integer
    map agree with cv2, is the FFT cross-correlation really the integer it was
    rounded to) or a WRITTEN ARTIFACT (is the stress case still the 820x307
    envelope, are the tags unique, is the manifest inside MAX_CASES).  An
    `assert` for that job is a check that disappears in exactly the mode this
    generator is also expected to run in — the manifests would still be
    written, unverified, and byte-compared against the verified ones as though
    the comparison meant something.

    The §4.6 flat-template rejection in `score_map` was already a raise for
    this reason, with a comment saying so; the remaining 26 checks were not,
    and are now.  Nothing here is a performance path — the FFT and the cv2
    cross-check dominate every one of these by orders of magnitude — so there
    was never anything to buy by letting them be optimised away.
    """
    if not cond:
        raise AssertionError(msg or "generator self-check failed")


def bin_noise(rng, h, w, density):
    """Binary image, THRESH_BINARY_INV world: ink = 255, background = 0."""
    return ((rng.random((h, w)) < density) * 255).astype(np.uint8)


def window_sums(a, th, tw):
    """Exact int64 sliding-window sums of `a` over th x tw windows."""
    h, w = a.shape
    ii = np.zeros((h + 1, w + 1), np.int64)
    ii[1:, 1:] = a.astype(np.int64).cumsum(0).cumsum(1)
    rh, rw = h - th + 1, w - tw + 1
    return (ii[th:th + rh, tw:tw + rw] - ii[0:rh, tw:tw + rw]
            - ii[th:th + rh, 0:rw] + ii[0:rh, 0:rw])


def score_map(patch, templ):
    """Oracle 1: exact integer sums, float64 normalisation.  Returns the
    (rh, rw) float64 map plus the peak window ΣI² (for stress asserts)."""
    ph, pw = patch.shape
    th, tw = templ.shape
    rh, rw = ph - th + 1, pw - tw + 1
    P = patch.astype(np.int64)
    T = templ.astype(np.int64)
    n = tw * th

    si = window_sums(patch, th, tw)
    sii = window_sums(P * P, th, tw)
    st = int(T.sum())
    stt = int((T * T).sum())
    dt = n * stt - st * st
    if dt <= 0:
        # Contract §4.6: a flat template is illegal input, not a scored case.
        # A ValueError rather than an assert, deliberately: under `python -O`
        # an assert vanishes and the generator would go on to divide by a zero
        # denominator, writing a manifest of NaNs — or of whatever cv2 happened
        # to return — that the TB would then treat as golden.
        raise ValueError(
            f"flat template ({tw}x{th}, all pixels = {int(T.flat[0])}): "
            f"dt = N·ΣT² − (ΣT)² = {dt}. The DUT returns 0 here; cv2 may "
            f"return ones or a patch-dependent numerical result, INCLUDING "
            f"zero, and no contractual agreement exists on this illegal "
            f"domain (its templNorm < DBL_EPSILON branch is not reached by "
            f"every flat template — a 7x7 of 2s computes 4.44e-16). A "
            f"coincidental 0 would not make the two agree. No golden can be "
            f"written for this input. Fix the builder (contract §4.6).")

    # Cross-correlation ΣTI via zero-padded FFT, rounded back to the exact
    # integer it represents.  Worst-case FFT noise here is ~1, far under the
    # 0.5 rounding radius; the spot checks below make that an assertion
    # rather than a hope.
    s0, s1 = ph + th, pw + tw
    f = np.fft.rfft2(P, s=(s0, s1)) * np.conj(np.fft.rfft2(T, s=(s0, s1)))
    sti = np.rint(np.fft.irfft2(f, s=(s0, s1))[:rh, :rw]).astype(np.int64)

    spot = np.random.default_rng(0xC0FFEE)
    for _ in range(N_SPOT):
        v = int(spot.integers(0, rh))
        u = int(spot.integers(0, rw))
        direct = int((P[v:v + th, u:u + tw] * T).sum())
        require(direct == sti[v, u], f"FFT STI off at ({u},{v}): {sti[v, u]} != {direct}")

    num = n * sti - st * si
    di = n * sii - si * si
    require((di >= 0).all())
    denom = np.sqrt(np.float64(dt) * di.astype(np.float64))
    with np.errstate(divide="ignore", invalid="ignore"):
        smap = np.where(di == 0, 0.0, num / np.where(denom == 0, 1.0, denom))
    return smap, int(sii.max())


def golden(patch, templ):
    """Peak of oracle 1, first-occurrence row-major (the tie-break both
    cv2.minMaxLoc and the DUT's strict > comparison implement), plus the
    margin over the runner-up.  Cross-checks oracle 2 (cv2)."""
    smap, sii_max = score_map(patch, templ)
    rh, rw = smap.shape
    flat_idx = int(np.argmax(smap))
    gy, gx = divmod(flat_idx, rw)
    score = float(smap[gy, gx])
    if smap.size > 1:
        rest = smap.copy()
        rest.flat[flat_idx] = -np.inf
        margin = score - float(rest.max())
    else:
        margin = 999.0

    ref = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
    require(ref.shape == (rh, rw),
            f"cv2 map {ref.shape} vs (pw-tw+1, ph-th+1)=({rh},{rw}) — §4.4 drift")
    require(abs(float(ref[gy, gx]) - score) <= SCORE_XCHECK,
            f"oracle disagreement at ({gx},{gy}): {score} vs cv2 {ref[gy, gx]}")
    if smap.size > 1 and margin >= 0.01:
        ry, rx = divmod(int(np.argmax(ref)), rw)
        require((rx, ry) == (gx, gy), f"cv2 argmax ({rx},{ry}) != exact argmax ({gx},{gy}) "
            f"despite margin {margin:.4f}")
    return score, gx, gy, margin, sii_max


def selftest_flat_template_rejected():
    """The §4.6 rejection must hold under `python -O`, where asserts vanish.

    Run from main() rather than a separate test file: this generator has no
    test harness of its own, and a rejection path nothing exercises is a
    rejection path that silently stops working.

    Scope, precisely: the flat cases below are rejected on the INTEGER `dt`,
    before cv2 is consulted at all, so they say nothing on their own about
    which side of `DBL_EPSILON` OpenCV puts them.  `selftest_opencv_epsilon()`
    covers that separately — asserting only the direction §4.6 actually depends
    on, and reporting the other.
    """
    # Both sides of §4.6's asymmetry.  4x4 is a power-of-two N, where cv2's
    # float64 templNorm is a clean 0 and its early return fires; 7x7 is not,
    # and 157 of its 256 possible flat fills compute templNorm >= DBL_EPSILON,
    # miss the branch, and get correlated like any other template.  The
    # rejection here must not care: it is decided on the exact integer dt, so
    # it fires identically for both — which is the whole reason the rejection
    # lives here rather than being delegated to cv2's epsilon test.
    for h, w, fill in ((4, 4, 0), (4, 4, 127), (4, 4, 255),
                       (7, 7, 2), (7, 7, 127), (7, 7, 255)):
        templ = np.full((h, w), fill, np.uint8)
        patch = np.arange(144, dtype=np.uint8).reshape(12, 12)
        try:
            score_map(patch, templ)
        except ValueError:
            continue
        raise RuntimeError(
            f"score_map accepted a flat {h}x{w} template (all {fill}) — §4.6 "
            f"says it must raise ValueError. Running under `python -O` with "
            f"the check written as `assert` is exactly how this regresses.")

    # A template one grey level from flat must still be accepted: dt = 15 at
    # N = 16, the global legal minimum (§4.6).  The rejection must be
    # `min == max`, not a threshold someone can drift upward.
    edge = np.full((4, 4), 127, np.uint8)
    edge[3, 3] = 128
    score_map(np.arange(16, dtype=np.uint8).reshape(4, 4) % 7, edge)
    n = 16
    st, stt = int(edge.sum()), int((edge.astype(np.int64) ** 2).sum())
    dt = n * stt - st * st
    if dt != 15:
        raise RuntimeError(f"minimum-nonflat template has dt = {dt}, expected 15")
    print("§4.6 self-test: flat templates rejected on integer dt, dt=15 "
          "template accepted (no cv2 involved)")


def use_generic_opencv() -> bool:
    """Turn IPP off and CONFIRM it is off.  Returns whether the confirm held.

    §4.6's roundoff bound is derived for OpenCV's generic C path — integer
    accumulation of ΣT and ΣT², one `binary64` scaling, `sqrt`, then the square
    in `common_matchTemplate`. A build with IPP enabled (this one reports
    `ippIP AVX2`) does not necessarily execute that code at all, so the bound
    would be a statement about source that never ran.

    Called both from `main()` — so the cv2 cross-check oracle is deterministic
    across machines with different IPP builds — and from the epsilon self-test.
    It cannot change any written golden: the manifests come from the exact
    integer/FFT oracle, and cv2 is only ever a cross-check.

    False covers two distinct states — IPP present and still on, and no `cv2.ipp`
    at all — and neither is "generic path confirmed", so both are treated the
    same way by `require_generic_opencv`.
    """
    try:
        cv2.ipp.setUseIPP(False)
        return not cv2.ipp.useIPP()
    except Exception:                       # no cv2.ipp in this build
        return False


def require_generic_opencv() -> None:
    """`use_generic_opencv()` or refuse to run.  Raises RuntimeError.

    Downgrading this to a warning was wrong.  The two dispatches do not merely
    round differently: on an all-127 7x7 against the §4.6 ramp patch, IPP
    returns an all-zero map and the generic path returns a 5.49e-08 peak, from
    identical inputs.  So on an IPP build the cv2 cross-check is not a weaker
    check of the same function, it is a check of a different one, and the
    epsilon numbers §4.6 quotes are not reproducible from the run that printed
    them.

    The written goldens would in fact be byte-identical either way — they come
    from oracle 1, exact integer arithmetic with no cv2 in the path.  Failing
    anyway is the point: the manifests are only worth what their cross-check is
    worth, and shipping them from a run whose independent oracle was silently a
    different function is how an unverified claim gets laundered into the
    contract.  A generator that cannot check its own output should produce
    none.

    Remedy, in order: this needs `cv2.ipp.setUseIPP(False)` to take, so first
    check the build actually exposes `cv2.ipp` (`_ipp_state()` prints
    `unavailable` if not); failing that, set `OPENCV_IPP=disabled` in the
    environment BEFORE cv2 is imported, which OpenCV honours at dispatch-table
    construction and cannot be overridden from Python afterwards.
    """
    if use_generic_opencv():
        return
    raise RuntimeError(
        f"refusing to generate: OpenCV's IPP dispatch could not be disabled "
        f"and confirmed off (IPP is {_ipp_state()}). The cv2 cross-check "
        f"oracle would then run a different function from the one §4.6's "
        f"roundoff bound describes — the two disagree on flat input, so this "
        f"is not a tolerance question. Set OPENCV_IPP=disabled in the "
        f"environment before importing cv2 and re-run.")


def selftest_opencv_epsilon():
    """Test §4.6's claim about OpenCV's `templNorm < DBL_EPSILON` branch.

    One direction is load-bearing and is ASSERTED: a legal non-flat template
    must land far above `DBL_EPSILON`, or the host's `min == max` rejection and
    OpenCV's epsilon test would be judging different sets of inputs.  §4.6
    bounds the roundoff at under 1e-10 against a legal floor of 4.822298e-05,
    so this is a proof being checked, not a hope — but the proof describes the
    GENERIC path, so IPP is disabled and the disable is verified FIRST, and the
    self-test refuses to run at all if that confirmation does not come.  There
    is no "measured, not proved" mode here any more: it produced a run that
    printed PASS beside numbers no proof covered.

    The other direction is NOT asserted, only reported: a mathematically flat
    template may or may not reach OpenCV's branch, that is float cancellation
    rather than contract, and nothing here may depend on it.  Printing the
    counts means a cv2 version bump shows up in the log instead of silently
    invalidating the numbers quoted in §4.6.
    """
    eps = sys.float_info.epsilon
    ipp_before = _ipp_state()
    require_generic_opencv()
    print(f"\n§4.6 OpenCV epsilon self-test (cv2 {cv2.__version__}, "
          f"DBL_EPSILON = {eps:.6e})")
    print(f"  IPP: {ipp_before} -> {_ipp_state()} "
          f"(generic path confirmed — the bound applies)")

    def ramp_scores(tag: str) -> None:
        """What a flat template actually scores, WITH the patch attached.

        The patch is the one §4.6 names: a 10x10 crop of a 16x16 ramp,
        `p[r][c] = 16r + c`.  A score quoted without its patch is meaningless —
        and the two rows below also show the dispatch mattering: an all-127 7x7
        gives an all-zero map under IPP and a 5.49e-08 peak on the generic path,
        from identical inputs.  That is the concrete reason the oracle pins the
        path instead of trusting whichever one the build dispatches to.
        """
        ramp = np.arange(256, dtype=np.uint8).reshape(16, 16)[:10, :10].copy()
        for fill in (2, 127):
            t = np.full((7, 7), fill, np.uint8)
            r = cv2.matchTemplate(ramp, t, cv2.TM_CCOEFF_NORMED)
            print(f"  [info] {tag} flat 7x7 all-{fill:<3d} vs the §4.6 ramp "
                  f"patch: peak {float(r.max()):.6e}, min {float(r.min()):.6e}")

    def check(tag: str, fatal: bool) -> int:
        """Minimum-nonflat templates: above epsilon, and inside the bound.

        `fatal` is not just "does a violation raise" — it is whether a bound
        covers these numbers at all.  Only the generic path has one, so only
        the generic path may print PASS.  An IPP row that happens to satisfy
        the same inequality is still an unbounded measurement and prints
        `[info]`, because a reader scanning for PASS/FAIL cannot otherwise tell
        which of the two dispatches produced the line.
        """
        bad = 0
        for tw, th in ((4, 4), (7, 7), (40, 30), (212, 87), (216, 96)):
            t = np.full((th, tw), 127, np.uint8)
            t[th - 1, tw - 1] = 128
            n = tw * th
            tn = float(cv2.meanStdDev(t)[1][0][0] ** 2)
            exact = (n - 1) / n ** 2
            err = abs(tn - exact)
            ok = tn > eps and err <= 1e-10
            if not ok and fatal:
                raise RuntimeError(
                    f"{tw}x{th} minimum-nonflat template on the generic path: "
                    f"cv2 templNorm {tn!r} vs exact {exact!r} (err {err:.3e}, "
                    f"above eps: {tn > eps}). §4.6's surviving direction rests "
                    f"on this; re-derive the bound before trusting it.")
            bad += not ok
            print(f"  [{('PASS' if ok else 'FAIL') if fatal else 'info'}] "
                  f"{tag} {tw:3d}x{th:<3d} non-flat: templNorm {tn:.6e} "
                  f"(exact {exact:.6e}, err {err:.1e}) — {tn / eps:.2e} x eps")
        return bad

    try:
        check("generic  ", fatal=True)

        # REPORTED ONLY: which flat templates reach the early return.  The
        # sizes are exactly the ones §4.6 quotes, so every number in the
        # contract is reproducible from this run.
        for tw, th in ((4, 4), (7, 7), (8, 8), (40, 30), (216, 96)):
            miss = [f for f in range(256)
                    if not float(cv2.meanStdDev(np.full((th, tw), f, np.uint8))[1][0][0] ** 2) < eps]
            print(f"  [info] {tw:3d}x{th:<3d} flat: {len(miss):3d}/256 fills "
                  f"miss the < DBL_EPSILON branch"
                  + (f" (e.g. {miss[:4]})" if miss else ""))
        print("  [info] the misses are correlated as ordinary templates and "
              "score patch-dependent values; §4.6 depends on none of it")

        ramp_scores("generic  ")

        # The IPP path is what a caller who does NOT disable it will execute.
        # Measured, never asserted — a bound was not derived for it, so every
        # line this produces is tagged [info] whatever the numbers come out to.
        try:
            cv2.ipp.setUseIPP(True)
            if cv2.ipp.useIPP():
                print("  [info] re-checking with IPP ENABLED — measured "
                      "only, outside the proof; all rows below are [info] "
                      "even where the generic bound would have been met:")
                check("IPP      ", fatal=False)
                ramp_scores("IPP      ")
        except Exception as exc:                       # noqa: BLE001
            print(f"  [info] could not re-enable IPP to measure it: {exc}")
    finally:
        # Leave the process on the generic path: main() wants it for the
        # cross-check oracle, and a self-test must not silently change it.
        # Best-effort here on purpose — raising out of a `finally` would
        # replace whatever real failure sent us here.  main() re-confirms
        # afterwards, which is where a failed restore has to be caught.
        use_generic_opencv()


def _ipp_state() -> str:
    try:
        return f"useIPP={cv2.ipp.useIPP()} ({cv2.ipp.getIppVersion()})"
    except Exception:                                  # noqa: BLE001
        return "unavailable"


def check_envelope(patch, templ):
    ph, pw = patch.shape
    th, tw = templ.shape
    require(MIN_TEMPL_DIM <= tw <= MAX_TEMPL_W, f"tw {tw}")
    require(MIN_TEMPL_DIM <= th <= MAX_TEMPL_H, f"th {th}")
    require(tw <= pw <= MAX_PATCH_W, f"pw {pw}")      # equality legal (§4.4 opt 1)
    require(th <= ph <= MAX_PATCH_H, f"ph {ph}")


def solve(tag, category, builder, base_seed, min_margin=MIN_MARGIN,
          max_tries=60):
    """Deterministic seed search: first seed whose case meets every
    constraint wins.  builder(rng) -> (patch, templ, expect_loc or None)."""
    for k in range(max_tries):
        rng = np.random.default_rng(base_seed * 1000 + k)
        patch, templ, expect = builder(rng)
        check_envelope(patch, templ)
        score, gx, gy, margin, sii_max = golden(patch, templ)
        if expect is not None and (gx, gy) != expect:
            continue
        if margin < min_margin:
            continue
        return dict(tag=tag, category=category, patch=patch, templ=templ,
                    score=score, x=gx, y=gy, margin=margin, sii_max=sii_max)
    raise RuntimeError(f"{tag}: no seed met constraints in {max_tries} tries")


# ---- Case builders -------------------------------------------------------

def planted(rng, pw, ph, tw, th, ux, uy, density=0.3):
    """Noise patch with the template cut from (ux, uy) — an exact match
    there, so the expected peak location is known by construction."""
    patch = bin_noise(rng, ph, pw, density)
    templ = patch[uy:uy + th, ux:ux + tw].copy()
    return patch, templ, (ux, uy)


def build_csim():
    cases = []

    # Blank patch: every window flat, every score exactly 0, peak (0,0) by
    # first-occurrence.  Continuity with the old sole golden — but now it is
    # one case among many instead of the entire suite.
    def blank(rng):
        patch = np.zeros((120, 200), np.uint8)
        templ = bin_noise(rng, 24, 40, 0.3)
        return patch, templ, (0, 0)
    c = solve("blank-patch", "degenerate", blank, 101, min_margin=0.0)
    require(c["score"] == 0.0)
    cases.append(c)

    # Flat and non-flat windows in one search: the di==0 short-circuit must
    # not outscore or displace the real peak.
    def half_blank(rng):
        patch = bin_noise(rng, 160, 240, 0.3)
        patch[:, :120] = 0
        return patch, patch[70:102, 150:198].copy(), (150, 70)
    cases.append(solve("half-blank-peak", "peak", half_blank, 102))

    cases.append(solve("peak-interior", "peak",
                       lambda r: planted(r, 300, 200, 48, 32, 137, 61), 103))

    # The §4.4 cases: peaks at the final column and/or row of the result
    # map.  A DUT still computing pw-tw instead of pw-tw+1 cannot reach
    # these locations and fails the exact-loc assert.
    cases.append(solve("peak-final-corner", "peak",
                       lambda r: planted(r, 260, 140, 36, 28, 224, 112), 104))
    cases.append(solve("peak-final-col", "peak",
                       lambda r: planted(r, 220, 150, 32, 24, 188, 63), 105))
    cases.append(solve("peak-final-row", "peak",
                       lambda r: planted(r, 220, 150, 32, 24, 95, 126), 106))

    # patch == template: exactly one search position (§4.4 option 1 makes
    # equality legal; the map is 1x1, not empty).
    def eq_identical(rng):
        patch = bin_noise(rng, 48, 64, 0.35)
        return patch, patch.copy(), (0, 0)
    c = solve("equality-identical", "equality", eq_identical, 107)
    require(c["score"] > 0.999)
    cases.append(c)

    # Same geometry, template unrelated to the patch: still one position,
    # whose (near-zero, possibly negative) score must be reported as-is.
    # Its 0.009578 is also the only non-round mantissa in the small cases, so
    # build_cosim lifts it.
    def eq_different(rng):
        patch = bin_noise(rng, 48, 64, 0.35)
        templ = bin_noise(rng, 48, 64, 0.35)
        return patch, templ, (0, 0)
    cases.append(solve("equality-different", "equality", eq_different, 108,
                       min_margin=0.0))

    # A winning score that is NEGATIVE.  Every other case in every suite
    # reports a best score of exactly 0.0, exactly 1.0, or a small positive —
    # so the sign bit of `result_score` was never exercised anywhere, and on
    # hardware it rides an AXI4-Lite register as raw IEEE-754 bits that
    # software reinterprets.  `anti-match` does not cover this: it puts -1.0
    # in the result *map* but its reported best is +0.12 elsewhere.
    #
    # A 1x1 result map is what forces the issue — with one position there is
    # nowhere better to go, so the anti-correlated score is the answer.  The
    # quarter-randomisation keeps it off exactly -1.0 (0xBF800000), which
    # would be as round a bit pattern as the 1.0 it replaces.
    def eq_negative(rng):
        patch = bin_noise(rng, 48, 64, 0.5)
        templ = (255 - patch).astype(np.uint8)
        keep = rng.random((48, 64)) < 0.25
        templ = np.where(keep, bin_noise(rng, 48, 64, 0.5), templ)
        return patch, templ.astype(np.uint8), (0, 0)
    c = solve("equality-negative", "equality", eq_negative, 116,
              min_margin=0.0)
    require(c["score"] < -0.2, f"equality-negative scored {c['score']:.4f} — not negative enough to "
        f"be a sign-bit test")
    require(abs(c["score"] + 1.0) > 0.01, "score landed on exactly -1.0")
    cases.append(c)

    # Equality in one dimension only: single-column / single-row maps.
    cases.append(solve("equality-width", "equality",
                       lambda r: planted(r, 120, 90, 120, 40, 0, 33), 109))
    cases.append(solve("equality-height", "equality",
                       lambda r: planted(r, 200, 80, 56, 80, 77, 0), 110))

    # Smallest legal template.  A 4x4 binary pattern recurs by chance, so
    # this leans on the seed search for uniqueness.
    cases.append(solve("min-templ-4x4", "edge",
                       lambda r: planted(r, 40, 30, 4, 4, 17, 9), 111))

    # Inverted slice: exact anti-correlation (-1.0) at the cut location.
    # The best score is a modest noise match elsewhere — exercises negative
    # scores and proves best-tracking is not fooled by |score|.
    def anti(rng):
        patch = bin_noise(rng, 150, 200, 0.3)
        templ = (255 - patch[40:72, 60:108]).astype(np.uint8)
        return patch, templ, None
    c = solve("anti-match", "edge", anti, 112)
    require(c["score"] < 0.9, "anti-match found a perfect match?")
    cases.append(c)

    # General uint8 robustness — the pipeline only ever sends 0/255, but the
    # arithmetic must not depend on that.
    def gray(rng):
        patch = rng.integers(0, 256, (160, 240), dtype=np.uint8)
        templ = patch[90:122, 150:198].copy()
        return patch, templ, (150, 90)
    cases.append(solve("grayscale-random", "robustness", gray, 113))

    # Maximum storage, near-maximum energies: full 820x307 patch, full
    # 216x96 template cut from the bottom-right corner — final row AND
    # column at the full envelope — with the template region densified so
    # the window ΣI² approaches its 1.348e9 ceiling.  This is the case that
    # wraps the previous ap_fixed accumulators.
    def stress_max(rng):
        patch = bin_noise(rng, MAX_PATCH_H, MAX_PATCH_W, 0.6)
        y0, x0 = MAX_PATCH_H - MAX_TEMPL_H, MAX_PATCH_W - MAX_TEMPL_W
        patch[y0:, x0:] = bin_noise(rng, MAX_TEMPL_H, MAX_TEMPL_W, 0.92)
        return patch, patch[y0:, x0:].copy(), (x0, y0)
    c = solve("stress-max-envelope", "stress", stress_max, 114)
    require(c["sii_max"] > 1.1e9,
            f"stress window ΣI² {c['sii_max']:.3e} too low to stress sumsq_t")
    require((c["x"], c["y"]) == (MAX_PATCH_W - MAX_TEMPL_W,
                                 MAX_PATCH_H - MAX_TEMPL_H))
    cases.append(c)

    # Same envelope, peak near the origin (band v=0 side of the search).
    cases.append(solve("stress-max-origin", "stress",
                       lambda r: planted(r, MAX_PATCH_W, MAX_PATCH_H,
                                         MAX_TEMPL_W, MAX_TEMPL_H, 3, 2,
                                         density=0.55), 115))

    # MAXIMUM RESULT MAP — a different stress axis from the two above.
    #
    # stress-max-envelope maximises *storage* (the 216x96 template fills
    # templ_buf) but its result map is only 605 x 212.  MAX_RESULT_W/H are
    # 817/304, sized for the smallest legal 4x4 template, and nothing reached
    # them: the top 212 entries of sti_col/sii_col/si_col were never written,
    # and isq_slide/norm_cols never ran past u = 604.  This case is the one
    # that fills those arrays to their declared bounds and puts the peak at
    # (816, 303) — the final column AND row of the largest possible map.
    #
    # Grayscale, not binary, and deliberately so: a 4x4 binary window recurs
    # by chance within a few hundred positions, and there are 248,368 of them
    # here, so a binary build could not have a unique peak to assert.  With
    # 8-bit pixels a 4x4 window is effectively unique.
    def stress_max_result(rng):
        patch = rng.integers(0, 256, (MAX_PATCH_H, MAX_PATCH_W), dtype=np.uint8)
        ux, uy = MAX_PATCH_W - MIN_TEMPL_DIM, MAX_PATCH_H - MIN_TEMPL_DIM
        templ = patch[uy:uy + MIN_TEMPL_DIM, ux:ux + MIN_TEMPL_DIM].copy()
        return patch, templ, (ux, uy)
    c = solve("stress-max-result", "stress", stress_max_result, 117)
    rw = MAX_PATCH_W - MIN_TEMPL_DIM + 1
    rh = MAX_PATCH_H - MIN_TEMPL_DIM + 1
    require((rw, rh) == (817, 304), f"result map {rw}x{rh}, expected 817x304")
    require((c["x"], c["y"]) == (rw - 1, rh - 1), f"peak at ({c['x']},{c['y']}), expected the final cell "
        f"({rw - 1},{rh - 1})")
    cases.append(c)

    # Randomised geometry sweep.
    rng_geo = np.random.default_rng(20260804)
    for i in range(6):
        tw = int(rng_geo.integers(8, MAX_TEMPL_W + 1))
        th = int(rng_geo.integers(8, MAX_TEMPL_H + 1))
        pw = int(rng_geo.integers(tw, min(tw + 300, MAX_PATCH_W) + 1))
        ph = int(rng_geo.integers(th, min(th + 150, MAX_PATCH_H) + 1))
        ux = int(rng_geo.integers(0, pw - tw + 1))
        uy = int(rng_geo.integers(0, ph - th + 1))
        dens = float(rng_geo.uniform(0.15, 0.6))
        cases.append(solve(
            f"random-{i:02d}-{pw}x{ph}-t{tw}x{th}", "random",
            lambda r, pw=pw, ph=ph, tw=tw, th=th, ux=ux, uy=uy, dens=dens:
                planted(r, pw, ph, tw, th, ux, uy, dens),
            200 + i))

    return cases


def build_phase_s():
    """Priority 3 suite: every case has a 96x64 RESULT MAP.

    Phase S crops the patch on the PS so each trial searches a 96x64 result
    area, i.e. pw = tw + 95 and ph = th + 63.  The core is UNCHANGED - pw/ph
    are already runtime arguments and the compiled 820x307 MAX_PATCH is only a
    bound - so this suite measures Phase-S cycles on the existing bitstream
    without any RTL work.

    Two stress axes the other suites carry disappear here, and that is a
    finding rather than an omission:

      * the RESULT MAP is constant at 96x64, so MAX_RESULT_W/H (817/304) are
        never approached.  `stress-max-result` has no Phase-S analogue.
      * the largest patch is 311x159 = 49,449 B against the §3.1 bound of
        262,143, so the single-DMA-transfer limit stops being tight.  Under
        Phase S no real trial comes within 5x of it.

    Geometry selection is from the 20,680-trial workload trace, not invented:
    `workload-mode` is the most common trial (3.6% of all trials),
    `workload-max` and `workload-wide` are the largest and the widest that
    actually occur, and `max` is the compiled bound, which sits above every
    real trial and is the case tme_cycle_model calls PHASE_S_GEOMETRY.
    """
    roi_w, roi_h = 96, 64
    cases = []

    def geom(tw, th):
        return tw + roi_w - 1, th + roi_h - 1

    # ORDER MATTERS AND IS LOAD-BEARING.  sw/tme_standalone_bringup.py re-runs
    # `cases[0]` after the whole suite to catch stale `static` BRAM residue, so
    # the suite must ASCEND: the smallest case first, the largest last.  With
    # the largest first the re-invocation re-runs the largest, which tests the
    # grow direction the sequence already covered and never tests the shrink
    # direction the check exists for -- and it silently adds that case's cycles
    # to the run a second time (23,476,737 of them here, taking a 77,949,151
    # suite to 101,425,888 actually executed).
    #
    # Smallest first: 99x67 patch, 4x4 template.  This is the case that gets
    # re-run after 311x159, so it is also the stale-residue probe.
    pw, ph = geom(MIN_TEMPL_DIM, MIN_TEMPL_DIM)
    cases.append(solve("phase-s-min-templ", "phase_s",
                       lambda r: planted(r, pw, ph, MIN_TEMPL_DIM, MIN_TEMPL_DIM,
                                         51, 33), 306))

    # Peak at the origin - one corner of the 96x64 map.
    pw, ph = geom(52, 31)
    cases.append(solve("phase-s-origin", "phase_s",
                       lambda r: planted(r, pw, ph, 52, 31, 0, 0), 302))

    # The most common real trial: male base 74x45 at scale 0.70.
    pw, ph = geom(52, 31)
    cases.append(solve("phase-s-workload-mode", "phase_s",
                       lambda r: planted(r, pw, ph, 52, 31, 44, 17), 303))

    # Widest: ferrule base 109x28 at 1.50 - tw is ~4x th, the opposite aspect
    # ratio from everything else, and the shape that drives the tw-linear
    # template-staging term.
    pw, ph = geom(164, 42)
    cases.append(solve("phase-s-workload-wide", "phase_s",
                       lambda r: planted(r, pw, ph, 164, 42, 70, 33), 305))

    # Peak in the final cell of the 96x64 map - the §4.4 +1 fix, re-asserted at
    # Phase-S geometry rather than assumed to carry over from the 820 envelope.
    pw, ph = geom(120, 94)
    cases.append(solve("phase-s-final-cell", "phase_s",
                       lambda r: planted(r, pw, ph, 120, 94,
                                         roi_w - 1, roi_h - 1), 301))

    # Largest geometry that actually occurs: female base 80x63 at 1.50.
    pw, ph = geom(120, 94)
    cases.append(solve("phase-s-workload-max", "phase_s",
                       lambda r: planted(r, pw, ph, 120, 94, 61, 29), 304))

    # LAST, and the largest: the compiled bound, 216x96 template in a 311x159
    # patch.  This is the case the model prices at 23,476,737 cycles = 0.187814 s
    # at 125 MHz, and it is what the board session times.
    pw, ph = geom(MAX_TEMPL_W, MAX_TEMPL_H)
    require((pw, ph) == (311, 159), f"Phase-S max geometry moved: {pw}x{ph}")
    cases.append(solve("phase-s-max", "phase_s",
                       lambda r: planted(r, pw, ph, MAX_TEMPL_W, MAX_TEMPL_H,
                                         37, 21, density=0.55), 300))

    # Every case must have exactly the 96x64 result map the suite is named for,
    # and must stay inside the compiled envelope and the DMA bound.
    for c in cases:
        ph_, pw_ = c["patch"].shape
        th_, tw_ = c["templ"].shape
        require((pw_ - tw_ + 1, ph_ - th_ + 1) == (roi_w, roi_h),
                f"{c['tag']}: result map {pw_ - tw_ + 1}x{ph_ - th_ + 1}, "
                f"not the {roi_w}x{roi_h} this suite exists to measure")
        require(pw_ <= MAX_PATCH_W and ph_ <= MAX_PATCH_H,
                f"{c['tag']}: patch {pw_}x{ph_} exceeds the compiled envelope")
        require(tw_ <= MAX_TEMPL_W and th_ <= MAX_TEMPL_H,
                f"{c['tag']}: template {tw_}x{th_} exceeds templ_buf")
        require(pw_ * ph_ <= DMA_MAX_BYTES, f"{c['tag']}: patch over §3.1")

    # The ascending order the re-invocation check depends on is an ASSERTION,
    # not a convention: a later edit that reorders these must fail here rather
    # than quietly turn the board's shrink-direction test back into a re-run of
    # the largest case.
    areas = [c["patch"].shape[0] * c["patch"].shape[1] for c in cases]
    require(areas[0] == min(areas),
            f"phase-s case 0 is {cases[0]['tag']} at {areas[0]:,} B but the "
            f"smallest is {min(areas):,} B — cases[0] is what the board re-runs "
            f"after the largest case, so it must BE the smallest")
    require(areas[-1] == max(areas),
            f"phase-s last case is {cases[-1]['tag']} but the largest is "
            f"{max(areas):,} B — the largest must run last for the "
            f"re-invocation to test the shrink direction")
    require(cases[-1]["tag"] == "phase-s-max",
            f"the board session times 'phase-s-max' as the final case; it is "
            f"currently '{cases[-1]['tag']}'")

    biggest = max(areas)
    print(f"\nphase_s suite: largest patch {biggest:,} B of the {DMA_MAX_BYTES:,} B "
          f"§3.1 bound ({100.0 * biggest / DMA_MAX_BYTES:.1f}%) — Phase S makes "
          f"that bound slack, it does not approach it")
    print(f"  order: {cases[0]['tag']} ({areas[0]:,} B) first — re-run after "
          f"{cases[-1]['tag']} ({areas[-1]:,} B) last")
    return cases


def pick(cases, tag):
    """Lift an already-solved case out of another suite, by tag.

    Taking the object rather than re-running the builder makes byte-identity
    structural: every suite carrying a given tag carries the same pixels, and
    an edit to one builder cannot silently desync them.
    """
    for c in cases:
        if c["tag"] == tag:
            return c
    raise RuntimeError(
        f"no case tagged '{tag}' to lift — if it was renamed, rename it here "
        f"too rather than dropping it from the suite that needs it")


def build_cosim(csim_cases):
    """Small enough to finish in RTL, broad enough to matter: equality,
    final-corner (§4.4), interior peak, all-flat, minimum template — plus two
    cases carrying scores that are neither 0.0 nor 1.0.

    That last part is not cosmetic.  Before they were added, every score in
    this suite (and in `hw`, which is built from it) was exactly 0.0
    (0x00000000) or exactly 1.0 (0x3F800000) — two bit patterns that between
    them exercise one mantissa bit and no sign bit.  `result_score` crosses
    AXI4-Lite as raw IEEE-754 bits that software reinterprets, so a suite made
    of round numbers is a weak test of that path.  Both additions are 64x48
    equality cases: one search position each, so they cost nothing in xsim.
    """
    cases = []

    def eq_identical(rng):
        patch = bin_noise(rng, 48, 64, 0.35)
        return patch, patch.copy(), (0, 0)
    c = solve("cosim-eq-identical", "equality", eq_identical, 301)
    require(c["score"] > 0.999)
    cases.append(c)

    cases.append(solve("cosim-final-corner", "peak",
                       lambda r: planted(r, 80, 56, 20, 14, 60, 42), 302))
    cases.append(solve("cosim-interior", "peak",
                       lambda r: planted(r, 64, 48, 16, 12, 23, 17), 303))

    def blank(rng):
        patch = np.zeros((48, 64), np.uint8)
        templ = bin_noise(rng, 12, 16, 0.3)
        return patch, templ, (0, 0)
    c = solve("cosim-blank", "degenerate", blank, 304, min_margin=0.0)
    require(c["score"] == 0.0)
    cases.append(c)

    cases.append(solve("cosim-min-4x4", "edge",
                       lambda r: planted(r, 40, 30, 4, 4, 17, 9), 305))

    # The two non-round scores, lifted from csim so all three suites run the
    # same pixels: a small positive (~0.0096) and a negative (~-0.5).
    cases.append(pick(csim_cases, "equality-different"))
    cases.append(pick(csim_cases, "equality-negative"))

    return cases


def build_hw(cosim_cases, csim_cases):
    """The silicon suite: every cosim case, plus the two 820x307 stress cases.

    Both stress cases are lifted from the already-solved csim list (see
    `pick`), and they stress different things:

      stress-max-envelope  216x96 template — maximum STORAGE, and the
                           251,740-byte single DMA transfer that is the only
                           reason this suite exists (contract §3.1).
      stress-max-result    4x4 template — maximum RESULT MAP, 817x304, which
                           is what MAX_RESULT_W/H are actually sized for.  The
                           envelope case only reaches 605x212, so without this
                           the top 212 accumulator entries are never touched
                           on hardware.

    `stress-max-result` is deliberately NOT in the cosim suite: at 820x307 it
    is far too slow for xsim, and like the envelope case it tests something
    (array bounds under a real DMA-fed patch) that RTL simulation of a design
    with no DMA cannot speak to.
    """
    stress = pick(csim_cases, "stress-max-envelope")
    cases = list(cosim_cases) + [stress, pick(csim_cases, "stress-max-result")]

    # §3.1 is the reason this suite exists, so assert both halves of it: the
    # big case must actually be the full envelope, and every case must fit one
    # transfer.  A patch over the bound would be truncated by the DMA and the
    # matcher would correlate against whatever the tail of its BRAM held.
    ph, pw = stress["patch"].shape
    require((pw, ph) == (MAX_PATCH_W, MAX_PATCH_H),
            f"stress case is {pw}x{ph}, not the {MAX_PATCH_W}x{MAX_PATCH_H} "
            f"envelope — it no longer exercises §3.1")
    require(pw * ph == 251740, f"envelope moved: {pw}x{ph} = {pw * ph} B")
    for c in cases:
        h, w = c["patch"].shape
        require(w * h <= DMA_MAX_BYTES, f"{c['tag']}: patch {w}x{h} = {w * h} B exceeds the §3.1 "
            f"single-transfer bound of {DMA_MAX_BYTES} B")
        th, tw = c["templ"].shape
        require(tw * th <= DMA_MAX_BYTES, f"{c['tag']}: template too large")

    print(f"\nhw suite §3.1 headroom: {DMA_MAX_BYTES - pw * ph:,} B "
          f"({pw * ph} of {DMA_MAX_BYTES} used by stress-max-envelope)")
    return cases


# ---- Emission ------------------------------------------------------------

def write_suite(cases, name, out=Path(".")):
    require(len(cases) <= MAX_CASES)
    tags = [c["tag"] for c in cases]
    require(len(set(tags)) == len(tags), "duplicate tag")

    patches = bytearray()
    templs = bytearray()
    rows = []
    for i, c in enumerate(cases):
        ph, pw = c["patch"].shape
        th, tw = c["templ"].shape
        rows.append(
            f"{i} {pw} {ph} {tw} {th} {len(patches)} {len(templs)} "
            f"{c['score']:.6f} {c['x']} {c['y']} {min(c['margin'], 999.0):.6f} "
            f"{c['category']} {c['tag']}")
        patches += c["patch"].tobytes()
        templs += c["templ"].tobytes()

    header = f"{len(cases)} {len(patches)} {len(templs)}"
    (out / f"tb_tme_cases_{name}.txt").write_text(
        "\n".join([header] + rows) + "\n")
    (out / f"tb_tme_patches_{name}.bin").write_bytes(bytes(patches))
    (out / f"tb_tme_templs_{name}.bin").write_bytes(bytes(templs))

    print(f"\n{name}: {len(cases)} cases, patches {len(patches)} B, "
          f"templates {len(templs)} B")
    for r in rows:
        print("  " + r)


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Generate tme_tb golden suites.")
    ap.add_argument("--only", choices=("csim", "cosim", "hw", "phase_s"),
                    help="write just this suite and leave the others on disk "
                         "untouched.  Use --only phase_s to add the Phase-S "
                         "suite without rewriting csim/cosim/hw, whose files "
                         "are hash-bound gate evidence (GATE4/GATE5 .sha256).")
    args = ap.parse_args()

    # The cv2 cross-check oracle runs on the generic path, so it is the same
    # arithmetic on every machine and the §4.6 bound describes what actually
    # executed.  Cannot affect a written golden (those come from oracle 1) —
    # see require_generic_opencv() for why it is fatal regardless.
    require_generic_opencv()
    selftest_flat_template_rejected()
    selftest_opencv_epsilon()
    # The epsilon self-test deliberately turns IPP back on to measure it, and
    # restores the generic path in a `finally` that cannot raise.  Everything
    # below cross-checks against cv2, so confirm the restore actually took
    # before a single case is built.
    require_generic_opencv()

    if args.only == "phase_s":
        write_suite(build_phase_s(), "phase_s")
        print("\nOK — phase_s suite written; csim/cosim/hw untouched.  Send it "
              "with sw/tme_standalone_bringup.py --suite phase_s (validate "
              "first with run_hls.tcl's csim_design -argv \"phase_s\")")
        return

    csim = build_csim()
    if args.only == "csim":
        write_suite(csim, "csim")
        return
    cosim = build_cosim(csim)
    if args.only == "cosim":
        write_suite(cosim, "cosim")
        return
    if args.only == "hw":
        write_suite(build_hw(cosim, csim), "hw")
        return

    write_suite(csim, "csim")
    write_suite(cosim, "cosim")
    write_suite(build_hw(cosim, csim), "hw")
    print("\nOK — csim/cosim via run_hls.tcl; the hw suite goes to the board "
          "with sw/tme_standalone_bringup.py (validate it first with "
          "run_hls.tcl's csim_design -argv \"hw\")")


if __name__ == "__main__":
    main()
