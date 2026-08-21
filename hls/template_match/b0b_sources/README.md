# Pinned `tme_top.cpp` snapshots — Priority 6 (B0b)

`b1_sources/` pins `correlation_core.cpp`, because that is the file
Priorities 4 and 5 varied. B0b varies **`tme_top.cpp`** — the window
statistics live there — so the two files swap roles: here
`tme_top.<variant>.cpp` is the pinned variable and
`b1_sources/correlation_core.b2.cpp` is the pinned constant.

These files are **immutable evidence**, not working sources.
`run_hls_b0b.tcl` compiles one of them per solution and refuses to proceed if
its SHA-256 has moved. Edit `../tme_top.cpp` — the shipped file — and add a
*new* snapshot here if a new variant needs measuring.

| file | sha256 | what it is |
|---|---|---|
| `tme_top.b2.cpp` | `bcccd44c…b30f59db` | the shipped core, byte-identical to the `tme_top.cpp` that `logs/b2_20260819/MANIFEST.sha256` records as B2's build input |
| `tme_top.shadow.cpp` | `0d598d29…9485ef04` | b2 **plus** the hoisted pass, computed alongside and compared at every result position; nothing removed |
| `tme_top.b0b.cpp` | `5e791fe3…c2092102` | the hoisted pass **only**; the repeated statistics loops deleted |

## Why three solutions and not two

The usual A/B is control-plus-variant. B0b is two changes that a single
difference would fold together — adding a pass and deleting the loops it
replaces — so the middle solution separates them:

    D1 = shadow - b2ctl     the hoisted pass PLUS the shadow's comparator
    D2 = b0b    - shadow    -(the repeated loops) MINUS the same comparator
    D  = b0b    - b2ctl     the frozen net law -- comparator-free

    NEITHER HALF IS A COMPONENT COST.  The shadow's comparison lives inside
    norm_cols and reschedules it by +2 cycles a call (97 ~ 3361 -> 99 ~ 3363,
    with II and iteration latency unchanged, which is why the checker missed it
    until 2026-08-20).  norm_cols runs once per output row, so D1 carries +2*rh
    that the pass does not and D2 is short by the same.  They cancel in D.
    Shifting them across gives S*(pw + 29) + th + 3 and
    -[rh*th*(tw + rw + 24) + rh], which fit the same fourteen transactions just
    as exactly -- so the split is not identified by these three solutions, and
    a comparator-free fourth cannot exist (an unread copy of the statistics is
    dead code and gets deleted).  See tme_b0b_ab.py check 3b.

`D2` is worth as much as `D1`. `sw/tme_cycle_model.py` splits the measured
per-(output row, template row) cost `3*tw + 3*rw + 33` into four terms —
template-row staging `2*tw + 3`, window statistics `tw + rw + 21`,
correlation writeback/control `2*rw + 8`, and one FSM cycle — and **only the
sum has ever been measured**. `check()` proves the four expressions add up;
it cannot prove any one of them is the right share. `D2` is the first direct
measurement of one of the four — **and it refuted the attribution**: the
measurement is `tw + rw + 24` per (output row, template row) plus 3 per output
row. See *What the snapshots measured* below.

## The control is byte-exact, and that is checkable

`tme_top.b2.cpp` is a copy of the working-tree `tme_top.cpp` taken before any
B0b edit, and its digest `bcccd44c187bd7c008d8de7bbd87dfa33c1d7bf72c1fbfbae1b8046bb30f59db`
is the one `logs/b2_20260819/MANIFEST.sha256` line 12 records as B2's build
input. `correlation_core.b2.cpp` is the same snapshot `run_hls_b1.tcl`
compiles for `TME_SOLUTION=b2`. So `b2ctl` is the same pair of files B2 was
measured from, and Check 1 of `sw/tme_b0b_ab.py` requires it to reproduce
B2's published term on every transaction. If it does not, the pair is not a
pair and nothing else in the comparison is attributable.

`tme_top.h` is pinned here too. It was an unrecorded live input to B1's
measurement — an edit to the accumulator widths would have changed both
halves of a "paired" measurement silently — and it was only pinned
afterwards. This build gates it before the first `add_files`.

## Shadow mode: how a disagreement gets out

The shadow build carries both computations and compares them at every
`(v, u)` with `u < rw`. A single mismatch sets a sticky flag that replaces
the result with

    result_score = -3.0f,  result_x = result_y = 0xFFFF

