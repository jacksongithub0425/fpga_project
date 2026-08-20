# B2 board session — fail-closed plan and retained pre-run predictions

**Status: NOT RUN.** This directory contains only the prepared protocol,
checksum manifest, and host-side hash record. There is no `03_run.txt`, no
`02_hashes_remote.txt` and no `04_restore.txt`, and B2 has never been on
silicon.

**This file is committed before the session deliberately.** Priority 5's
evidence had to withdraw a "registered before the build" claim because the
retained timestamps contradicted it (see `PRIORITY5_EVIDENCE.md`, change note
item 3). Commit ancestry can record that this plan precedes a later board-result
commit. It is not an external timestamp: `c8aa9c1` is unsigned and, at the time
of preparation, unpushed, so its date alone does not prove when a third party
could first observe it.

---

## The predictions

Per-case cycle counts from `tme_cycle_model.cycles(..., "B1"/"B2")`, and B1's
**measured** board wall times from `logs/b1_board_20260818/03_run.txt`:

| case | patch | templ | T | B1 cycles | B2 cycles | Δ | B2/B1 | B1 measured | **B2 predicted** |
|---|---|---|---|---|---|---|---|---|---|
| phase-s-min-templ | 99×67 | 4×4 | 6 | 204,481 | 203,201 | −1,280 | 0.994 | 0.004 s | 0.004 s |
| phase-s-origin | 147×94 | 52×31 | 6 | 2,725,330 | 2,239,250 | −486,080 | 0.822 | 0.024 s | 0.020 s |
| phase-s-workload-mode | 147×94 | 52×31 | 6 | 2,725,330 | 2,239,250 | −486,080 | 0.822 | 0.024 s | 0.020 s |
| phase-s-workload-wide | 259×105 | 164×42 | 6 | 8,203,539 | 6,039,699 | −2,163,840 | 0.736 | 0.068 s | 0.050 s |
| phase-s-final-cell | 215×157 | 120×94 | 6 | 14,316,723 | 10,797,363 | −3,519,360 | 0.754 | 0.117 s | 0.088 s |
| phase-s-workload-max | 215×157 | 120×94 | 6 | 14,316,723 | 10,797,363 | −3,519,360 | 0.754 | 0.117 s | 0.088 s |
| **phase-s-max** | 311×159 | 216×96 | 6 | 23,482,881 | **16,939,521** | −6,543,360 | 0.721 | 0.190 s | **0.137 s** |
| **total** | | | | **65,975,007** | **49,255,647** | **−16,719,360** | **0.747** | **0.544 s** | — |

Pure-core time at 125 MHz: B1 **0.527800 s**, B2 **0.394045 s**, saving
**0.133755 s**. B1's measured wall total was 0.544 s, so non-core overhead
(DMA setup, PS-side marshalling) is **at least 0.016 s** — about 3%. The `B2
predicted` column scales B1's measured wall time by the cycle ratio, which
*assumes that overhead scales too*; it does not, so those figures are a
**lower bound on the wall time**, i.e. an upper bound on the speed-up. Accounting
for the per-case millisecond print floor and the fact that non-core overhead
does not scale with the cycle ratio, treat the seven-case prediction as roughly
**0.407–0.414 s, excluding re-invocation**. Including the small-case rerun gives
roughly **0.414 s**. These are estimates, against B1's measured 0.544 s.

**What would falsify the cycle model.** The cycle counts above are not
adjustable. If the measured wall times come in materially above these — beyond
what a constant 0.016 s of overhead explains — then either the term is wrong or
the clock is not what the gate says.

---

## What this session can and cannot establish

**Every phase_s case has T = 6.** The suite's result map is fixed at 96×64, so
`rw = 96` and `T = ceil(96/16) = 6` for all seven cases. It supplies the paired
B1/B2 timing comparison on unchanged stimulus.

* It **can** establish: that the B2 RTL runs on silicon at an observed 125 MHz,
  produces the correct `(x, y)` and tolerance-bounded score on both pinned
  suites, is re-invocable after each suite's largest case, and that phase_s wall
  time tracks the measured cycle term.
