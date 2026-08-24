#!/usr/bin/env python3
"""The corpus's anonymous label space, and the gate that keeps stems out of it.

WHY THIS EXISTS
---------------
The corpus is 35 confidential drawings, 36 pages, and their FILENAMES are
themselves identifying.  Two tools in this repository already said so --
`tme_trace_capture.redact()` ("a source DRAWING FILENAME is itself
identifying") and `tme_full_search_baseline.page_labels()` ("the stems are
drawing filenames and must not appear in any committable output").  Both were
right; neither was ENFORCED.  Eight commits of B2-PROD evidence carried 35
identifiers into 20 tracked files, and the branch was one `git push` from
publishing them.  It was caught first and the commits were rewritten.

This module is the enforcement those two comments assumed.

TWO KINDS OF LEAK, AND WHY ONE REGEX CANNOT FIND BOTH
-----------------------------------------------------
**Structural.** A whole drawing number, in any surface form.  Found by SHAPE,
so `assert_clean()` needs no corpus and works from a bare clone.

**Fragment.** A distinctive PIECE of one -- `stripe_variants.py` once picked
its three sample documents by testing `<six digits of a filename> in p.name`,
which left three such pieces in a tracked file.  The structural regex cannot
see that, and MUST NOT BE WIDENED to try: a shape loose enough to match a bare
six-digit run matches `14-versus-16` in ordinary prose, and a gate that cries
wolf is a gate that gets switched off.  (The values are not written out here
either -- this file passes its own `--check`, both passes.)

So fragments are matched by VALUE, against a list derived from the corpus at
scan time.  That pass needs the corpus and is therefore the one that cannot
run from a clone -- `--check` says so out loud and exits non-zero rather than
reporting a clean scan it did not perform.

TWO LABEL SPACES, AND HOW THEY DIVERGE
--------------------------------------
    doc_001 .. doc_035     one per FILE
    page_001 .. page_036   one per PAGE

One space would be ambiguous.  Exactly one document has two pages, so naming a
document by its first page would emit `{"page": 2, "pdf": "page_001"}`, which
reads as a contradiction.

**The two spaces are NOT interchangeable, and they do not "agree for
single-page documents".**  `doc_001` is the two-page document and covers
`page_001` and `page_002`; every document after it is offset by one, so
`doc_002` is `page_003`, `doc_003` is `page_004`, and so on to `doc_035` /
`page_036`.  All 34 documents after the first diverge.  Never convert between
the spaces by arithmetic -- `--map` is the crosswalk, and it is LOCAL ONLY
because it is the mapping the labels exist to hide.

THE ORDERING RULE
-----------------
Sorted by `name.lower()`, numbered from 1, pages within a document in reading
order.  Deliberately the rule `page_labels()` already used, which sorts
`*_trials.jsonl` basenames the same way: the trace stems and the corpus files
carry the same names, so the two label spaces coincide -- and that coincidence
is CHECKED by `--check-trace`, not assumed.  A label depends on nothing but
which files are in the corpus directory: no stored map, nothing to keep in
sync, nothing secret to lose.

DIAGNOSTICS DO NOT REPEAT THE SECRET
------------------------------------
A gate that prints what it found writes the identifier into whatever captured
its output -- a CI log, an evidence file, a terminal transcript.  So a finding
names the input by a SCRUBBED path when that path verifies clean after
scrubbing, and by ORDINAL when it does not -- a path ends in a filename, and a
filename can be the identifier.  Plus a count and offsets, never the matched
text.  No digest either: a hash of a secret is a token for the secret, and the
labels already give a stable name.

Printing a scrubbed-and-rechecked path is what keeps a finding ACTIONABLE.
"Something under logs/ leaks" is not something anyone can act on, and a gate
nobody can act on gets bypassed.

USING IT
--------
    import corpus_labels as CL

    pdf = CL.resolve("doc_002", SAMPLES)     # a real path, for a real run
    lab = CL.labels(SAMPLES).page(name, 1)   # a label, for anything written
    CL.write_json_checked(path, rec)         # refuses to write a leak

    python corpus_labels.py --check FILE...  # both passes; non-zero on a leak
    python corpus_labels.py --check --structural-only FILE...
    python corpus_labels.py --map            # LOCAL ONLY: the crosswalk
"""

