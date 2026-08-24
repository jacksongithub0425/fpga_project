"""Reproduce the two fail-open paths the audit named, before any fix."""
import sys, io
from pathlib import Path
SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
sys.path.insert(0, str(SW))

import board_gate_extract as G
import board_gate_recovery as R
import tme_driver as d
import test_board_gate_recovery as T


# ---- hole 1: an unexpected R5 result escapes run_all's abort handler -------
def repro_r5_escape():
    sc = T.Scenario(T.load_vectors())
    rep = G.Report()
    real_start = d.PLPipeline._start

    def reworded(self, core, label):
        # Same guard, different wording: an "unexpected R5 result".
        if not self._transfers_outstanding:
            raise AssertionError("no _begin_stage")
        ctrl = core.read(d._AP_CTRL_OFF)
        if not ctrl & d._AP_IDLE:
            raise RuntimeError(f"{label}: core busy (AP_CTRL=0x{ctrl:08X})")
        core.write(d._AP_CTRL_OFF, d._AP_START)

    d.PLPipeline._start = reworded
    err = None
    try:
        with T._Swapped(sc.vc):
            try:
                R.run_all(sc.make_pipeline, "fake.bit", sc.cases, sc.patches,
                          sc.templs, rep, reset_fn=sc.reset_fn,
                          hold_fn=sc.hold_fn, deadline_s=R.DEADLINE_S,
                          natural_s=2.5715)
            except Exception as exc:
                err = exc
    finally:
        d.PLPipeline._start = real_start

    pl5 = sc.pipelines[-1]
    return {
        "err": f"{type(err).__name__}: {str(err)[:70]}",
        "resets": sc.resets,
        "holds": len(sc.holds),
        "outstanding": getattr(pl5, "_transfers_outstanding", None),
        "closed": getattr(pl5, "_closed", None),
        "pipelines": len(sc.pipelines),
    }


# ---- hole 2: fail_stop() prints before it holds ----------------------------
class DeadStdout(io.TextIOBase):
    def write(self, s):
        raise BrokenPipeError("stdout is gone")
    def flush(self):
        raise BrokenPipeError("stdout is gone")


def repro_fail_stop_print():
    held = []
    def hold_fn(objs, bitfile):
        held.append(list(objs))
        raise T.HeldForever("held")
    old = sys.stdout
    sys.stdout = DeadStdout()
    outcome = None
    try:
        R.fail_stop(hold_fn, None, "fake.bit", "why")
    except T.HeldForever:
        outcome = "held"
    except BaseException as exc:
        outcome = f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout = old
    return {"outcome": outcome, "hold_calls": len(held)}


def repro_final_reprogram_print():
    old = sys.stdout
    sys.stdout = DeadStdout()
    outcome = None
    try:
        R.final_reprogram("no-such-file.bit")   # Overlay import fails -> print
        outcome = "returned"
    except BaseException as exc:
        outcome = f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout = old
    return {"outcome": outcome}


if __name__ == "__main__":
    a = repro_r5_escape()
    b = repro_fail_stop_print()
    c = repro_final_reprogram_print()
    sys.stdout.write("\n=== HOLE 1: unexpected R5 result ===\n")
    for k, v in a.items():
        sys.stdout.write(f"  {k:14} = {v}\n")
    sys.stdout.write("=== HOLE 2a: fail_stop() with broken stdout ===\n")
    for k, v in b.items():
        sys.stdout.write(f"  {k:14} = {v}\n")
    sys.stdout.write("=== HOLE 2b: final_reprogram() with broken stdout ===\n")
    for k, v in c.items():
        sys.stdout.write(f"  {k:14} = {v}\n")
