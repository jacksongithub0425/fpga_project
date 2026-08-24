#!/usr/bin/env python3
"""Scan everything a push would publish, and refuse the push if it leaks.

WHY A HEAD-ONLY SCAN IS NOT ENOUGH
-----------------------------------
`git push` publishes HISTORY, not a snapshot.  The B2-PROD leak was found at
HEAD and redacted at HEAD -- and the blobs were still in the eight commits
behind it, where a push would have carried them out just the same.  Twice: the
second time it was three six-digit fragments introduced four commits back,
invisible at the tip because a later commit had already replaced that line.

So the unit of scanning is the COMMIT RANGE, and within it three things, all
of which travel:

    tree contents   the blobs
    pathnames       a filename can BE the identifier
    commit messages nobody redacts a commit message

WHAT IT READS
-------------
git hands a pre-push hook one line per ref on stdin:

    <local ref> <local sha> <remote ref> <remote sha>

A zero `local sha` is a deletion and publishes nothing.  A zero `remote sha`
is a NEW ref, and the range is then everything not already on some remote --
`--not --remotes`, not `HEAD~1..HEAD`, because a new branch carries its whole
unpushed ancestry with it.

FAILING CLOSED
--------------
The fragment pass needs the corpus.  With no corpus this exits non-zero and
says which pass it could not run, rather than printing a clean result it did
not earn.  `TME_ALLOW_STRUCTURAL_ONLY=1` overrides that, loudly, and is for a
machine that genuinely has no corpus -- not for getting past a finding.

    python sw/pre_push_scan.py            # reads stdin, for the hook
    python sw/pre_push_scan.py --range A..B   # the same scan, by hand
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_labels as CL  # noqa: E402

ZERO = "0" * 40


def git(*args: str) -> bytes:
    r = subprocess.run(["git"] + list(args), capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git %s failed:\n%s"
                         % (" ".join(args),
                            r.stderr.decode("utf-8", "replace")))
    return r.stdout


def git_text(*args: str) -> str:
    return git(*args).decode("utf-8", "replace")


def ranges_from_stdin(lines):
    """Commit lists, one per pushed ref."""
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == ZERO:
            continue                       # deleting a ref publishes nothing
        if remote_sha == ZERO:
            revs = git_text("rev-list", local_sha, "--not", "--remotes")
            how = "new ref, everything not already on a remote"
        else:
            revs = git_text("rev-list", "%s..%s" % (remote_sha, local_sha))
            how = "%s..%s" % (remote_sha[:7], local_sha[:7])
        out.append((local_ref, how, revs.split()))
    return out


def collect(commits):
    """Unique blobs, unique pathnames and messages across a commit list.

    De-duplicated by object id: a 33-commit range over 461 files is 15,000
    blob reads done naively and about 500 done once each, and the answer is
    identical because a blob's content does not depend on which commit
    carries it.
    """
    blobs = {}          # sha -> first path seen (for the report only)
    paths = set()
    messages = []
    for c in commits:
        for entry in git_text("ls-tree", "-r", "-z", c).split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            _mode, typ, sha = meta.split(" ", 2)
            paths.add(path)
            if typ == "blob":
                blobs.setdefault(sha, path)
        raw = git("cat-file", "commit", c)
        messages.append((c, raw.split(b"\n\n", 1)[1]))
    return blobs, paths, messages


def scan_all(blobs, paths, messages, frags, lb, out=sys.stdout) -> int:
    bad = 0

    for i, path in enumerate(sorted(paths), 1):
        hits = CL.scan(path, frags)
        if hits:
            bad += 1
            safe = CL.safe_name(path, frags, lb)
            who = safe if safe else "pathname #%d (withheld)" % i
            print("LEAK  pathname: %s, %d occurrence(s)" % (who, len(hits)),
                  file=out)

    for i, (sha, path) in enumerate(sorted(blobs.items()), 1):
        data = git("cat-file", "blob", sha)
        try:
            text, enc = CL.decode(data)
        except CL.UnsupportedEncoding as exc:
            bad += 1
            safe = CL.safe_name(path, frags, lb)
            print("UNSUPPORTED  blob in %s: %s"
                  % (safe or "blob #%d" % i, exc), file=out)
            continue
        if enc == "binary":
            hits = CL.scan(data.decode("latin-1"), frags)
            basis = "byte"
        else:
            hits = CL.scan(text, frags)
            basis = "character"
        if hits:
            bad += 1
            safe = CL.safe_name(path, frags, lb)
            who = safe if safe else "blob #%d (path withheld)" % i
            kinds = sorted({h.kind for h in hits})
            offs = ",".join(str(h.offset) for h in hits[:12])
            print("LEAK  content: %s (%s), %s, %d occurrence(s), %s offset(s) %s"
                  % (who, enc, "+".join(kinds), len(hits), basis, offs),
                  file=out)

    for c, msg in messages:
        hits = CL.scan(msg.decode("utf-8", "replace"), frags)
        if hits:
            bad += 1
            print("LEAK  commit message: %s, %d occurrence(s), offset(s) %s"
                  % (c[:12], len(hits),
                     ",".join(str(h.offset) for h in hits[:12])), file=out)

    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--range", metavar="A..B",
                    help="scan this range instead of reading stdin")
    ap.add_argument("--corpus", default=None)
    args = ap.parse_args(argv)

    structural_only = os.environ.get("TME_ALLOW_STRUCTURAL_ONLY") == "1"
    frags = None
    lb = None
    if structural_only:
        print("pre-push: STRUCTURAL ONLY (TME_ALLOW_STRUCTURAL_ONLY=1). The "
              "fragment pass did NOT run; a clean result here does not mean "
              "no piece of a corpus filename is being published.")
    else:
        try:
            lb = CL.labels(args.corpus)
        except Exception as exc:                             # noqa: BLE001
            print("pre-push REFUSED: the corpus is unusable (%s), so the "
                  "fragment pass cannot run." % exc)
            return 1
        if not lb.paths:
            print("pre-push REFUSED: no corpus at %s, so the fragment pass "
                  "cannot run. Set %s, or set TME_ALLOW_STRUCTURAL_ONLY=1 and "
                  "accept a partial scan." % (lb.corpus_dir, CL.CORPUS_ENV))
            return 1
        frags = lb.fragments

    if args.range:
        work = [("(--range)", args.range,
                 git_text("rev-list", args.range).split())]
    else:
        work = ranges_from_stdin(sys.stdin.read().splitlines())

    if not work:
        print("pre-push: nothing to scan.")
        return 0

    total_bad = 0
    for ref, how, commits in work:
        if not commits:
            print("pre-push: %s -- 0 commit(s) to publish." % ref)
            continue
        blobs, paths, messages = collect(commits)
        print("pre-push: %s -- %d commit(s) [%s], %d unique blob(s), "
              "%d unique path(s)"
              % (ref, len(commits), how, len(blobs), len(paths)))
        total_bad += scan_all(blobs, paths, messages, frags, lb)

    if total_bad:
        print("\nPUSH REFUSED: %d finding(s). The offending content is in the "
              "COMMITS, not just at HEAD -- redacting the tip does not remove "
              "it. Rewrite the range." % total_bad)
        return 1

    scope = "structural" if structural_only else "structural + fragment"
    print("pre-push: clean (%s), nothing published carries a corpus "
          "identifier." % scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
