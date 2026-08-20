#!/bin/sh
# Fail-closed B2 board gate. Run from the new tme_b2 session directory only.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECKSUM_FILE="$SCRIPT_DIR/B2_BOARD_INPUTS.sha256"
CHECKSUM_FILE_SHA256=26101c12df931dd5235fec12a99adbf2464ed3b00b9bb74d353a124ca5b93a51
PRESTATE_FILE=${PRESTATE_FILE:-"$SCRIPT_DIR/prestate_fclks.json"}
RESTORE_LOG=${RESTORE_LOG:-"$SCRIPT_DIR/04_restore.txt"}
EXPECTED_HWH_VLNV=TermCountB2:hls:tme_top:0.2

cd "$SCRIPT_DIR"

if [ ! -f "$PRESTATE_FILE" ]; then
    echo "ABORT: run 00_prestate.sh first; $PRESTATE_FILE is missing" >&2
    exit 2
fi

echo "=== FAIL-CLOSED INPUT CHECKSUM GATE ==="
date -u +%Y-%m-%dT%H:%M:%SZ
# This command must finish successfully before either Python invocation can
# construct Overlay(), which is the operation that configures the PL.
printf '%s  %s\n' "$CHECKSUM_FILE_SHA256" "$(basename "$CHECKSUM_FILE")" | \
    sha256sum -c -
sha256sum -c "$CHECKSUM_FILE"
echo "CHECKSUM_GATE_PASS 9/9 (original six plus three hw-suite vectors)"

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

# Install cleanup only after the checksum gate. From here onward a failing
# phase_s or hw invocation still restores base.bit and the captured clocks.
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
