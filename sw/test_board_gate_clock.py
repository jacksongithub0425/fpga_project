"""Drive board_gate_clock's probe off the board, on a fabric with a dial.

    python test_board_gate_clock.py       # from sw/
    pytest test_board_gate_clock.py

No PYNQ and no board, and no real seconds either: `tme_driver.time` is
replaced by a VIRTUAL clock that only advances when the simulated fabric says
it should.  That is what makes this suite possible at all — the gate's probe
is a 2.57-second transaction and the point of the suite is to run it at half a
dozen different clock frequencies.

THE FAKE HAS A FREQUENCY DIAL, AND THAT IS THE WHOLE DESIGN.  `TimedCore`
computes the modelled cycle count for whatever geometry the driver actually
staged, divides it by a settable `f_mhz`, and advances the virtual clock by
that much plus a settable per-invocation overhead.  So "the board is at
125 MHz when the variant says 100" is a real scenario here, not a mocked
return value, and the gate has to notice it the same way it would on silicon.

Every line of driver code still runs for real: validation, staging, the four
register writes, both arms, the ap_ctrl poll and the result decode.  Only the
cores and the DMA channels are simulated, and they are gate 4's fakes.

THE FIRST TEST IS THE ONE THAT MATTERS MOST.  `board_gate_clock.cycles()` is a
hand transcription of `tme_cycle_model.cycles()`, made so the board does not
have to carry a 150 KB analysis tool that discovers a corpus at import.  A
typo in that transcription would move the gate's expectations rather than
fail, so it is checked against the real model for every case in the hw
manifest, under both laws.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_gate_clock as C
import board_gate_extract as G
import board_expect as X
import tme_driver as d
from test_board_gate_extract import FakeBuf, FakeCore, MatcherRegMap  # noqa: F401
import test_board_gate_extract as TG


HERE = Path(__file__).resolve().parent


# -- the virtual clock -----------------------------------------------------

class VClock:
    """A monotonic clock that only moves when something says it should.

    Substituted for the `time` module inside `tme_driver`, so the driver's own
    polling loop advances it 0.5 ms per pass exactly as it would burn 0.5 ms
    of real time, and `TimedCore` jumps it by the modelled fabric duration.
    """

    def __init__(self, t0: float = 1000.0):
        self.t = float(t0)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


class TimedCore(FakeCore):
    """A matcher whose invocation takes `cycles(geometry) / f_mhz` seconds.

    `overhead_s` stands in for everything the gate's differential reading is
    designed to cancel: arming two channels, four scalar writes, the poll
    granularity.  It is charged once per invocation regardless of geometry,
    which is exactly the assumption the differential reading rests on — so a
    test that varies it is a test of that assumption.
    """

    def __init__(self, regmap, vc: VClock, f_mhz: float, overhead_s: float,
                 law: str = "B2", jitter=None):
        super().__init__(regmap)
        self.vc = vc
        self.f_mhz = f_mhz
        self.overhead_s = overhead_s
        self.law = law
        self.jitter = list(jitter or ())
        self.starts = 0

    def write(self, off, val):
        rm = self.register_map
        n = C.cycles(int(rm.patch_w), int(rm.patch_h),
                     int(rm.templ_w), int(rm.templ_h), self.law)
        extra = self.jitter[self.starts] if self.starts < len(self.jitter) else 0.0
        self.starts += 1
        self.vc.advance(n / (self.f_mhz * 1e6) + self.overhead_s + extra)
        super().write(off, val)


def load_vectors():
    g = G.load_bpe_golden(HERE)
    cases, patches, templs = G.load_hw_manifest(HERE)
    return g, cases, patches, templs


def make_timed_pipeline(g, cases, patches, templs, f_mhz=100.0,
                        overhead_s=0.002, law="B2", jitter=None):
    """Gate 4's fake pipeline, with its matcher put on a clock."""
    p = TG.make_fake_pipeline(g, cases, patches, templs)
    vc = VClock()
    regmap = p._tme.register_map          # keeps the golden result table
    p._tme = TimedCore(regmap, vc, f_mhz, overhead_s, law, jitter)
    return p, vc


