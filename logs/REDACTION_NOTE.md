# Anonymisation of the B2-PROD evidence, 2026-08-23

## What was found

An audit of the branch before its first push found **35 distinct confidential
drawing identifiers in 20 tracked files**, across the eight `B2-PROD` commits.
The identifiers were the corpus filenames: the source documents' drawing
numbers, in prose, in fixed-width transcripts, in JSON records, and hard-coded
as paths in six Python sources.

**352 structural occurrences**: 348 full tokens plus four written without the
revision suffix — two in the plan, one in `STAGE1_EVIDENCE.md`, one in
`AUDIT_CORRECTIONS.md`. The suffix-less form is why a grep for exact stems
returns 19 files and the real answer is 20; it is also why the gate matches a
SHAPE and not a list.

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

**The two spaces are not interchangeable.** An earlier version of this note
and of `corpus_labels.py` said they "agree wherever a document has one page".
That is false. `doc_001` is the two-page document and covers `page_001` and
`page_002`, so every document after it is offset by one: `doc_002` is
`page_003`, and so on to `doc_035` / `page_036`. **All 34 documents after the
first diverge** — the agreement holds for exactly one of the 35, the one that
does not need it. There is no arithmetic that converts between the spaces;
`corpus_labels.py --map` is the crosswalk, and it is local-only because that
mapping is the thing the labels hide.

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

Exit non-zero if any file carries something shaped like a drawing number. That
pass is **corpus-independent by construction** — it matches the structure of a
drawing number, not a list of known ones — so it runs from a bare clone, in
CI, and on a machine that has never held the drawings.

It is not sufficient on its own, which the second finding below establishes: a
FRAGMENT of a filename has no structure to match, and a second pass matching
by value against the local corpus was added for it. That pass does need the
corpus, and says so rather than reporting a scan it did not run.

The producers call `scrub()` on the way in and `assert_clean()` on the way
out, so a new evidence file cannot acquire a stem without the tool refusing to
write it.

---

# Second finding, same day: fragments, and a five-commit suffix rewrite

## What was found

Three **six-digit fragments** of corpus filenames, in
`logs/b2prod_20260822/stripe_variants.py`, which picked its three sample
documents by testing whether each filename contained one of them.

The structural gate could not see them, and it was right not to: a bare
six-digit run is not shaped like a drawing number, and widening the shape to
catch one matches `14-versus-16` in ordinary prose. That false positive
already happened once during the first redaction, in a file present in every
commit including the pushed base — which is exactly how a gate stops being
run.

A fragment is not less identifying than the whole number. The corpus is 35
documents; six digits pick one out of it outright.

## Why the tip was not enough

The fragments entered at the fourth of the eight rewritten commits and were
still there at the tip, so a scan of HEAD would have found them. The general
case is worse: had a later commit rewritten that line, HEAD would have been
clean and the push would still have published the fragment-bearing blob. **A
push publishes history.** Fixing HEAD is not fixing the push, which is the
same lesson as the first finding and the reason the gate now runs over a
commit range.

## What was done

The five-commit suffix was rewritten — the fragment-bearing commit and its
four descendants. The three preceding B2-PROD commits and the 25 before them
were untouched.

| was | now | | was | now |
|---|---|---|---|---|
| `fe3e621` | `9e3afda` | | `1589ee9` | `d3557f2` |
| `22e6b5b` | `b22b370` | | `5246cab` | `18dec35` |
| `6a28cda` | `8cd2b34` | | | |

Exactly two paths differ across the rewrite: `stripe_variants.py`, which now
selects its three documents by label (`doc_002`, `doc_003`, `doc_035`), and
`12_protocol_v2_RESULT.md`, whose preregistration citation was retargeted.
Preregistration remains the parent of the result. Messages, authorship and
dates are byte-identical. The push is still a plain fast-forward.

## The recurrence work, in a separate commit

Deliberately not folded into a rewritten commit: a rewrite should contain the
redaction and nothing else, or the history stops being a record of what
happened.

* **A second detection pass, by VALUE.** `corpus_labels.fragments()` derives
  the distinctive middle group of each drawing number from the local corpus at
  scan time. Nothing is hardcoded and the structural regex is unchanged. The
  fragment pass needs the corpus, so `--check` exits non-zero and says which
  pass it could not run rather than reporting a scan it did not perform.
  `--structural-only` is the explicit, loud opt-out.
* **Diagnostics that survive their own gate.** A finding names its input by
  scrubbed path when that path verifies clean, and by ordinal when it does
  not; it reports counts and offsets, never the matched text, never a raw
  path, and never a digest — a hash of a secret is a token for the secret, and
  the labels already give a stable name. The gate's own source passes both
  passes: the examples in it are written `NNN-AAAAAA-NNN`, not spelled out.
* **Encodings, explicitly.** BOM first, then UTF-16 **before** UTF-8 when NUL
  bytes are present — UTF-8 accepts NUL, so BOM-less UTF-16 of ASCII decodes
  "successfully" into a string whose identifiers are split by NULs and
  invisible. Endianness is chosen by ASCII score, because byte-swapped ASCII
  lands in CJK and passes any printability test. Then CP1252 (this project's
  Windows transcripts carry a 0x97 em-dash), then `binary`, whose raw bytes are
  still scanned. Nothing is decoded with `errors="replace"`. Missing,
  unreadable and encoding-violating inputs are non-zero outcomes: "I could not
  look" and "I looked and it was clean" are different answers.
* **Pathnames are scanned**, both file and directory components.
* **Every evidence producer emits labels** — `gray_parity.py`,
  `otsu_corpus.py`, `render_parity.py`, `stripe_proto.py`,
  `stripe_variants.py`, plus the three already converted. They now take their
  corpus ordering from `CL.documents()` rather than each re-deriving it, so
  the iteration order and the label numbering cannot drift apart. Re-running
  them reproduces the committed transcripts byte-for-byte.
* **`tme_backend_parity.py` has two serialisations.** `--json` writes the
  PUBLIC record: labels, `first_diff` aggregated to `"REDACTED"` plus a count,
  the inline rung-C mismatch list replaced by its count, every aggregate
  verbatim. `--private-json` writes the full record with diagnostic geometry
  and requires a name ending in `.private.json` — the anonymisation gate
  cannot catch that file, because its boxes are already label-keyed and
  nothing in it is shaped like a drawing number, so the name is the only
  guard and it is made a precondition.
* **A pre-push hook**, `.githooks/pre-push` → `sw/pre_push_scan.py`. It reads
  the refs git puts on stdin, works out the commit range for each (a new ref
  publishes its whole unpushed ancestry, so `--not --remotes`, not
  `HEAD~1..HEAD`), and scans tree contents, pathnames and commit messages,
  de-duplicated by object id. Enable with `git config core.hooksPath
  .githooks`; hooks are not cloned.
* **23 synthetic tests**, `sw/test_corpus_labels.py`. Full, suffix-less,
  fragment, pathname, directory component, UTF-16 both ways, CP1252, binary,
  missing, unreadable, violated-BOM, no-corpus, and the `14-versus-16`
  false-positive regression. Every planted value is read from the corpus at
  run time, so the test file itself carries none. One test builds a synthetic
  two-commit history where the tip is clean and the range is not, which is the
  claim the hook rests on. And every detection test asserts the diagnostics
  contain none of the planted values — `CL.scan()` over the tool's own output
  must come back empty.

## What is still not guarded

`.private.json` is kept out by naming and by this note, not by `.gitignore`:
the working `.gitignore` carries unrelated uncommitted Priority 7A edits, and
adding a line to it would have swept those into this commit. Add
`*.private.json` to it when that work lands.
