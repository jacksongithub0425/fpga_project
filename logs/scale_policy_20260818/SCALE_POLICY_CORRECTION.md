# `tme_scale_policy.py --study` latency table: corrected — 2026-08-18

The accuracy half of Priority 1 is unaffected. What changed is the **modelled
latency** table at the bottom of `--study`, which had three defects. All three
are fixed and the tool now recomputes every cycle figure from
`tme_cycle_model` instead of summing a captured column.

## What was wrong

**1. It printed nothing at all.** The variant list included
`("S_B0b", "cycles_S_B0b")`, and no trace has ever carried that key — the
capture writes `cycles_S_B0b_base`, which is the window-statistics *deletion*
and not a runnable variant. `--study` raised `KeyError: 'cycles_S_B0b'` and
died after the per-endpoint section. Any latency figure quoted from this tool
came from a run that predates that variant being added.

**2. It summed a stale column.** `cycles_S_B1` in `trace_20260818b` was
computed with the tile term `T*(2*tw + 40)`, which Priority 4 withdrew in
favour of the measured `T*(2*tw + 41) + 1`
(`trace_20260818b/B1_COLUMN_STALE.md`). Every B1 number was therefore
understated.

**3. It divided by 34.** `pages` was `len({c["page"] for c in calls})` — pages
that produced at least one call. Two of the 36 corpus pages hold no endpoint,
so every s/page was inflated by 36/34 = 5.9% against a model that divides by
36 throughout.

## The correction

| | was | now |
|---|---|---|
| B1 refinement, s/page | 1.039 (stale column ÷ 34 rounds to 1.100; the literal in the file said 1.039) | **1.042909980** |
| B0b columns | crashed | II=1 and II=3 endpoints, via `model.cycles_b0b` |
| divisor | 34 | 36 |

The full-oracle leg now reproduces the freeze exactly — 36.476 / 26.334 /
20.175 / 17.514 / 17.806 — and `latency()` asserts that its own B1 initial
total equals `FROZEN["b1"]["aggregate_cycles"]`, so the tool and the model
cannot drift apart silently again.

## Corrected tables

Captured in `corrected_latency.txt`. Both are **modelled, not measured.**

Full eight-scale oracle (the baseline):

    variant     initial   fallback  refinement     TOTAL
    S            36.476      0.000       1.492    37.969
    S_B1         26.334      0.000       1.043    27.377
    S_B2         20.175      0.000       0.805    20.980
    S_B0b@1      17.514      0.000       0.697    18.211
    S_B0b@3      17.806      0.000       0.708    18.514

Policy {0.8, 1.0, 1.2}:

    variant     initial   fallback  refinement     TOTAL   vs oracle
    S            12.456      4.637       1.492    18.585      2.04x
    S_B1          8.658      3.083       1.043    12.784      2.14x
    S_B2          6.693      2.411       0.805     9.909      2.12x
    S_B0b@1       5.799      2.086       0.697     8.581      2.12x
    S_B0b@3       5.903      2.130       0.708     8.740      2.12x

## Read the 5.799 carefully

**5.799 s/page is the initial-trial column only.** With the fallbacks that
policy triggers (2.086) and all 808 refinement calls (0.697), the same policy
and the same variant come to **8.581 s/page**. The sub-10-second reading was
never wrong about the arithmetic in the column it named; it was a partial sum
being read as a page time.

And it is moot for selection: `{0.8, 1.0, 1.2}` **fails the parity gate** —
7.45% of endpoints change class and 19 pages change count. It stays here as
the worked example of the correction, not as a candidate.
