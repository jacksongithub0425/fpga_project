# Gate 7 fail-open exit on a failed reprogram — closed

    OFF-BOARD ONLY. No board time, no bitstream, no re-run of Stage 1.

Closes the one non-blocking defect left open by Stage 1's audit:
`board_gate_recovery.py` could exit, and release retained CMA pages, if every
reset/reprogram attempt failed.

## The defect

R3 and R5 both reach their `reset_pl` call **holding CMA pages that `close()`
has just refused to free** — that refusal is the assertion those phases exist
to make. The code then did:

```python
ok = reset_fn(bitfile)
rep.require(ok is True, "reprogramming the PL succeeded, …", …)
```

`reset_pl` returning False is the one state with no in-process recovery: the
fabric may still hold a command against those pages and nothing this process
can do will retire it. Recording a failure and running on ends `main()`, drops
the references, and hands the pages straight back — the precise release
`_RETAINED_BUFFERS` and `safe_teardown.fail_stop_holding` exist to refuse.
`safe_teardown` has had `fail_stop_holding` all along; the gate never called
it.

Four sites, not one:

| site | before | after |
|---|---|---|
| R3 reset | records the failure, runs on into R4 | hands over to the hold |
| R5 reset | records the failure, returns | hands over to the hold |
| `run_all` abort handler | `reset_fn(bitfile)` return value **discarded** | holds if it failed |
| `main()` closing reprogram | `status = status or 1`, then returns | holds *if* buffers are still retained |

The last one is the subtle one: `_discard()` prints a note and carries on when
a *healthy* pipeline's `close()` refuses, so a failed closing reprogram could
also exit over live pages.

## The fix

* `held_objects(pl)` — the pipeline, whatever buffers can still be read off it,
  and `tme_driver._RETAINED_BUFFERS`. Three sources because no single one is
  complete at every call site: after `close()` the pipeline's attributes are
  nulled, and that module-level list is the only place a retained page stays
  reachable.
* `fail_stop(hold_fn, pl, bitfile, why)` — **must not return.** Sets
  `_FAIL_STOP_ENGAGED`, then calls the hold. If an injected hold *does* return,
  it raises `GateError` rather than falling through into the next phase.
* `run_all`'s abort handler returns immediately when the hold is already
  engaged, instead of attempting a second reprogram over a recovery that has
  already been given up on.
* `rep.check`, not `rep.require`, at the two reset sites. `require` **raises**,
  and the `GateError` would unwind into the abort handler and out through
  `main()` — reaching the exit this fix exists to prevent. `check` records the
  same FAIL line and returns, so the hold is reached from where the pages are.

`hold_fn` is injectable so the off-board suite can assert the hold without
hanging; on the board it defaults to `safe_teardown.fail_stop_holding`.

## Proof

`test_board_gate_recovery.py` — **13/13**, up from 10 tests / 9 injected
defects. Added: `reset_fails_late` (the reprogram fails only at the wedge, so
R3 and R5 are covered as separate sites) and three tests —

* `test_a_failed_reprogram_fail_stops_instead_of_exiting`
* `test_a_failed_reprogram_at_the_wedge_also_fail_stops`
* `test_a_hold_that_returns_is_still_refused`

Each asserts the hold was entered, that it was handed a **non-empty** list, and
that no phase ran afterwards (a third pipeline would mean R4 proceeded on a
board whose fabric was never put back).

### Mutation matrix

Run in a sandbox copy of `sw/`, one subprocess per mutant, each mutant applied
to the *fixed* source so it still parses and still imports — a mutant that
failed to build would be "detected" by every test for a reason unrelated to the
property under test, so `ast.parse` and the import are checked first and report
`INVALID` rather than `DETECTED`.

| mutant | verdict | caught by |
|---|---|---|
| `none` (control) | **CLEAN** | — |
| `r3_runs_on` — the pre-fix behaviour at R3 | DETECTED | the R3 test, and the return-guard test |
| `r5_runs_on` — the pre-fix behaviour at R5 | DETECTED | the R5 test |
| `both_run_on` | DETECTED | all three |
| `hold_gets_nothing` — hold entered with an empty list | DETECTED | both hold tests |
| `no_return_guard` — a returning hold falls through | DETECTED | the return-guard test |

The control matters: an earlier attempt ran in a sandbox missing the gate-4 and
`hw` manifest vectors, and **every mutant scored DETECTED via `SetupError`**
while the control failed the same way. Nothing was concluded from that run.

All twelve off-board suites re-run and pass:

    test_board_preflight.py       47 checks    test_board_gate_clock.py     12/12
    test_board_gate_extract.py    14/14        test_board_gate_full_dma.py  13/13
    test_board_gate_protocol.py    6/6         test_gate_signals.py           6/6
    test_board_gate_recovery.py   13/13        test_safe_teardown.py        23/23
    test_cand_packing.py           9/9         test_driver_close.py         12/12
    test_driver_state.py          11/11        test_binarize_dma_checks.py    8/8*

\* pre-existing: it does `from sw import …` with no `sw/__init__.py`, so it
needs `sw` importable as a package. Unrelated to this change.

## What this does NOT establish

* **No silicon.** The fail-stop path is still deliberately not provoked on the
  board: it exists for a board that cannot be reprogrammed, and provoking it
  would leave a board that cannot be reprogrammed.
* **Stage 1 is unaffected but gate 7 is now a different program.**
  `10_gate7_recovery.txt` lines 58 and 91 record `reset_pl returned True` at
  both sites, so the changed branch was never taken and Stage 1 remains PASS —
  but the version that passed on silicon is not the version that would run now.
  Re-run gate 7 before quoting it again.
* The `.as_run` snapshot is untouched and still matches its `PAYLOAD.sha256`
  entry `2a81bcda…4ac7715`; the living tool is now `70c5ebba…61b1d314`.

## Changed files

    sw/board_gate_recovery.py        MOD  held_objects(), fail_stop(),
                                          _FAIL_STOP_ENGAGED, 4 call sites,
                                          require -> check at 2 sites
    sw/test_board_gate_recovery.py   MOD  13 tests, 10 injected defects
    sw/BOARD_RUNBOOK.md              MOD  "If a reprogram fails, this gate will
                                          NOT exit"; test count 10 -> 13
    logs/…/stage1/STAGE1_EVIDENCE.md MOD  post-run change #2 named
    logs/…/stage1/FAILSTOP_FIX.md    NEW  this document
