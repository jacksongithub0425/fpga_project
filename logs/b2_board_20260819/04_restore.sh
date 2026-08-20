#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PRESTATE_FILE=${PRESTATE_FILE:-"$SCRIPT_DIR/prestate_fclks.json"}

echo "=== RESTORE ==="
date -u +%Y-%m-%dT%H:%M:%SZ
hostname
if [ ! -f "$PRESTATE_FILE" ]; then
    echo "RESTORE_FAILED: captured pre-state is missing: $PRESTATE_FILE" >&2
    exit 1
fi
echo "--- clocks BEFORE restore ---"
if ! python3 -c "from pynq.ps import Clocks; [print(f'fclk{i}_mhz', getattr(Clocks, f'fclk{i}_mhz')) for i in range(4)]"; then
    echo "WARNING: could not read clocks before restore; continuing with base.bit load" >&2
fi
echo "--- loading base.bit and restoring captured clocks ---"
python3 - "$PRESTATE_FILE" <<'PY'
import json
import math
import sys
from pynq import Overlay
from pynq.ps import Clocks

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        saved = json.load(fh)
    keys = [f"fclk{i}_mhz" for i in range(4)]
    if set(saved) != set(keys):
        raise RuntimeError(f"captured keys are {sorted(saved)}, expected {keys}")
    want = [float(saved[key]) for key in keys]
    if any(not math.isfinite(v) or v <= 0 for v in want):
        raise RuntimeError(f"captured clocks are not finite and positive: {want}")

    Overlay('/usr/local/share/pynq-venv/lib/python3.10/site-packages/'
            'pynq/overlays/base/base.bit')
    print('base.bit loaded')
    for i, value in enumerate(want):
        setattr(Clocks, f'fclk{i}_mhz', value)
    print('captured clocks written')

    ok = True
    for i, expected in enumerate(want):
        got = float(getattr(Clocks, f'fclk{i}_mhz'))
        good = math.isfinite(got) and abs(got - expected) < 1e-6
        ok = ok and good
        print(f'fclk{i}_mhz {got!r}  captured {expected!r}  ' +
              ('OK' if good else 'MISMATCH'))
    if not ok:
        raise RuntimeError('one or more restored clocks do not match pre-state')
except Exception as exc:
    print(f'RESTORE_FAILED: {exc}', file=sys.stderr)
    raise SystemExit(1)

print('RESTORE_VERIFIED')
PY
echo "--- shipping artifacts untouched? ---"
ls -ld /home/xilinx/jupyter_notebooks/tme_* 2>/dev/null || true
echo "=== RESTORE END ==="
