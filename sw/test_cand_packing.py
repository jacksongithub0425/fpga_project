"""
Unit test for the candidate descriptor packing in tme_driver.

    python test_cand_packing.py        # from sw/
    pytest test_cand_packing.py

Needs no hardware and no PYNQ: tme_driver's module-level imports are numpy and
struct only, and everything PYNQ-specific is imported lazily inside methods.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tme_driver import (_CAND_STRUCT_SIZE, _MAX_CANDIDATES,
                        _META_STRUCT_SIZE, _validate_batch_size,
                        pack_candidate)

# The generator's manifest.  Its packed_hex column is an independent
# implementation of the same layout (patch_extract_generate_golden.py:pack),
# already validated against the core by csim and cosim — so it makes a much
# better oracle than anything this file could assert on its own.
MANIFEST = (Path(__file__).resolve().parents[1]
            / "hls" / "patch_extract" / "tb_patch_extract_cases_csim.txt")


def decode(word: int) -> tuple[int, int, int, int, int]:
    """Field extraction exactly as patch_extract_core.cpp:69-73 does it."""
    return (
        word & 0xFFFF,           # ep_x
        (word >> 16) & 0xFFFF,   # ep_y
        (word >> 32) & 0x3,      # side
        (word >> 34) & 0x3FFF,   # max_tw
        (word >> 48) & 0xFFFF,   # max_th
    )


def test_word_is_eight_bytes():
    assert _CAND_STRUCT_SIZE == 8
    assert len(pack_candidate(0, 0, 0, 0, 0)) == 8


def test_round_trip():
    cases = [
        (0, 0, 0, 0, 0),
        (600, 251, 0, 216, 96),
        (400, 251, 1, 216, 96),
        (65535, 65535, 1, 0x3FFF, 0xFFFF),   # every field at its ceiling
        (1, 2, 1, 3, 4),
    ]
    for ep_x, ep_y, side, max_tw, max_th in cases:
        word = struct.unpack("<Q", pack_candidate(ep_x, ep_y, side, max_tw, max_th))[0]
        assert decode(word) == (ep_x, ep_y, side, max_tw, max_th), (
            f"round trip failed for {(ep_x, ep_y, side, max_tw, max_th)}"
        )


def test_fields_do_not_bleed():
    """One field at its maximum must leave the others at zero — the failure
    mode that a mis-sized shift produces."""
    for i, args in enumerate([
        (0xFFFF, 0, 0, 0, 0),
        (0, 0xFFFF, 0, 0, 0),
        (0, 0, 0x3, 0, 0),
        (0, 0, 0, 0x3FFF, 0),
        (0, 0, 0, 0, 0xFFFF),
    ]):
        word = struct.unpack("<Q", pack_candidate(*args))[0]
        got = decode(word)
        assert got == args, f"field {i} bled: packed {args}, decoded {got}"


def test_matches_generator_manifest():
    """Cross-check against all 59 golden candidates, including the 65535
    endpoints and the 0-width/height degenerate descriptors."""
    # Fail rather than skip.  This is the only check here with an independent
    # oracle; quietly passing without it would report a green suite that had
    # verified nothing but its own arithmetic.
    if not MANIFEST.exists():
        raise AssertionError(
            f"{MANIFEST} not found — run patch_extract_generate_golden.py from "
            f"hls/patch_extract/ first.  This test is not meaningful without it."
        )

    lines = MANIFEST.read_text().strip().splitlines()
    n = 0
    for line in lines[1:]:          # row 0 is the header
        f = line.split()
        packed_hex = f[1]
        ep_x, ep_y, side, max_tw, max_th = (int(f[3]), int(f[4]), int(f[5]),
                                            int(f[6]), int(f[7]))
        ours = struct.unpack("<Q", pack_candidate(ep_x, ep_y, side, max_tw, max_th))[0]
        assert ours == int(packed_hex, 16), (
            f"{f[16]}: driver packed {ours:016x}, manifest has {packed_hex}"
        )
        n += 1
    assert n > 0, "manifest had no candidate rows"
    print(f"  cross-checked {n} manifest candidates")


def test_old_layout_regression():
    """The retired "<HHBBHHxx" layout, kept as an executable record of what it
    actually did wrong — so nobody restores it thinking it was only a stride
    problem."""
    ep_x, ep_y, side, max_tw, max_th = 600, 251, 0, 216, 96

    old = struct.pack("<HHBBHHxx", ep_x, ep_y, side, 0, max_tw, max_th)
    assert len(old) == 12, "old layout was 12 bytes against an 8-byte AXIS word"

    # What the core would have decoded from the first 8 bytes of candidate 0.
    o_ep_x, o_ep_y, o_side, o_tw, o_th = decode(struct.unpack("<Q", old[:8])[0])
    assert (o_ep_x, o_ep_y, o_side) == (ep_x, ep_y, side)  # these did survive
    assert o_tw == 0, "max_tw used to decode as 0"
    assert o_th == max_tw, "max_th used to receive the driver's max_tw"

    # The replacement gets all five right.
    new = decode(struct.unpack("<Q", pack_candidate(ep_x, ep_y, side, max_tw, max_th))[0])
    assert new == (ep_x, ep_y, side, max_tw, max_th)


def test_rejects_out_of_range():
    bad = [
        ((0x10000, 0, 0, 0, 0), "ep_x"),
        ((0, 0x10000, 0, 0, 0), "ep_y"),
        ((0, 0, 4, 0, 0), "side"),
        ((0, 0, 0, 0x4000, 0), "max_tw"),
        ((0, 0, 0, 0, 0x10000), "max_th"),
        ((-1, 0, 0, 0, 0), "ep_x"),
    ]
    for args, field in bad:
        try:
            pack_candidate(*args)
        except ValueError as e:
            assert field in str(e), f"expected a {field} error, got: {e}"
        else:
            raise AssertionError(f"{args} should have been rejected ({field})")


def test_batch_size_boundary():
    """64 admitted, 65 rejected — and nothing silently truncated.

    run_candidates() used to do `n = min(len(candidates), _MAX_CANDIDATES)`,
    so a page with 65 endpoints returned 64 results and looked entirely
    healthy: nothing downstream compares the result count against the input
    count, so the 65th candidate simply never existed.  This asserts the
    replacement rejects instead.
    """
    # Everything up to and including the limit is admitted.
    for n in (0, 1, _MAX_CANDIDATES - 1, _MAX_CANDIDATES):
        _validate_batch_size(n)         # must not raise

    # One past it, and well past it, are rejected.
    for n in (_MAX_CANDIDATES + 1, 1000):
        try:
            _validate_batch_size(n)
        except ValueError as e:
            assert str(n) in str(e), f"error should name the count {n}: {e}"
            assert str(_MAX_CANDIDATES) in str(e), (
                f"error should name the limit {_MAX_CANDIDATES}: {e}")
        else:
            raise AssertionError(
                f"{n} candidates must be rejected, not truncated to "
                f"{_MAX_CANDIDATES}")


def test_batch_limit_matches_buffer_allocation():
    """The limit is only meaningful if the buffers are actually that size.

    _MAX_CANDIDATES bounds the batch because _cand_buf and _meta_buf are
    allocated at _MAX_CANDIDATES * struct size (PLPipeline.__init__).  If the
    limit is raised without the allocations, the guard starts admitting
    batches that overrun them — so tie them together here rather than trusting
    a comment.

    Scope: this proves the *allocation* scales with the limit.  For the
    CANDIDATE path that is the whole story, because 8 bytes is also the PL's
    wire size and test_matches_generator_manifest checks the layout against
    the core's own manifest.  The metadata path additionally relies on
    _META_STRUCT_SIZE matching the PL's §6.2 record, which the extractor's
    own golden manifest covers.
    """
    src = (Path(__file__).resolve().parent / "tme_driver.py").read_text(
        encoding="utf-8")
    # The allocation statement may wrap; search a whitespace-flattened copy
    # of the source rather than individual lines.
    flat = " ".join(src.split())
    for buf in ("_cand_buf", "_meta_buf"):
        needle = f"self.{buf} = allocate("
        start = flat.find(needle)
        assert start >= 0, f"could not find the {buf} allocation"
        stmt = flat[start:start + 120]
        assert "_MAX_CANDIDATES" in stmt, (
            f"{buf} is not sized from _MAX_CANDIDATES, so the batch guard "
            f"does not actually protect it: {stmt}")

    assert _CAND_STRUCT_SIZE == 8, "candidate word is one 64-bit AXIS beat"
    assert _META_STRUCT_SIZE == 16, "§6.2 metadata record is 128 bits"


def test_result_record_path_stays_deleted():
    """Tripwire, inverted 2026-08-11: the §6.3 result record must NOT exist.

    This test used to assert the driver's known-wrong 14-byte unpack so the
    14-vs-16 mismatch could not be half-fixed.  §6.3 was then CLOSED by
    removing the record from the MVP ABI outright — the overlay has no
    class_score_core and no result DMA, match_template() reads tme_top_0's
    scalar result registers, and the PS owns argmax and box construction
    (contract §10 items 4-5).

    So the guarded failure mode is now the record CREEPING BACK without a
    contract amendment: a _RESULT_STRUCT_FMT reappearing in the driver means
    someone is unpacking a stream that nothing in the MVP produces.  If a PL
    classifier is ever re-instated (§6.4's standing condition), amend §6.3
    first, then replace this test with layout assertions against the new
    record.
    """
    import tme_driver
    assert not hasattr(tme_driver, "_RESULT_STRUCT_FMT"), (
        "a result-record format is back in tme_driver — §6.3 was closed by "
        "DELETING this path (2026-08-11); amend the contract before "
        "reintroducing it")
    assert not hasattr(tme_driver, "_RESULT_STRUCT_SIZE"), (
        "a result-record size is back in tme_driver — §6.3 was closed by "
        "DELETING this path (2026-08-11); amend the contract before "
        "reintroducing it")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:                      # noqa: BLE001
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
