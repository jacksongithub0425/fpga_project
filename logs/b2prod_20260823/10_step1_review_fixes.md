# 10 — Step 1 closure: the four fail-open holes, the alias chain, the rename

2026-08-23, after review of `06_memory_sampler.md`. The sampler was built
and shaken down; it was **not closed**, because four ways of reporting PASS
on a run that had not passed were still reachable, and one claim rested on a
fake the board never presents.

Suites after: `test_mem_sampler.py` **56/56**, `test_pl_backends.py`
**45/45**.

## 1. `verdict_from_records` could say PASS on a failed run

`teardown_complete` is marked from the runner's `finally`, so a page that
raised **after** `geometry_flushed` produced the entire checkpoint sequence
and a perfectly healthy set of numbers. `summarise()` read neither the
`failed` flag, nor `teardown_status`, nor whether the `/proc` reads had
actually returned anything.

Three rules now block PASS, and the vocabulary grew by two verdicts:

* **`FAIL`** — `failed=True`, or a non-zero PL teardown status. A run that
  ended holding fabric state is a different memory result from one that
  ended clean, and saying `HOLD` for it would invite "reduce memory" when
  the fact is "it broke". The runner writes the outcome into the
  `teardown_complete` **checkpoint** as well as into the trailer, and
  `summarise()` reads whichever survives — the runs that matter most have
  no trailer.
* **`NOT-A-GATE` on absent fields.** `_parse_kb_table` returns only the keys
  it found, so a `/proc/self/status` that opened and parsed to nothing gave
  `{"source": "proc"}` with no numbers, and every rule then read its own
  default — for the swap rule, `0`. A rule that cannot fire is not a rule
  that passed. `REQUIRED_MEM_FIELDS` / `REQUIRED_SYS_FIELDS` are split by
  *why* each is required: `VmSwap`, `SwapTotal`, `SwapFree` because the
  rules read them, `VmRSS` and `VmHWM` because they are the measurement.
  `MemAvailable`, `CmaFree` and `VmData` stay diagnostic and are not
  required, so a kernel that omits one does not fail an evidenced run.
* **`NOT-A-GATE` on a source that changes.** `self._source` was last-wins,
  so a `/proc` read that failed in the middle and worked again afterwards
  left the state saying `"proc"`. Every source seen is now kept and the set
  must be exactly `{"proc"}`.

Precedence, documented in the module: NOT-A-GATE, FAIL, HOLD, MALFORMED,
INCOMPLETE, PASS.

## 2. Membership was the whole sequence test

`missing = [c for c in CHECKPOINTS if c not in seen]` is empty for the
correct sequence, for the **reversed** sequence, and for one with
`page_complete` emitted three times. All three read PASS.

`check_sequence()` now matches the grammar `process_pdf` actually emits:

    pipeline_ready  (per-page block)+  geometry_flushed  teardown_complete

and returns `ok` / `prefix` / `malformed`. The distinction matters: a killed
run leaves a **prefix**, which is what `INCOMPLETE` means and is the OOM
signature. A sequence that is not a prefix is `MALFORMED` and must not
borrow that signature. Splitting the constants into
`PRE_PAGE_CHECKPOINTS` / `PER_PAGE_CHECKPOINTS` / `POST_PAGE_CHECKPOINTS` is
what lets a two-page run be *valid* rather than either wrong or unchecked;
`sequence_pages` counts completed phase blocks, which is a stronger
statement than the page labels the runner sets.

## 3. `peak_is_per_phase` was unreachable

`header()` writes its record and **then** takes the first reset, so the
header's `peak_window` is `"run"` on every run, including one where every
reset succeeded. It was in the set the flag was computed over, so the flag
was always `False` — and `_report` therefore printed *"running peak since
process start"* over a column that was already per-phase, which is the
opposite of what a reader needs.

The flag is now computed over the **checkpoint** rows. `peak_windows` still
carries the union over all records, so the header's row is visible rather
than quietly dropped, and `checkpoint_peak_windows` is reported beside it.
One `"run"` among the checkpoints still makes it `False`: a phase whose
column means the other thing cannot be read as per-phase.

## 4. The full-page CMA grey buffer was missing from the accounting

A `pl-*` process holds **three** page-sized things, not the two the host
arrays name: the host grey page, the CMA grey buffer the MM2S reads, and the
CMA binary buffer the S2MM writes. `page_bin` and `clean_bin` are views of
the binary one, so it reached the record already. **`_gray_buf` is
referenced by nothing the detector holds**, so it was absent from
`distinct_bytes` entirely — 62 MB of a ~290 MiB budget, missing from the
arithmetic while `VmRSS` and `CmaFree` saw it perfectly well.

