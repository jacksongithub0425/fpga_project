#!/usr/bin/env python3
"""Priority 1 - offline validation of scale-set reduction.

Trial reduction is the only modelled route below ten seconds per page, so it is
gated on accuracy, not on speed.  This tool answers the accuracy question in two
passes, because they answer different things and only the second is a gate:

  --study    Offline replay over the captured trace.  For every one of the
             20,680 initial and 808 refinement trials it reconstructs the
             detector's SELECTION score, picks the winner under the full
             eight-scale oracle and under a candidate policy, and reports where
             they diverge - per call, per endpoint classification, and in
             modelled latency.  No re-rendering; runs in about a second.

             LIMIT: the trace was captured with the patch envelope sized at
             max(scales)=1.50.  This pass therefore isolates the SCALE-CHOICE
             effect while holding geometry fixed.  It cannot see the second-order
             effect of a smaller envelope changing the raw argmax.

  --parity   Re-runs the real detector over the corpus with the policy in force
             and compares per-page counts and the detection digest against
             baseline_cpu_20260811.  This is the gate.  It sees everything,
             including the envelope change.  About 30 s per policy.

             --pin-envelope keeps the patch envelope at 1.50 while searching
             only the policy's scales, which is the geometry contract Phase S
             wants anyway (search window decoupled from the template maximum).
             Without it, reducing the scale list also shrinks the patch, and the
             two effects are confounded.

Usage:

    python tme_scale_policy.py --trace ../../trace_20260817 --study
    python tme_scale_policy.py --trace ../../trace_20260817 --study --policy 0.8,1.0,1.2
    python tme_scale_policy.py --parity "../../sample/*" --policy 0.8,1.0,1.2 --pin-envelope

Run with the HLS venv python.

HOW THE SELECTION SCORE IS RECONSTRUCTED
----------------------------------------
`best_template_match_local` does not rank by the raw correlation.  It ranks by

    adjusted = raw - 0.12 * hypot(anchor - endpoint) / max(8, 0.5*(tw+th))

where the anchor is `side_template_anchor(template, side)`, a pure function of
the base template size and the side: (w-1, h/2) for left, (0, h/2) for right.
Every input to that expression is in the trace, so the ranking is reproducible
exactly.  Refinement calls rank by the location-penalised map argmax instead,
which the trace already carries as `adj_score`.

Ties resolve on original global order, matching the detector's strict `>`.
"""

from __future__ import annotations

import argparse
import csv
import glob as globmod
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tme_cycle_model as model                        # noqa: E402

ANCHOR_WEIGHT = 0.12
FULL_SCALES = (0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.35, 1.50)
ENVELOPE_SCALE = 1.50

SCORE_THRESH = 0.33
FERRULE_SCORE_THRESH = 0.24
SCORE_MARGIN = 0.03

# Phase S crops each trial's search area to this many result positions
# (Priority 3).  The matcher geometry that follows from it is the one the
# capture used: pw = tw + RESULT_W - 1, ph = th + RESULT_H - 1.
RESULT_W, RESULT_H = 96, 64

# The columns this file reports.  B0b appears as the two ENDPOINTS its
# projected count-pass II brackets, because a single B0b number would imply an
# II that synthesis has never reported.
VARIANTS = ("S", "S_B1", "S_B2", "S_B0b@1", "S_B0b@3")

_CYCLE_CACHE = {}


def trial_cycles(t, variant):
    """Cycles for one trial under `variant`, recomputed from the model.

    Deliberately NOT the trace's cycles_* columns -- see latency().  Memoised
    on (tw, th, variant): the workload has 20,680 trials but only a few hundred
    distinct template geometries.
    """
    key = (t["tw"], t["th"], variant)
    hit = _CYCLE_CACHE.get(key)
    if hit is not None:
        return hit
    tw, th = t["tw"], t["th"]
    pw, ph = tw + RESULT_W - 1, th + RESULT_H - 1
    if variant == "S":
        v = model.cycles(pw, ph, tw, th, "cur")
    elif variant == "S_B1":
        v = model.cycles(pw, ph, tw, th, "B1")
    elif variant == "S_B2":
        v = model.cycles(pw, ph, tw, th, "B2")
    elif variant == "S_B0b@1":
        v = model.cycles_b0b(pw, ph, tw, th, 1)
    elif variant == "S_B0b@3":
        v = model.cycles_b0b(pw, ph, tw, th, 3)
    else:
        raise ValueError("unknown variant: " + variant)
    _CYCLE_CACHE[key] = v
    return v


