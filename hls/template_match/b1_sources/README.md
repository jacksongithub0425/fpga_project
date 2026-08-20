# Pinned `correlation_core.cpp` snapshots — Priorities 4 (B1) and 5 (B2)

The directory name is B1's, because that is the measurement it was created
for. It is now the pinned-snapshot directory for `correlation_core`
generally: Priority 5 added `correlation_core.b2.cpp` here rather than
forking the flow, which is exactly what the paragraph below told it to do.
Renaming the directory would break every path in
`logs/b1_20260818/MANIFEST.sha256`.

These four files are **immutable evidence**, not working sources. `run_hls_b1.tcl`
compiles one of them per solution and refuses to proceed if its SHA-256 has
moved. Edit `../correlation_core.cpp` — the shipped file — and add a *new*
snapshot here if a new variant needs measuring.

| file | sha256 | what it is |
|---|---|---|
| `correlation_core.cur.cpp` | `9ca36c47…b699d2b` | the unmodified core; byte-identical to git `eb1c8ac` |
| `correlation_core.b1.cpp` | `e33fe219…54c4a51` | runtime `seg_len`, `if (i >= seg_len) break` — **the shipped form** |
| `correlation_core.b1b.cpp` | `17e3b1ec…f9519242` | hoisted clamped bound, no per-iteration exit test |
| `correlation_core.b2.cpp` | `c8c7b088…caec5d8ce` | B1 **plus horizontal overlap reuse** — **the shipped form** |

## Why they exist

B1's evidence is a *paired* co-simulation: the same 14 invocations through the
unmodified and the modified core. The first version of `run_hls_b1.tcl` added
the working-tree `correlation_core.cpp` for every solution, so the control was
correct **only because it happened to be run before the edit was applied**.
Re-running `TME_SOLUTION=cur` afterwards would have rebuilt the control out of
B1 source and reported a zero difference — a reproduction step that destroys the
thing it claims to reproduce. Pinning removes that ordering dependency entirely.

## Provenance of each snapshot

* **`cur`** — copied from the working tree before any B1 edit, and verified
  against `git show eb1c8ac:hls/template_match/correlation_core.cpp`.
* **`b1`** — the shipped `correlation_core.cpp`. Its comments were corrected
  *after* the original co-simulation, so this file is **not byte-identical** to
  what xsim compiled then; the pre-correction file is retained at
  `logs/b1_20260818/correlation_core.cpp.b1_break`. The difference is comments
  only — and that is no longer an argument, it is a measurement: the 2026-08-18
  rebuild from this snapshot produced a **byte-identical transaction report**
  (`logs/b1_rerun_20260818/`).
* **`b1b`** — reconstructed by applying the hoisted-bound edit to
  `correlation_core.cpp.b1_break`, i.e. to the exact source the `b1` solution
  was simulated from. This reproduces what the `b1b` solution compiled.
* **`b2`** — copied from the working tree at the moment it was pinned, and
  byte-identical to the shipped `../correlation_core.cpp` **as compiled**. The
  shipped file has since gained the MEASURED tile term in its comments, so the
  two now differ — **in comments only**, and `diff` over the non-comment lines
  is empty. This is the same situation `b1` is in and it arises the same way:
  the measurement cannot be written down until after the thing is measured, and
  the snapshot cannot change afterwards without invalidating the digests that
  bind the measurement to it. The snapshot is the authority on *what was
  compiled*; `../correlation_core.cpp` is the authority on *current wording*.
  Do **not** reconcile them by editing the snapshot.

## Which report came from which

| project / solution | source | result |
|---|---|---|
| `template_match_b1_cur/cur` | `correlation_core.cur.cpp` | control, 14 transactions, reproduces the published `cur` tile term (k = 25) |
| `template_match_b1_b1/b1` | `correlation_core.b1.cpp` | 14 transactions, `tile = T*(2*tw + 41) + 1` |
| `template_match_b1_b1b/b1b` | `correlation_core.b1b.cpp` | **byte-identical report to `b1`** — the negative result |
| `template_match_b1_b2/b2` | `correlation_core.b2.cpp` | 14 transactions, `tile = T*(tw + 44) + tw - 2` |

