set -u
echo "=== PRE-STATE: B2 board session ==="
date -u +%Y-%m-%dT%H:%M:%SZ
hostname
uname -a
echo "--- uptime ---"
uptime
echo "--- clocks BEFORE (must be restored to these) ---"
python3 -c "from pynq.ps import Clocks; [print(f'fclk{i}_mhz', getattr(Clocks, f'fclk{i}_mhz')) for i in range(4)]"
echo "--- memory ---"
free -m
echo "--- CMA ---"
grep Cma /proc/meminfo
echo "--- existing tme dirs: shipping artifacts, MUST NOT BE TOUCHED ---"
ls -ld /home/xilinx/jupyter_notebooks/tme_* 2>/dev/null
echo "--- contents of the NEW session dir (should hold only this script) ---"
ls -la /home/xilinx/jupyter_notebooks/tme_b2 2>/dev/null
echo "=== PRE-STATE END ==="
