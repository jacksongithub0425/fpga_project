#!/bin/sh
# Fail-closed B2 board gate. Run from the new tme_b2 session directory only.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECKSUM_FILE="$SCRIPT_DIR/B2_BOARD_INPUTS.sha256"
CHECKSUM_FILE_SHA256=92578a6ede5dc96a438a2232727b5fba4e174fc3da48b6c640e0d5f216f9cb49
PRESTATE_FILE=${PRESTATE_FILE:-"$SCRIPT_DIR/prestate_fclks.json"}
REMOTE_HASH_LOG="$SCRIPT_DIR/02_hashes_remote.txt"
RESTORE_LOG=${RESTORE_LOG:-"$SCRIPT_DIR/04_restore.txt"}
EXPECTED_HWH_VLNV=TermCountB2:hls:tme_top:0.2

cd "$SCRIPT_DIR"

if [ ! -f "$PRESTATE_FILE" ]; then
    echo "ABORT: run 00_prestate.sh first; $PRESTATE_FILE is missing" >&2
    exit 2
fi

echo "=== MANDATORY BOARD-SIDE CONTROL HASHES ==="
date -u +%Y-%m-%dT%H:%M:%SZ
{
    sha256sum 00_prestate.sh 03_run.sh 04_restore.sh \
        "$(basename "$CHECKSUM_FILE")"
} >"$REMOTE_HASH_LOG"
cat "$REMOTE_HASH_LOG"

echo "=== FAIL-CLOSED INPUT CHECKSUM GATE ==="
date -u +%Y-%m-%dT%H:%M:%SZ
# This command must finish successfully before either Python invocation can
# construct Overlay(), which is the operation that configures the PL.
printf '%s  %s\n' "$CHECKSUM_FILE_SHA256" "$(basename "$CHECKSUM_FILE")" | \
    sha256sum -c -
sha256sum -c "$CHECKSUM_FILE"
echo "CHECKSUM_GATE_PASS 10/10 (nine suite inputs plus authenticated restore)"

echo "=== PRE-STATE VALIDATION BEFORE PL CONFIGURATION ==="
python3 - "$PRESTATE_FILE" <<'PY'
import json
import math
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    saved = json.load(fh)
keys = [f"fclk{i}_mhz" for i in range(4)]
if set(saved) != set(keys):
    raise RuntimeError(f"captured keys are {sorted(saved)}, expected {keys}")
values = [float(saved[key]) for key in keys]
if any(not math.isfinite(value) or value <= 0 for value in values):
    raise RuntimeError(f"captured clocks are not finite and positive: {values}")
for key, value in zip(keys, values):
    print(key, repr(value))
print("PRESTATE_VALIDATION_PASS 4/4")
PY

restore_on_exit() {
    run_rc=$?
    trap - EXIT HUP INT TERM
    set +e
    PRESTATE_FILE="$PRESTATE_FILE" sh "$SCRIPT_DIR/04_restore.sh" \
        >"$RESTORE_LOG" 2>&1
    restore_rc=$?
    cat "$RESTORE_LOG"
    if [ "$run_rc" -ne 0 ]; then
        echo "B2_GATE_FAILED: suite/check exit $run_rc; restore exit $restore_rc" >&2
        exit "$run_rc"
    fi
    if [ "$restore_rc" -ne 0 ]; then
        echo "B2_GATE_FAILED: suites passed but restore exit $restore_rc" >&2
        exit "$restore_rc"
    fi
    echo "B2_GATE_PASS: both suites passed and pre-state was restored"
    exit 0
}

# Install cleanup only after the checksum and pre-state gates. From here onward
# a failing phase_s or hw invocation still restores base.bit and the captured
# clocks.
trap restore_on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "=== SUITE 1/2: phase_s (paired B1/B2 timing comparison, T=6) ==="
python3 tme_standalone_bringup.py \
    --overlay tme_standalone.bit \
    --suite phase_s \
    --expect-fclk-mhz 125 \
    --fclk-tol-mhz 0.01 \
    --expect-hwh-vlnv "$EXPECTED_HWH_VLNV"

echo "=== SUITE 2/2: hw (B2 function, T through 52 and maximum DMA geometry) ==="
python3 tme_standalone_bringup.py \
    --overlay tme_standalone.bit \
    --suite hw \
    --expect-fclk-mhz 125 \
    --fclk-tol-mhz 0.01 \
    --expect-hwh-vlnv "$EXPECTED_HWH_VLNV"

echo "BOTH_SUITES_PASS"