* The `hw` suite adds silicon function checks at larger tile counts: the
  820×307 / 216×96 case has `rw = 605`, approximately `T = 38`; the 820×307 /
  4×4 case reaches `rw = 817`, `T = 52`; and the former moves the maximum
  251,740-byte DMA geometry. Without a passing `hw` suite, the result must be
  reported only as **Phase-S-only silicon validation**.

---

## Procedure — fail-closed requirements, and where each is enforced

| # | requirement | enforced by |
|---|---|---|
| 1 | reuse the hash-bound Phase-S suite | `--suite phase_s`; all three vector files byte-identical to the Priority 3 **and** B1 sessions (`01_hashes_local.txt`) |
| 2 | observed FCLK0 = 125 MHz | `--expect-fclk-mhz 125 --fclk-tol-mhz 0.01`, **fail-closed**: an unreadable clock aborts too |
| 3 | re-invocation | built into the runner — a smaller case is re-run after the largest, catching stale BRAM; its result gates the exit status |
| 4 | DMA halt | teardown drives RS=0 and requires a **positive register read-back** within 0.5 s per channel; an unproved halt holds the buffer and fails the run |
| 5 | artifact hashes | `03_run.sh` first checks the checksum manifest against the digest embedded in the committed wrapper, then runs `sha256sum -c B2_BOARD_INPUTS.sha256` over all nine loaded inputs before either `Overlay()` call; the original six-file gate is preserved and the three `hw` vectors extend it |
| 6 | HWH consistency | both runner calls require `TermCountB2:hls:tme_top:0.2`; this gates the VLNV parsed from the HWH metadata and is explicitly **not fabric readback** |
| 7 | broad silicon geometry | `--suite hw` covers approximately `T=38`, maximum `T=52`, and the 251,740-byte DMA geometry |
| 8 | verified restoration | `00_prestate.sh` persists the four measured clocks; an exit trap runs `04_restore.sh` after either suite succeeds or fails, and restore mismatch/failure exits nonzero |

### Steps

    0. sh 00_prestate.sh       -> 00_prestate.txt + prestate_fclks.json
    1. transfer the nine inputs and the prepared scripts/manifest into tme_b2/
    2. optional diagnostic hash listing -> 02_hashes_remote.txt
    3. sh 03_run.sh            -> 03_run.txt
         - sha256sum -c over the pinned checksum manifest, then all nine inputs,
           before overlay load
         - phase_s at expected 125 MHz and expected HWH VLNV
         - hw at expected 125 MHz and expected HWH VLNV
         - EXIT trap invokes 04_restore.sh even if either suite fails
    4. the trap writes 04_restore.txt and echoes it into 03_run.txt; the
       restore log must contain RESTORE_VERIFIED and the wrapper must exit zero

**Abort conditions.** Any of these voids the run: any of the nine checksums
fails; FCLK0 is unreadable, non-finite, or outside 125 ± 0.01 MHz; the HWH IP
dictionary reports a VLNV other than `TermCountB2:hls:tme_top:0.2`; either
suite fails; a DMA channel cannot be proved halted; or restoration does not
print `RESTORE_VERIFIED` and exit zero.

**Identity is established by the exact bit/HWH hashes plus retained build
provenance.** `Overlay.ip_dict` is parsed from HWH metadata, not read back from
fabric. Requiring the expected VLNV proves that PYNQ associated the expected
HWH identity with the load; it does not independently prove fabric contents.

**Do not touch** `/home/xilinx/jupyter_notebooks/tme_*` — those are shipping
artifacts. The session writes only to `tme_b2/`.

---

## PASS criterion

Per case: `|score − gold| <= 0.005` **and** exact `(x, y)`. Both, on all seven
`phase_s` cases and all nine `hw` cases, plus both re-invocations.

This is the criterion the B1 session was held to, and the wording matters:
**do not write "N/N exact score"** — the scores are compared to a tolerance and
only the coordinates are exact. Report each suite separately: "7/7" for
`phase_s` and "9/9" for `hw`, each within 0.005 with exact `(x, y)`.

The wall times are a **corroboration** of the cycle term, not the pass
criterion. A run that passes 7/7 but comes in at B1's wall times would mean the
core is correct and the model is wrong, and both halves have to be reported.
