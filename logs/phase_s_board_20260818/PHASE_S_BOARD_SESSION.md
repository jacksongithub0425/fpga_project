# Phase-S board session — 2026-08-19T02:00–02:05Z

Bounded session under the conditional authorization of 2026-08-18. Board `pynq`
(hostname-resolved to 10.0.0.227 — the DHCP address moved again, which is why
the hostname is always used). Unchanged RTL, `TermCount:hls:tme_top:0.2`, the
same 125 MHz probe overlay Priority 2 routed at 8.000 ns / WNS +0.063836 ns.

Retained transcripts, in order:

    00_prestate.txt      pre-state, read-only
    01_hashes_local.txt  host-side hashes, taken immediately before the load
    02_hashes_remote.txt board-side hashes of the same six files
    03_run.txt           configure + 7 cases + re-invocation
    04_restore.txt       base.bit + clocks restored and verified

## Acceptance criteria — all met

| requirement | result |
|---|---|
| new board directory, shipping artifacts untouched | `tme_phase_s/` created this session. `tme_probe125` (Aug 18 04:51) and `tme_test` (Aug 9 21:40) kept their mtimes across the session |
| hash bit, HWH, runner, manifest, vectors immediately before loading, in the retained transcript | all six, host **and** board, in `01_`/`02_`, all matching |
| pre-state recorded | `00_prestate.txt` — fresh boot (up 9 min), fclk 100.0 / 142.857143 / 200.0 / 100.0 |
| observed FCLK0 = 125.0 MHz | **125.0000 MHz**, and the new fail-closed gate reports `FCLK0 gate: PASS` |
| 7/7 cases | **7/7 PASS** — `abs(score - gold) <= 0.005` **and** exact (x, y) on every case. The location is exact; the score is checked against a tolerance, not for equality, and the transcript prints four decimals. Score equality is not established by this session and is not claimed. |
| re-invocation PASS | **PASS** — `phase-s-min-templ` re-run after the 49,449 B largest case |
| verified DMA halt | runner exit 0. `_exit_status(cases_ok, halt_ok)` returns 0 only if *both* hold, and neither the halt warning banner nor the `EXIT 1` line appeared |
| remote `__EXIT__ 0` | present on every step |
| restore even after failure | `RESTORE_VERIFIED` — all four clocks match pre-state exactly, `base.bit` reloaded |

## The measurement

Predeclared before the run: **`phase-s-max` wall time vs 0.187814 s, +/-5 ms.**

    measured   0.189 s
    model      0.187814 s
    delta      +1.186 ms          PASS (within +/-5 ms)

Full suite, measured against the model:

| case | patch | templ | model s | measured s | delta ms | rel |
|---|---|---|---|---|---|---|
| phase-s-min-templ | 99x67 | 4x4 | 0.004239 | 0.006 | +1.76 | +41.5% |
| phase-s-origin | 147x94 | 52x31 | 0.037405 | 0.040 | +2.60 | +6.9% |
| phase-s-workload-mode | 147x94 | 52x31 | 0.037405 | 0.040 | +2.60 | +6.9% |
| phase-s-workload-wide | 259x105 | 164x42 | 0.072316 | 0.075 | +2.68 | +3.7% |
| phase-s-final-cell | 215x157 | 120x94 | 0.142207 | 0.144 | +1.79 | +1.3% |
| phase-s-workload-max | 215x157 | 120x94 | 0.142207 | 0.144 | +1.79 | +1.3% |
| **phase-s-max** | 311x159 | 216x96 | **0.187814** | **0.189** | **+1.19** | **+0.63%** |

Every residual is positive and lies in 1.19–2.68 ms while the relative error
falls from 41.5% to 0.63% as the compute grows. That is the signature of a fixed
DMA/control/polling cost, and it is the same pattern the 125 MHz gate recorded
(1.48% on its 4x4 case against 0.055% on its envelope case).

**Precision caveat:** the runner prints seconds to three decimals, so each
measurement carries a +/-0.5 ms quantisation floor. The deltas above are good to
about +/-0.5 ms and should not be quoted more tightly.

## What this does and does not establish

**It is not a cycle measurement.** The RTL carries no cycle counter, so nothing
on the board counts cycles. What was measured is PS-side **DMA + core + polling
wall time** at the frozen Phase-S geometry. The model is being tested against an
end-to-end time that includes fixed overhead the model does not describe — which
is precisely why the residuals are all positive rather than scattered.

So `phase-s-max` at 0.189 s is consistent with 23,476,737 cycles at 125 MHz plus
~1.2 ms of fixed overhead. It does not independently confirm the cycle count.

**What it does establish:** the unchanged core computes correct scores and exact
locations at all seven Phase-S geometries on silicon, at a verified 125 MHz, and
the cycle model predicts the largest Phase-S trial's wall time to +0.63%.

Phase S therefore moves from **core-only projection** to **silicon-anchored** for
the geometry, while the 36.476 s/page page-level figure remains a projection: it
aggregates 20,680 modelled trials, and no page has been run end to end.

## Audit chain — closed this time

The 125 MHz gate's chain was corroborated but **not closed**: nothing in its
retained record bound the configure step to a hash taken at that moment.
`PROBE125_ARTIFACTS.sha256` says closure would need "hashing immediately before a
repeated gate run, in the same transcript as the configure and the vectors".

That is what this session did. `02_hashes_remote.txt` (02:03:05Z) hashes the
board-side `.bit`/`.hwh`/runner/manifest/vectors, and `03_run.txt` (02:03:45Z)
re-hashes the bit, runner and manifest *inside the same transcript that loads the
overlay and runs the cases*. The bitstream hash

    fdebaf75597a4e43e2a3bc590e2d7395e4fd03584962f90ac4e047e3a126c6c1

matches `PROBE125_ARTIFACTS.sha256` and appears in the configure transcript
itself. For **this** session the chain from bytes to result is unbroken.

Note the scope: this closes the chain for the Phase-S run. It does not
retroactively close the 2026-08-17 one, which remains post-hoc corroboration.

## Preflights that gated this run (2026-08-18, off-board)

1. `tme_tb.cpp` accepted only csim/cosim/hw, so `phase_s` could not be
   C-simulated at all. Whitelisted; **C-sim 7/7 PASS** plus §4.6 direct tests at
   0 failures, in a scratch HLS project so `-reset` could not touch the repo's.
2. `--expect-fclk-mhz` added, **fail-closed**: a wrong clock *or an unreadable
   one* now aborts. Previously both merely warned, and the unreadable case
   produced a clean-looking run whose times were uninterpretable. Stale
   "31.25 MHz / 20 ns" guidance no longer printed for non-shipping builds.
3. Suite reordered ascending — smallest first, largest last — because the runner
   re-runs `cases[0]` after the suite. With `phase-s-max` at index 0 the check
   re-ran the *largest* case, testing the grow direction the sequence already
   covered, and added 23,476,737 cycles a second time: **101,425,888** executed
   rather than the 77,949,151 the seven unique cases sum to. Now 78,479,008
   executed, and `build_phase_s()` asserts the ordering.