# ---------------------------------------------------------------------------
# selection-score reconstruction
# ---------------------------------------------------------------------------
def selection_score(r):
    """The value best_template_match_local actually ranks this trial by."""
    if r["call_kind"] == "refinement":
        return r["adj_score"]
    if r["side"] == "left":
        ax0, ay0 = float(r["base_w"] - 1), 0.5 * r["base_h"]
    else:
        ax0, ay0 = 0.0, 0.5 * r["base_h"]
    x = r["px0"] + r["raw_loc"][0]
    y = r["py0"] + r["raw_loc"][1]
    anchor_x = x + ax0 * r["scale"]
    anchor_y = y + ay0 * r["scale"]
    dist = math.hypot(anchor_x - r["endpoint"][0], anchor_y - r["endpoint"][1])
    norm = dist / max(8.0, 0.5 * (r["tw"] + r["th"]))
    return r["raw_score"] - ANCHOR_WEIGHT * norm


def box_of(r):
    return (r["px0"] + r["raw_loc"][0], r["py0"] + r["raw_loc"][1], r["tw"], r["th"])


def load_trace(trace_dir):
    """Group trials into calls, preserving original global order.

    Returns (calls, pages).  `pages` counts every *_trials.jsonl the capture
    wrote, INCLUDING the empty ones -- a page with no endpoint still costs a
    page of wall time, and the model divides by the whole corpus.  Counting
    distinct pages among the calls instead gives 34 and silently rebases every
    s/page figure downstream.
    """
    calls = []
    files = sorted(globmod.glob(os.path.join(trace_dir, "*_trials.jsonl")))
    for f in files:
        page = os.path.basename(f)[: -len("_trials.jsonl")]
        cur, key = None, None
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            r["sel"] = selection_score(r)
            r["page"] = page
            k = (r["call_kind"], r["side"], r["kind"], r["templ_index"],
                 r["endpoint"][0], r["endpoint"][1])
            if k != key:
                cur = {"page": page, "key": k, "call_kind": r["call_kind"],
                       "side": r["side"], "kind": r["kind"],
                       "templ_index": r["templ_index"],
                       "endpoint": tuple(r["endpoint"]), "trials": []}
                calls.append(cur)
                key = k
            cur["trials"].append(r)
    return calls, len(files)


def unsearched_neighbours(scale, allowed):
    """Oracle scales adjacent to `scale` that the policy does not already search."""
    i = FULL_SCALES.index(scale)
    return {FULL_SCALES[j] for j in (i - 1, i + 1)
            if 0 <= j < len(FULL_SCALES) and FULL_SCALES[j] not in allowed}


def pick(trials, allowed):
    """Winner under a scale set.  Strict >, so ties keep the earliest trial."""
    best = None
    for t in trials:
        if allowed is not None and t["scale"] not in allowed:
            continue
        if best is None or t["sel"] > best["sel"]:
            best = t
    return best


# ---------------------------------------------------------------------------
# endpoint-level classification replay
# ---------------------------------------------------------------------------
def classify(per_kind_best):
    """Replay classify_endpoint's ranking from per-kind winners."""
    hits = {k: v for k, v in per_kind_best.items() if v is not None}
    if not hits:
        return "unknown", -1.0, None
    ranked = sorted(hits.items(), key=lambda kv: kv[1]["sel"], reverse=True)
    best_kind, best_hit = ranked[0]
    second = ranked[1][1]["sel"] if len(ranked) > 1 else -1.0
    needed = FERRULE_SCORE_THRESH if best_kind == "ferrule" else SCORE_THRESH
    if best_hit["sel"] < needed or (best_hit["sel"] - second) < SCORE_MARGIN:
        return "unknown", best_hit["sel"], box_of(best_hit)
    return best_kind, best_hit["sel"], box_of(best_hit)


