# `cycles_S_B1` in this trace is STALE — 2026-08-18

**The trace itself is not stale.** Every other column, the workload counts
(20,680 / 808 / 371 / 367), the digests and `provenance.json` all still stand,
and this directory is **not** superseded. Exactly one column is affected.

## What is wrong

`trace_summary.csv`'s **`cycles_S_B1`** was computed with
`tme_cycle_model.cycles(..., "B1")` as it stood at capture time:

    tile = T * (tw + (16 + tw - 1) + 25)   ==  T * (2*tw + 40)      WITHDRAWN

That was a PROJECTION, written before any B1 RTL existed. Priority 4 built the
RTL and measured it by paired co-simulation (14/14 transactions exact):

    tile = T * (2*tw + 41) + 1                                      MEASURED

So every value in the column is **too small**. The projection was optimistic.

## By how much

On the initial-trial basis the model computes (20,680 trials, 36 pages,
125 MHz):

| | cycles | s/page |
|---|---|---|
| withdrawn projection | 118,078,633,847 | 26.239696410444 |
| measured | **118,504,314,487** | **26.334292108222** |
| understatement | **425,680,640** | 0.094595697778 |

`cycles_S_B1` in this file also carries the 808 refinement calls, so its column
total (122,753,879,541) is on a *different* basis and is not comparable to the
118.5e9 above. The refinement part is understated too, by the same
`rh*th*(T + 1)` per invocation — **this file does not state the amount**,
because the per-invocation records needed to recompute it
(`*_trials.jsonl`) are empty in the committed tree by design (they carry
drawing-derived labels). Recomputing it needs a fresh capture.

## What to use instead

`sw/tme_cycle_model.py` is the authority. `FROZEN["b1"]["aggregate_cycles"]` is
frozen as an exact integer and asserted with `==`, precisely so that a rounded
s/page can never again hide a term-level error:

    python tme_cycle_model.py --assert

Also note `FROZEN["b1"]["withdrawn_projection"]` and `["projection_miss"]`,
which freeze the size of this correction so it cannot be quietly rounded away.

## Why this file rather than an edit

Traces are **versioned, not edited** — the same rule that produced
`trace_20260817/STALE.md` and `trace_20260818/SUPERSEDED.md`. Editing a column
in place would break `provenance.json`'s digests and destroy the only record of
what the model said when the capture ran. The committed copy
`sw/trace_20260818b_summary.csv` carries the same stale column and the same
marker applies to it.

Related: `logs/b1_20260818/PRIORITY4_EVIDENCE.md`.
