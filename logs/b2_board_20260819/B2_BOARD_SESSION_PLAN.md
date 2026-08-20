# B2 board session — plan, and predictions registered BEFORE the run

**Status: NOT RUN.** Nothing in this directory except this plan, the two
scripts and the host-side hash record exists yet. There is no `03_run.txt`,
no `02_hashes_remote.txt` and no `04_restore.txt`, and B2 has never been on
silicon.

**This file is committed before the session deliberately.** Priority 5's
evidence had to withdraw a "registered before the build" claim because the
retained timestamps contradicted it (see `PRIORITY5_EVIDENCE.md`, change note
item 3). The fix is not to write a better sentence afterwards; it is to put the
prediction in git first, where the commit date is not something a later edit
can manufacture. That is what this file is for.

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
**lower bound on the wall time**, i.e. an upper bound on the speed-up. The
honest total prediction is **≈ 0.394 s of core plus ≈ 0.016 s of overhead
≈ 0.410 s**, against B1's 0.544 s.

**What would falsify the cycle model.** The cycle counts above are not
adjustable. If the measured wall times come in materially above these — beyond
what a constant 0.016 s of overhead explains — then either the term is wrong or
the clock is not what the gate says.

---

## What this session can and cannot establish

**Every phase_s case has T = 6.** The suite's result map is fixed at 96×64, so
`rw = 96` and `T = ceil(96/16) = 6` for all seven cases. This session therefore
exercises **one tile count**, the same one the co-simulation already covered.

* It **can** establish: that the B2 RTL runs on silicon at an observed 125 MHz,
  produces the correct `(x, y)` and score on the pinned suite, is re-invocable
  after the largest case, and that the measured wall time tracks the measured
  cycle term.
* It **cannot** establish anything about `T` between 7 and 52. The only
  evidence there is `csim_prod_b2.tcl` — C simulation of the source, not the
  RTL. **That gap survives this session** and should not be quietly closed by
  a green board result.

---

## Procedure — six requirements, and where each is enforced

| # | requirement | enforced by |
|---|---|---|
| 1 | reuse the hash-bound Phase-S suite | `--suite phase_s`; all three vector files byte-identical to the Priority 3 **and** B1 sessions (`01_hashes_local.txt`) |
| 2 | observed FCLK0 = 125 MHz | `--expect-fclk-mhz 125 --fclk-tol-mhz 0.01`, **fail-closed**: an unreadable clock aborts too |
| 3 | re-invocation | built into the runner — a smaller case is re-run after the largest, catching stale BRAM; its result gates the exit status |
| 4 | DMA halt | teardown drives RS=0 and requires a **positive register read-back** within 0.5 s per channel; an unproved halt holds the buffer and fails the run |
| 5 | artifact hashes | host-side in `01_hashes_local.txt`, board-side in `02_hashes_remote.txt`, and **re-hashed again inside** `03_run.txt` so the transcript is self-contained |
| 6 | verified restoration | `04_restore.sh` reloads `base.bit`, rewrites all four FCLKs and **re-reads them**, printing `RESTORE_VERIFIED` only on an exact match to the pre-state |

### Steps

    0. 00_prestate.sh          -> 00_prestate.txt   (clocks, CMA, shipping dirs)
    1. (host) 01_hashes_local.txt                    already generated
    2. transfer; (board) sha256sum -> 02_hashes_remote.txt
    3. runner                  -> 03_run.txt
         python3 tme_standalone_bringup.py \
           --overlay tme_standalone.bit \
           --suite phase_s \
           --expect-fclk-mhz 125 --fclk-tol-mhz 0.01
    4. 04_restore.sh           -> 04_restore.txt    (must print RESTORE_VERIFIED)

**Abort conditions.** Any of these voids the run: the FCLK0 gate fails; the
overlay's IP dictionary reports a VLNV other than
`TermCountB2:hls:tme_top:0.2`; a host-side and board-side hash disagree; a DMA
channel cannot be proved halted; `04_restore.txt` does not print
`RESTORE_VERIFIED`.

**The core identity check is not the bitstream hash alone.** The hash proves
which bytes were sent. The VLNV the runner reads back out of the loaded overlay
is what proves the fabric holds the B2 core — B1's transcript says
`TermCountB1`, and this one must say `TermCountB2`.

**Do not touch** `/home/xilinx/jupyter_notebooks/tme_*` — those are shipping
artifacts. The session writes only to `tme_b2/`.

---

## PASS criterion

Per case: `|score − gold| <= 0.005` **and** exact `(x, y)`. Both, on all seven.

This is the criterion the B1 session was held to, and the wording matters:
**do not write "N/N exact score"** — the scores are compared to a tolerance and
only the coordinates are exact. Write "7/7 within 0.005 and exact (x, y)".

The wall times are a **corroboration** of the cycle term, not the pass
criterion. A run that passes 7/7 but comes in at B1's wall times would mean the
core is correct and the model is wrong, and both halves have to be reported.
