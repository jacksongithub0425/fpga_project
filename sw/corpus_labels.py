#!/usr/bin/env python3
"""The corpus's anonymous label space, and the gate that keeps stems out of it.

WHY THIS EXISTS
---------------
The corpus is 35 confidential drawings, 36 pages, and their FILENAMES are
themselves identifying.  Two tools in this repository already said so --
`tme_trace_capture.redact()` ("a source DRAWING FILENAME is itself
identifying -- reducing a path to its basename does not redact it") and
`tme_full_search_baseline.page_labels()` ("the stems are drawing filenames
and must not appear in any committable output").  Both were right; neither
was ENFORCED.  So eight commits of B2-PROD evidence carried 35 of those
identifiers into 20 tracked files, and the branch was one `git push` from
publishing them.  It was caught first and the eight commits were rewritten.

This module is the enforcement the two comments assumed.  The rule it
implements is not new -- it is the existing rule, given an implementation.

TWO LABEL SPACES, AND WHY NOT ONE
---------------------------------
    doc_001 .. doc_035     one per FILE
    page_001 .. page_036   one per PAGE

One space would be ambiguous.  Exactly one document has two pages, so a
"name the document by its first page" rule would emit `{"page": 2, "pdf":
"page_001"}` -- which reads as a contradiction.  Keeping the spaces apart
costs one lookup table and buys records that cannot be misread.  Both are
ordered by the SAME rule, so `doc_NNN` and `page_NNN` agree wherever a
document has one page, and `--map` prints the correspondence.

THE ORDERING RULE
-----------------
Sorted by `name.lower()`, numbered from 1, pages within a document in reading
order.  This is deliberately the rule `page_labels()` already used, which
sorts `*_trials.jsonl` basenames the same way: the trace stems and the corpus
files carry the same names, so the two label spaces coincide -- and that
coincidence is CHECKED, by `--check-trace`, rather than assumed.  A label
therefore depends on nothing but which files are in the corpus directory: no
stored map, nothing to keep in sync, and nothing secret to lose.

WHAT IS AND IS NOT CORPUS-DEPENDENT
-----------------------------------
`scrub()` needs the corpus: it cannot map an identifier to a label without
knowing the file order.  With no corpus it RAISES rather than passing text
through -- a scrubber that silently no-ops on an unconfigured machine would
make the guarantee depend on the machine.

`assert_clean()` does NOT need the corpus.  It matches the STRUCTURE of a
drawing number, so it works from a bare clone, in CI, and on a machine that
has never seen the corpus.  That is what makes it usable as a gate: the check
that must never fail open is the one with no dependencies.  Its regex is
generic (`<digits>-<alnum>-<digits>`) rather than a list of known prefixes,
so the gate does not itself record the numbering scheme.

Page counts need `fitz`, and are read LAZILY -- `resolve()` and the doc
labels do not open a single PDF.  A test that only needs Stage 2's file pays
nothing for the other 34.

USING IT
--------
    import corpus_labels as CL

    pdf = CL.resolve("doc_002", SAMPLES)     # a real path, for a real run
    rec = CL.scrub_obj(rec)                  # labels in, stems out
    CL.write_json_checked(path, rec)         # refuses to write a leak

    python corpus_labels.py --check FILE...  # the gate; non-zero on a leak
    python corpus_labels.py --map            # LOCAL ONLY: label -> number
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: Override the corpus location without editing anything.
CORPUS_ENV = "TME_CORPUS_DIR"

#: The structural detector.  Corpus-independent BY DESIGN -- see the module
#: docstring.  It matches a drawing number in every surface form the audit
#: found.  Writing `N` for a digit and `A` for an alphanumeric, those are
#: `NNN-AAAAAA-NNN_R` (with a revision), `NNN-AAAAAA-NNN` (without one), the
#: same lower-cased, `NNN-AAAAAA-NNN_R_pN` (carrying a capture page), and
#: `NN-NNNNNN-NN_NN_N` (the one document whose number has extra
#: underscore-separated fields).  The examples are written in that notation
#: and not spelled out, so THIS FILE passes its own `--check`; a gate whose
#: source has to be exempted from it is not a gate.
#:
#: The middle group must CARRY DIGITS -- five or six of them, optionally
#: behind one letter.  An earlier version allowed any alphanumerics there and
#: matched `14-versus-16` in docs/pl_interface_contract.md, which is the
#: failure mode that makes people switch a gate off.  Generic enough not to
#: record the numbering scheme, specific enough not to cry wolf.
IDENTIFIER_RE = re.compile(
    r"(?<![0-9A-Za-z_-])"
    r"[0-9]{2,3}-(?:[A-Za-z][0-9]{5}|[0-9]{5,6})-[0-9]{2,3}"
    r"(?:_[0-9A-Za-z]+)*"
    r"(?![0-9A-Za-z-])"
)

#: A reference AS WRITTEN: the number, then optionally a copy marker, an
#: extension, and a page.  The trailing groups must be consumed BY THIS MATCH
#: -- if ` p1` were left behind, `<id>.PDF p1` would scrub to `doc_003 p1`,
#: which names a page that `doc_003` may not have.  The whitespace classes are
#: `[ \t]` and not `\s` deliberately: `\s` crosses newlines, and a document
#: named at the end of one line must not absorb a `p3` opening the next.
_REFERENCE_RE = re.compile(
    r"(?<![0-9A-Za-z_-])"
    r"(?P<num>[0-9]{2,3}-(?:[A-Za-z][0-9]{5}|[0-9]{5,6})-[0-9]{2,3}"
    r"(?:_[0-9A-Za-z]+)*)"
    r"(?P<copy>[ \t]*\(\d+\))?"
    r"(?P<ext>\.[Pp][Dd][Ff])?"
    r"(?P<page>[ \t]*#?[ \t]*[Pp](?P<pageno>\d+))?"
    r"(?![0-9A-Za-z-])"
)

_PDF_SUFFIXES = (".pdf",)


class CorpusUnavailable(RuntimeError):
    """Scrubbing was asked for with no corpus to map identifiers against."""


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------
def default_corpus_dir() -> Path:
    """`sample/` beside the repository, or wherever CORPUS_ENV points."""
    override = os.environ.get(CORPUS_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "sample"


def documents(corpus_dir: Optional[Path] = None) -> List[Path]:
    """Every corpus PDF, in label order.

    Sorted by `name.lower()`.  The extension case varies across the corpus
    (`.PDF` and `.pdf` both occur); a case-sensitive sort would group by
    extension first, making a file's label depend on how it happened to be
    named rather than on what it is.
    """
    d = Path(corpus_dir) if corpus_dir is not None else default_corpus_dir()
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in _PDF_SUFFIXES),
                  key=lambda p: p.name.lower())


class Labels:
    """The label tables for one corpus directory.

    Document labels are built eagerly from filenames alone.  PAGE labels need
    each document's page count, so they are built on first use and only then.
    """

    def __init__(self, corpus_dir: Optional[Path] = None):
        self.corpus_dir = (Path(corpus_dir) if corpus_dir is not None
                           else default_corpus_dir())
        self.paths = documents(self.corpus_dir)
        self.numbers: List[str] = []             # in label order
        self.doc_label: Dict[str, str] = {}      # number -> doc_NNN
        self.label_path: Dict[str, Path] = {}    # doc_NNN / page_NNN -> Path
        self.label_page: Dict[str, int] = {}     # page_NNN -> 1-based page
        self.page_label: Dict[Tuple[str, int], str] = {}
        self.npages: Dict[str, int] = {}
        self._by_key: Dict[str, str] = {}        # lookup key -> number
        self._pages_built = False

        for i, p in enumerate(self.paths, 1):
            num = self.number_of(p.name)
            if num is None:
                raise RuntimeError(
                    "corpus file does not carry a drawing number: %s\n"
                    "Every file in %s must match IDENTIFIER_RE, or the label "
                    "space would silently skip it."
                    % (p.name, self.corpus_dir))
            k = self.key(num)
            if k in self._by_key:
                raise RuntimeError(
                    "two corpus documents fold to the same lookup key %r: %s "
                    "and %s.\nLabels.key() truncates at the first underscore, "
                    "which is unambiguous only while the truncated numbers "
                    "stay distinct." % (k, self._by_key[k], num))
            dlab = "doc_{:03d}".format(i)
            self.numbers.append(num)
            self._by_key[k] = num
            self.doc_label[num] = dlab
            self.label_path[dlab] = p

    # -- extraction ---------------------------------------------------------
    @staticmethod
    def number_of(text: str) -> Optional[str]:
        """The drawing number inside a filename or reference, or None."""
        m = IDENTIFIER_RE.search(text)
        return m.group(0) if m else None

    @staticmethod
    def key(number: str) -> str:
        """The lookup key: case-folded, truncated at the first underscore.

        Everything after the first `_` is a suffix somebody may or may not
        have written -- a revision (`_A`), a capture page (`_p0`), or further
        drawing fields (`_NN_N` on one of them).  All of those must reach the
        same document, and truncation is the only rule that folds the CORPUS
        FILENAME and the REFERENCE identically without a table of which
        suffix is which.

        Safe only while the truncated numbers stay distinct, so `__init__`
        checks exactly that instead of trusting it.
        """
        return number.lower().split("_", 1)[0]

    # -- page labels, built on demand ---------------------------------------
    def _build_pages(self) -> None:
        if self._pages_built:
            return
        import fitz
        page_no = 0
        for num, p in zip(self.numbers, self.paths):
            with fitz.open(str(p)) as doc:
                n = doc.page_count
            self.npages[num] = n
            for k in range(1, n + 1):
                page_no += 1
                plab = "page_{:03d}".format(page_no)
                self.page_label[(num, k)] = plab
                self.label_path[plab] = p
                self.label_page[plab] = k
        self._pages_built = True

    # -- lookups ------------------------------------------------------------
    def doc(self, ref: str) -> Optional[str]:
        num = self.number_of(ref)
        if not num:
            return None
        real = self._by_key.get(self.key(num))
        return self.doc_label.get(real) if real else None

    def page(self, ref: str, page_no: int) -> Optional[str]:
        num = self.number_of(ref)
        if not num:
            return None
        real = self._by_key.get(self.key(num))
        if not real:
            return None
        self._build_pages()
        return self.page_label.get((real, page_no))

    def path_for(self, label: str) -> Optional[Path]:
        if label.startswith("page_"):
            self._build_pages()
        return self.label_path.get(label)


_CACHE: Dict[str, Labels] = {}


def labels(corpus_dir: Optional[Path] = None) -> Labels:
    d = str(Path(corpus_dir) if corpus_dir is not None
            else default_corpus_dir())
    if d not in _CACHE:
        _CACHE[d] = Labels(d)
    return _CACHE[d]


def resolve(label: str, corpus_dir: Optional[Path] = None) -> Optional[Path]:
    """`doc_002` or `page_003` -> the real PDF path, or None if absent.

    None rather than an exception: every caller is a test or a producer that
    must SKIP cleanly when the corpus is not on this machine, which is the
    normal state of a clone.  `doc_*` resolves without opening any PDF.
    """
    try:
        p = labels(corpus_dir).path_for(label)
    except Exception:
        return None
    return p if p is not None and p.exists() else None


def resolve_page(label: str, corpus_dir: Optional[Path] = None
                 ) -> Optional[Tuple[Path, int]]:
    """`page_004` -> (path, 1-based page within that document), or None."""
    try:
        lb = labels(corpus_dir)
        p = lb.path_for(label)
    except Exception:
        return None
    if p is None or not p.exists():
        return None
    return p, lb.label_page.get(label, 1)


# ---------------------------------------------------------------------------
# scrubbing -- corpus REQUIRED, fails closed
# ---------------------------------------------------------------------------
def scrub(text: str, corpus_dir: Optional[Path] = None) -> str:
    """Replace every corpus drawing reference with its label.

    A reference carrying a page becomes `page_NNN`; one without becomes
    `doc_NNN`.  A drawing-shaped token that is NOT a corpus document is left
    alone -- this maps identities, it does not censor text -- and
    `assert_clean()` is what refuses to write the result if one survives.

    Raises `CorpusUnavailable` rather than returning `text` unchanged when
    there is nothing to map against and there is something to map.
    """
    lb = labels(corpus_dir)
    if not lb.paths:
        if not IDENTIFIER_RE.search(text):
            return text
        raise CorpusUnavailable(
            "text carries drawing identifiers but no corpus is at %s, so they "
            "cannot be mapped to labels. Set %s to the corpus directory."
            % (lb.corpus_dir, CORPUS_ENV))

    def sub(m: "re.Match[str]") -> str:
        num = m.group("num")
        dlab = lb.doc(num)
        if dlab is None:
            return m.group(0)
        if m.group("pageno"):
            plab = lb.page(num, int(m.group("pageno")))
            if plab:
                return plab
        return dlab

    return _REFERENCE_RE.sub(sub, text)


def scrub_obj(obj, corpus_dir: Optional[Path] = None):
    """`scrub()` over every string in a JSON-shaped structure, keys included."""
    if isinstance(obj, str):
        return scrub(obj, corpus_dir)
    if isinstance(obj, dict):
        return {scrub_obj(k, corpus_dir): scrub_obj(v, corpus_dir)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v, corpus_dir) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub_obj(v, corpus_dir) for v in obj)
    return obj


# ---------------------------------------------------------------------------
# the gate -- corpus NOT required, never fails open
# ---------------------------------------------------------------------------
def find_identifiers(text: str) -> List[str]:
    """Every structurally drawing-shaped token, in order, duplicates kept."""
    return [m.group(0) for m in IDENTIFIER_RE.finditer(text)]


def assert_clean(text: str, where: str = "<text>") -> None:
    """Raise if `text` carries anything shaped like a drawing number."""
    hits = find_identifiers(text)
    if hits:
        uniq = sorted(set(hits))
        raise RuntimeError(
            "%s carries %d drawing identifier(s), %d distinct: %s\n"
            "Committable output names pages by label. Put the value through "
            "corpus_labels.scrub() before writing it."
            % (where, len(hits), len(uniq), ", ".join(uniq[:8])))


def write_text_checked(path, text: str, encoding: str = "utf-8") -> None:
    """`Path.write_text` that refuses to write a leak."""
    assert_clean(text, str(path))
    Path(path).write_text(text, encoding=encoding)


def write_json_checked(path, obj, encoding: str = "utf-8", **kw) -> None:
    """`json.dump` to a file: scrubbed where possible, checked always."""
    try:
        obj = scrub_obj(obj)
    except CorpusUnavailable:
        pass                    # assert_clean below still refuses a leak
    kw.setdefault("indent", 2)
    write_text_checked(path, json.dumps(obj, **kw), encoding=encoding)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_check(paths: Sequence[str]) -> int:
    bad = 0
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError) as exc:
            print("SKIP  %s (%s)" % (p, exc))
            continue
        hits = find_identifiers(text)
        if hits:
            bad += 1
            uniq = sorted(set(hits))
            print("LEAK  %s: %d occurrence(s), %d distinct"
                  % (p, len(hits), len(uniq)))
            for u in uniq[:10]:
                print("        %s" % u)
    if bad:
        print("\n%d file(s) carry drawing identifiers." % bad)
        return 1
    print("clean: %d file(s), no drawing identifiers" % len(paths))
    return 0


def _cmd_map(corpus_dir: Optional[str]) -> int:
    lb = labels(corpus_dir)
    if not lb.paths:
        print("no corpus at %s" % lb.corpus_dir)
        return 1
    lb._build_pages()
    print("# LOCAL ONLY -- this mapping is the thing the labels hide.")
    print("# corpus: %s" % lb.corpus_dir)
    for num in lb.numbers:
        pages = [lab for (n, _), lab in lb.page_label.items() if n == num]
        print("%-9s %-22s %s" % (lb.doc_label[num], ",".join(pages), num))
    return 0


def _cmd_check_trace(trace_dir: str, corpus_dir: Optional[str]) -> int:
    """Prove the corpus-derived page labels equal `page_labels()`'s.

    Two independent derivations of one numbering -- one from the corpus
    directory, one from the capture's JSONL filenames.  A disagreement would
    mean `page_007` in an evidence file and `page_007` in a trace roll-up are
    different pages, which is the kind of error anonymisation makes silent.
    """
    import glob as globmod
    files = sorted(globmod.glob(os.path.join(trace_dir, "*_trials.jsonl")),
                   key=lambda p: os.path.basename(p).lower())
    if not files:
        print("no *_trials.jsonl under %s" % trace_dir)
        return 1

    lb = labels(corpus_dir)
    if not lb.paths:
        print("no corpus at %s" % lb.corpus_dir)
        return 1

    bad = 0
    for i, f in enumerate(files, 1):
        stem = os.path.basename(f)[: -len("_trials.jsonl")]
        expect = "page_{:03d}".format(i)
        m = re.search(r"_p(\d+)$", stem)
        page = int(m.group(1)) + 1 if m else 1
        mine = lb.page(stem, page)
        if mine != expect:
            bad += 1
            print("MISMATCH %s: trace says %s, corpus says %s"
                  % (expect, expect, mine))
    if bad:
        print("\n%d label(s) disagree." % bad)
        return 1
    print("%d page labels agree between the corpus and %s"
          % (len(files), trace_dir))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default=None,
                    help="corpus directory (default: $%s, else sample/ beside "
                         "the repository)" % CORPUS_ENV)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", nargs="+", metavar="PATH",
                   help="exit non-zero if any file carries a drawing "
                        "identifier; needs no corpus")
    g.add_argument("--map", action="store_true",
                   help="print label -> drawing number (LOCAL ONLY)")
    g.add_argument("--check-trace", metavar="DIR",
                   help="check these labels against page_labels()'s")
    args = ap.parse_args(argv)

    if args.check:
        return _cmd_check(args.check)
    if args.map:
        return _cmd_map(args.corpus)
    return _cmd_check_trace(args.check_trace, args.corpus)


if __name__ == "__main__":
    sys.exit(main())
