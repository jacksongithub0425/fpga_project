# B1 board session — 2026-08-19T05:13–05:16Z

Bounded session under the explicit authorization of 2026-08-18 ("I approve the
B1 board session"). Board `pynq`, hostname-resolved. Overlay
**`TermCountB1:hls:tme_top:0.2`** — the B1 core, routed at 8.000 ns with
WNS +0.134571 ns.

Retained transcripts, in order:

    00_prestate.txt      pre-state, read-only
    01_hashes_local.txt  host-side hashes, immediately before the load
    02_hashes_remote.txt board-side hashes of the same six files
    03_run.txt           re-hash + configure + 7 cases + re-invocation
    04_restore.txt       base.bit + clocks restored and verified

---

## Why this is a controlled experiment, not just another run

The runner and **all three vector files are byte-identical** to the Priority 3
session that measured the *unmodified* core at the same 125 MHz:

| artifact | Priority 3 | this session |
|---|---|---|
| `tme_standalone_bringup.py` | `f7b00b0e…` | `f7b00b0e…` **same** |
| `tb_tme_cases_phase_s.txt` | `9970f100…` | `9970f100…` **same** |
| `tb_tme_patches_phase_s.bin` | `3c22a422…` | `3c22a422…` **same** |
| `tb_tme_templs_phase_s.bin` | `9fd283bf…` | `9fd283bf…` **same** |
| `tme_standalone.bit` | `fdebaf75…` | **`2cd4a2b0…`** ← the only change |
| `tme_standalone.hwh` | `614a8bb7…` | **`ffa94282…`** |

Same inputs, same driver, same clock, same DMA path. **Only the RTL differs**,
so the wall-time difference is attributable to B1. Reusing the hash-bound
`phase_s` suite also means no gate evidence was regenerated.

> **The runner has since been amended.** On 2026-08-18 a stale-banner notice was
> added to `sw/tme_standalone_bringup.py` (its `--suite phase_s` header claimed
> "the unchanged core", which stopped being true the moment this session pointed
> the same runner at the B1 bitstream). That edit changes the file's digest away
> from the `f7b00b0e…` pinned above, so **the bytes that ran are retained
> separately** as `tme_standalone_bringup.py.as_run` in this directory, at
> `f7b00b0e7cdff74fc0940b46a5b01cb06b0f3fd61955c0ec41319340a8720153`. Both the
> as-run snapshot and the current working copy are in
> `logs/b1_20260818/MANIFEST.sha256`. The table above describes the session; it
> is not a claim about the file in the tree today.

## Acceptance criteria — all met

| requirement | result |
|---|---|
| new board directory, shipping artifacts untouched | `tme_b1/` created. `tme_phase_s` (Aug 19 02:04), `tme_probe125` (Aug 18 04:51), `tme_test` (Aug 9 21:40) all kept their mtimes |
| hash bit/HWH/runner/manifest/vectors immediately before loading, in the retained transcript | all six, host **and** board, in `01_`/`02_`, all matching |
| re-hash inside the transcript that configures the PL | `03_run.txt` re-hashes `.bit`, runner and manifest before the overlay load — bitstream `2cd4a2b0…` appears in the configure transcript itself |
| pre-state recorded | `00_prestate.txt` — fclk 100.0 / 142.857143 / 200.0 / 100.0, CmaFree 51,460 kB |
| observed FCLK0 = 125 MHz | **`FCLK0 gate: PASS — 125.0000 MHz`** (fail-closed: a wrong *or unreadable* clock aborts) |
| functional vectors | **7/7 PASS** — `abs(score - gold) <= 0.005` **and** exact (x, y) on every case. The location is exact; the score is checked against a tolerance, not for equality. All seven print `+1.0000` against `+1.0000`, but at four decimals, so this session does not establish score equality and does not claim it. |
| genuine re-invocation | **PASS** — `phase-s-min-templ` (6,633 B) re-run after the 49,449 B largest case, i.e. the *shrink* direction |
| verified DMA halt | runner `__EXIT__ 0`; `_exit_status(cases_ok, halt_ok)` returns 0 only if both hold |
| clock/overlay restoration | **`RESTORE_VERIFIED`** — `base.bit` reloaded, all four clocks match pre-state exactly |

## The measurement

Predeclared: the workload-width cases must move by the modelled amount. The
max-width case was predeclared as **uninformative** — its predicted delta is
−0.049 ms against a 1 ms print resolution.

| case | patch / templ | cur (P3) | B1 | measured Δ | predicted Δ | residual |
|---|---|---|---|---|---|---|
| phase-s-min-templ | 99×67 / 4×4 | 0.006 | 0.004 | +2.0 ms | +2.603 ms | −0.60 |
| phase-s-origin | 147×94 / 52×31 | 0.040 | 0.024 | +16.0 ms | +15.602 ms | +0.40 |
| phase-s-workload-mode | 147×94 / 52×31 | 0.040 | 0.024 | +16.0 ms | +15.602 ms | +0.40 |
| phase-s-workload-wide | 259×105 / 164×42 | 0.075 | 0.068 | +7.0 ms | +6.688 ms | +0.31 |
| phase-s-final-cell | 215×157 / 120×94 | 0.144 | 0.117 | +27.0 ms | +27.674 ms | −0.67 |
| phase-s-workload-max | 215×157 / 120×94 | 0.144 | 0.117 | +27.0 ms | +27.674 ms | −0.67 |
| **phase-s-max** | 311×159 / 216×96 | 0.189 | 0.190 | **−1.0 ms** | **−0.049 ms** | −0.95 |
| total | | 0.638 | 0.544 | +94.0 ms | +95.793 ms | −1.79 |

**Every residual is inside the quantisation floor.** Each wall time prints to
milliseconds, so each *difference* of two of them carries ±1 ms; the six
informative residuals span −0.67 to +0.40 ms.

**`phase-s-max` is consistent but proves nothing**, and that was predeclared.
Its predicted change is −49 µs — twenty times under the print resolution. Its
measured −1.0 ms is within ±1 ms of the prediction and equally within ±1 ms of
zero, so it discriminates nothing. It is in the table because it is in the
suite, not because it is evidence. This is exactly why the session had to carry
workload widths.

## What this establishes, and what it does not

**Establishes.** The B1 core computes correct scores and exact locations at all
seven Phase-S geometries on silicon at a verified 125 MHz, and the measured
saving matches the cycle term at every geometry where the saving is
resolvable. The cycle term `T*(2*tw + 41) + 1` is now silicon-corroborated at
workload widths, not only cosim-measured.

**Does not establish.**

* **Not a cycle measurement.** The RTL has no cycle counter. What was measured
  is PS-side DMA + core + polling wall time. Agreement is at ±1 ms, which at
  125 MHz is ±125,000 cycles — three orders of magnitude coarser than the
  co-simulation, which is where the exact term comes from.
* **Not a page time.** 26.334292108222 s/page remains a **workload projection**
  summing 20,680 modelled trials. No page has been run end to end, here or
  anywhere.
* **Nothing about B2 or B0b.** Their terms remain unmeasured projections.

## Audit chain

Closed for this session, on the Priority 3 pattern: `01_` and `02_` hash the six
artifacts host- and board-side at 05:13:49Z / 05:14:52Z, and `03_run.txt`
re-hashes the bitstream, runner and manifest **inside the transcript that loads
the overlay and runs the cases**. The chain from bytes to result is unbroken for
this run. It says nothing about earlier sessions.
