#!/usr/bin/env python3
"""The privacy gate's own tests: does it find leaks, and does it stay quiet?

TWO OBLIGATIONS, AND THE SECOND IS THE EASY ONE TO FORGET
---------------------------------------------------------
A gate has to FIND the thing.  It also has to not REPEAT it: whatever prints
the finding -- a CI log, a terminal transcript, an evidence file -- becomes a
new copy of the identifier the gate exists to suppress.  Every detection test
here is paired with an assertion that the diagnostics contain none of the
planted values, and `test_the_diagnostics_never_repeat_a_planted_value` does
it across every case at once.

WHY THE PLANTED VALUES ARE NOT WRITTEN DOWN
-------------------------------------------
They are read from the corpus at run time.  A test file that spelled out a
drawing number to test for drawing numbers would be the leak it is testing
for -- and this file has to pass `corpus_labels.py --check` like everything
else.  With no corpus on the machine these tests SKIP, which is the ordinary
state of a clone.
"""

from __future__ import annotations

import codecs
import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import corpus_labels as CL

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent.parent / "sample"


class SkipTest(Exception):
    pass


def _labels():
    try:
        lb = CL.labels(SAMPLES)
    except Exception as exc:                                 # noqa: BLE001
        raise SkipTest("corpus unusable: %s" % exc)
    if not lb.paths:
        raise SkipTest("no corpus under %s" % SAMPLES)
    return lb


def planted():
    """(full, suffix_less, fragment) taken from the real corpus.

    `full` is a whole drawing number with its revision; `suffix_less` drops
    everything from the first underscore, which is the form that made the
    audit's file count 19 instead of 20; `fragment` is the distinctive middle
    that the structural regex cannot see.
    """
    lb = _labels()
    full = lb.numbers[0]
    suffix_less = full.split("_", 1)[0]
    fragment = lb.fragments[0]
    return full, suffix_less, fragment, lb


def run_check(args, corpus=True):
    """Run the CLI, capture stdout, return (exit_code, text)."""
    buf = io.StringIO()
    argv = ["--corpus", str(SAMPLES)] if corpus else []
    with redirect_stdout(buf):
        rc = CL.main(argv + args)
    return rc, buf.getvalue()


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def test_a_full_identifier_in_content_is_found():
    full, _, _, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "a.txt", ("see %s here\n" % full).encode("utf-8"))
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "structural" in out, out
    assert full not in out, "the diagnostic repeated the identifier"


def test_a_suffix_less_identifier_is_found():
    """The form that made the first audit count 19 files instead of 20."""
    _, suffix_less, _, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "b.md",
                  ("Stage 2 (`%s`)\n" % suffix_less).encode("utf-8"))
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "structural" in out, out
    assert suffix_less not in out, "the diagnostic repeated the identifier"


def test_a_bare_fragment_is_found():
    """No dashes, no prefix -- invisible to the structural pass by design."""
    _, _, fragment, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "c.py",
                  ('sample = [x for x in xs if "%s" in x.name]\n'
                   % fragment).encode("utf-8"))
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "fragment" in out, out
    assert fragment not in out, "the diagnostic repeated the fragment"


def test_a_fragment_is_invisible_to_the_structural_pass_alone():
    """The reason the fragment pass exists, stated as a test.

    If this ever fails, somebody widened IDENTIFIER_RE -- which is what
    starts matching ordinary prose.
    """
    _, _, fragment, _ = planted()
    assert CL.find_identifiers("x %s y" % fragment) == []
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "d.py", ("%s\n" % fragment).encode("utf-8"))
        # `--structural-only` goes BEFORE `--check`: `--check` takes nargs="+"
        # and would otherwise swallow nothing and error out.
        rc, out = run_check(["--structural-only", "--check", str(p)])
    assert rc == 0, (rc, out)
    assert "NOT performed" in out, "a partial scan reported itself as clean"


def test_an_identifier_in_the_pathname_is_found():
    """A filename can BE the identifier; the content need not carry it."""
    full, _, _, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "%s.txt" % full, b"nothing interesting inside\n")
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "pathname" in out, out
    assert full not in out, "the diagnostic repeated the pathname"


def test_a_fragment_in_a_directory_component_is_found():
    """And the pathname is WITHHELD, because scrub() cannot label a fragment.

    `scrub()` maps whole references; a bare fragment has nothing to map, so
    the scrubbed path is still dirty, `safe_name()` refuses it, and the
    finding falls back to the ordinal.  That fallback is the reason
    `safe_name` re-scans instead of trusting the scrub.
    """
    _, _, fragment, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        sub = Path(d) / ("run_%s" % fragment)
        sub.mkdir()
        p = write(sub, "e.txt", b"clean content\n")
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "pathname" in out, out
    assert "pathname withheld" in out, out
    assert fragment not in out