One project per variant, each opened with `-reset`. They shared a single
`template_match_b1` until 2026-08-18, which made the A/B build unsafe to
re-run: `open_project` without `-reset` reopens what is on disk and `add_files`
accumulates, while `-reset` on a shared project deletes the sibling solutions —
the other half of the pair. The old tree is kept, marked, and read by nothing
(`template_match_b1/SUPERSEDED.md`).

Adjudicate any of them with:

    python tme_b1_ab.py --sol {b1|b1b} --assert       # control `cur`
    python tme_b2_ab.py --assert                      # control `b1`

**B2's control is `b1`, not `cur`.** B2 is B1 plus the reuse, so pairing it
against the unmodified core would fold two changes into one difference and
leave neither attributable. The `cur` report is still what anchors B1.

## The comments inside these files are FROZEN, and one of them is now wrong

These snapshots are hash-pinned in `run_hls_b1.tcl` and in
`logs/b1_20260818/MANIFEST.sha256`, so their bytes cannot be corrected without
invalidating every digest that binds the measurement to a source. That includes
their comments.

`correlation_core.b1.cpp` still attributes the measured `T + 1` overhead to
"the `i >= seg_len` exit test ... and +1 per CALL for the bound setup". **That
attribution is not supported.** The `b1b` experiment in this very directory
removed the per-iteration predicate and left the transaction report
byte-identical — so the source-level form of the test costs nothing. Note what
that does *not* show: `b1b` still has a runtime loop bound (`i < seg_n`), so
runtime-bounded control is neither confirmed nor excluded. Only the *shape* of
the overhead is established; the mechanism stays unlocalized.

The correction lives in the shipped `../correlation_core.cpp`, which is the
authority on current wording. These files are the authority on what was
compiled. Do not reconcile them by editing a snapshot.

## Priority 5 (B2): what the snapshot measured, and what it did not

`correlation_core.b2.cpp` was co-simulated against `b1` on the same fourteen
invocations and the same pinned vectors. The measured tile term is

    tile = T*(tw + 44) + tw - 2      per (output row, template row)

exact on 14/14, with the `b1` control still reproducing its own published term
on all 14 in the same comparison. The saving over B1 is `(T-1)*(tw-3)`, which
is zero at `T = 1` and positive for every legal `tw >= 4` — so unlike B1, which
**loses** at `tw = 216`, B2 is never a regression.

**The measured term matched neither reconstructed baseline.** The pre-RTL
projection is retained in commit ancestry before the B2 source/build commit;
`tme_b2_ab.py --predict` and its snapshot were created after the measurement.
This is repository ordering, not an external timestamp: an unsigned, unpushed
commit date does not prove when a third party could observe it. Against the
pre-RTL projection the miss is `3*T - 1 = (T + 1) + 2*(T - 1)`: B1's `T + 1`
overhead recurs, and `2*(T - 1)` is the additional miss against the
control-naive baseline that already includes B1's correction.

**B2 has a board session, and it is broader than B1's.** 2026-08-20,
`logs/b2_board_20260819/`: the routed 8.000 ns image at a gated 125.0000 MHz,
`phase_s` 7/7 and `hw` 9/9, score within +/-0.005 and exact `(x, y)`, with a
verified re-invocation after each suite's largest case. B1's session ran
`phase_s` only, so every case had `T = 6`; the `hw` suite reaches `T = 38` and
`T = 52` -- the compiled maximum -- and moves the full 251,740 B transfer. For
an indexing change, that is the axis that mattered.

**The s/page figure is still a projection.** The board validates the tile
*term*, at one tile count per case; 20.405 s/page sums that term over 20,680
modelled trials. No page has been run, on any hardware, at any clock.