`PLPipeline.image_buffers()` → `Backend.sampler_arrays()` →
`_backend_arrays()` puts `cma_gray` and `cma_binary` into the array set at
`render_complete`, `preprocess_complete`, `extraction_complete`,
`initial_match_complete` and `page_complete`. They read `null` before the
first `binarize_page()` and not at the start of page 2, because
`_ensure_image_bufs` keeps them — a difference worth being able to see. The
CPU path returns `{}`: "this backend has no such buffer" and "it is not
allocated yet" are different answers.

## 5. The PynqBuffer alias chain, measured rather than modelled

`distinct_bytes` is the number offered as the prediction for RSS, and it is
computed by walking `.base`. Everything asserted about that walk had been
asserted against `FakePL`, which kept a plain owning `np.ndarray` — a case
the board never presents. Measured on the board
(`07_pynq_alias_chain.txt`):

```
cma_gray    PynqBuffer(nbytes=6144)  -> memoryview(nbytes=8192)
cma_binary  PynqBuffer(nbytes=10240) -> memoryview(nbytes=10240)
binary_view ndarray(6144) -> ndarray(6144) -> PynqBuffer(10240)
                                           -> memoryview(10240)
```

Two things follow, and one of them is a bug the fake could not have shown.

**numpy does not stop at the buffer.** The chain runs past the `PynqBuffer`
to the `memoryview` the CMA pages were mapped through. The old `_root_object`
followed it to the end and charged that object — and `cma_gray` proves the
sizes differ: a **6,144 B buffer inside an 8,192 B memoryview**. Page
rounding here; if a PYNQ release ever carves buffers out of one pool
mapping, it would be the size of the pool, and the CMA pool is reserved at
boot whatever a page does. `_root_object` now charges the **innermost
ndarray** — a refcounted allocation boundary, and the thing whose
`freebuffer()` releases the pages — and `describe_arrays` reports the
provider beside the charged size so the case stays visible. The known limit
(`np.frombuffer(b, count=k)` over plain `bytes` reports `k`) is documented;
nothing in this pipeline has that shape.

**The grouping is right.** With the driver's real objects,
`alias_groups == [['cma_binary', 'clean_bin', 'page_bin']]`, `cma_gray` is
its own group, `distinct_bytes` is exactly the two buffers, and the sampler
holds no reference to the view after the record is written.

`FakePL` was rebuilt to present the same shape — `FakeCmaBuffer` is an
ndarray subclass over an `mmap`, `binary_view()` is the driver's expression
including the stride slice, and `image_buffers()` exists — so the off-board
suite now exercises a four-deep chain with a non-ndarray provider instead of
a plain array.

## 6. `match_complete` → `initial_match_complete`

Kept where it was; only the name changed. The mark sits between the
`build_detection` loop and `refine_misaligned_terminal_boxes`, so what it
closes is the **initial** match — on `pl-*`, the pass that runs on the
fabric — and everything after it (refinement, dedupe) is ARM work in every
backend. `match_complete` invited the reading that matching as a whole was
finished there, which is the opposite of the split the checkpoint records.
All three added checkpoints are kept, and `SPECIFIED_CHECKPOINTS` /
`ADDED_CHECKPOINTS` still separate them.

## 6b. A rename must not invalidate last week's records

`06_sampler_offboard_*.jsonl` were written before the rename, so a strict
reader turns them into `MALFORMED` -- a rename quietly invalidating honest
evidence. `LEGACY_CHECKPOINT_NAMES` maps the old name for the READER only:
`check_sequence()` and `summarise()` accept `match_complete`, and `mark()`
still raises on it. The reader understands the old vocabulary, the writer
cannot emit it, and that asymmetry is what stops the alias becoming a
second live name. Confirmed against the real pre-rename file, which now
summarises as `sequence ok over 1 page block(s)` and still reads
`NOT-A-GATE` for the reason it always did.

While there: the verdict strings printed by `_report` are now ASCII. Two
em dashes in the NOT-A-GATE reason arrived as `?` on the board's console
encoding, in the one line a reader is most likely to copy into a record.

## 7. Carried in from the board: the render path

`08_samples_mv.txt` and `09_step2_fitz_1192.md` have the finding. In short:
the detector chose the render path with `hasattr(fitz.Pixmap, "samples_mv")`,
which is **False on PyMuPDF 1.19.2 while every pixmap instance has the
attribute** — so on the board's candidate runtime it would have taken the
`pix.samples` path, a `bytes` copy of **186,126,336 B** per production page.
The guard is now asked of the pixmap; the record carries the path taken and
the class-level answer separately; `mem_sampler.header()` makes a 1×1 pixmap
rather than asking the class.

## What this still does not establish

Nothing about the board's memory. Every off-board record remains
`NOT-A-GATE` by construction. The instrument is now closed against the four
ways it could have said PASS wrongly, and the one number it offers as a
prediction has been checked against the allocator that actually produces it —
but the measurement is not made until a page is rendered on the board under
`/proc`.
