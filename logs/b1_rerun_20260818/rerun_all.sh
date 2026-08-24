#!/usr/bin/env bash
# Re-run the B1 variants through the isolated per-variant reset projects.
# Sequential on purpose: one tool, one license, and a failure part-way through
# must leave the earlier variants' evidence intact.
set -u
cd /c/Users/lychee/Desktop/FPGA/hls/template_match || exit 1
OUT=/c/Users/lychee/Desktop/FPGA/logs/b1_rerun_20260818
VR=/c/AMDDesignTools/2025.2/Vitis/bin/vitis-run.bat
rc_all=0
for v in "$@"; do
  echo "=== $v begin $(date -Is) ===" | tee -a "$OUT/rerun_status.txt"
  TME_SOLUTION=$v "$VR" --mode hls --tcl run_hls_b1.tcl > "$OUT/run_${v}.log" 2>&1
  rc=$?
  echo "=== $v end $(date -Is) rc=$rc ===" | tee -a "$OUT/rerun_status.txt"
  [ $rc -ne 0 ] && rc_all=$rc
done
echo "ALL DONE rc=$rc_all $(date -Is)" | tee -a "$OUT/rerun_status.txt"
exit $rc_all
