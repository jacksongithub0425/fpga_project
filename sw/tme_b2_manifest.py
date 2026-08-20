#!/usr/bin/env python3
"""Bind the Priority 5 (B2) evidence to digests, and verify it later.

    python tme_b2_manifest.py --write     # create/refresh MANIFEST.sha256
    python tme_b2_manifest.py --verify    # exit 1 on any drift
    python tme_b2_manifest.py --mirror    # copy the evidence into the git tree

SEPARATE FROM B1'S MANIFEST, DELIBERATELY.  Priority 4's manifest is itself
pinned evidence -- its own digest appears in logs/b1_20260818/MANIFEST.sha256 --
so growing it with B2 entries would rewrite a record that a finished
measurement rests on.  Two manifests also make the dependency explicit rather
than implicit: B2's claim is a PAIRED one, so the `b1` control's transaction
report is listed HERE as well as in B1's manifest.  Both bind it, and if it
ever moves both fail.  That is the intended redundancy, not an oversight.

THE HASHING, EOL AND MIRROR RULES ARE B1'S, IMPORTED RATHER THAN REPEATED.  A
second implementation of "hash text with CRLF collapsed, hash binaries raw" is
a second thing that can drift from the first, and the two manifests would then
disagree about a file they both pin.  `resolve`, `digest`, `write`, `verify`
and `mirror` come from tme_b1_manifest; only the entry list and the output path
are this file's.

WHAT IS AND IS NOT COVERED.  This binds files.  It does not re-derive them:
`tme_b2_ab.py --assert`, `tme_b2_mutants.py --assert` and
`tme_cycle_model.py --assert` are what say the numbers are right; this says
they are the same numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tme_b1_manifest as B1M                                  # noqa: E402

HLS = B1M.HLS                       # "hls/template_match"
SWD = "sw"
LOGS = "logs/b2_20260819"
B2BOARD = "logs/b2_board_20260819"
B1LOGS = B1M.LOGS                   # "logs/b1_20260818"
VIVADO = "vivado/tme_standalone"

MANIFEST_REL = LOGS + "/MANIFEST.sha256"


def entries() -> list[tuple[str, str]]:
    """(role, checkout-relative path) for everything Priority 5 rests on."""
    return [
        # The pinned source under measurement, and the shipped file it came
        # from.  run_hls_b1.tcl carries the same digest for the snapshot, so
        # this is a second independent record of it.
        ("source b2", HLS + "/b1_sources/correlation_core.b2.cpp"),
        ("source shipped", HLS + "/correlation_core.cpp"),
        # THE CONTROL.  B2's whole claim is a difference against `b1`, so the
        # control's report is evidence for B2 exactly as much as B2's own is.
        # It is pinned in B1's manifest too -- deliberately, see the docstring.
        ("source b1 (control)", HLS + "/b1_sources/correlation_core.b1.cpp"),
        # The two files that are compiled ALONGSIDE the snapshot in every one
        # of these projects.  They were unpinned here until 2026-08-19: the
        # snapshot was verified by digest and its two build partners were not,
        # so an edit to either would have changed both halves of a "paired"
        # measurement without any manifest noticing.  They are pinned in B1's
        # manifest for the same reason.
        ("build input tme_top.cpp", HLS + "/tme_top.cpp"),
        ("build input tme_top.h", HLS + "/tme_top.h"),
        ("cosim b1 (control)",
         B1M.project("b1") + "/b1/sim/report/verilog/result.transaction.rpt"),
        ("cosim b2", B1M.project("b2")
         + "/b2/sim/report/verilog/result.transaction.rpt"),
        ("csynth b1 (control)", B1M.project("b1") + "/b1/syn/report/csynth.rpt"),
        ("csynth b2", B1M.project("b2") + "/b2/syn/report/csynth.rpt"),
        # Routed timing for the B2 core.
        ("routed wns", LOGS + "/b2_post_route_wns.txt"),
        ("routed utilisation", LOGS + "/b2_post_route_utilization.rpt"),
        ("routed timing summary", LOGS + "/post_route_timing_summary.rpt"),
        ("routed worst paths", LOGS + "/post_route_worst_paths.rpt"),
        # The image itself.  Retained on the same argument B1's is: it is built
        # from a Vivado block design that lives OUTSIDE this repository
        # (C:/Users/lychee/tc25/vivado_project/), so it is not regenerable from
        # anything committed here.  Unlike B1's, this one has NEVER RUN on a
        # board -- keeping it is what makes a future board session possible
        # against the artifact these cycle numbers were routed from, rather
        # than against a rebuild that would have its own WNS.
        ("bitstream (never run on hardware)", LOGS + "/tme_standalone.bit"),
        ("hardware handoff", LOGS + "/tme_standalone.hwh"),
        # The prepared board gate.  These are protocol inputs, not board
        # results: no transcript exists yet.  The board-side checksum manifest
        # binds the nine suite inputs plus the authenticated restore script
        # consumed by 03_run.sh, while listing them here also makes a clean
        # checkout independently verifiable.
        ("board runner", SWD + "/tme_standalone_bringup.py"),
        ("board phase-s cases", HLS + "/tb_tme_cases_phase_s.txt"),
        ("board phase-s patches", HLS + "/tb_tme_patches_phase_s.bin"),
        ("board phase-s templates", HLS + "/tb_tme_templs_phase_s.bin"),
        ("board hw cases", HLS + "/tb_tme_cases_hw.txt"),
        ("board hw patches", HLS + "/tb_tme_patches_hw.bin"),
        ("board hw templates", HLS + "/tb_tme_templs_hw.bin"),
        ("board plan", B2BOARD + "/B2_BOARD_SESSION_PLAN.md"),
        ("board prestate script", B2BOARD + "/00_prestate.sh"),
        ("board input checksum gate", B2BOARD + "/B2_BOARD_INPUTS.sha256"),
        ("board host hash record", B2BOARD + "/01_hashes_local.txt"),
        ("board run wrapper", B2BOARD + "/03_run.sh"),
        ("board restore script", B2BOARD + "/04_restore.sh"),
        # THE PACKAGED IP, AND READ THE ROLE -- IT IS NOT B1'S.  B1's manifest
        # pins its IP as a RE-EXPORT: package_b1.tcl ran after the Vivado build
        # and overwrote the directory the bitstream had been built from, so
        # those bytes post-date the image and are not its input.  B2's do not.
        # package_b2.tcl finished at 19:16:09; vivado_b2_125.log records
        # `Loaded user IP repository
        # '.../template_match_b1_b2/b2/impl/ip'` and its first synthesis run
        # launched at 19:17:10; the directory has not been written since.
        # These are the exact bytes tme_standalone.bit was built from, and
        # pinning them is what lets a future board session say WHICH core ran
        # rather than inferring it from a log line.
        #
        # They are also MIRRORED INTO GIT (--mirror), which is the part that
        # actually preserves them: a re-run of package_b2.tcl overwrites the
        # live directory, and then --verify fails loudly while the committed
        # copy still holds the original.  That is the lesson B1 paid for.
        ("packaged IP (the image's input)",
         B1M.project("b2") + "/b2/impl/ip/component.xml"),
        ("packaged IP archive (the image's input)",
         B1M.project("b2") + "/b2/impl/ip/TermCountB2_hls_tme_top_0_2.zip"),
        # What produced them.
        ("build script", VIVADO + "/build_tme_standalone.tcl"),
        ("packaging script", HLS + "/package_b2.tcl"),
        ("ab project script", HLS + "/run_hls_b1.tcl"),
        # The broad-geometry C simulation.  The b1 cosim suite reaches T = 6
        # tiles; this one reaches T = 52, the compiled maximum.  Both the
        # script and its transcript are pinned because the claim "B2 is
        # functionally correct across the tile-count range" rests on the
        # transcript, and the transcript is only evidence about the measured
        # RTL's source if the script verified the same snapshot digest.
        ("prod csim script b2", HLS + "/csim_prod_b2.tcl"),
        ("prod csim log b2", LOGS + "/csim_prod_b2.log"),
        ("testbench", HLS + "/tme_tb.cpp"),
        ("generator", HLS + "/tme_generate_production.py"),
        # The vector record.  The payload is gitignored and regenerates from
        # pinned seeds; this file is what says a regeneration produced the
        # pixels both halves of the pair were actually measured on.
        ("vector record b1", HLS + "/tb_tme_b1.sha256"),
        # The production stimulus, on the same argument: csim_prod_b2.tcl
        # checks all four of its entries before it will run, so this file is
        # what authenticates the 1.6 MB of pixels the broad-geometry pass was
        # verified against.
        ("vector record prod", HLS + "/tb_tme_prod.sha256"),
        # The tools that turn artifacts into claims.
        ("model", SWD + "/tme_cycle_model.py"),
        ("model pre-B2", LOGS + "/tme_cycle_model.py.pre_b2"),
        ("adjudicator", SWD + "/tme_b2_ab.py"),
        ("mutant gate", SWD + "/tme_b2_mutants.py"),
        ("manifest tool", SWD + "/tme_b2_manifest.py"),
        # THE IMPORTED IMPLEMENTATION.  Everything above is hashed by
        # tme_b1_manifest's `digest`, written by its `write` and copied by its
        # `mirror` -- this file supplies only the entry list.  Leaving it
        # unpinned meant the rule that produced every digest here was itself
        # unrecorded, so a change to the EOL or binary-suffix policy would have
        # silently reinterpreted the whole manifest.  It is pinned in B1's
        # manifest too; both bind it, and if it moves both fail.
        ("manifest tool (imported)", SWD + "/tme_b1_manifest.py"),
        # The transcripts.
        ("log b2", LOGS + "/run_b2.log"),
        ("log packaging", LOGS + "/package_b2.log"),
        ("log vivado", LOGS + "/vivado_b2_125.log"),
        # The reconstructed pre-RTL baselines.  READ THE ROLE, IT WAS WRONG
        # ONCE: this file used to be pinned as "prediction (pre-registered)",
        # and it was written at 19:19:23, nine minutes AFTER the b2 transaction
        # report existed at 19:10:13.  The PROJECTION it recomputes is retained
        # in ancestor commit e762cbf before the B2 source/build commit; that is
        # repository ordering, not an external timestamp.  Pinning still
        # matters: this is the artifact anyone can
        # re-derive from the snapshot and the control report to check the miss.
        ("pre-RTL baselines (reconstructed)", LOGS + "/PREDICTION.txt"),
        ("mutant gate output", LOGS + "/b2_mutants.txt"),
        ("adjudicator output", LOGS + "/b2_ab.txt"),
        ("adjudicator negative control", LOGS + "/b2_ab_negative_control.txt"),
        ("cycle model output", LOGS + "/cycle_model_assert.txt"),
        ("evidence", LOGS + "/PRIORITY5_EVIDENCE.md"),
        ("pinned-source README", HLS + "/b1_sources/README.md"),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--mirror", action="store_true")
    args = ap.parse_args()

    # Point B1's machinery at THIS manifest's entries and output path.  The
    # rebinding is explicit and total: every function below reads these two
    # module globals and nothing else distinguishes the two manifests.
    B1M.entries = entries
    B1M.MANIFEST_REL = MANIFEST_REL
    B1M.write.__doc__ = "Priority 5 (B2) evidence manifest"

    if args.mirror:
        return B1M.mirror()
    if args.write:
        rc = B1M.write()
        if rc == 0:
            # write() stamps B1's header text; correct it in place so the file
            # says what it is.  Doing this here rather than forking write()
            # keeps ONE implementation of the hashing rule.
            path = B1M.resolve(MANIFEST_REL)
            text = path.read_text()
            text = (text
                    .replace("# Priority 4 (B1) evidence manifest",
                             "# Priority 5 (B2) evidence manifest")
                    .replace("python tme_b1_manifest.py --verify",
                             "python tme_b2_manifest.py --verify")
                    .replace("# tme_b1_ab.py --assert and",
                             "# tme_b2_ab.py --assert, tme_b2_mutants.py "
                             "--assert and"))
            path.write_text(text, newline="\n")
        return rc
    return B1M.verify()


if __name__ == "__main__":
    sys.exit(main())