def test_utf16_with_a_bom_is_decoded_not_replaced():
    """The case `errors="replace"` would have turned into mojibake and missed."""
    full, _, _, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "f.txt", ("head %s tail" % full).encode("utf-16"))
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "utf-16" in out, out
    assert full not in out


def test_utf16_without_a_bom_is_decoded_either_way_round():
    """Both endiannesses, because byte-swapped ASCII lands in CJK.

    A printability test cannot tell the two apart -- CJK "looks like text" --
    so the wrong one wins by accident unless the choice is scored on ASCII.
    """
    full, _, _, _ = planted()
    for i, enc in enumerate(("utf-16-le", "utf-16-be")):
        with tempfile.TemporaryDirectory() as d:
            p = write(Path(d), "g%d.txt" % i,
                      ("head %s tail" % full).encode(enc))
            rc, out = run_check(["--check", str(p)])
        assert rc == 1, (enc, rc, out)
        assert enc in out, (enc, out)
        assert full not in out


def test_cp1252_content_is_decoded():
    """This project's Windows transcripts carry a 0x97 that is not UTF-8."""
    full, _, _, _ = planted()
    body = b"PS-side \x97 not cycles\n" + full.encode("ascii") + b"\n"
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "h.txt", body)
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "cp1252" in out, out
    assert full not in out


def test_an_ascii_identifier_inside_binary_is_found():
    full, _, _, _ = planted()
    body = b"\x00\x01\x02" + full.encode("ascii") + b"\x00\xff\xfe\x00"
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "i.bin", body)
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "byte offset" in out, out
    assert full not in out


# ---------------------------------------------------------------------------
# "I could not look" is not "it was clean"
# ---------------------------------------------------------------------------
def test_a_missing_input_is_non_zero():
    with tempfile.TemporaryDirectory() as d:
        rc, out = run_check(["--check", str(Path(d) / "nope.txt")])
    assert rc == 1, (rc, out)
    assert "MISSING" in out, out


def test_an_unreadable_input_is_non_zero():
    """A directory stands in for any OSError the reader can raise."""
    with tempfile.TemporaryDirectory() as d:
        sub = Path(d) / "adir"
        sub.mkdir()
        rc, out = run_check(["--check", str(sub)])
    assert rc == 1, (rc, out)
    assert "UNREADABLE" in out, out


def test_a_violated_bom_is_unsupported_not_guessed():
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "j.txt", codecs.BOM_UTF8 + b"\xff\xfe\xff\xfe")
        rc, out = run_check(["--check", str(p)])
    assert rc == 1, (rc, out)
    assert "UNSUPPORTED" in out, out


def test_no_corpus_refuses_rather_than_reporting_clean():
    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty"
        empty.mkdir()
        p = write(Path(d), "k.txt", b"clean\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = CL.main(["--corpus", str(empty), "--check", str(p)])
        out = buf.getvalue()
    assert rc == 2, (rc, out)
    assert "CANNOT SCAN" in out, out


# ---------------------------------------------------------------------------
# quiet on clean input
# ---------------------------------------------------------------------------
def test_a_clean_file_passes_both_passes():
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "l.txt",
                  b"page_004 doc_003 and nothing identifying\n")
        rc, out = run_check(["--check", str(p)])
    assert rc == 0, (rc, out)
    assert "clean" in out


def test_ordinary_prose_is_not_flagged():
    """The `14-versus-16` regression: a loose middle group matched English."""
    body = (b"Closing this section did not close 6.3 (the 14-versus-16-byte "
            b"result record) and the 32-versus-64-bit question.\n")
    with tempfile.TemporaryDirectory() as d:
        p = write(Path(d), "m.md", body)
        rc, out = run_check(["--check", str(p)])
    assert rc == 0, (rc, out)


# ---------------------------------------------------------------------------
# the label spaces
# ---------------------------------------------------------------------------
def test_the_two_label_spaces_diverge_after_the_first_document():
    """`doc_NNN` and `page_NNN` are NOT interchangeable.

    The module once claimed they agreed for single-page documents. They do
    not: `doc_001` spans two pages, so every later document is offset by one
    and all 34 of them diverge.
    """
    lb = _labels()
    lb._build_pages()
    agree = diverge = 0
    for i, num in enumerate(lb.numbers, 1):
        pages = [lab for (n, _), lab in lb.page_label.items() if n == num]
        if "doc_{:03d}".format(i) == pages[0].replace("page_", "doc_"):
            agree += 1
        else:
            diverge += 1
    assert agree == 1, ("only the first document should line up", agree)
    assert diverge == len(lb.numbers) - 1, diverge


def test_a_document_label_and_its_page_label_resolve_to_one_file():
    lb = _labels()
    lb._build_pages()
    for i, num in enumerate(lb.numbers, 1):
        dlab = "doc_{:03d}".format(i)
        pages = [lab for (n, _), lab in lb.page_label.items() if n == num]
        assert CL.resolve(dlab, SAMPLES) == CL.resolve(pages[0], SAMPLES)