`-3.0f` is unreachable by construction: `best_score` starts at `-2.0f` and
every score written to it has been clamped into `[-1, +1]`. So the sentinel
needs **no new port and no driver change** — the testbench compares score
bits and exact location, RTL co-simulation compares against the same
goldens, and the board runner's tolerance check rejects it. On agreement the
outputs are bit-identical to `b2`'s.

"No mismatch" is also what a comparison that never ran would report, so the
C simulation prints the number of positions actually compared per invocation
and it must equal `rh*rw`. That line is the positive half of the claim.

## Counts or sums

The priority list calls this the "foreground count" pass. It would be one if
the patch were guaranteed binary: for pixels in `{0, 255}`, `ΣI = 255·C` and
`ΣI² = 255²·C`, so a single counter would serve both statistics and cost less
per iteration.

**The contract does not guarantee that.** §4.1 bounds the geometry and says
nothing about pixel *values*, and the vector suites deliberately contain
grayscale patches — `stress-max-result` is grayscale *by necessity* (a 4×4
binary window recurs within a few hundred of its 248,368 positions, so there
would be no unique peak to assert), and it is one of the two cases that reach
the 817×304 result map and `T = 52` on silicon. A count-only core would be an
ABI narrowing that invalidated those vectors.

So the pass carries the general `ΣI` and `ΣI²`. **The iteration count is
identical either way** — `tme_cycle_model.b0b_count_pass_iterations` does not
depend on which statistic rides the scan — so nothing in the cycle figures
turns on this choice, and the count-only specialisation stays available as a
later, cheaper-per-iteration variant if it is ever worth an ABI change.

## Why the subtract and the add are separate scans

Fusing them into one loop over `u` would cost `pw*ph` iterations instead of
`pw*(2*ph - th)` — **fewer**. It is not free, though. A fused body reads four
patch pixels per iteration: the outgoing and incoming column of the outgoing
row *and* of the incoming row. `patch_buf` is `RAM_2P`, cyclic-partitioned by
`PAR_COLS` on the column dimension, so the two reads at column `u-1` land in
the same bank and the two at `u+tw-1` land in another same bank — two reads
per bank, exactly the port budget, **unless `tw` is a multiple of
`PAR_COLS`**, in which case all four collide on two banks and the loop cannot
hold II=1.

The separate scans read two pixels of one row per iteration and are
structurally the loop `b2` already ran at II=1 — and csynth confirms the
hoisted versions keep that: `scan_init` and `scan_slide` come out at II=1 with
iteration latencies 7 and 14, identical to the `isq_init`/`isq_slide` they
replace. The fused form is a real candidate for a later variant; it is not what
the measured term describes, and switching to it silently would make the model
wrong in the flattering direction.

## Underflow is impossible by ordering, not by width

The outgoing row is subtracted **before** the incoming row is added, so every
intermediate is the exact sum over the `th-1` rows that stay in the window —
non-negative, and smaller than either endpoint. Reversing the order would
leave a `th+1`-row intermediate, which also fits (`97·216·255 = 5,342,760 <
2²³` and `97·216·255² = 1,362,403,800 < 2³¹`), but the correctness argument
would then rest on a width bound instead of on a sign. Do not reorder the two
calls.

## Nothing is carried across invocations

`si_col` and `sii_col` are `static` and the B0b version no longer resets them
per output row, so this has to be argued rather than assumed. At `v == 0` the
first scan is a `SET`, which overwrites every `u < rw` before anything reads
it. Entries at `u >= rw` keep whatever a previous, wider invocation left there
and are never read — the situation `sti_col` has always been in.

Every suite runs its cases back-to-back through one DUT instance, and the
`-argv b0b` corners include a maximum window (`216×96`, `rh = 1`) immediately
followed by smaller geometries, so the property is exercised rather than only
argued.

## The corners the manifest suites cannot express

`tme_tb.cpp -argv "b0b"` runs the csim manifest plus eight direct cases. The
generator cannot express them: it refuses a flat template, and it needs a
unique non-degenerate peak.