class _Swapped:
    """Swap `tme_driver.time` for the virtual clock, and put it back."""

    def __init__(self, vc):
        self.vc = vc

    def __enter__(self):
        self.old = d.time
        d.time = self.vc
        return self.vc

    def __exit__(self, *exc):
        d.time = self.old
        return False


def run_probe(f_mhz=100.0, overhead_s=0.002, law="B2", variant="combined_b2_100",
              reps=2, jitter=None, vectors=None):
    """Run the gate's probe against a fabric at `f_mhz`.

    Returns (ok, report, error).  `ok` is False if any check failed or if
    anything raised — a run that dies is a run that did not pass.
    """
    g, cases, patches, templs = vectors or load_vectors()
    p, vc = make_timed_pipeline(g, cases, patches, templs, f_mhz, overhead_s,
                                law, jitter)
    cfg = X.variant(variant)
    rep = G.Report()
    err = None
    with _Swapped(vc):
        try:
            C.phase_probe(p, HERE, cfg, C.law_for(cfg), reps, rep)
        except Exception as exc:                             # noqa: BLE001
            err = exc
    return (not rep.failures and err is None), rep, err


# -- the transcription check ----------------------------------------------

def _analysis_model():
    """`tme_cycle_model` loaded by path — it is a tool, not a board module."""
    path = HERE / "tme_cycle_model.py"
    spec = importlib.util.spec_from_file_location("_tme_cycle_model", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_cycle_law_is_a_faithful_transcription():
    m = _analysis_model()
    _, cases, _, _ = load_vectors()
    n = 0
    for law in ("cur", "B2"):
        for c in cases:
            want = m.cycles(c.pw, c.ph, c.tw, c.th, law)
            got = C.cycles(c.pw, c.ph, c.tw, c.th, law)
            assert got == want, (
                f"{law} law at {c.pw}x{c.ph}/{c.tw}x{c.th}: gate says "
                f"{got:,}, tme_cycle_model says {want:,}")
            n += 1
    # And the two probe geometries specifically, since those are the numbers
    # the gate actually times against.
    for geom in (C.LONG_GEOM, C.SHORT_GEOM):
        for law in ("cur", "B2"):
            assert C.cycles(*geom, law) == m.cycles(*geom, law)
            n += 1
    assert n == 22, n
    print(f"  {n} geometry/law pairs agree with tme_cycle_model")


def test_the_pinned_probe_numbers_are_what_the_plan_quotes():
    """B2 at 820x307/216x96 is 257,145,732 cycles = 2.5715 s at 100 MHz."""
    n = C.cycles(*C.LONG_GEOM, "B2")
    assert n == 257_145_732, f"{n:,}"
    assert abs(n / 100e6 - 2.5715) < 5e-5
    diff = n - C.cycles(*C.SHORT_GEOM, "B2")
    assert diff == 249_549_328, f"{diff:,}"


# -- the golden run --------------------------------------------------------

def test_a_fabric_at_100_passes():
    ok, rep, err = run_probe(f_mhz=100.0)
    assert err is None, err
    assert ok, rep.failures
    assert rep.checks >= 8, rep.checks


def test_the_minimum_is_taken_so_jitter_only_hurts_a_slow_clock():
    """One badly delayed repetition must not move the verdict."""
    ok, rep, err = run_probe(f_mhz=100.0, reps=3,
                             jitter=[0.0, 0.250, 0.0, 0.0, 0.400, 0.0])
    assert err is None, err
    assert ok, rep.failures


def test_the_baseline_variant_is_timed_against_its_own_clock_and_law():
    """31.25 MHz with the `cur` law — the same machinery, different numbers."""
    ok, rep, err = run_probe(f_mhz=31.25, law="cur", variant="baseline",
                             overhead_s=0.002)
    assert err is None, err
    assert ok, rep.failures


# -- injected defects, each required to FAIL the gate ----------------------

MUTATIONS = {
    "clock_125":      dict(f_mhz=125.0),
    "clock_62_5":     dict(f_mhz=62.5),
    "clock_90_9":     dict(f_mhz=1000.0 / 11),
    "clock_off_1pct": dict(f_mhz=101.0),
    "clock_off_slow": dict(f_mhz=99.0),
    "overhead_huge":  dict(overhead_s=0.050),
    "wrong_law":      dict(law="cur"),
}


def test_every_injected_clock_defect_fails_the_gate():
    vectors = load_vectors()
    for name, kw in MUTATIONS.items():
        ok, rep, err = run_probe(vectors=vectors, **kw)
        assert not ok, (
            f"{name}: the gate PASSED a fabric it should have rejected "
            f"({kw})")
        print(f"  [detected] {name:<16} {len(rep.failures)} check(s) failed"
              + (f", {type(err).__name__}" if err else ""))


def test_a_fabric_below_the_modelled_time_is_a_finding_not_slack():
    """A clock FASTER than claimed must fail, not pass quietly."""
    ok, rep, _ = run_probe(f_mhz=100.5, overhead_s=0.0)
    assert not ok
    assert any("wall time matches" in f or "implied fabric clock" in f
               for f in rep.failures), rep.failures


def test_a_wrong_result_invalidates_the_timing():
    """A probe that computed the wrong answer must not yield a clock reading."""
    g, cases, patches, templs = load_vectors()
    p, vc = make_timed_pipeline(g, cases, patches, templs)
    tbl = p._tme.register_map._table
    long_c = C.pick_probe(cases, C.LONG_GEOM, "long")
    key = (templs[long_c.templ_off:long_c.templ_off + long_c.templ_bytes],
           long_c.pw, long_c.ph, long_c.tw, long_c.th)
    score, x, y = tbl[key]
    # One column LEFT, not right: the golden peak sits on the last column of
    # the 605x212 result map, so x+1 would be out of range and the driver's
    # own check_result would reject it before the gate ever asserted.  The
    # defect under test is a wrong-but-legal location.
    tbl[key] = (score, x - 1, y)
    cfg = X.variant("combined_b2_100")
    rep = G.Report()
    with _Swapped(vc):
        try:
            C.phase_probe(p, HERE, cfg, "B2", 1, rep)
        except G.GateError:
            pass
    assert rep.failures, "a wrong location passed the clock gate"
    assert "golden result" in rep.failures[0], rep.failures


# -- structural -------------------------------------------------------------

def test_a_missing_probe_case_is_a_setup_error_not_a_failure():
    _, cases, _, _ = load_vectors()
    thinned = [c for c in cases if (c.pw, c.ph, c.tw, c.th) != C.SHORT_GEOM]
    try:
        C.pick_probe(thinned, C.SHORT_GEOM, "short probe")
    except G.SetupError:
        return
    raise AssertionError("a manifest missing the short probe was accepted")


def test_an_unknown_matcher_core_is_refused():
    try:
        C.law_for({"matcher_vlnv": "SomethingElse:hls:tme_top:0.3"})
    except G.SetupError as exc:
        assert "cycle law" in str(exc)
        return
    raise AssertionError("an unpinned matcher core was given a cycle law")


def test_the_rung_search_names_the_real_divisor():
    for div in (8, 9, 10, 11, 16, 32):
        d_, f_ = C.nearest_rung(1000.0 / div)
        assert d_ == div, (div, d_, f_)
    # 62.5 is the trap rung and must be identified as 1000/16.
    assert C.nearest_rung(62.5) == (16, 62.5)


def test_the_selftest_passes_for_every_pinned_variant():
    for name in X.VARIANTS:
        rc = C.selftest(name)
        assert rc == 0, f"{name}: self-test returned {rc}"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:                               # noqa: BLE001
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        else:
            print(f"ok   {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
