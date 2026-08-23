# Anonymisation of the B2-PROD evidence, 2026-08-23

## What was found

An audit of the branch before its first push found **35 confidential drawing
identifiers in 20 tracked files**, across the eight `B2-PROD` commits. The
identifiers were the corpus filenames: the source documents' drawing numbers,
in prose, in fixed-width transcripts, in JSON records, and hard-coded as paths
in six Python sources.

`logs/b2prod_20260822/corpus_cpu_oracle_gap.json` carried more than names. Its
`first_diff` fields held **58 exact detection records across 29 pages** — an
id, a class, a score and a pixel box per record, each one a feature read off a
drawing.

This contradicted the repository's own stated standard, in two places that
predate the B2-PROD work:

* `sw/tme_trace_capture.py`, `redact()` — "a source DRAWING FILENAME is itself
  identifying — reducing a path to its basename does not redact it";
* `sw/tme_full_search_baseline.py`, `page_labels()` — "the stems are drawing
  filenames and must not appear in any committable output".

Both were right. Neither was enforced by anything.

## Why this is a decision and not an incident

Nothing had left the machine. At the moment of the finding:

* all 33 local commits were absent from every remote branch and tag;
* the pushed history contained **zero** identifiers;
* no PDFs, pin-label or word payloads were tracked.

The remote — `jacksongithub0425/FPGA_Accelerator` — is **public**, which is
what made the choice consequential rather than cosmetic.

## The choice

Two defensible options, and the one thing that was not defensible was pushing
without choosing.

**Scope the rule down** — declare the identifiers acceptable in this mirror
and apply anonymisation only to aggregate or public outputs. Cheap: no
rewriting. But it requires weakening the two comments quoted above *and* the
frozen-detections rule in `sw/cpu_baseline_snapshot.py` ("the
`*_detections.json` dumps stay local — they carry labels read off the
drawings"). That is a larger change to the project's standards than the leak
is to its history.

**Enforce the rule** — redact and rewrite, because a redaction commit stacked
on top still pushes the old blobs in history.

**Option 2 was chosen.**

## What the rewrite cost, and what it preserved

The rewrite was smaller than first estimated: only the final **eight** commits
(`470c423..b823bd6` in the pre-rewrite numbering) contained identifiers. The
preceding 25 unpushed commits were verified clean and were not touched.

Preserved:

* the eight-commit ordering, unchanged;
* **preregistration immediately before the result** — the protocol-v2
  registration remains the parent of the run that tested it, so the claim
  "registered before it ran" is still proved by the graph and not by a date in
  a file;
* byte-identical blobs for the renderer, the preregistration document, the
  board transcript and every `.as_run` snapshot;
* every digest in `PAYLOAD.sha256`, `03_script_identity.txt` and the older
  `MANIFEST.sha256` sets — none of the 20 redacted files was digest-pinned,
  which was checked rather than assumed;
* an ordinary fast-forward push afterwards. No force-push is required.

Cost: **the eight commit IDs changed.** Citations to the old IDs were
retargeted inside the rewritten history itself. The preregistration proof is
relational and survives: unchanged preregistration content is still an
ancestor of the unchanged result transcript.

## What was changed, file by file

**Labels replace names.** Two anonymous spaces, both ordered by the corpus's
own sorted filenames:

    doc_001 .. doc_035     one per file
    page_001 .. page_036   one per page

`page_NNN` is deliberately the numbering `page_labels()` already produced, so
a label in a B2-PROD record and a label in a trace roll-up mean the same page.
`sw/corpus_labels.py --check-trace` proves it rather than asserting it.

| what | files | change |
|---|---|---|
| fixed-width transcripts | 7 `.txt` in `b2prod_20260822` | names → labels, columns held |
| prose | the plan, `STAGE1_EVIDENCE.md`, `AUDIT_CORRECTIONS.md`, `06_memory_sampler.md`, `BOARD_RUNBOOK.md` | names → labels |
| structured records | `corpus_cpu_oracle_gap.json`, `corpus_parity_offboard.json` | names → labels; `first_diff` withheld |
| producers | `tme_backend_parity.py`, `test_pl_backends.py`, `test_mem_sampler.py`, `otsu_exactness.py`, `stage3_cycles.py`, `stage3_refine.py` | resolve the file from a label at runtime |
| policy | `EVIDENCE_ALLOWLIST.txt` | the rule, recorded next to the allowlist |

**The withheld detections.** In `corpus_cpu_oracle_gap.json`, `first_diff`
becomes the string `"REDACTED"` where a difference existed and stays `null`
where none did — so "withheld" and "identical" remain distinguishable, which a
blanket `null` would have destroyed. Everything that made the file an argument
survives verbatim: `loc_mismatch`, `kind_mismatch`, `score_over_tol`,
`max_abs_score_delta`, the class counts and the candidate counts. The rung's
conclusion is unchanged and still checkable.

**The six producers now resolve a label to a path at runtime** rather than
carrying a filename. They keep working on this machine and skip cleanly
without the corpus, which is what a clone sees. Three of them
(`otsu_exactness.py`, `stage3_cycles.py`, `stage3_refine.py`) live under
`logs/` as evidence of how their numbers were made: **they are no longer
byte-identical to the scripts that ran.** The change is the input path and
nothing else — no arithmetic, no constant, no output format — and no digest
pinned them. Recorded here because "the script as run" is a claim this file
would otherwise quietly weaken.

## The correction that came with it

`EVIDENCE_ALLOWLIST.txt` described the excluded
`logs/b2prod_20260821/gate[AB]/*.stdout` files as "byte-duplicates of the
.log". They are not byte-identical to the `.log` files; they are the same
run's output through a second capture. The line now says **semantically
redundant** and says what it used to claim.

## What stops this recurring

`sw/corpus_labels.py`:

    python sw/corpus_labels.py --check <paths>

Exit non-zero if any file carries something shaped like a drawing number. The
gate is **corpus-independent by construction** — it matches the structure of a
drawing number, not a list of known ones — so it runs from a bare clone, in
CI, and on a machine that has never held the drawings. A check that needs the
secret in order to detect the secret fails open exactly where it matters, and
this one does not.

The producers call `scrub()` on the way in and `assert_clean()` on the way
out, so a new evidence file cannot acquire a stem without the tool refusing to
write it.
