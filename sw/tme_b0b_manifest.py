#!/usr/bin/env python3
"""Bind the Priority 6 (B0b) evidence to digests, and verify it later.

    python tme_b0b_manifest.py --write     # create/refresh MANIFEST.sha256
    python tme_b0b_manifest.py --verify    # exit 1 on any drift
    python tme_b0b_manifest.py --mirror    # copy the evidence into the git tree

A THIRD MANIFEST, FOR THE REASON THERE WAS A SECOND.  B1's manifest is itself
pinned evidence -- its digest appears inside logs/b1_20260818/MANIFEST.sha256 --
and B2's is bound the same way, so growing either with B0b entries would
rewrite a record that a finished measurement rests on.

The hashing, EOL and mirror rules are B1's, imported rather than reimplemented:
a second copy of "hash text with CRLF collapsed, hash binaries raw" is a second
thing that can drift, and then two manifests would disagree about a file they
both pin.

THREE SOLUTIONS ARE PINNED, NOT ONE.  B0b's claim is a pair of DIFFERENCES
(shadow - b2ctl and b0b - b2ctl), so the control's transaction report is
evidence for B0b exactly as much as B0b's own is.  All three reports, all three
sources and both build partners are listed here.

WHAT IS AND IS NOT COVERED.  This binds files.  It does not re-derive them:
tme_b0b_ab.py --assert, tme_b0b_mutants.py --assert, tme_b0b_synth.py --assert
and tme_cycle_model.py --assert are what say the numbers are right; this says
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
LOGS = "logs/b0b_20260820"
B2LOGS = "logs/b2_20260819"

MANIFEST_REL = LOGS + "/MANIFEST.sha256"

SOLUTIONS = ("b2ctl", "shadow", "b0b")


def project(sol: str) -> str:
    return f"{HLS}/template_match_b0b_{sol}"


def entries() -> list[tuple[str, str]]:
    """(role, checkout-relative path) for everything Priority 6 rests on."""
    out: list[tuple[str, str]] = [
        # --- the three sources under measurement ---------------------------
        # run_hls_b0b.tcl carries the same digests, so this is a second
        # independent record of each.
        ("source b2ctl (control)", HLS + "/b0b_sources/tme_top.b2.cpp"),
        ("source shadow", HLS + "/b0b_sources/tme_top.shadow.cpp"),
        ("source b0b", HLS + "/b0b_sources/tme_top.b0b.cpp"),
        ("source shipped tme_top.cpp", HLS + "/tme_top.cpp"),
        ("snapshot README", HLS + "/b0b_sources/README.md"),
        # --- the constant half of the pair --------------------------------
        # correlation_core is NOT what B0b varies, so it is pinned to the same
        # snapshot B2 was measured from.  An edit to it would have changed
        # every one of these three reports at once, silently.
        ("build input correlation_core.b2.cpp",
         HLS + "/b1_sources/correlation_core.b2.cpp"),
        ("build input tme_top.h", HLS + "/tme_top.h"),
        ("build input tme_tb.cpp", HLS + "/tme_tb.cpp"),
        # --- the build and adjudication tooling ---------------------------
        ("build script", HLS + "/run_hls_b0b.tcl"),
        ("mutant build script", HLS + "/run_hls_b0b_mutant.tcl"),
        ("adjudicator", SWD + "/tme_b0b_ab.py"),
        ("mutant gate", SWD + "/tme_b0b_mutants.py"),
        ("synthesis reader", SWD + "/tme_b0b_synth.py"),
        ("cycle model", SWD + "/tme_cycle_model.py"),
        # The hashing rule every digest here rests on.
        ("manifest rule", SWD + "/tme_b1_manifest.py"),
        # --- the pre-registration, committed before the first build -------
        ("prediction", LOGS + "/PREDICTION.txt"),
        ("prediction generator", LOGS + "/make_prediction.py"),
        ("direct-case expectations", LOGS + "/b0b_direct_expect.py"),
        # --- the stimulus --------------------------------------------------
        # Half of a paired measurement is the vectors.  The b1 suite is
        # regenerable from pinned seeds and its digests are the record.
        ("vector digests", HLS + "/tb_tme_b1.sha256"),
        # --- the correctness evidence --------------------------------------
        ("csim shadow, five suites", LOGS + "/csim_shadow_broad.log"),
        ("csim shadow smoke", LOGS + "/smoke_shadow_b1.log"),
        ("csim b0b smoke", LOGS + "/smoke_b0b_b1.log"),
        ("mutant gate transcript", LOGS + "/b0b_mutants.txt"),
        ("mutant edits", LOGS + "/b0b_mutants_list.txt"),
        # --- the adjudicated result ----------------------------------------
        ("evidence document", LOGS + "/PRIORITY6_EVIDENCE.md"),
        ("ab adjudication", LOGS + "/b0b_ab.txt"),
        ("ab negative control", LOGS + "/b0b_ab_negative_control.txt"),
        ("synthesis comparison", LOGS + "/b0b_synth.txt"),
        ("cycle model assertion", LOGS + "/cycle_model_assert.txt"),
        # --- packaging and routed timing -----------------------------------
        ("package log", LOGS + "/package_b0b.log"),
        ("package script", HLS + "/package_b0b.tcl"),
        ("vivado stdout", LOGS + "/vivado_b0b_125.stdout"),
        ("vivado log", LOGS + "/vivado_b0b_125.log"),
        ("routed wns", LOGS + "/b0b_post_route_wns.txt"),
        ("routed utilisation", LOGS + "/b0b_post_route_utilization.rpt"),
        ("routed timing summary", LOGS + "/post_route_timing_summary.rpt"),
        ("routed worst paths", LOGS + "/post_route_worst_paths.rpt"),
    ]
    # --- per-solution build outputs ---------------------------------------
    for sol in SOLUTIONS:
        out.append((f"run log {sol}", LOGS + f"/run_{sol}.log"))
        out.append((f"cosim {sol}",
                    project(sol) + f"/{sol}/sim/report/verilog/"
                                   "result.transaction.rpt"))
        out.append((f"csynth {sol}",
                    project(sol) + f"/{sol}/syn/report/csynth.rpt"))
        out.append((f"csynth top {sol}",
                    project(sol) + f"/{sol}/syn/report/tme_top_csynth.rpt"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    ap.add_argument("--rebaseline", action="store_true",
                    help="see tme_b1_manifest.write(); required to move an "
                         "already-pinned digest")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--mirror", action="store_true")
    args = ap.parse_args()

    # Point B1's machinery at THIS manifest's entries and output path, exactly
    # as tme_b2_manifest.py does.  The rebinding is total: every function
    # below reads these two module globals and nothing else distinguishes the
    # three manifests.
    B1M.entries = entries
    B1M.MANIFEST_REL = MANIFEST_REL

    if args.mirror:
        return B1M.mirror()
    if args.write:
        rc = B1M.write(args.rebaseline)
        if rc == 0:
            path = B1M.resolve(MANIFEST_REL)
            text = path.read_text()
            text = (text
                    .replace("# Priority 4 (B1) evidence manifest",
                             "# Priority 6 (B0b) evidence manifest")
                    .replace("python tme_b1_manifest.py --verify",
                             "python tme_b0b_manifest.py --verify")
                    .replace("# tme_b1_ab.py --assert and",
                             "# tme_b0b_ab.py --assert, tme_b0b_mutants.py"
                             " --assert, tme_b0b_synth.py --assert and"))
            path.write_text(text, newline="\n")
        return rc
    return B1M.verify()


if __name__ == "__main__":
    sys.exit(main())
