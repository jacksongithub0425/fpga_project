# `cycles_S_B0b_base` in this trace is STALE — 2026-08-20

**The trace itself is not stale.** Every other column, the workload counts
(20,680 / 808 / 371 / 367), the digests and `provenance.json` all still stand,
and this directory is **not** superseded. This is the second column-level
correction it has taken; see `B1_COLUMN_STALE.md` for the first, which is
independent of this one.

## What is wrong

`trace_summary.csv`'s **`cycles_S_B0b_base`** was computed with
`tme_cycle_model.cycles(..., "B0b_base")` as it stood at capture time. That
variant deleted the window statistics using the model's **attributed** share of
the fitted per-(output row, template row) term:

    window statistics = rh*th*(tw + rw + 21)     and nothing per output row
                                                                    WITHDRAWN

That attribution was never measured. It was one term of a four-way split, and
only the **sum** of that split had evidence. Priority 6 measured the removal
directly, by paired RTL co-simulation of a `b0b` solution against a `shadow`
one over the same fourteen invocations (14/14 exact):

    window statistics = rh*th*(tw + rw + 24) + 3*rh                  MEASURED

So the column **understates the deletion** at every row. `B0b_base` is also not
a runnable variant on its own — it is the removal without the pass that
replaces it — which is why the column was never quotable as a B0b figure.

## What else the capture used to derive, and no longer does

`tme_trace_capture.py` used to combine that column with `count_pass_iterations`
to print the **II = 1 and II = 3 endpoints**, 17.743730541333 and
18.035794052889. **Both are withdrawn.** Both of their inputs were wrong:

* the removal, as above;
* the hoisted pass. The endpoints modelled it as `N × iterations`. The
  **initiation interval really is 1** — csynth puts `scan_init` and
  `scan_slide` at II=1 with iteration latencies 7 and 14, identical to the
  `isq_init` and `isq_slide` they replace — but the pass also carries 30 cycles
  per scan and 5 per invocation that no multiple of the iteration count can
  express. Measured: `S·(pw + 30) + 5` with `S = th + 2·(rh − 1)`.

The measured B0b is **17.726035892444 s/page**, which is **below both
withdrawn endpoints**: the two errors point in opposite directions and the
removal is the larger. The pair was published as a bracket and did not contain
the answer.

## What is NOT affected

**`count_pass_iterations` is untouched and still correct.** It is the derived
iteration count `I = pw·(th + 2·(rh − 1)) == pw·(2·ph − th)`, it does not depend
on any cost attribution, and this trace's own sum over the 20,680 initial
invocations — **657,142,901** — still matches the model's independently
computed total. That cross-check was the point of storing iterations rather
than cycles, and it survived.

`cycles_current`, `cycles_S` and `cycles_S_B2` are unaffected by this
correction. (`cycles_S_B1` has its own, older problem — see
`B1_COLUMN_STALE.md`.)

## What to do instead

Do not re-capture to fix a column. Recompute from the model:

    python sw/tme_cycle_model.py --assert          # the measured B0b term
    python sw/tme_b0b_ab.py --assert               # what measured it

`sw/tme_scale_policy.py` already recomputes every cycle figure from the model
and reads no captured column, for exactly this reason. `tme_trace_capture.py`
now writes a `cycles_S_B0b` column carrying the measured term; any trace
captured before 2026-08-20 does not have it.

Evidence: `logs/b0b_20260820/PRIORITY6_EVIDENCE.md`.
