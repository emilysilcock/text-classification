"""Shared log.jsonl writer.

Used by the labelling skill (and any future script wanting a workspace
diary). Kept identical to agentic-clustering's `_log.py` so a workspace
that's shared between both plugins ends up with one log shape, not two.

Stdlib only (no third-party deps); safe to import from any PEP 723 script
in this directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def append_log(
    log_path: Path,
    action: str,
    detail: str,
    metadata: dict | None = None,
) -> None:
    """Append one JSON line to ``log_path``.

    Schema: ``{"timestamp": "<iso8601 Z>", "action": ..., "detail": ...}``,
    plus an optional ``"metadata": {...}`` when ``metadata`` is non-empty.
    Timestamps are UTC, second precision, matching the rest of the codebase.
    """
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "detail": detail,
    }
    if metadata:
        entry["metadata"] = metadata
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
