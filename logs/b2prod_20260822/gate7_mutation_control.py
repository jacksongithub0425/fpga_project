"""Mutation control for the gate 7 fail-open fixes.

Each fix is reverted ON ITS OWN in a scratch copy of board_gate_recovery.py,
that copy is imported under the real module name, and the extended suite is
run against it.  A fix whose revert does not fail a test is a fix with no
test, which is how the last two fail-open gates survived review.

    python mutctl.py            # from the scratchpad
"""
import importlib.util
import io
import subprocess
import sys
from pathlib import Path

SW = Path(r"C:\Users\lychee\Desktop\FPGA\.github-upload\sw")
HERE = Path(__file__).resolve().parent
LIVE = io.open(SW / "board_gate_recovery.py", encoding="utf-8").read()

# (name, what the revert undoes, [(fixed_text, original_text), ...],
#  tests that MUST fail)
REVERTS = [
    (
        "r5_guard",
        "R5's body runs outside any abort handler again",
        [(
            r'''    try:
        return _phase_r5_body(pl, cases, patches, templs, stall_case,
                              bitfile, rep, reset_fn, hold_fn, observe_s)
    except BaseException:''',
            r'''    if True:
        return _phase_r5_body(pl, cases, patches, templs, stall_case,
                              bitfile, rep, reset_fn, hold_fn, observe_s)
    if False:''',
        )],
        ["test_an_unexpected_r5_result_still_closes_and_reprograms",
         "test_no_run_ever_leaves_a_pipeline_holding_unprotected_pages"],
    ),
    (
        "r4_guard",
        "R4 goes back to _discard-in-a-finally",
        [(
            r'''    except BaseException:
        # Not `_discard`: this phase can abort with a transfer still armed --''',
            r'''    except BaseException:
        _discard(pl)
        raise
    if False:
        # Not `_discard`: this phase can abort with a transfer still armed --''',
        )],
        ["test_no_run_ever_leaves_a_pipeline_holding_unprotected_pages"],
    ),
    (
        "fail_stop_say",
        "fail_stop() announces itself with print() again",
        [(r'''    safe_teardown.say("\n" + why)
    held = held_objects(pl)''',
          r'''    print("\n" + why)
    held = held_objects(pl)''')],
        ["test_a_broken_stdout_cannot_skip_the_fail_stop_hold"],
    ),
    (
        "final_reprogram_say",
        "final_reprogram()'s failure message is a print() again",
        [(r'''        safe_teardown.say(f"\nFINAL REPROGRAM FAILED: "
                          f"{type(exc).__name__}: {exc}")''',
          r'''        print(f"\nFINAL REPROGRAM FAILED: "
              f"{type(exc).__name__}: {exc}")''')],
        ["test_a_broken_stdout_cannot_skip_the_final_reprograms_verdict"],
    ),
]

RUNNER = r'''
import importlib.util, sys
from pathlib import Path
SW = Path(sys.argv[1]); MUT = Path(sys.argv[2])
sys.path.insert(0, str(SW))
spec = importlib.util.spec_from_file_location("board_gate_recovery", MUT)
m = importlib.util.module_from_spec(spec)
sys.modules["board_gate_recovery"] = m
spec.loader.exec_module(m)
import test_board_gate_recovery as T
assert T.R is m, "the reverted module was not the one under test"
out = []
for name in sys.argv[3:]:
    t = getattr(T, name)
    try:
        t()
    except BaseException as e:
        out.append(f"{name}\tFAILED\t{type(e).__name__}: {str(e)[:90]}")
    else:
        out.append(f"{name}\tpassed\t-")
sys.stderr.write("RESULTS\n" + "\n".join(out) + "\n")
'''

runner_path = HERE / "_mutctl_runner.py"
io.open(runner_path, "w", encoding="utf-8").write(RUNNER)

overall = 0
for name, what, pairs, must_fail in REVERTS:
    src = LIVE
    for fixed, original in pairs:
        assert src.count(fixed) == 1, (name, src.count(fixed), fixed[:60])
        src = src.replace(fixed, original)
    mut = HERE / f"_mut_{name}.py"
    io.open(mut, "w", encoding="utf-8", newline="").write(src)

    r = subprocess.run([sys.executable, str(runner_path), str(SW), str(mut),
                        *must_fail],
                       capture_output=True, text=True)
    tail = r.stderr.split("RESULTS\n")[-1].strip().splitlines()
    print(f"\n=== revert: {name} -- {what}")
    if not tail or "RESULTS" not in r.stderr:
        print(f"  RUNNER ERROR (exit {r.returncode}):")
        print("  " + "\n  ".join(r.stderr.strip().splitlines()[-8:]))
        overall = 1
        continue
    for line in tail:
        test, verdict, detail = line.split("\t")
        mark = "OK  " if verdict == "FAILED" else "MISS"
        if verdict != "FAILED":
            overall = 1
        print(f"  [{mark}] {test}")
        print(f"         {verdict}: {detail}")

print("\n" + ("MUTATION CONTROL PASSED: every reverted fix is caught"
              if overall == 0 else
              "MUTATION CONTROL FAILED: a reverted fix went undetected"))
raise SystemExit(overall)