| case | geometry | what it corners |
|---|---|---|
| `b0b-zero-40x30` | 40×30 / 4×4 | 26 shifts subtracting from an accumulator that is identically zero |
| `b0b-ones-40x30` | 40×30 / 4×4 | the same, at 255 — a flat window, `di == 0` |
| `b0b-ones-rh1-216x96` | 216×96 / 216×96 | `rh == 1`: the initialising pass runs and **no shift ever happens**; also the maximum window, `ΣI = 5,287,680` and `ΣI² = 1,348,358,400` |
| `b0b-ones-216x98` | 216×98 / 216×96 | the maximum window with two shifts over it |
| `b0b-step-rh2` | 40×5 / 4×4 | `rh == 2`: exactly one shift, and the winner is on the far side of it |
| `b0b-step-first-row` | 40×30 / 4×4 | winner at `v = 0` |
| `b0b-step-mid-row` | 40×30 / 4×4 | winner at `v = 13` of 27 |
| `b0b-step-last-row` | 40×30 / 4×4 | winner at `v = rh-1 = 26` |

Every expected value is **derived, not measured**. The flat cases score `+0.0`
because `di == 0` is the contract value (§4.6) and `best_score` starts at
`-2.0f`, so the first window keeps the location. The step cases put an exact
copy of the template in the patch, where `ΣTI = ΣT²`, `ΣI = ΣT` and
`ΣI² = ΣT²`, so `num = di = dt` and the score is `dt_f / sqrtf(dt_f*dt_f) =
1.0f` exactly — `sqrt(fl(x·x)) == x` under round-to-nearest, and
`dt = 4,161,600` is itself exact in float32. Every column of a step patch is
identical, so the winning row is reached at every `u` and the row-major first
occurrence selects `u = 0`. `logs/b0b_20260820/b0b_direct_expect.py`
recomputes all eight from the DUT's arithmetic.

They are gated to `-argv b0b` on purpose. A direct DUT call is also a
co-simulation transaction, and `sw/tme_b1_ab.py` and `sw/tme_b2_ab.py` map
transaction indices onto (2 direct + 12 manifest) for `-argv b1`; adding
invocations to every suite would silently renumber B1's and B2's evidence.

## What the shadow build's own latency is not

`shadow` is `b2ctl` plus a pass. It is **slower** than `b2ctl` by
construction, it is not a variant anyone would ship, and its total latency is
not a B0b figure. Only the differences mean anything.

## What the snapshots measured

Paired RTL co-simulation, the same fourteen invocations and the same pinned
vectors for all three solutions (`sw/tme_b0b_ab.py`, 2026-08-20):

    pass    = S * (pw + 30) + 5           S = th + 2*(rh - 1)
    removal = rh*th*(tw + rw + 24) + 3*rh

each exact on 14/14, with `b2ctl` reproducing B2's published term on all 14 in
the same comparison. Over the modelled workload that is **17.726036 s/page**,
aggregate 79,767,161,516 cycles.

**Both projections were wrong, and only one was about B0b.** The model
bracketed the pass at `N*I` for `N` in `[1, 3]`; csynth says the II really is 1
(`scan_init` and `scan_slide` at II=1, iteration latencies 7 and 14 —
*identical* to the `isq_init`/`isq_slide` they replace), so the entire 25% miss
is the per-scan constant that was never modelled. The removal was
pre-registered as `rh*th*(tw + rw + 21)` and is **refuted**: only the *sum* of
the model's four-way split of the fitted per-row term ever had evidence, and
this is the first of its four terms to be measured. See
`logs/b0b_20260820/PRIORITY6_EVIDENCE.md`.

The measured 17.726036 is **below both withdrawn endpoints** (17.743731 and
18.035794). The two errors point in opposite directions and the removal wins;
the pair was published as a bracket and did not contain the answer.

**B0b is not a uniform improvement.** At `rh == 1` it loses, by exactly
`5*th + 2` cycles.

**No routed timing and no silicon** at the time this was written. B1's and B2's
board sessions say nothing about B0b.

## The comments inside these files are FROZEN

These snapshots are hash-pinned in `run_hls_b0b.tcl` and in
`logs/b0b_20260820/MANIFEST.sha256`, so their bytes cannot be corrected without
invalidating every digest that binds the measurement to a source. That includes
their comments — and `tme_top.b0b.cpp` was necessarily written *before* its own
measurement existed, so it points at the model and the adjudicator rather than
quoting a term.

`../tme_top.cpp` is the shipped file and the authority on **current wording**;
it carries the measured terms. These files are the authority on **what was
compiled**. `diff` over their non-comment lines is empty. Do not reconcile them
by editing a snapshot — that is the same situation `b1_sources/README.md`
records for `b1` and `b2`, and it arises the same way: the measurement cannot be
written down until after the thing is measured.
