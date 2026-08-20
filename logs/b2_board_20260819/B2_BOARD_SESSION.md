# B2 board session — 2026-08-20T05:52–05:53Z

Bounded session under the explicit authorization of 2026-08-19 ("GO. I
authorize the exact standalone B2 board gate at commit `729582e`"), which
covered exactly: upload into a new `tme_b2/`, load the pinned B2 bit/HWH, run
`phase_s` and `hw` at a gated 125 MHz, the re-invocation and DMA-halt checks,
and restoration of `base.bit` and all captured clocks. Nothing else was done:
no shipping-directory change, no combined-image work, no push, no page-level
execution.

Board `pynq` (armv7l, kernel `6.6.10-xilinx-v2024.1-g916a1f7c7222`), reached
over the Jupyter API. Overlay **`TermCountB2:hls:tme_top:0.2`** — the B2 core,
routed at 8.000 ns with WNS +0.011710 ns.

**Result: the gate passed.** `phase_s` 7/7 and `hw` 9/9, both within
`|score - gold| <= 0.005` and at exact `(x, y)`, at a gated 125.0000 MHz, with
a verified re-invocation after each suite's largest case, a proved DMA halt,
and `RESTORE_VERIFIED`. The wrapper exited zero.

Retained transcripts, in order:

    00_prestate.txt        pre-state, read-only, and prestate_fclks.json
    01_hashes_local.txt    host-side hashes, committed before the session
    02_hashes_remote.txt   board-side control hashes, written before PL config
    03_run.txt             gate: hashes + checksums + both suites + restore
    04_restore.txt         base.bit and all four captured clocks, verified

---

## There were two attempts, and the first one's transcript is incomplete

This comes before the results, because a reader who finds
`03_run_attempt1_TRUNCATED.txt` in this directory is owed the reason.

**Attempt 1 (05:46:18Z) was not a failed gate. It was a failed capture.** The
board-side run was driven correctly and its own artifacts are intact, but the
host-side command piped `tee` into `head -120`; `head` exited first, `tee` took
`SIGPIPE`, and the local transcript was cut at exactly 10,240 bytes — a clean
10 KiB buffer boundary. What survived shows every gate passing, `phase_s` 7/7
with its re-invocation, and `hw` cases `[0]`–`[5]` all `PASS`. What was lost is
`hw` cases `[6]`–`[8]`, the `hw` tally, its re-invocation, the DMA-halt result
and the wrapper's exit status.

**The board state could not settle it.** `04_restore.sh` runs from an `EXIT`
trap whether the suites pass or fail — that is the fail-closed behaviour this
protocol was revised to have — so a verified restoration is consistent with
either outcome. Nothing board-side records the suite tallies. The outcome of
attempt 1's `hw` suite is therefore **unknown and is not claimed here.**

**Attempt 2 was authorized before it was run.** The board was confirmed back at
its captured pre-state, attempt 1's four artifacts were preserved under
`*_attempt1*` names rather than overwritten, and the gate was re-run with a
non-truncating capture. Attempt 1's files are retained precisely so that this
section can be checked rather than believed:

| attempt 1 artifact | bytes | what it shows |
|---|---|---|
| `00_prestate_attempt1.txt` | 1,594 | pre-state, clocks 100.0 / 142.857143 / 200.0 / 100.0 |
| `02_hashes_remote_attempt1.txt` | 326 | the four control hashes, identical to attempt 2's |
| `03_run_attempt1_TRUNCATED.txt` | 10,240 | truncated mid-line in `hw` case `[6]` |
| `04_restore_attempt1.txt` | 868 | `RESTORE_VERIFIED`, all four clocks matched |

The honest summary of attempt 1 is: **gates passed, `phase_s` passed, `hw`
incomplete in the record, board restored.** It is not evidence that `hw` passed,
and the result below rests on attempt 2 alone.

---

## Acceptance criteria — all seven met, on attempt 2

| # | criterion | result |
|---|---|---|
| 1 | remote control hashes match the committed host record | **4/4 identical** to `01_hashes_local.txt`, and identical to attempt 1's |
| 2 | checksum gate | **`CHECKSUM_GATE_PASS 10/10`** — the manifest first verified against the digest embedded in `03_run.sh`, then nine suite inputs plus `04_restore.sh`, all before either `Overlay()` |
| 3 | FCLK gate | **`FCLK0 gate: PASS`, 125.0000 MHz**, on both invocations |
| 4 | HWH-VLNV gate | **PASS, `TermCountB2:hls:tme_top:0.2`**, on both invocations |
| 5 | both suites and re-invocations | **`phase_s` 7/7, `hw` 9/9**, `re-invocation check: PASS` twice, `BOTH_SUITES_PASS` |
| 6 | DMA halt verified | no halt warning in the transcript; the runner's exit status returns 0 only if the halt read back, and `set -e` would have stopped the wrapper otherwise |
| 7 | `RESTORE_VERIFIED` and wrapper exits zero | **`RESTORE_VERIFIED`**, all four clocks exact, then **`B2_GATE_PASS`** and `__EXIT__ 0` |

Pre-state was captured at 05:52:09Z and restored at 05:53:16Z: `fclk0` 100.0,
`fclk1` 142.857143, `fclk2` 200.0, `fclk3` 100.0 — restored from the captured
JSON rather than from hardcoded defaults. The four shipping directories
(`tme_b1`, `tme_phase_s`, `tme_probe125`, `tme_test`) kept their mtimes.

---

## The measurement

### `phase_s` — the paired B1/B2 comparison at `T = 6`

Same three vector files and the same gated clock as the B1 session; only the
bitstream differs. Predictions are from `B2_BOARD_SESSION_PLAN.md`, committed
before the run.

| case | patch / templ | B1 measured | **B2 predicted** | **B2 measured** | delta |
|---|---|---|---|---|---|
| phase-s-min-templ | 99×67 / 4×4 | 0.004 s | 0.004 s | **0.004 s** | 0 |
| phase-s-origin | 147×94 / 52×31 | 0.024 s | 0.020 s | **0.020 s** | 0 |
| phase-s-workload-mode | 147×94 / 52×31 | 0.024 s | 0.020 s | **0.020 s** | 0 |
| phase-s-workload-wide | 259×105 / 164×42 | 0.068 s | 0.050 s | **0.050 s** | 0 |
| phase-s-final-cell | 215×157 / 120×94 | 0.117 s | 0.088 s | **0.088 s** | 0 |
| phase-s-workload-max | 215×157 / 120×94 | 0.117 s | 0.088 s | **0.088 s** | 0 |
| phase-s-max | 311×159 / 216×96 | 0.190 s | 0.137 s | **0.138 s** | +1 ms |
| **total** | | **0.544 s** | 0.407–0.414 s | **0.408 s** | — |

Six of seven cases land exactly on the prediction at the millisecond print
floor; one is 1 ms high, which is the floor itself. The total, **0.408 s**, is
inside the predicted 0.407–0.414 s band. Against B1's measured 0.544 s that is
a **0.136 s saving**, versus the 0.133755 s pure-core saving the cycle model
projects — the difference being overhead that does not scale.

Attempt 1's `phase_s` totalled 0.406 s, 1 ms below the band's lower edge and
well inside the ±3.5 ms its own per-case rounding carries. Both runs are
reported; neither is discarded.

### `hw` — function at tile counts the co-simulation never reached

This is what attempt 1 could not establish, and what makes the session more
than a repeat of B1's.

| case | patch / templ | `T` | measured | B2 model | residual |
|---|---|---|---|---|---|
| cosim-eq-identical | 64×48 / 64×48 | 1 | 0.003 s | — | — |
| cosim-final-corner | 80×56 / 20×14 | 4 | 0.005 s | — | — |
| cosim-interior | 64×48 / 16×12 | 4 | 0.004 s | — | — |
| cosim-blank | 64×48 / 16×12 | 4 | 0.004 s | — | — |
| cosim-min-4x4 | 40×30 / 4×4 | 3 | 0.003 s | — | — |
| equality-different | 64×48 / 64×48 | 1 | 0.002 s | — | — |
| equality-negative | 64×48 / 64×48 | 1 | 0.003 s | — | — |
| **stress-max-envelope** | 820×307 / 216×96 | **38** | **2.059 s** | 2.0572 s | **+1.8 ms** |
| **stress-max-result** | 820×307 / 4×4 | **52** | **0.063 s** | 0.0608 s | **+2.2 ms** |

`stress-max-envelope` also moved **251,740 B in one transfer**, 10,403 B under
the §3.1 bound, so the maximum DMA geometry is exercised. The re-invocation
re-ran `cosim-eq-identical` (3,072 B) after it — the shrink direction.

**Why the two stress rows matter more than the other seven.** B2 is an indexing
change whose entire behaviour is "what tile `t` inherits from tile `t-1`". The
co-simulation pinned its term exactly but only to `T = 6`; `csim_prod_b2.tcl`
reached `T = 52` in **C simulation of the source**, not the RTL. These two rows
are the RTL itself at `T = 38` and `T = 52`, the compiled maximum. A 2.059 s
measurement against a 2.0572 s model is **0.09%**.

**Read every residual as overhead, not error.** All nine are positive, between
+1.6 and +2.5 ms, with no trend against case size — fixed per-invocation DMA
setup, marshalling and polling that the cycle model does not describe. A
negative residual would mean silicon beat the term and would falsify it;
`tme_cycle_model.check()` now fails on one.

### Against the unmodified core, same suite and clock

The `cur` rows frozen from the 2026-08-17 probe ran this same `hw` suite at the
same gated 125 MHz, so this is a paired comparison with only the bitstream
changed:

| case | `cur` | B2 | speed-up |
|---|---|---|---|
| stress-max-envelope | 3.342 s | 2.059 s | 1.623× |
| stress-max-result | 0.171 s | 0.063 s | 2.714× |

---

## What this establishes, and what it does not

**It establishes:**

* The B2 RTL runs on silicon at an observed, gated 125.0000 MHz — the
  231-element shift register that routed with only 0.011710 ns of slack does
  close on this part in practice, not only in the report.
* It is functionally correct on both pinned suites, at every tile count from
  `T = 1` to `T = 52`, and at the maximum single-transfer geometry.
* It is re-invocable after each suite's largest case, so the `static` BRAMs and
  column accumulators are not carrying residue across invocations.
* The measured cycle term tracks silicon to within a constant few milliseconds
  of per-invocation overhead, across three orders of magnitude of wall time.

**It does not establish:**

* **Any page time.** 20.405 s/page sums the term over 20,680 modelled trials.
  No page has been run, on any hardware, at any clock.
* **Score equality.** The criterion is `|score - gold| <= 0.005` **and** exact
  `(x, y)`. The location is exact; the score is held to a tolerance. Write
  "7/7 and 9/9 within 0.005 and exact `(x, y)`" — never "N/N exact score",
  even though every case printed its gold value at four decimals.
* **Fabric readback.** `Overlay.ip_dict` is parsed from the HWH sidecar. The
  VLNV gate proves PYNQ associated the expected HWH identity with the load; it
  is **not** a read of what the fabric holds. Identity rests on the exact
  bit/HWH hashes plus the retained packaged-IP → Vivado → bitstream provenance.
* **Timing margin.** WNS comes from the routed report. A run that passes says
  the part met the constraint on this die at this temperature; it is not a
  margin measurement, and 0.011710 ns is 0.15% of the period.
* **Anything about the combined image**, which remains unproved at 125 MHz.
