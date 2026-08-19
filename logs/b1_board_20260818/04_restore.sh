set -u
echo "=== RESTORE ==="
date -u +%Y-%m-%dT%H:%M:%SZ
hostname
echo "--- clocks BEFORE restore ---"
python3 -c "from pynq.ps import Clocks; [print(f'fclk{i}_mhz', getattr(Clocks, f'fclk{i}_mhz')) for i in range(4)]"
echo "--- loading base.bit ---"
python3 -c "
from pynq import Overlay
from pynq.ps import Clocks
Overlay('/usr/local/share/pynq-venv/lib/python3.10/site-packages/pynq/overlays/base/base.bit')
print('base.bit loaded')
Clocks.fclk0_mhz = 100.0
Clocks.fclk1_mhz = 142.857143
Clocks.fclk2_mhz = 200.0
Clocks.fclk3_mhz = 100.0
print('clocks written')
"
echo "--- clocks AFTER restore (must match pre-state) ---"
python3 -c "
from pynq.ps import Clocks
want = [100.0, 142.857143, 200.0, 100.0]
ok = True
for i, w in enumerate(want):
    g = getattr(Clocks, f'fclk{i}_mhz')
    good = abs(g - w) < 1e-6
    ok &= good
    print(f'fclk{i}_mhz {g}  want {w}  ' + ('OK' if good else 'MISMATCH'))
print('RESTORE_VERIFIED' if ok else 'RESTORE_FAILED')
"
echo "--- shipping artifacts untouched? ---"
ls -ld /home/xilinx/jupyter_notebooks/tme_* 
echo "=== RESTORE END ==="
