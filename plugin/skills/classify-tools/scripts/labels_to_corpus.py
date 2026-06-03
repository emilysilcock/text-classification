#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Convert a labels.json file into a classifier-input corpus.

`/classify-label` writes labels.json as a list of ``{id, label, text}``
objects (one per hand-labelled text). `/classify-tune` needs to run the
same hand-labelled texts through classify.py, which expects an input of
``{id, text}`` objects with no label column. This script does the
extraction so each tuning run doesn't reinvent it.

Accepts either labels.json shape:
  - List form: ``[{"id": ..., "label": ..., "text": ...}, ...]``
    (and accepts ``"cluster"`` as an alias for ``"label"`` so labels.json
    files written by older agentic-clustering /cluster-label still work)
  - Dict form: ``{"id1": "label_a", "id2": "none", ...}`` — only useful
    when the dict was built from a corpus the caller can still cite via
    --corpus; without text bodies we cannot reconstruct an input file.

Writes a JSON list of ``{id, text}`` objects, suitable for
``classify.py --input <out> --text-col text --id-col id``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr — Windows defaults to cp1252 and crashes on
# non-ASCII category names / corpus content. Idempotent; no-op on streams
# that aren't TextIOWrapper (e.g. captured in tests).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_labels(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        # Dict form has no text bodies — caller must supply --corpus.
        return [{"id": str(k), "label": str(v)} for k, v in data.items()]
    print(
        f"error: unrecognised labels shape in {path}: expected list or dict",
        file=sys.stderr,
    )
    sys.exit(1)


def _load_corpus_lookup(path: Path) -> dict[str, str]:
    """Build {id: text} from a corpus.json (the workspace copy written by
    init.py upstream, or any similar [{id, text}, ...] list)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        print(f"error: expected a JSON list at {path}", file=sys.stderr)
        sys.exit(1)
    return {str(r["id"]): r["text"] for r in data if isinstance(r, dict) and "id" in r and "text" in r}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--labels", required=True, help="Path to labels.json (from /classify-label)")
    p.add_argument(
        "--output",
        required=True,
        help="Path to write the {id, text} JSON list (consumed by classify.py)",
    )
    p.add_argument(
        "--corpus",
        help=(
            "Optional corpus.json to source text bodies from when labels.json "
            "doesn't carry them (dict-form labels, or list-form entries with "
            "no `text` field)."
        ),
    )
    args = p.parse_args()

    labels_path = Path(args.labels)
    if not labels_path.exists():
        print(f"error: labels file not found: {labels_path}", file=sys.stderr)
        return 1

    entries = _load_labels(labels_path)
    if not entries:
        print(f"error: no usable label entries in {labels_path}", file=sys.stderr)
        return 1

    corpus_lookup: dict[str, str] = {}
    if args.corpus:
        corpus_path = Path(args.corpus)
        if not corpus_path.exists():
            print(f"error: corpus file not found: {corpus_path}", file=sys.stderr)
            return 1
        corpus_lookup = _load_corpus_lookup(corpus_path)

    records: list[dict] = []
    missing_ids: list[str] = []
    for item in entries:
        tid = str(item.get("id", ""))
        if not tid:
            continue
        text = item.get("text")
        if not text and corpus_lookup:
            text = corpus_lookup.get(tid)
        if not text:
            missing_ids.append(tid)
            continue
        records.append({"id": tid, "text": str(text).strip()})

    if missing_ids:
        print(
            f"warning: {len(missing_ids)} labelled ids had no text body and were "
            f"dropped (e.g. {missing_ids[:5]}). Pass --corpus to recover them.",
            file=sys.stderr,
        )

    if not records:
        print(
            "error: produced an empty corpus — labels.json had no text bodies "
            "and --corpus was either missing or didn't contain any of the ids.",
            file=sys.stderr,
        )
        return 1

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path} ({len(records)} records)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
