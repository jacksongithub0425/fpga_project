# Pinned `correlation_core.cpp` snapshots — Priority 4 (B1)

These three files are **immutable evidence**, not working sources. `run_hls_b1.tcl`
compiles one of them per solution and refuses to proceed if its SHA-256 has
moved. Edit `../correlation_core.cpp` — the shipped file — and add a *new*
snapshot here if a new variant needs measuring.

| file | sha256 | what it is |
|---|---|---|
| `correlation_core.cur.cpp` | `9ca36c47…b699d2b` | the unmodified core; byte-identical to git `eb1c8ac` |
| `correlation_core.b1.cpp` | `e33fe219…54c4a51` | runtime `seg_len`, `if (i >= seg_len) break` — **the shipped form** |
| `correlation_core.b1b.cpp` | `17e3b1ec…f9519242` | hoisted clamped bound, no per-iteration exit test |

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

## Which report came from which

| project / solution | source | result |
|---|---|---|
| `template_match_b1_cur/cur` | `correlation_core.cur.cpp` | control, 14 transactions, reproduces the published `cur` tile term (k = 25) |
| `template_match_b1_b1/b1` | `correlation_core.b1.cpp` | 14 transactions, `tile = T*(2*tw + 41) + 1` |
| `template_match_b1_b1b/b1b` | `correlation_core.b1b.cpp` | **byte-identical report to `b1`** — the negative result |

One project per variant, each opened with `-reset`. They shared a single
`template_match_b1` until 2026-08-18, which made the A/B build unsafe to
re-run: `open_project` without `-reset` reopens what is on disk and `add_files`
accumulates, while `-reset` on a shared project deletes the sibling solutions —
the other half of the pair. The old tree is kept, marked, and read by nothing
(`template_match_b1/SUPERSEDED.md`).

Adjudicate any of them with:

    python tme_b1_ab.py --sol {b1|b1b} --assert