from __future__ import annotations

import argparse
import codecs
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
#: matched `14-versus-16` in docs/pl_interface_contract.md.  Do not loosen it
#: to chase fragments; that is what `fragments()` is for.
IDENTIFIER_RE = re.compile(
    r"(?<![0-9A-Za-z_-])"
    r"[0-9]{2,3}-(?:[A-Za-z][0-9]{5}|[0-9]{5,6})-[0-9]{2,3}"
    r"(?:_[0-9A-Za-z]+)*"
    r"(?![0-9A-Za-z-])"
)

#: A reference AS WRITTEN: the number, then optionally a copy marker, an
#: extension, and a page.  The trailing groups must be consumed BY THIS MATCH
#: -- if ` p1` were left behind, `<id>.PDF p1` would scrub to `doc_003 p1`,
#: naming a page `doc_003` may not have.  The whitespace classes are `[ \t]`
#: and not `\s` deliberately: `\s` crosses newlines, and a document named at
#: the end of one line must not absorb a `p3` opening the next.
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

#: A fragment is distinctive enough to match by value only if it is this long
#: and carries this many digits.  The trailing group of a drawing number
#: (`001`, `124`) fails both and is deliberately not a fragment: it occurs in
#: ordinary text constantly.
_FRAGMENT_MIN_LEN = 5
_FRAGMENT_MIN_DIGITS = 4


class CorpusUnavailable(RuntimeError):
    """Scrubbing or a fragment scan was asked for with no corpus to map against."""