def endpoint_view(calls, allowed, call_kind="initial"):
    """endpoint -> (kind, score, box) under a scale set."""
    by_ep = defaultdict(dict)
    for c in calls:
        if c["call_kind"] != call_kind:
            continue
        w = pick(c["trials"], allowed)
        if w is None:
            continue
        ep = (c["page"], c["endpoint"], c["side"])
        prev = by_ep[ep].get(c["kind"])
        if prev is None or w["sel"] > prev["sel"]:
            by_ep[ep][c["kind"]] = w
    return {ep: classify(kb) for ep, kb in by_ep.items()}


# ---------------------------------------------------------------------------
# study
# ---------------------------------------------------------------------------
def study(calls, policy, pages, fallback_edges=True):
    allowed = set(policy)
    edges = {min(policy), max(policy)}

    stats = {"calls": 0, "same_scale": 0, "same_box": 0, "fallback": 0}
    deficits = []
    per_kindstat = defaultdict(lambda: {"n": 0, "same_box": 0})

    for c in calls:
        if c["call_kind"] != "initial":
            continue
        o = pick(c["trials"], None)
        p = pick(c["trials"], allowed)
        if o is None or p is None:
            continue
        stats["calls"] += 1
        if o["scale"] == p["scale"]:
            stats["same_scale"] += 1
        same_box = box_of(o) == box_of(p)
        if same_box:
            stats["same_box"] += 1
        deficits.append(o["sel"] - p["sel"])
        ks = per_kindstat[c["kind"]]
        ks["n"] += 1
        ks["same_box"] += int(same_box)
        # A winner on the edge of the searched range triggers widening by one
        # oracle step.  It only counts as a trigger when such a step EXISTS:
        # 0.70 and 1.50 are the oracle's own boundaries, so a winner there has
        # nothing further to fall back to and costs nothing.
        if fallback_edges and p["scale"] in edges and unsearched_neighbours(p["scale"], allowed):
            stats["fallback"] += 1

    ev_o = endpoint_view(calls, None)
    ev_p = endpoint_view(calls, allowed)
    changed_class = sum(1 for ep in ev_o if ev_o[ep][0] != ev_p.get(ep, ("unknown",))[0])
    changed_box = sum(1 for ep in ev_o if ev_o[ep][2] != ev_p.get(ep, (None, None, None))[2])
    counts_o = defaultdict(int)
    counts_p = defaultdict(int)
    for ep, (k, _, _) in ev_o.items():
        counts_o[(ep[0], k)] += 1
    for ep, (k, _, _) in ev_p.items():
        counts_p[(ep[0], k)] += 1
    pages_count_changed = len({p for (p, k) in set(counts_o) | set(counts_p)
                               if counts_o[(p, k)] != counts_p[(p, k)]})

    deficits.sort()

    def pct(v):
        return 100.0 * v / max(stats["calls"], 1)

    print("SCALE POLICY STUDY - offline replay over the captured trace")
    print("=" * 78)
    print("policy            {}".format(", ".join("{:.2f}".format(s) for s in policy)))
    print("oracle            {}".format(", ".join("{:.2f}".format(s) for s in FULL_SCALES)))
    print("geometry          held at the 1.50 envelope (scale choice isolated)")
    print()
    print("PER CALL  (one candidate x one template)")
    print("-" * 78)
    print("  calls compared           {}".format(stats["calls"]))
    print("  same winning scale       {} ({:.2f}%)".format(stats["same_scale"], pct(stats["same_scale"])))
    print("  same winning box         {} ({:.2f}%)".format(stats["same_box"], pct(stats["same_box"])))
    print("  MISSED WINNERS           {} ({:.2f}%)".format(
        stats["calls"] - stats["same_box"], pct(stats["calls"] - stats["same_box"])))
    print("  winner on policy edge    {} ({:.2f}%)   <- fallback trigger rate".format(
        stats["fallback"], pct(stats["fallback"])))
    if deficits:
        n = len(deficits)
        print("  selection-score deficit  mean {:.4f}  p50 {:.4f}  p95 {:.4f}  max {:.4f}".format(
            sum(deficits) / n, deficits[n // 2], deficits[min(n - 1, int(n * 0.95))], deficits[-1]))
    print()
    print("  by kind:")
    for k in sorted(per_kindstat):
        s = per_kindstat[k]
        print("    {:<9} {:5d} calls   {:.2f}% same box".format(
            k, s["n"], 100.0 * s["same_box"] / max(s["n"], 1)))
    print()
    print("PER ENDPOINT  (after classify_endpoint ranking, thresholds and margin)")
    print("-" * 78)
    print("  endpoints                {}".format(len(ev_o)))
    print("  CHANGED CLASS            {} ({:.2f}%)".format(
        changed_class, 100.0 * changed_class / max(len(ev_o), 1)))
    print("  changed box              {} ({:.2f}%)".format(
        changed_box, 100.0 * changed_box / max(len(ev_o), 1)))
    print("  pages with a count delta {}".format(pages_count_changed))
    print()
    latency(calls, policy, pages)
    print()
    print("  This pass is a screen, not the gate.  Run --parity for class/box/count")
    print("  parity against baseline_cpu_20260811, which also sees the envelope change.")
    return {"missed": stats["calls"] - stats["same_box"], "changed_class": changed_class,
            "fallback_rate": pct(stats["fallback"])}


def latency(calls, policy, pages):
    """s/page under the policy, including fallbacks and all refinement calls.

    EVERY CYCLE FIGURE HERE IS RECOMPUTED from tme_cycle_model at the Phase-S
    geometry.  None of them is read from the trace's cycles_* columns.  Those
    columns were computed by the model AS IT STOOD ON CAPTURE DAY, and one of
    them has since gone stale: `cycles_S_B1` in trace_20260818b carries the
    WITHDRAWN tile projection T*(2*tw + 40) rather than the measured
    T*(2*tw + 41) + 1, understating B1 by 425,680,640 cycles on the initial
    trials alone (trace_20260818b/B1_COLUMN_STALE.md).  This function used to
    sum that column and report 1.039 s/page of B1 refinement; the recomputed
    figure is 1.042909980.  Summing a captured column silently inherits
    whatever the model believed when the capture ran.  Recomputing cannot.

    It also used to read `cycles_S_B0b`, a key no trace has ever carried -- the
    capture writes `cycles_S_B0b_base`, the window-statistics DELETION, which
    is not a runnable variant.  That raised KeyError and killed the whole
    table, so no latency figure printed at all.  B0b is now computed through
    model.cycles_b0b() and reported as the II=1 / II=3 ENDPOINTS it is honestly
    bracketed by, never as a single number.

    `pages` is the CORPUS page count, not the number of pages that happened to
    produce a call.  Two of the 36 pages hold no endpoint, so the old
    `len({c["page"] for c in calls})` gave 34 and inflated every s/page here by
    36/34 = 5.9%, against a model that divides by 36 throughout.
    """
    corpus = model.FROZEN["workload"]["pages"]
    if pages != corpus:
        raise SystemExit(
            "trace covers {} pages, the frozen corpus is {}.  s/page is only "
            "comparable to the model when both divide by the same corpus."
            .format(pages, corpus))

    allowed = set(policy)
    edges = {min(policy), max(policy)}
    tot = defaultdict(int)

    for c in calls:
        trials = c["trials"]
        # The oracle leg: all eight scales, no fallback.  This is the
        # denominator of the "vs 8-scale" column, DERIVED here from the same
        # trials rather than transcribed from a frozen literal.
        for k in VARIANTS:
            tot[("oracle", k)] += sum(trial_cycles(t, k) for t in trials)
        if c["call_kind"] == "refinement":
            # Refinement is unconditional: the policy does not reduce it, and
            # all 808 calls are charged to every column.
            for k in VARIANTS:
                tot[("refine", k)] += sum(trial_cycles(t, k) for t in trials)
            continue
        kept = [t for t in trials if t["scale"] in allowed]
        for k in VARIANTS:
            tot[("initial", k)] += sum(trial_cycles(t, k) for t in kept)
        w = pick(trials, allowed)
        if w is not None and w["scale"] in edges:
            # Fallback: widen by one oracle step on each side of the winner.
            nb = unsearched_neighbours(w["scale"], allowed)
            extra = [t for t in trials if t["scale"] in nb]
            for k in VARIANTS:
                tot[("fallback", k)] += sum(trial_cycles(t, k) for t in extra)

    # Bind this tool to the freeze.  The full-oracle initial leg IS the frozen
    # workload aggregate; if these disagree, one of the two files has drifted
    # and neither number should be quoted until that is resolved.
    oracle_initial_b1 = tot[("oracle", "S_B1")] - tot[("refine", "S_B1")]
    frozen_b1 = model.FROZEN["b1"]["aggregate_cycles"]
    if oracle_initial_b1 != frozen_b1:
        raise SystemExit(
            "full-oracle B1 initial total {} != FROZEN['b1']['aggregate_cycles'] "
            "{}.  The trace and the model disagree about the same workload."
            .format(oracle_initial_b1, frozen_b1))

    hz = model.TARGET_CLOCK_HZ
    print("MODELLED LATENCY  s/page @ {:g} MHz  (initial + fallback + refinement)"
          .format(hz / 1e6))
    print("-" * 78)
    print("  recomputed from tme_cycle_model at the Phase-S geometry over {} pages;"
          .format(pages))
    print("  the trace's own cycles_* columns are NOT read (cycles_S_B1 is stale).")
    print()
    print("  {:<9} {:>9} {:>10} {:>11} {:>9} {:>10}   {}".format(
        "variant", "initial", "fallback", "refinement", "TOTAL", "8-scale", "speedup"))
    for k in VARIANTS:
        i = tot[("initial", k)] / pages / hz
        f = tot[("fallback", k)] / pages / hz
        rf = tot[("refine", k)] / pages / hz
        base = tot[("oracle", k)] / pages / hz
        print("  {:<9} {:9.3f} {:10.3f} {:11.3f} {:9.3f} {:10.3f}   {:.2f}x".format(
            k, i, f, rf, i + f + rf, base, base / max(i + f + rf, 1e-9)))
    print()
    print("  MODELLED, NOT MEASURED.  No page has been run on hardware at any")
    print("  clock.  S_B1's tile term is measured; its page figure is not.  S_B2")
    print("  and S_B0b have no RTL at all, and S_B0b's II is bracketed, not known.")


# ---------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------
class _Args:
    """The subset of cpu_baseline_snapshot's CLI namespace that run_one reads."""
    zoom = 4.0
    score_thresh = SCORE_THRESH
    ferrule_score_thresh = FERRULE_SCORE_THRESH
    score_margin = SCORE_MARGIN


def parity(patterns, policy, pin_envelope, baseline_dir):
    """Re-run the real detector under the policy and diff against the baseline.

    Drives cpu_baseline_snapshot's own run_one/counts_of/detections_digest so
    the canonicalisation is identical to the one that produced the baseline
    manifest.  Rolling a second serialisation here would compare two different
    orderings and report spurious divergence.
    """
    import terminal_counter_endpoint_first as det
    import cpu_baseline_snapshot as snap

    manifest = {}
    with open(os.path.join(baseline_dir, "baseline_manifest.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            manifest[(row["input_sha256"], int(row["page"]))] = row

    orig_scales = det.MATCH_SCALES
    orig_bep = det.build_endpoint_patch
    det.MATCH_SCALES = tuple(policy)

    if pin_envelope:
        ratio = ENVELOPE_SCALE / max(policy)

        def pinned(ex, ey, side, img_w, img_h, max_tw, max_th):
            # Keep the search envelope at the 1.50 geometry while the scale set
            # shrinks, so scale choice and geometry are not confounded.
            return orig_bep(ex, ey, side, img_w, img_h,
                            int(max_tw * ratio), int(max_th * ratio))
        det.build_endpoint_patch = pinned

    pdfs = sorted({p for pat in patterns for p in globmod.glob(pat)
                   if os.path.isfile(p) and p.lower().endswith(".pdf")},
                  key=lambda p: os.path.basename(p).lower())

    print("PARITY  policy {}  envelope {}".format(
        ",".join("{:.2f}".format(s) for s in policy),
        "pinned at 1.50" if pin_envelope else "follows max(policy)"))
    print("=" * 78)
    same = diff = count_delta = 0
    delta_total = {"male": 0, "female": 0, "ferrule": 0, "unknown": 0}
    try:
        side_templates = snap.load_templates(_Args)
        for pdf in pdfs:
            p = Path(pdf)
            sha = snap._sha256_file(p)
            for page_index, detections, _n in snap.run_one(p, side_templates, _Args):
                base = manifest.get((sha, page_index))
                if base is None:
                    print("  {} p{}  NO BASELINE ROW".format(p.name, page_index))
                    continue
                got = snap.detections_digest(detections)
                nc = snap.counts_of(detections)
                bc = {k: int(base[k]) for k in ("male", "female", "ferrule", "unknown")}
                if got == base["detections_sha256"]:
                    same += 1
                    continue
                diff += 1
                if bc != nc:
                    count_delta += 1
                    for k in delta_total:
                        delta_total[k] += nc[k] - bc[k]
                    print("  {:<28} p{}  {} -> {}".format(
                        base["anon_id"], page_index,
                        {k: bc[k] for k in bc if bc[k] or nc[k]},
                        {k: nc[k] for k in nc if bc[k] or nc[k]}))
    finally:
        det.MATCH_SCALES = orig_scales
        det.build_endpoint_patch = orig_bep

    total = same + diff
    print()
    print("  pages compared             {}".format(total))
    print("  byte-identical detections  {} ({:.1f}%)".format(
        same, 100.0 * same / max(total, 1)))
    print("  differing detections       {} ({:.1f}%)".format(
        diff, 100.0 * diff / max(total, 1)))
    print("  of which counts changed    {}".format(count_delta))
    print("  net count delta            {}".format(
        {k: v for k, v in delta_total.items() if v}))
    print()
    print("  GATE: a policy is selectable only when this accuracy loss is accepted.")
    return diff, count_delta


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("patterns", nargs="*", help="PDF glob(s), for --parity")
    ap.add_argument("--trace", help="trace dir from tme_trace_capture.py")
    ap.add_argument("--policy", default="0.8,1.0,1.2")
    ap.add_argument("--study", action="store_true")
    ap.add_argument("--parity", action="store_true")
    ap.add_argument("--pin-envelope", action="store_true",
                    help="keep the patch envelope at 1.50 under a reduced scale set")
    ap.add_argument("--baseline", default=str(HERE.parents[1] / "baseline_cpu_20260811"))
    args = ap.parse_args()

    policy = tuple(float(s) for s in args.policy.split(","))
    for s in policy:
        if s not in FULL_SCALES:
            raise SystemExit("policy scale {} is not in the oracle set".format(s))

    if args.study:
        if not args.trace:
            raise SystemExit("--study needs --trace")
        calls, pages = load_trace(args.trace)
        study(calls, policy, pages)
    if args.parity:
        if not args.patterns:
            raise SystemExit("--parity needs a PDF glob")
        parity(args.patterns, policy, args.pin_envelope, args.baseline)
    if not args.study and not args.parity:
        raise SystemExit("pick --study and/or --parity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
