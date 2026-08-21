#!/usr/bin/env python3
"""Bind the Priority 4 (B1) evidence to digests, and verify it later.

    python tme_b1_manifest.py --write     # create/refresh MANIFEST.sha256
    python tme_b1_manifest.py --verify    # exit 1 on any drift
    python tme_b1_manifest.py --mirror    # copy the evidence into the git tree

WHY THIS EXISTS.  Everything else in Priority 4 is reproducible -- the vectors
regenerate byte-identically from pinned seeds, the cycle model re-proves its own
figures, the adjudicator re-reads the transaction reports.  What was NOT bound
is the set of artifacts those claims rest on: three co-simulation reports, three
pinned sources, a routed timing report and a synthesis report.  Nothing stopped
one of them being replaced by a later run and the evidence document still
reading as if it described the original.

WHAT IS AND IS NOT COVERED.  This binds files.  It does not re-derive them: a
manifest cannot tell you a transaction report is CORRECT, only that it is the
one that was there when the claim was written.  `tme_b1_ab.py --assert` is what
says the numbers are right; this says they are the same numbers.

DURABILITY.  A manifest over files that git never carries is a local
checksum, not evidence: a clean checkout would contain none of the artifacts it
pins.  Most of this evidence is built under Desktop/FPGA, outside the
`.github-upload` worktree, so `--mirror` copies every entry to the path the
manifest names -- which is the path a CLONE has -- and `--verify` then passes
in both trees.  Run it before committing; it is a copy, never a delete.

EOL NORMALISATION.  Text artifacts are hashed with CRLF collapsed to LF, the
same rule sw/tme_trace_capture.py uses for `code_sha256`, so a clone on any
platform verifies.  Binaries (.bit) are hashed raw -- they must survive
checkout byte-for-byte and `.gitattributes` pins them `-text`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# TWO LAYOUTS, ONE MANIFEST.
#
# On the development machine the split is real: `hls/` and `logs/` are edited
# under Desktop/FPGA (that is where HLS resolves relative `add_files` from),
# while `sw/` exists ONLY inside the `.github-upload` worktree.  Manifest paths
# are relative to Desktop/FPGA so both trees are addressable from one root,
# which is why the sw entries read `.github-upload/sw/...`.
#
# In a CLEAN CHECKOUT there is no such split -- `sw/`, `hls/` and `logs/` are
# all siblings at the repository root.  Hard-coding either layout makes the
# manifest verifiable in only one of them, and a manifest that cannot be
# verified from a fresh clone is a local checksum, not evidence.  So the root
# is detected and the sw prefix follows from it.
SW = Path(__file__).resolve().parent
SPLIT = SW.parent.name == ".github-upload"
REPO = SW.parents[1] if SPLIT else SW.parent

# THE PATHS IN THE MANIFEST ARE CHECKOUT-RELATIVE, always -- `sw/...`,
# `hls/...`, `logs/...`.  That is the layout a clone has, and it is the only
# layout in which the same string means the same file on every machine.  On the
# development tree `sw/` is reached through `.github-upload/`; resolve() is the
# only place that knows it, so the manifest text never records the difference.
def resolve(rel: str) -> Path:
    """checkout-relative manifest path -> a real file on this machine."""
    if SPLIT and (rel == "sw" or rel.startswith("sw/")):
        return REPO / ".github-upload" / rel
    if SPLIT and rel.startswith("hls/template_match/tb_tme_") and "_phase_s" in rel:
        # The board vectors live only in the upload worktree: the runner is
        # started from sw/ and resolves them as siblings there.  Everything
        # else under hls/ is the HLS build tree, where `add_files` resolves.
        return REPO / ".github-upload" / rel
    if SPLIT and rel.startswith("logs/b2_board_20260819/"):
        # The not-yet-run B2 board protocol is prepared and committed directly
        # in the upload worktree.  It is not a build output mirrored from the
        # outer logs tree, and no result transcript exists yet.
        return REPO / ".github-upload" / rel
    return REPO / rel


HLS = "hls/template_match"
LOGS = "logs/b1_20260818"
BOARD = "logs/b1_board_20260818"
RERUN = "logs/b1_rerun_20260818"
OVERLAY = LOGS + "/overlay_output"
VIVADO = "vivado/tme_standalone"
SWD = "sw"
SUPERSEDED = HLS + "/template_match_b1"

# One HLS project per variant.  run_hls_b1.tcl used to build all three
# solutions inside a single `template_match_b1`, which could not be re-run
# safely -- `open_project` without -reset reopens what is on disk and
# `add_files` accumulates.  Each variant now owns an isolated reset project.
def project(variant: str) -> str:
    return f"{HLS}/template_match_b1_{variant}"


# `SUPERSEDED` above is the old single-project tree.  Nothing is read from it
# any more EXCEPT the packaged IP: `template_match_b1/b1/impl/ip` is the
# directory vivado_b1_125.log names as its IP repository, so it is the closest
# retained thing to the bitstream's actual input.  The isolated projects' IP
# exports have no relationship to that image at all.

MANIFEST_REL = LOGS + "/MANIFEST.sha256"

# Hashed raw rather than EOL-normalised.  Anything whose bytes ARE the artifact.
BINARY_SUFFIXES = {".bit", ".hwh", ".bin", ".zip", ".dcp"}


def entries() -> list[tuple[str, str]]:
    """(role, checkout-relative path) for everything a claim rests on.

    Roles are not decoration: `--verify` prints them, and a missing file reads
    as "the routed timing report is gone", not as a bare path.
    """
    out: list[tuple[str, str]] = [
        # The three pinned sources.  These are what run_hls_b1.tcl compiles, and
        # each carries its own digest inside that script as well -- this is a
        # second, independent record of the same three numbers.
        ("source cur", HLS + "/b1_sources/correlation_core.cur.cpp"),
        ("source b1", HLS + "/b1_sources/correlation_core.b1.cpp"),
        ("source b1b", HLS + "/b1_sources/correlation_core.b1b.cpp"),
        ("source shipped", HLS + "/correlation_core.cpp"),
        ("source cosimmed b1", LOGS + "/correlation_core.cpp.b1_break"),
        ("source pre-B1", LOGS + "/correlation_core.cpp.pre_b1"),
        # The measurements themselves.
        ("cosim cur", project("cur") + "/cur/sim/report/verilog/result.transaction.rpt"),
        ("cosim b1", project("b1") + "/b1/sim/report/verilog/result.transaction.rpt"),
        ("cosim b1b", project("b1b") + "/b1b/sim/report/verilog/result.transaction.rpt"),
        ("csynth cur", project("cur") + "/cur/syn/report/csynth.rpt"),
        ("csynth b1", project("b1") + "/b1/syn/report/csynth.rpt"),
        ("csynth b1b", project("b1b") + "/b1b/syn/report/csynth.rpt"),
        # Routed timing.  The first two are the extracts this document quotes;
        # the last two are the FULL reports they were extracted from, without
        # which "the four worst paths are still templ_buf -> t_row" is an
        # assertion the reader cannot check.
        ("routed wns", LOGS + "/b1_post_route_wns.txt"),
        ("routed utilisation", LOGS + "/b1_post_route_utilization.rpt"),
        ("routed timing summary", OVERLAY + "/post_route_timing_summary.rpt"),
        ("routed worst paths", OVERLAY + "/post_route_worst_paths.rpt"),
        # The image itself.  Copied in from the Vivado build root, which lives
        # outside this repo (C:/Users/lychee/tc25/vivado_project/tme_b1_125).
        # The board session re-hashed the .bit INSIDE the transcript that
        # configured the PL, so these two bind the whole chain: the bytes here
        # are the bytes that ran.
        ("bitstream", OVERLAY + "/tme_standalone.bit"),
        ("hardware handoff", OVERLAY + "/tme_standalone.hwh"),
        # What turned the packaged IP into that image.
        ("build script", VIVADO + "/build_tme_standalone.tcl"),
        # The packaged IP.  READ THE ROLE: this is a RE-EXPORT.  package_b1.tcl
        # ran at 22:06 and overwrote the ip/ directory that the 21:35 Vivado
        # build had read, so these bytes POST-DATE the bitstream above and are
        # not the ones it was built from -- those were not retained.  What ties
        # the image to a core is vivado_b1_125.log, which names both the IP
        # repository path and the VLNV it resolved.
        ("packaged IP (re-export)", SUPERSEDED + "/b1/impl/ip/component.xml"),
        ("packaged IP archive (re-export)",
         SUPERSEDED + "/b1/impl/ip/TermCountB1_hls_tme_top_0_2.zip"),
        ("superseded project marker", SUPERSEDED + "/SUPERSEDED.md"),
        # The rebuild that proved the isolated projects reproduce the originals.
        ("rerun driver", RERUN + "/rerun_all.sh"),
        ("rerun status", RERUN + "/rerun_status.txt"),
        ("rerun log cur", RERUN + "/run_cur.log"),
        ("rerun log b1", RERUN + "/run_b1.log"),
        ("rerun log b1b", RERUN + "/run_b1b.log"),
        # The tools that turn the above into claims.
        ("model", SWD + "/tme_cycle_model.py"),
        ("model pre-B1", LOGS + "/tme_cycle_model.py.pre_b1"),
        ("adjudicator", SWD + "/tme_b1_ab.py"),
        ("scale policy", SWD + "/tme_scale_policy.py"),
        ("manifest tool", SWD + "/tme_b1_manifest.py"),
        ("generator", HLS + "/tme_generate_production.py"),
        ("testbench", HLS + "/tme_tb.cpp"),
        ("ab project script", HLS + "/run_hls_b1.tcl"),
        ("packaging script", HLS + "/package_b1.tcl"),
        ("prod csim script", HLS + "/csim_prod_b1.tcl"),
        # The vector records.  Both payloads are gitignored and regenerate from
        # pinned seeds, so these files are what says a regeneration produced the
        # pixels that were actually verified.  The prod record earns its place
        # here as of 2026-08-19: csim_prod_b1.tcl now checks all four of its
        # entries before it will run, so it authenticates the stimulus of the
        # 15/15 production result and is no longer merely informational.
        ("vector record b1", HLS + "/tb_tme_b1.sha256"),
        ("vector record prod", HLS + "/tb_tme_prod.sha256"),
        # The transcripts.
        ("log cur", LOGS + "/run_cur.log"),
        ("log b1", LOGS + "/run_b1.log"),
        ("log b1b", LOGS + "/run_b1b.log"),
        ("log prod csim", LOGS + "/csim_prod.log"),
        ("log packaging", LOGS + "/package_b1.log"),
        ("log vivado", LOGS + "/vivado_b1_125.log"),
        # The document that reads all of it.
        # The board session.  Its five transcripts are the ONLY record that the
        # B1 bitstream ran, and 03_run.txt is where the audit chain closes --
        # it carries the bitstream hash taken inside the configure transcript.
        ("board prestate", BOARD + "/00_prestate.txt"),
        ("board hashes host", BOARD + "/01_hashes_local.txt"),
        ("board hashes remote", BOARD + "/02_hashes_remote.txt"),
        ("board run", BOARD + "/03_run.txt"),
        ("board restore", BOARD + "/04_restore.txt"),
        ("board session", BOARD + "/B1_BOARD_SESSION.md"),
        ("board prestate script", BOARD + "/00_prestate.sh"),
        ("board restore script", BOARD + "/04_restore.sh"),
        # The runner AS IT RAN.  The working copy in sw/ has since gained a
        # stale-banner notice, which changed its digest away from the
        # f7b00b0e... the session transcripts pin.  Without this snapshot the
        # "byte-identical runner" claim would be uncheckable from the tree.
        ("board runner as-run", BOARD + "/tme_standalone_bringup.py.as_run"),
        ("board runner current", SWD + "/tme_standalone_bringup.py"),
        # The vector PAYLOADS the board consumed, not just their digest file.
        # These three are committed (unlike the b1 csim suite, which is
        # regenerable from pinned seeds) because the board session's whole
        # claim to being a controlled comparison is that they did not change.
        ("board vectors cases", HLS + "/tb_tme_cases_phase_s.txt"),
        ("board vectors patches", HLS + "/tb_tme_patches_phase_s.bin"),
        ("board vectors templates", HLS + "/tb_tme_templs_phase_s.bin"),
        ("board vector record", HLS + "/tb_tme_phase_s.sha256"),
        ("evidence", LOGS + "/PRIORITY4_EVIDENCE.md"),
        ("pinned-source README", HLS + "/b1_sources/README.md"),
        ("stale-column marker", "trace_20260818b/B1_COLUMN_STALE.md"),
    ]
    return out


def digest(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() not in BINARY_SUFFIXES:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _pinned_digests(rel_manifest: str) -> dict[str, str]:
    """Path -> digest from an EXISTING manifest, or {} if there is none."""
    man = resolve(rel_manifest)
    if not man.exists():
        return {}
    out = {}
    for line in man.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        sha, rest = line.split("  ", 1)
        out[rest.split("  #")[0].strip()] = sha
    return out


def write(rebaseline: bool = False) -> int:
    lines = ["# Priority 4 (B1) evidence manifest",
             "# sha256 of each artifact; text is EOL-normalised to LF first.",
             "# Verify with: python tme_b1_manifest.py --verify",
             "#",
             "# This binds files, not claims.  tme_b1_ab.py --assert and",
             "# tme_cycle_model.py --assert are what say the numbers are right;",
             "# this says they are the same numbers.",
             ""]
    missing = []
    fresh: dict[str, str] = {}
    for role, rel in entries():
        path = resolve(rel)
        if not path.exists():
            missing.append((role, path))
            continue
        fresh[rel] = digest(path)
        lines.append(f"{fresh[rel]}  {rel}  # {role}")
    if missing:
        print("REFUSING TO WRITE -- these artifacts are absent:", file=sys.stderr)
        for role, path in missing:
            print(f"  {role:22s} {path}", file=sys.stderr)
        print("\nA manifest that silently omits what it cannot find is worse "
              "than none:\nit reads as complete.", file=sys.stderr)
        return 1

    # RE-BASELINING IS THE FAILURE MODE THIS GUARD EXISTS FOR, AND IT ALREADY
    # HAPPENED ONCE.  B2's manifest pinned the LIVING hls/template_match/
    # tme_top.cpp.  B0b edits that file, so a routine --write on 2026-08-20
    # replaced B2's recorded build input with B0b's core and said nothing.
    # --verify then passed, against a file B2 was never built from.  A manifest
    # whose refresh silently adopts whatever is on disk records the present,
    # not the past, and the present needs no manifest.
    #
    # ADDING and REMOVING entries stays quiet: those are structural edits the
    # caller just made on purpose, visible in the entry list and in the diff.
    # A CHANGED DIGEST on an entry that is still listed is different -- it means
    # a file the manifest already bound has moved underneath it -- so say so and
    # stop.  Sometimes that IS legitimate (a shared living file moves and every
    # manifest naming it must follow; that convention is B2's).  Then pass
    # --rebaseline and the change is a deliberate, greppable act rather than a
    # side effect.
    prev = _pinned_digests(MANIFEST_REL)
    moved = [(rel, prev[rel], fresh[rel]) for rel in fresh
             if rel in prev and prev[rel] != fresh[rel]]
    if moved and not rebaseline:
        print("REFUSING TO WRITE -- these already-pinned artifacts have MOVED:",
              file=sys.stderr)
        for rel, old, new in sorted(moved):
            print(f"  {rel}\n    pinned {old}\n    on disk {new}", file=sys.stderr)
        print("\nRe-writing would replace the recorded evidence with whatever "
              "is on disk\nnow.  If the file is a LIVING one that legitimately "
              "moved, re-run with\n--rebaseline.  If it is HISTORICAL evidence, "
              "pin an immutable snapshot\ninstead of the living path.",
              file=sys.stderr)
        return 1
    if moved:
        print(f"REBASELINED {len(moved)} already-pinned artifact(s):")
        for rel, old, new in sorted(moved):
            print(f"  {rel}\n    {old} -> {new}")

    out = resolve(MANIFEST_REL)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", newline="\n")
    print(f"wrote {MANIFEST_REL} ({len(entries())} artifacts)")
    return 0


def verify() -> int:
    man = resolve(MANIFEST_REL)
    if not man.exists():
        print(f"no manifest at {man}", file=sys.stderr)
        return 1
    pinned = {}
    for line in man.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        sha, rest = line.split("  ", 1)
        rel = rest.split("  #")[0].strip()
        role = rest.split("#", 1)[1].strip() if "#" in rest else ""
        pinned[rel] = (sha, role)

    bad = 0
    for rel, (want, role) in sorted(pinned.items()):
        path = resolve(rel)
        if not path.exists():
            print(f"  MISSING  {role:22s} {rel}")
            bad += 1
            continue
        got = digest(path)
        if got != want:
            print(f"  CHANGED  {role:22s} {rel}")
            print(f"           pinned {want}")
            print(f"           now    {got}")
            bad += 1
    listed = {rel for _, rel in entries()}
    for extra in sorted(listed - set(pinned)):
        print(f"  UNPINNED {extra} -- entries() lists it, the manifest does not")
        bad += 1

    if bad:
        print(f"\nFAIL: {bad} artifact(s) drifted.  Either the evidence moved "
              f"or the manifest is\nout of date -- decide which before "
              f"quoting anything from it.", file=sys.stderr)
        return 1
    print(f"OK -- {len(pinned)} artifacts verify against the manifest.")
    return 0


def mirror() -> int:
    """Copy every manifest entry into the git worktree at its manifest path.

    No-op outside the development tree: in a clone the manifest path IS the
    file's path, so there is nothing to mirror and nothing to get out of sync.
    """
    if not SPLIT:
        print("not the development tree -- nothing to mirror")
        return 0
    import shutil
    root = REPO / ".github-upload"
    copied = same = 0
    missing = []
    # The manifest itself is not one of its own entries, but a mirrored tree
    # without it is unverifiable -- so it is copied first and by the same rule.
    for role, rel in [("manifest", MANIFEST_REL)] + entries():
        src = resolve(rel)
        if not src.exists():
            missing.append((role, rel))
            continue
        dst = root / rel
        if dst.resolve() == src.resolve():
            same += 1                      # already lives in the worktree
            continue
        if dst.exists() and digest(dst) == digest(src):
            same += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    if missing:
        print("REFUSING TO MIRROR -- these artifacts are absent:", file=sys.stderr)
        for role, rel in missing:
            print(f"  {role:26s} {rel}", file=sys.stderr)
        return 1
    print(f"mirrored into .github-upload: {copied} copied, {same} already current")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    ap.add_argument("--rebaseline", action="store_true",
                    help="allow --write to replace the pinned digest of an "
                         "artifact that has MOVED; without it, write() "
                         "refuses rather than silently adopting what is on "
                         "disk")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--mirror", action="store_true")
    args = ap.parse_args()
    if args.mirror:
        return mirror()
    return write(args.rebaseline) if args.write else verify()


if __name__ == "__main__":
    sys.exit(main())