class UnsupportedEncoding(RuntimeError):
    """A file declared an encoding and then violated it; refuse to guess."""


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

    Document labels and fragments are built eagerly from filenames alone.
    PAGE labels need each document's page count, so they are built on first
    use and only then -- a test that wants Stage 2's file pays nothing for
    the other 34.
    """

    def __init__(self, corpus_dir: Optional[Path] = None):
        self.corpus_dir = (Path(corpus_dir) if corpus_dir is not None
                           else default_corpus_dir())
        self.paths = documents(self.corpus_dir)
        self.numbers: List[str] = []
        self.doc_label: Dict[str, str] = {}
        self.label_path: Dict[str, Path] = {}
        self.label_page: Dict[str, int] = {}
        self.page_label: Dict[Tuple[str, int], str] = {}
        self.npages: Dict[str, int] = {}
        self._by_key: Dict[str, str] = {}
        self._pages_built = False

        for i, p in enumerate(self.paths, 1):
            num = self.number_of(p.name)
            if num is None:
                # BY POSITION, NOT BY NAME.  This message is printed by
                # callers -- the pre-push hook among them -- and a filename
                # in it is the identifier the whole module exists to keep
                # out of transcripts.  The position is enough to find the
                # file locally, where the directory listing is right there.
                raise RuntimeError(
                    "corpus file #%d of %d does not carry a drawing number; "
                    "every file in the corpus directory must match "
                    "IDENTIFIER_RE, or the label space would silently skip "
                    "it. Listed in name order; look at it locally."
                    % (i, len(self.paths)))
            k = self.key(num)
            if k in self._by_key:
                # Positions again, and NOT the key: a lookup key is the
                # drawing number up to its first underscore, which is most
                # of the identifier.
                first = self.numbers.index(self._by_key[k]) + 1
                raise RuntimeError(
                    "corpus documents #%d and #%d fold to the same lookup "
                    "key. Labels.key() truncates at the first underscore, "
                    "which is unambiguous only while the truncated numbers "
                    "stay distinct -- run --map locally to see which two."
                    % (first, i))
            dlab = "doc_{:03d}".format(i)
            self.numbers.append(num)
            self._by_key[k] = num
            self.doc_label[num] = dlab
            self.label_path[dlab] = p

        self.fragments = self._build_fragments()

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
        drawing fields.  All of those must reach the same document, and
        truncation is the only rule that folds the CORPUS FILENAME and the
        REFERENCE identically without a table of which suffix is which.

        Safe only while the truncated numbers stay distinct, so `__init__`
        checks exactly that instead of trusting it.
        """
        return number.lower().split("_", 1)[0]

    def _build_fragments(self) -> List[str]:
        """The distinctive pieces of the corpus numbers, lower-cased.

        The MIDDLE group of each drawing number, which is what a person
        reaches for when they want to name one document in a hurry.  Leading
        and trailing groups are two or three characters and occur constantly
        in ordinary text, so they are not fragments -- and a corpus whose
        middles were that short would make this pass useless, which is why
        the length and digit rules RAISE rather than silently dropping.
        """
        out = []
        for num in self.numbers:
            parts = num.split("_", 1)[0].split("-")
            if len(parts) < 3:
                raise RuntimeError(
                    "corpus document #%d: its drawing number does not have "
                    "three dash-separated groups, so the distinctive middle "
                    "cannot be taken from it."
                    % (self.numbers.index(num) + 1))
            mid = parts[1]
            digits = sum(c.isdigit() for c in mid)
            if len(mid) < _FRAGMENT_MIN_LEN or digits < _FRAGMENT_MIN_DIGITS:
                raise RuntimeError(
                    "the middle group of a corpus drawing number is not "
                    "distinctive enough to match by value (needs >= %d chars "
                    "and >= %d digits). Matching it would flag ordinary text; "
                    "the fragment pass must be rethought rather than run."
                    % (_FRAGMENT_MIN_LEN, _FRAGMENT_MIN_DIGITS))
            low = mid.lower()
            if low not in out:
                out.append(low)
        return sorted(out)

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

    def label_of_path(self, path) -> Optional[str]:
        """`doc_NNN` if this path is a corpus document, else None."""
        return self.doc(Path(path).name)


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
# decoding
# ---------------------------------------------------------------------------
def views(data: bytes) -> List[Tuple[str, str, str]]:
    """EVERY plausible textual reading of these bytes: (encoding, text, basis).

    Not one reading -- all of them, de-duplicated by the hits they produce.

    WHY NOT A SINGLE DECODING.  Picking one makes that choice a
    security boundary, and it has a hole: a file that is mostly CJK with one
    ASCII drawing number in it scores below any "does this look like text"
    threshold, falls through to `binary`, and its UTF-16 identifier is then
    NUL-split and invisible in the raw bytes.  Mixed-script UTF-16 without a
    BOM is a real evasion, not a hypothetical one.

    So the score now chooses the LABEL and never decides whether to look.  The
    raw `bytes` view is always included, because an ASCII identifier inside a
    bitstream is still a leak; the UTF-16 views are included whenever they
    decode at all, because that is the only reading in which a wide-character
    identifier is contiguous.

    A violated BOM still raises: a file that declares an encoding and then
    breaks it is the one case where there is nothing safe to assume.
    """
    out: List[Tuple[str, str, str]] = []
    seen = set()

    def add(enc: str, text: str, basis: str) -> None:
        if text and text not in seen:
            seen.add(text)
            out.append((enc, text, basis))

    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"),
                     (codecs.BOM_UTF32_LE, "utf-32-le"),
                     (codecs.BOM_UTF32_BE, "utf-32-be"),
                     (codecs.BOM_UTF16_LE, "utf-16-le"),
                     (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            try:
                add(enc, data[len(bom):].decode(enc), "character")
            except UnicodeDecodeError as exc:
                raise UnsupportedEncoding(
                    "file starts with a %s BOM and then does not decode as "
                    "%s (%s)" % (enc, enc, exc.reason)) from None
            break

    # A NUL byte means this is not ordinary text, so the RAW view goes first
    # and wins the de-duplication -- reporting an embedded ASCII identifier as
    # a "cp1252 character offset" would name a reading nobody applies to a
    # bitstream.  The UTF-16 views still run and still report, because their
    # hits are at different offsets and so survive the dedup on their own.
    if b"\x00" in data:
        add("bytes", data.decode("latin-1"), "byte")

    for enc in ("utf-8", "cp1252"):
        try:
            add(enc, data.decode(enc), "character")
        except (UnicodeDecodeError, LookupError):
            pass

    # UTF-16 at BOTH byte alignments, each truncated to a MAXIMAL EVEN SLICE.
    #
    # Decoding the whole buffer once assumes the stream starts at byte 0 and
    # has even length. Neither is guaranteed. One leading byte -- a stray
    # separator, a concatenation, a header -- pairs every byte with the wrong
    # neighbour, and the identifier is not in the result. One TRAILING byte is
    # worse: `decode` raises on truncated data and the view is dropped
    # entirely, so the scanner looks at a UTF-16 payload and reports nothing.
    # Both were reproduced against real corpus values before this was written.
    #
    # Offsets are relative to the slice, which is why the alignment is part of
    # the label: `utf-16-le@1` means "skip one byte, then decode".
    for off in (0, 1):
        chunk = data[off:]
        chunk = chunk[:len(chunk) - (len(chunk) % 2)]
        if not chunk:
            continue
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                add("%s@%d" % (enc, off), chunk.decode(enc), "character")
            except (UnicodeDecodeError, LookupError):
                pass

    add("bytes", data.decode("latin-1"), "byte")
    return out


def scan_bytes(data: bytes, frags: Optional[Sequence[str]] = None
               ) -> List[Tuple[str, str, List[Hit]]]:
    """Scan every view; return (encoding, basis, hits) for the ones that hit.

    Views whose hits are identical collapse to one entry -- a pure-ASCII file
    reads the same through UTF-8 and through raw bytes, and reporting it twice
    would train the reader to skim.
    """
    found: List[Tuple[str, str, List[Hit]]] = []
    signatures = set()
    for enc, text, basis in views(data):
        hits = scan(text, frags)
        if not hits:
            continue
        sig = tuple((h.kind, h.offset) for h in hits)
        if sig in signatures:
            continue
        signatures.add(sig)
        found.append((enc, basis, hits))
    return found


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
class Hit:
    """One finding.  Carries an offset, never the matched text."""

    __slots__ = ("kind", "offset")

    def __init__(self, kind: str, offset: int):
        self.kind = kind
        self.offset = offset

    def __repr__(self):
        return "Hit(%r, %d)" % (self.kind, self.offset)


def find_identifiers(text: str) -> List[str]:
    """Every structurally drawing-shaped token, in order, duplicates kept.

    Returns the matched text, so it is for callers that already hold the
    secret (a producer scrubbing its own output). The GATE uses `scan()`,
    which returns offsets only.
    """
    return [m.group(0) for m in IDENTIFIER_RE.finditer(text)]


def scan(text: str, frags: Optional[Sequence[str]] = None) -> List[Hit]:
    """Both passes over one string, offsets only.

    `frags` omitted runs the structural pass alone -- the caller is telling
    us there is no corpus, and the CLI is what refuses to call that a clean
    scan.
    """
    hits = [Hit("structural", m.start()) for m in IDENTIFIER_RE.finditer(text)]
    if frags:
        low = text.lower()
        for f in frags:
            start = low.find(f)
            while start != -1:
                hits.append(Hit("fragment", start))
                start = low.find(f, start + 1)
    hits.sort(key=lambda h: (h.offset, h.kind))
    return hits


def assert_clean(text: str, where: str = "<text>",
                 frags: Optional[Sequence[str]] = None) -> None:
    """Raise if `text` carries an identifier or a corpus fragment.

    The message counts and locates; it does not quote.  `where` is the
    caller's own description and is printed verbatim, so a caller that passes
    a path has chosen to print that path.
    """
    hits = scan(text, frags)
    if hits:
        by_kind: Dict[str, List[int]] = {}
        for h in hits:
            by_kind.setdefault(h.kind, []).append(h.offset)
        parts = ", ".join(
            "%d %s at %s" % (len(v), k, ",".join(str(o) for o in v[:8]))
            for k, v in sorted(by_kind.items()))
        raise RuntimeError(
            "%s carries corpus identifiers: %s.\n"
            "Committable output names pages by label. Put the value through "
            "corpus_labels.scrub() before writing it." % (where, parts))


def write_text_checked(path, text: str, encoding: str = "utf-8") -> None:
    """`Path.write_text` that refuses to write a leak."""
    frags = None
    try:
        frags = labels().fragments
    except Exception:
        pass
    assert_clean(text, "<output>", frags)
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
def safe_name(path, frags: Optional[Sequence[str]] = None,
              lb: Optional[Labels] = None) -> Optional[str]:
    """A path fit to PRINT, or None if there isn't one.

    Scrubbed, then re-scanned: a repository path carries no identifier and
    comes back unchanged, a corpus filename comes back as its label, and
    anything still dirty after both returns None so the caller falls back to
    the ordinal.  Scrubbing is not trusted to have worked -- it is checked.

    This is what keeps a diagnostic USABLE.  "Something under logs/ leaks" is
    not an actionable message, and a gate nobody can act on gets bypassed.
    """
    s = str(path)
    if lb is not None:
        try:
            s = scrub(s, lb.corpus_dir)
        except CorpusUnavailable:
            return None
    return s if not scan(s, frags) else None


def check_inputs(paths: Sequence[str], frags: Optional[Sequence[str]],
                 lb: Optional[Labels], out=None) -> int:
    """Scan files and their pathnames.  Returns the number of BAD inputs.

    An input is bad if it leaks, is missing, is unreadable, or declares an
    encoding it then violates.  Every one of those is a non-zero outcome:
    "I could not look" and "I looked and it was clean" are different answers
    and a gate must not print the second when it means the first.
    """
    out = out if out is not None else sys.stdout
    bad = 0

    def name(i: int, p: str) -> str:
        safe = safe_name(p, frags, lb)
        return "input #%d (%s)" % (i, safe) if safe else \
               "input #%d (pathname withheld)" % i

    def report(who: str, where: str, hits: List[Hit], basis: str) -> None:
        by_kind: Dict[str, List[int]] = {}
        for h in hits:
            by_kind.setdefault(h.kind, []).append(h.offset)
        for kind, offs in sorted(by_kind.items()):
            shown = ",".join(str(o) for o in offs[:12])
            more = "" if len(offs) <= 12 else ",... "
            print("LEAK  %s: %s, %s, %d occurrence(s), %s offset(s) %s%s"
                  % (who, where, kind, len(offs), basis, shown, more), file=out)

    for i, p in enumerate(paths, 1):
        who = name(i, p)

        # The pathname itself.  A directory component or a filename can BE
        # the identifier, which is why this is scanned and never printed.
        path_hits = scan(str(p), frags)
        if path_hits:
            bad += 1
            report(who, "pathname", path_hits, "character")

        try:
            data = Path(p).read_bytes()
        except FileNotFoundError:
            bad += 1
            print("MISSING  %s" % who, file=out)
            continue
        except OSError as exc:
            bad += 1
            print("UNREADABLE  %s: %s" % (who, exc.__class__.__name__), file=out)
            continue

        try:
            found = scan_bytes(data, frags)
        except UnsupportedEncoding as exc:
            bad += 1
            print("UNSUPPORTED  %s: %s" % (who, exc), file=out)
            continue

        if found:
            bad += 1
            for enc, basis, hits in found:
                report(who, "content (%s)" % enc, hits, basis)

    return bad


def _cmd_check(paths: Sequence[str], corpus_dir: Optional[str],
               structural_only: bool) -> int:
    frags: Optional[Sequence[str]] = None
    lb: Optional[Labels] = None
    if structural_only:
        print("STRUCTURAL ONLY: the fragment pass was NOT performed. A clean "
              "result here does not mean the inputs carry no piece of a "
              "corpus filename.")
    else:
        try:
            lb = labels(corpus_dir)
        except Exception as exc:                             # noqa: BLE001
            # The TYPE, never the message.  A corpus-construction failure
            # names the files that caused it, and this text goes wherever
            # the caller's output goes.
            print("CANNOT SCAN: the corpus could not be read (%s). Run "
                  "`--map` locally to see what it says; that output is "
                  "local-only by design."
                  % type(exc).__name__)
            return 2
        if not lb.paths:
            print("CANNOT SCAN: no corpus at %s, so the fragment pass cannot "
                  "run. Set %s, or pass --structural-only and accept that the "
                  "result is partial." % (lb.corpus_dir, CORPUS_ENV))
            return 2
        frags = lb.fragments

    bad = check_inputs(paths, frags, lb)
    if bad:
        print("\n%d of %d input(s) failed. Offsets index the input; the "
              "ordinal is its position in the argument list."
              % (bad, len(paths)))
        return 1
    scope = "structural" if structural_only else "structural + fragment"
    print("clean: %d input(s), %s, no corpus identifiers"
          % (len(paths), scope))
    return 0


def _cmd_map(corpus_dir: Optional[str]) -> int:
    lb = labels(corpus_dir)
    if not lb.paths:
        print("no corpus at %s" % lb.corpus_dir)
        return 1
    lb._build_pages()
    print("# LOCAL ONLY -- this mapping is the thing the labels hide.")
    print("# corpus: %s" % lb.corpus_dir)
    print("# doc_NNN and page_NNN are NOT interchangeable: doc_001 spans two")
    print("# pages, so every later document is offset by one. This is the")
    print("# crosswalk; there is no arithmetic that replaces it.")
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
        if lb.page(stem, page) != expect:
            bad += 1
            print("MISMATCH at capture position %d: expected %s" % (i, expect))
    if bad:
        print("\n%d label(s) disagree." % bad)
        return 1
    print("%d page labels agree between the corpus and the capture directory"
          % len(files))
    return 0


def _cmd_fragments(corpus_dir: Optional[str]) -> int:
    """How many fragments the value pass carries -- never which."""
    lb = labels(corpus_dir)
    if not lb.paths:
        print("no corpus at %s" % lb.corpus_dir)
        return 1
    lens = sorted({len(f) for f in lb.fragments})
    print("%d distinctive fragment(s) from %d document(s); length(s) %s"
          % (len(lb.fragments), len(lb.paths),
             ",".join(str(x) for x in lens)))
    print("The values are not printed: they are pieces of the filenames the "
          "labels exist to hide. `--map` is the local crosswalk.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default=None,
                    help="corpus directory (default: $%s, else sample/ beside "
                         "the repository)" % CORPUS_ENV)
    ap.add_argument("--structural-only", action="store_true",
                    help="skip the fragment pass and say so; the only way to "
                         "get a zero exit without a corpus")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", nargs="+", metavar="PATH",
                   help="exit non-zero if any input leaks, is missing, is "
                        "unreadable, or violates its declared encoding")
    g.add_argument("--map", action="store_true",
                   help="print the doc/page/number crosswalk (LOCAL ONLY)")
    g.add_argument("--check-trace", metavar="DIR",
                   help="check these labels against page_labels()'s")
    g.add_argument("--fragments", action="store_true",
                   help="how many fragments the value pass carries")
    args = ap.parse_args(argv)

    if args.check:
        return _cmd_check(args.check, args.corpus, args.structural_only)
    if args.map:
        return _cmd_map(args.corpus)
    if args.fragments:
        return _cmd_fragments(args.corpus)
    return _cmd_check_trace(args.check_trace, args.corpus)


if __name__ == "__main__":
    sys.exit(main())
