#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Evaluate classifications against human labels.

Reads a predictions CSV (output of classify.py) and a labels JSON, joins on
ID, and reports overall accuracy, per-label precision/recall/F1, and the
list of disagreements. Writes a JSON report to --output.

Labels JSON is one of:
  - ``{"id1": "label_a", "id2": "none", ...}``
  - ``[{"id": "id1", "label": "label_a"}, {"id": "id2", "label": "none"}, ...]``

The list form may also use ``"cluster"`` instead of ``"label"`` — kept as an
alias for backwards compatibility with labels.json files written by older
versions of /cluster-label.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

# Force UTF-8 on stdout/stderr — Windows defaults to cp1252 and crashes on
# non-ASCII category names / corpus content. Idempotent; no-op on streams
# that aren't TextIOWrapper (e.g. captured in tests).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_labels(path: Path) -> dict[str, str]:
    """Load labels JSON. Accepts both shapes. The ``"cluster"`` alias on
    list-form entries is intentional — older /cluster-label runs wrote that
    field name, and we don't want to break tuning against an existing
    labels.json after the rename."""
    # utf-8-sig tolerates BOM that PowerShell 5.1 puts on UTF-8 files.
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("id", item.get("text_id", "")))
            label = item.get("label", item.get("cluster"))
            if tid and label is not None:
                out[tid] = str(label)
        return out
    print(f"error: unrecognised labels format in {path}", file=sys.stderr)
    sys.exit(1)


def load_predictions(path: Path, id_col: str, label_col: str) -> dict[str, str]:
    out = {}
    # utf-8-sig handles a leading BOM in case the predictions CSV came from
    # PowerShell's `Set-Content -Encoding utf8`; without it csv.DictReader's
    # first column header would be `﻿text` and joins would silently miss.
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = str(row.get(id_col, "")).strip()
            label = (row.get(label_col) or "").strip()
            if tid and label:
                out[tid] = label
    return out


def compute_metrics(labels: dict[str, str], preds: dict[str, str]) -> dict:
    overlap = sorted(set(labels) & set(preds))
    if not overlap:
        return {"error": "no overlapping IDs between labels and predictions"}

    agree = 0
    disagreements = []
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    support = defaultdict(int)

    for tid in overlap:
        h = labels[tid]
        g = preds[tid]
        support[h] += 1
        if h == g:
            agree += 1
            tp[h] += 1
        else:
            disagreements.append({"id": tid, "human": h, "predicted": g})
            fp[g] += 1
            fn[h] += 1

    total = len(overlap)
    accuracy = agree / total

    per_label = {}
    for cid in sorted(set(list(tp) + list(fp) + list(fn) + list(support))):
        precision = tp[cid] / (tp[cid] + fp[cid]) if (tp[cid] + fp[cid]) else 0.0
        recall = tp[cid] / (tp[cid] + fn[cid]) if (tp[cid] + fn[cid]) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_label[cid] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support[cid],
            "predicted": tp[cid] + fp[cid],
        }

    return {
        "total": total,
        "agree": agree,
        "disagree": len(disagreements),
        "accuracy": round(accuracy, 4),
        # Field names describe what the ids ARE, not what's missing — the
        # earlier `missing_labels`/`missing_predictions` invited the reverse
        # reading (i.e. "labels that are missing").
        "predictions_without_labels": sorted(set(preds) - set(labels)),
        "labels_without_predictions": sorted(set(labels) - set(preds)),
        "per_label": per_label,
        "disagreements": disagreements,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--predictions", required=True, help="Path to classify.py output CSV")
    p.add_argument("--labels", required=True, help="Path to labels JSON")
    p.add_argument("--id-col", default="id")
    p.add_argument(
        "--label-col",
        default="label",
        help=(
            "Name of the prediction column in the CSV. Defaults to 'label' "
            "(classify.py's output). Pass 'cluster' to read predictions from "
            "older agentic-clustering CSVs."
        ),
    )
    p.add_argument(
        "--output",
        required=True,
        help=(
            "Path to write the eval JSON. Required so downstream consumers "
            "(e.g. /classify-tune's variant comparison) have a stable file to "
            "read; no stdout dump."
        ),
    )
    args = p.parse_args()

    labels = load_labels(Path(args.labels))
    preds = load_predictions(Path(args.predictions), args.id_col, args.label_col)
    metrics = compute_metrics(labels, preds)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.output}", file=sys.stderr)

    if "accuracy" in metrics:
        print(
            f"accuracy: {metrics['accuracy']:.1%} "
            f"({metrics['agree']}/{metrics['total']})",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
