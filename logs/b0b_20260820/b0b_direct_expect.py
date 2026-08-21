"""Derive the expected outputs of the Priority 6 B0b direct testbench cases.

Reproduces tme_top.cpp's arithmetic exactly: exact integer sums, one float32
divide by sqrtf of a float32 product, clamp to [-1, 1], strict-> argmax with
row-major scan order.  Run with the hls/.venv python.
"""
import struct
import numpy as np


def dut(patch: np.ndarray, templ: np.ndarray):
    ph, pw = patch.shape
    th, tw = templ.shape
    rw, rh = pw - tw + 1, ph - th + 1
    n = tw * th
    T = templ.astype(np.int64)
    t_sum = int(T.sum())
    t_sq = int((T * T).sum())
    dt = n * t_sq - t_sum * t_sum
    dt_f = np.float32(dt)

    P = patch.astype(np.int64)
    best = np.float32(-2.0)
    bx = by = 0
    for v in range(rh):
        for u in range(rw):
            w = P[v:v + th, u:u + tw]
            sti = int((w * T).sum())
            sii = int((w * w).sum())
            si = int(w.sum())
            num = n * sti - si * t_sum
            di = n * sii - si * si
            if di == 0 or dt == 0:
                s = np.float32(0.0)
            else:
                s = np.float32(num) / np.sqrt(dt_f * np.float32(di),
                                              dtype=np.float32)
            if s > np.float32(1.0):
                s = np.float32(1.0)
            if s < np.float32(-1.0):
                s = np.float32(-1.0)
            if s > best:
                best, bx, by = s, u, v
    bits = struct.unpack("<I", struct.pack("<f", float(best)))[0]
    return float(best), bits, bx, by, rw, rh, dt


def step_templ():
    t = np.zeros((4, 4), np.uint8)
    t[2:, :] = 255
    return t


def step_patch(pw, ph, split):
    p = np.zeros((ph, pw), np.uint8)
    p[split:, :] = 255
    return p


cases = []

t4 = step_templ()
cases.append(("b0b-zero-40x30", np.zeros((30, 40), np.uint8), t4))
cases.append(("b0b-ones-40x30", np.full((30, 40), 255, np.uint8), t4))

tbig = np.zeros((96, 216), np.uint8)
tbig[95, :] = 255
cases.append(("b0b-ones-rh1-216x96", np.full((96, 216), 255, np.uint8), tbig))
cases.append(("b0b-ones-216x98", np.full((98, 216), 255, np.uint8), tbig))

cases.append(("b0b-step-rh2", step_patch(40, 5, 3), t4))
cases.append(("b0b-step-first-row", step_patch(40, 30, 2), t4))
cases.append(("b0b-step-mid-row", step_patch(40, 30, 15), t4))
cases.append(("b0b-step-last-row", step_patch(40, 30, 28), t4))

print("dt(step template) =", 16 * int((step_templ().astype(np.int64) ** 2).sum())
      - int(step_templ().astype(np.int64).sum()) ** 2)
for name, p, t in cases:
    score, bits, bx, by, rw, rh, dt = dut(p, t)
    print("%-24s rw=%-4d rh=%-4d dt=%-16d score=%+.9f bits=0x%08X @(%d,%d)"
          % (name, rw, rh, dt, score, bits, bx, by))
