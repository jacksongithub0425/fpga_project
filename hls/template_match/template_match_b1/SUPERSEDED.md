# `template_match_b1` is SUPERSEDED — 2026-08-18

Replaced by one isolated reset project per variant:

    template_match_b1_cur/cur
    template_match_b1_b1/b1
    template_match_b1_b1b/b1b
    template_match_b1_prod/prod      (csim_prod_b1.tcl)

**Nothing here is wrong.** The reports in this tree are the ones the original
Priority 4 claims were written against, and the rebuild reproduces them — see
below. It is kept as the historical record and read by nothing.

## Why it was replaced

This project held all three solutions, and that made it unsafe to re-run:

* `open_project` **without** `-reset` reopens the project on disk, and
  `add_files` **accumulates** into `hls.app` rather than replacing. The
  `hls.app` here still names the working-tree `correlation_core.cpp`, because
  that is what the first version of `run_hls_b1.tcl` added. Once the script
  moved to pinned snapshots in `b1_sources/`, re-running it would have left
  **both** files in the project.
* `-reset` on the project was not an alternative: it deletes the sibling
  solutions, i.e. the other half of a paired measurement.

So the pinned-snapshot script had never actually produced the reports in this
tree, and could not have — the snapshot lives one directory below `tme_top.h`,
so it also needed an include path the script did not carry.

## What the rebuild found

All three variants were rebuilt from the pinned correlation snapshots into
isolated reset projects on 2026-08-18. The shared `tme_top.cpp/.h` and
`tme_tb.cpp` inputs were live, so these runs are not described as hermetic.

| | result |
|---|---|
| `cur` transaction report | **byte-identical** |
| `b1` transaction report | **byte-identical** |
| `csynth.rpt` timing and resource sections | **identical** |
| `csynth.rpt` remainder | differs only in the build date, the project name, and operator names derived from source line numbers (`add_ln74` → `add_ln104`) |

The line-number shift is expected: `correlation_core.cpp.b1_break`, the file
the original `b1` co-simulation compiled, differs from the `b1` snapshot **only
in comments**, and those comments moved the code down 30 lines.

Full record: `logs/b1_rerun_20260818/`, and the provenance section of
`logs/b1_20260818/PRIORITY4_EVIDENCE.md`.
