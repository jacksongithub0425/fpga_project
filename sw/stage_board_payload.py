#!/usr/bin/env python3
"""Assemble the flat directory a board session runs from, and hash it.

    python3 stage_board_payload.py --variant combined_b2_100 --stage 1 \
                                   --out ../../.stage/stage1

Run on the BUILD machine, not the board.  It gathers the bitstream pair, the
gate modules and every committed fixture into one directory, verifies each
against the digest record that governs it, and writes `PAYLOAD.sha256` over
what it produced.

WHY THIS IS A SCRIPT AND NOT A COPY COMMAND.  The board runs everything from
one flat directory, but in the repository the pieces live in four places — the
Vivado bundle, `sw/`, `hls/integration/` and `hls/template_match/`.  Assembling
that by hand is how a payload ends up holding seven of eight vectors, or a
`.hwh` from one build beside a `.bit` from another; the gates already refuse
both, but they refuse them after the board is booked.  This does the
assembling once, refuses the same way, and leaves a record of exactly what it
staged.

THREE DIGEST RECORDS, EACH GOVERNING ITS OWN GROUP, ALL CHECKED HERE:

    board_expect.VARIANTS[...]      the .bit / .hwh, cross-checked against the
                                    shipped BUILD_INFO.txt as well
    GATE4_VECTORS.sha256            gate 4's eight vectors
    GATE5_VECTORS.sha256            gate 5's five

`PAYLOAD.sha256` is then written over everything staged, in `sha256sum -b`
format, so the board can prove it received the same bytes:

    sha256sum -c PAYLOAD.sha256

The bitstream digests come from `board_expect`, which is why a variant with no
board bundle (`combined_current_100` was built to isolate a clock change and
never intended for silicon) is refused outright rather than staged without
digests to check.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_expect as X

SW = Path(__file__).resolve().parent

# TWO ROOTS, AND BOTH ARE REAL.  This project is checked out as two linked
# worktrees: the git mirror holds `sw/`, `hls/` and the committed baseline
# bundle, while the build machine's Vivado outputs — including every
# post-2026-08-11 board bundle — live in the parent project directory, which
# is not under version control.  `board_expect`'s `bundle` paths are relative
# to whichever of the two holds them, so a path is resolved against both, in
# order, and the one that answered is printed.  Guessing a single root is how
# this script would either fail to find the B2 bundle or silently stage the
# baseline's.
ROOTS = (SW.parent, SW.parent.parent)


def resolve(rel: str, what: str) -> Path:
    """`rel` under the first root that has it."""
    for root in ROOTS:
        p = root / rel
        if p.exists():
            return p
    raise SystemExit(
        f"{what}: {rel} is under none of "
        + ", ".join(str(r) for r in ROOTS)
        + " — the path in board_expect is wrong, or the build was not kept.")


# Modules every stage needs, then what each stage adds.  A stage is a superset
# of the one before it: the preflight modules stay because the runbook opens
# every session by re-running the preflight.
COMMON_MODULES = (
    "board_expect.py", "board_preflight.py", "board_idle_check.py",
    "inspect_overlay.py", "probe_cma_budget.py",
    "tme_driver.py", "tme_standalone_bringup.py", "safe_teardown.py",
)

STAGE_MODULES = {
    0: (),
    1: ("board_gate_full_dma.py", "board_gate_extract.py",
        "board_gate_protocol.py", "board_gate_clock.py",
        "board_gate_recovery.py", "binarize_dma_checks.py"),
}

# (record file, [vectors it governs], where they live in the repo)
FIXTURE_GROUPS = {
    0: (),
    1: (
        ("GATE4_VECTORS.sha256",
         ("tb_bpe_tme_cases.txt", "tb_bpe_tme_gray.bin", "tb_bpe_tme_bin.bin",
          "tb_bpe_tme_patch.bin", "tb_bpe_tme_templs.bin"),
         "hls/integration"),
        ("GATE4_VECTORS.sha256",
         ("tb_tme_cases_hw.txt", "tb_tme_patches_hw.bin",
          "tb_tme_templs_hw.bin"),
         "hls/template_match"),
        ("GATE5_VECTORS.sha256",
         ("tb_proto_cases.txt", "tb_proto_gray.bin", "tb_proto_bin.bin",
          "tb_proto_patches.bin", "tb_proto_templs.bin"),
         "hls/integration"),
    ),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_record(path: Path) -> dict:
    """A `sha256  path` record, keyed by basename."""
    want = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rest = line.partition("  ")
        if len(digest) != 64 or not rest:
            digest, _, rest = line.partition(" *")
        if len(digest) != 64 or not rest:
            raise SystemExit(f"{path}: malformed line {line!r}")
        want[Path(rest.strip()).name] = digest.lower()
    if not want:
        raise SystemExit(f"{path} lists no hashes")
    return want


def read_build_info(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def collect(variant: str, stage: int) -> list:
    """(source, name) for everything this stage needs, all verified.

    Nothing is copied until every check has passed: a half-staged directory is
    exactly the ambiguity this script exists to remove.
    """
    cfg = X.variant(variant)
    if not cfg.get("bundle"):
        raise SystemExit(
            f"variant {variant} has no board bundle. It was built as "
            f"implementation evidence and pins no bitstream digests, so it "
            f"cannot be staged for a board session.")
    bundle = resolve(cfg["bundle"], f"variant {variant}'s board bundle")
    if not bundle.is_dir():
        raise SystemExit(f"{bundle} is not a directory.")
    print(f"  bundle: {bundle}")

    items: list = []
    problems: list = []

    # -- the bitstream pair, against BOTH records ---------------------------
    bit = bundle / "three_stage_combined.bit"
    hwh = bundle / "three_stage_combined.hwh"
    info = bundle / "BUILD_INFO.txt"
    for f in (bit, hwh, info):
        if not f.is_file():
            problems.append(f"missing from the bundle: {f.name}")
    if not problems:
        rec = read_build_info(info)
        for kind, path in (("bit", bit), ("hwh", hwh)):
            got = sha256(path).upper()
            pinned = (cfg[f"{kind}_sha256"] or "").upper()
            shipped = rec.get(f"{kind}_sha256", "").upper()
            print(f"  {path.name:<28} {got}")
            print(f"    pinned ({variant}) -> "
                  f"{'OK' if got == pinned else 'MISMATCH'}")
            print(f"    BUILD_INFO.txt      -> "
                  f"{'OK' if got == shipped else 'MISMATCH'}")
            if got != pinned:
                problems.append(f"{path.name}: not the digest pinned for "
                                f"{variant}")
            if got != shipped:
                problems.append(f"{path.name}: disagrees with BUILD_INFO.txt")
        if rec.get("variant") not in (None, cfg["build_info_variant"]):
            problems.append(f"BUILD_INFO.txt says variant="
                            f"{rec.get('variant')!r}, expected "
                            f"{cfg['build_info_variant']!r}")
        items += [(bit, bit.name), (hwh, hwh.name), (info, info.name)]

    # -- modules ------------------------------------------------------------
    for name in COMMON_MODULES + STAGE_MODULES[stage]:
        src = SW / name
        if not src.is_file():
            problems.append(f"missing module: sw/{name}")
        else:
            items.append((src, name))

    # -- fixtures, each against the record that governs it ------------------
    records: dict = {}
    for record_name, names, where in FIXTURE_GROUPS[stage]:
        if record_name not in records:
            rp = SW / record_name
            if not rp.is_file():
                problems.append(f"missing digest record: sw/{record_name}")
                continue
            records[record_name] = read_record(rp)
            items.append((rp, record_name))
        want = records[record_name]
        for name in names:
            src = resolve(f"{where}/{name}", f"gate fixture {name}")
            if not src.is_file():
                problems.append(f"missing fixture: {where}/{name}")
                continue
            got = sha256(src)
            if name not in want:
                problems.append(f"{record_name} has no entry for {name}")
            elif got != want[name]:
                problems.append(f"{where}/{name}: sha256 {got} != "
                                f"{record_name}'s {want[name]}")
            items.append((src, name))

    if problems:
        print("\nPAYLOAD FAULTS:")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(2)

    seen: dict = {}
    for src, name in items:
        if name in seen and seen[name] != src:
            raise SystemExit(f"two different sources both stage as {name}: "
                             f"{seen[name]} and {src}")
        seen[name] = src
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", default="combined_b2_100")
    ap.add_argument("--stage", type=int, default=1, choices=sorted(STAGE_MODULES))
    ap.add_argument("--out", required=True, help="directory to assemble into")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a non-empty output directory")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()) and not args.force:
        print(f"CANNOT RUN: {out} is not empty. Pass --force to overwrite, or "
              f"choose an empty directory — a payload mixed with the leftovers "
              f"of a previous one is the ambiguity this script prevents.")
        return 2

    print(f"staging variant {args.variant}, stage {args.stage} -> {out}\n")
    items = collect(args.variant, args.stage)

    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for src, name in items:
        shutil.copy2(src, out / name)
        total += (out / name).stat().st_size

    lines = [f"{sha256(out / name)} *{name}"
             for _, name in sorted(items, key=lambda t: t[1])]
    # newline="\n" explicitly: this file is consumed by `sha256sum -c` on the
    # board, and Python's default translation would put CRLF on it when this
    # script runs on Windows.  sha256sum then looks for a filename with a
    # trailing carriage return and reports all 32 as unreadable — a payload
    # check that fails for a reason that has nothing to do with the payload.
    with open(out / "PAYLOAD.sha256", "w", encoding="ascii",
              newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\n{len(items)} files, {total:,} bytes")
    print(f"PAYLOAD.sha256 written over all {len(lines)} of them.")
    print("On the board, before running anything: sha256sum -c PAYLOAD.sha256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
