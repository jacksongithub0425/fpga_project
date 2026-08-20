#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PRESTATE_FILE=${PRESTATE_FILE:-"$SCRIPT_DIR/prestate_fclks.json"}

echo "=== PRE-STATE: B2 board session ==="
date -u +%Y-%m-%dT%H:%M:%SZ
hostname
uname -a
echo "--- uptime ---"
uptime
echo "--- clocks BEFORE (must be restored to these) ---"
python3 - "$PRESTATE_FILE" <<'PY'
import json
import math
import sys
from pathlib import Path
from pynq.ps import Clocks

path = Path(sys.argv[1])
values = {}
for i in range(4):
    key = f"fclk{i}_mhz"
    value = float(getattr(Clocks, key))
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{key} is not finite and positive: {value!r}")
    values[key] = value
    print(key, repr(value))
path.write_text(json.dumps(values, sort_keys=True) + "\n")
print(f"PRESTATE_SAVED {path}")
PY
echo "--- memory ---"
free -m
echo "--- CMA ---"
grep Cma /proc/meminfo
echo "--- existing tme dirs: shipping artifacts, MUST NOT BE TOUCHED ---"
ls -ld /home/xilinx/jupyter_notebooks/tme_* 2>/dev/null || true
echo "--- contents of the NEW session dir (should hold only this script) ---"
ls -la /home/xilinx/jupyter_notebooks/tme_b2 2>/dev/null
echo "=== PRE-STATE END ==="