def test_page_count_matches_the_pinned_corpus_shape():
    lb = _labels()
    lb._build_pages()
    assert len(lb.paths) == 35, len(lb.paths)
    assert len(lb.label_page) == 36, len(lb.label_page)


# ---------------------------------------------------------------------------
# the obligation that is easy to forget
# ---------------------------------------------------------------------------
def test_the_diagnostics_never_repeat_a_planted_value():
    """Every shape at once, against every planted value at once.

    Individually the tests above already assert this; doing it in one place
    means a NEW detection path cannot be added without this failing unless it
    is also quiet.
    """
    full, suffix_less, fragment, lb = planted()
    secrets = [full, suffix_less, fragment, full.lower(), full.upper()]

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        inputs = [
            write(tmp, "n1.txt", ("%s\n" % full).encode("utf-8")),
            write(tmp, "n2.txt", ("%s\n" % suffix_less).encode("utf-8")),
            write(tmp, "n3.txt", ("%s\n" % fragment).encode("utf-8")),
            write(tmp, "%s.txt" % full, b"clean\n"),
            write(tmp, "n5.txt", ("%s" % full).encode("utf-16")),
            write(tmp, "n6.bin", b"\x00" + full.encode() + b"\xff\x00"),
        ]
        rc, out = run_check(["--check"] + [str(p) for p in inputs])

    assert rc == 1, (rc, out)
    for s in secrets:
        assert s not in out, "the diagnostics repeated a planted value"
    # The property in its strongest form: THE DIAGNOSTICS THEMSELVES PASS THE
    # GATE. A path is printed only after being scrubbed and re-scanned, so a
    # clean one is expected -- that is what makes a finding actionable -- and
    # anything still dirty falls back to the ordinal. Scanning the output is
    # the check that covers both without enumerating cases.
    assert CL.scan(out, lb.fragments) == [], \
        "the diagnostics would not survive their own gate"
    assert out.count("LEAK") >= 6, out


def test_a_head_only_scan_misses_what_a_range_scan_catches():
    """The claim the pre-push hook rests on, as an executable test.

    A synthetic two-commit history: the first commit plants a fragment, the
    second removes it.  The TIP is clean.  The push is not -- both commits
    travel -- and that is the whole reason the hook scans a commit range
    rather than a working tree.
    """
    import subprocess
    _, _, fragment, _ = planted()
    scanner = HERE / "pre_push_scan.py"
    git_env = dict(os.environ)
    git_env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        def g(*a):
            r = subprocess.run(["git", "-C", str(repo)] + list(a),
                               capture_output=True, env=git_env)
            assert r.returncode == 0, r.stderr.decode("utf-8", "replace")

        g("init", "-q")
        (repo / "f.py").write_text('pick = "%s"\n' % fragment, encoding="utf-8")
        g("add", "f.py")
        g("commit", "-q", "-m", "plant")
        (repo / "f.py").write_text('pick = "doc_002"\n', encoding="utf-8")
        g("add", "f.py")
        g("commit", "-q", "-m", "remove")

        # The tip alone: clean.
        rc, out = run_check(["--check", str(repo / "f.py")])
        assert rc == 0, ("the tip should be clean", out)

        # The range: not clean.  `HEAD` and not `HEAD~1..HEAD` -- a root
        # commit has no parent, and a NEW ref publishes its whole ancestry,
        # which is the case the hook actually has to get right.
        r = subprocess.run(
            [sys.executable, str(scanner), "--corpus", str(SAMPLES),
             "--range", "HEAD"],
            capture_output=True, cwd=str(repo), env=git_env)
        text = r.stdout.decode("utf-8", "replace")
        assert r.returncode == 1, (r.returncode, text,
                                   r.stderr.decode("utf-8", "replace"))
        assert "PUSH REFUSED" in text, text
        assert fragment not in text, "the scanner repeated the fragment"


def test_assert_clean_reports_without_quoting():
    full, _, fragment, lb = planted()
    for value in (full, fragment):
        try:
            CL.assert_clean("x %s y" % value, "<unit>", lb.fragments)
        except RuntimeError as exc:
            assert value not in str(exc), "assert_clean quoted the value"
        else:
            raise AssertionError("assert_clean missed %r-shaped input"
                                 % ("identifier" if value is full
                                    else "fragment"))


def test_write_json_checked_refuses_a_fragment():
    _, _, fragment, _ = planted()
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "rec.json"
        try:
            CL.write_json_checked(out, {"note": "picked by %s" % fragment})
        except RuntimeError as exc:
            assert fragment not in str(exc)
            assert not out.exists(), "the file was written before the check"
        else:
            raise AssertionError("write_json_checked wrote a fragment")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for t in tests:
        try:
            t()
        except SkipTest as e:
            print(f"skip {t.__name__}: {e}")
            skipped += 1
        except Exception as e:                               # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed"
          + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
