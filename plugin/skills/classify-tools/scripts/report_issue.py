#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""File a GitHub issue against the text-classification repo.

Attaches workspace context (plugin commit, categories.json summary, last
classification run's error tail, log.jsonl tail when present) so a
maintainer can reproduce a problem without further back-and-forth. Raw
corpus text is never auto-included — only category structure, run
metadata, and the user's own description.

Two paths:
  1. Try ``gh issue create`` if the GitHub CLI is installed and authed.
  2. Otherwise print a pre-filled ``issues/new?title=...&body=...`` URL.

Either way, the constructed title and body are also echoed to stderr so
the LLM caller (and the user) can see exactly what was sent.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = "emilysilcock/text-classification"
WEB_NEW_URL = f"https://github.com/{REPO}/issues/new"

# GitHub's URL-prefill endpoint is fine with long bodies but browsers and the
# server both have ceilings. Stay well under both — truncate the auto-context
# rather than emit a URL that 414s. (Body itself goes much higher when filed
# via `gh`; this cap only constrains the URL-fallback path.)
MAX_URL_BODY_CHARS = 6000


def get_workspace() -> Path:
    """Resolve the workspace. Precedence:

    1. ``CLASSIFY_WORKSPACE`` env var
    2. ``.claude/clustering/`` if it exists (integrated agentic-clustering mode)
    3. ``.claude/text-classification/``

    Same resolution order as the classify skills, so report_issue.py picks up
    whatever context the latest run used.
    """
    env = os.environ.get("CLASSIFY_WORKSPACE")
    if env:
        return Path(env)
    cluster_ws = Path(".claude/clustering")
    if cluster_ws.exists() and (cluster_ws / "categories.json").exists():
        return cluster_ws
    return Path(".claude/text-classification")


def _plugin_commit() -> str | None:
    """Best-effort plugin commit hash. Returns None if not in a git checkout."""
    script_dir = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(script_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _read_tail(path: Path, n_lines: int) -> str | None:
    """Return the last ``n_lines`` of ``path`` (UTF-8). None if missing/empty."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])


def _categories_summary(workspace: Path) -> dict | None:
    """Pull a small non-PII summary of categories.json. None on miss/parse-fail."""
    cat_path = workspace / "categories.json"
    if not cat_path.exists():
        return None
    try:
        with open(cat_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return {
        "n_categories": len(data),
        "ids": [c.get("id") for c in data if isinstance(c, dict)],
        "has_none": any(isinstance(c, dict) and c.get("id") == "none" for c in data),
    }


def _last_run_errors(workspace: Path, max_errors: int = 10) -> dict | None:
    """Walk ``classifications/`` for the most recent run CSV and summarise its
    error column (rows with non-empty error). Returns None when no run exists."""
    # /classify-run writes to <workspace>/classifications/ for the standalone
    # case and <workspace>/classification/classifications/ for the integrated
    # (with-clustering) case. Try both.
    candidates = [
        workspace / "classifications",
        workspace / "classification" / "classifications",
    ]
    for runs_dir in candidates:
        if not runs_dir.exists():
            continue
        csvs = sorted(runs_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        if not csvs:
            continue
        latest = csvs[-1]
        try:
            with open(latest, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except (OSError, csv.Error):
            return {"latest_run": str(latest), "error": "could not read CSV"}
        n_total = len(rows)
        errs = [r for r in rows if r.get("error")]
        sample = [
            {"id": r.get("id"), "error": (r.get("error") or "")[:200]}
            for r in errs[:max_errors]
        ]
        return {
            "latest_run": str(latest),
            "n_total": n_total,
            "n_errors": len(errs),
            "error_sample": sample,
        }
    return None


def build_body(
    user_description: str,
    *,
    workspace: Path,
    include_categories: bool,
    include_log_tail: int,
    include_run_errors: bool,
) -> str:
    """Compose the issue body.

    The user's free-text description always leads. The auto-collected context
    sits below a separator, so a maintainer can fold/skim past it.
    """
    parts: list[str] = []
    parts.append("**User report**")
    parts.append(user_description.strip() or "(no description provided)")
    parts.append("")
    parts.append("---")
    parts.append("**Context** (auto-collected — no raw corpus text)")

    commit = _plugin_commit()
    if commit:
        parts.append(f"- Plugin commit: `{commit}`")
    parts.append(f"- Workspace: `{workspace}`")
    parts.append(f"- Platform: `{sys.platform}` / Python `{sys.version.split()[0]}`")

    if include_categories:
        cats = _categories_summary(workspace)
        if cats is not None:
            parts.append("")
            parts.append("**Categories summary**")
            parts.append("```json")
            parts.append(json.dumps(cats, indent=2, ensure_ascii=False))
            parts.append("```")
        else:
            parts.append("- Categories: *(no categories.json found in workspace)*")

    if include_run_errors:
        run = _last_run_errors(workspace)
        if run is not None:
            parts.append("")
            parts.append("**Last classification run**")
            parts.append("```json")
            parts.append(json.dumps(run, indent=2, ensure_ascii=False))
            parts.append("```")

    if include_log_tail > 0:
        tail = _read_tail(workspace / "log.jsonl", include_log_tail)
        if tail is not None:
            parts.append("")
            parts.append(f"**`log.jsonl` tail (last {include_log_tail} entries)**")
            parts.append("```")
            parts.append(tail)
            parts.append("```")

    return "\n".join(parts)


def try_gh_create(title: str, body: str) -> str | None:
    """Try ``gh issue create``. Return the issue URL on success, else None."""
    if shutil.which("gh") is None:
        return None
    # `gh` reads body from a file via --body-file to avoid command-line length
    # limits and quoting hazards. tempfile cleanup happens on exit.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        body_path = tf.name
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                REPO,
                "--title",
                title,
                "--body-file",
                body_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"gh invocation failed: {e}", file=sys.stderr)
        return None
    finally:
        try:
            Path(body_path).unlink()
        except OSError:
            pass

    if proc.returncode != 0:
        # Most common cause: not authed. Surface stderr so the LLM can decide
        # whether to retry, prompt the user to `gh auth login`, or fall back.
        print(
            f"gh issue create failed (exit {proc.returncode}):\n{proc.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return url or None


def build_web_url(title: str, body: str) -> str:
    """Build a pre-filled ``issues/new`` URL, truncating body to fit safely."""
    body_for_url = body
    if len(body_for_url) > MAX_URL_BODY_CHARS:
        head = body_for_url[: MAX_URL_BODY_CHARS - 80]
        body_for_url = (
            head + "\n\n…\n*[context truncated for URL; full context available locally]*"
        )
    params = urllib.parse.urlencode({"title": title, "body": body_for_url})
    return f"{WEB_NEW_URL}?{params}"


def main():
    parser = argparse.ArgumentParser(
        description="File a GitHub issue against text-classification with workspace context attached."
    )
    parser.add_argument("--title", required=True, help="Short issue title (one line).")
    parser.add_argument("--body", required=True, help="User description. Free text.")
    parser.add_argument(
        "--include-categories",
        action="store_true",
        default=True,
        help="Attach a categories.json summary (default: on).",
    )
    parser.add_argument(
        "--no-include-categories",
        action="store_false",
        dest="include_categories",
        help="Don't attach categories summary.",
    )
    parser.add_argument(
        "--include-log-tail",
        type=int,
        default=40,
        help="Number of trailing log.jsonl entries to attach. 0 disables.",
    )
    parser.add_argument(
        "--include-run-errors",
        action="store_true",
        default=True,
        help="Attach a summary of the latest classification run's errors (default: on).",
    )
    parser.add_argument(
        "--no-include-run-errors",
        action="store_false",
        dest="include_run_errors",
        help="Don't attach last-run error summary.",
    )
    parser.add_argument(
        "--prefer-url",
        action="store_true",
        help="Skip gh and always print a pre-filled web URL (useful for dry-run review).",
    )
    args = parser.parse_args()

    workspace = get_workspace()
    body = build_body(
        args.body,
        workspace=workspace,
        include_categories=args.include_categories,
        include_log_tail=args.include_log_tail,
        include_run_errors=args.include_run_errors,
    )

    print("=== Issue title ===", file=sys.stderr)
    print(args.title, file=sys.stderr)
    print("=== Issue body ===", file=sys.stderr)
    print(body, file=sys.stderr)
    print("=== end ===", file=sys.stderr)

    if not args.prefer_url:
        url = try_gh_create(args.title, body)
        if url:
            print(url)
            print(f"Filed via gh: {url}", file=sys.stderr)
            return

    url = build_web_url(args.title, body)
    print(url)
    if args.prefer_url:
        msg = "--prefer-url set; open the URL above in a browser to submit."
    else:
        msg = (
            "gh not available or authed; open the URL above in a browser "
            "to submit the pre-filled issue."
        )
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
