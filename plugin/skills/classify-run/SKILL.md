---
name: classify-run
description: >
  Classify every text in a corpus into a category from categories.json via
  Claude or GPT, with prompt caching, structured-output enforcement, and
  async or batch execution. Output is a CSV with label, confidence, and
  reasoning per text.
allowed-tools: Bash, Read, Write
---

# Classify Run

Apply a `categories.json` to a corpus. The skill assembles a system prompt
from the category descriptions (plus an optional `header.md`), then sends
each text to the chosen provider with structured outputs enforcing
`label ∈ {category_ids}`.

## Environment

Scripts at `$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/`. Resolve
the workspace at the top of every script call:

```bash
if [ -z "$CLASSIFY_WORKSPACE" ]; then
  if [ -f .claude/clustering/categories.json ]; then
    export CLASSIFY_WORKSPACE=.claude/clustering
  else
    export CLASSIFY_WORKSPACE=.claude/text-classification
  fi
fi
mkdir -p "$CLASSIFY_WORKSPACE/classification/classifications"
```

## API keys

Require one of these environment variables, depending on `--provider`:
- `OPENAI_API_KEY` (default; uses GPT-5-mini — cheap, fast, automatic prompt cache works at any prompt size ≥1024 tokens)
- `ANTHROPIC_API_KEY` (uses Claude Haiku 4.5 — comparable cost on large prompts but cache requires ~4096-token minimum, so small category sets don't cache)

If the relevant key is not set, stop and tell the user to set it.

## Workflow

### 1. Locate categories.json

Check `$CLASSIFY_WORKSPACE/categories.json`. If missing, ask the user for a
path and either:
- Copy it into the workspace, or
- Pass the path explicitly via `--categories <path>` to `classify.py` (the
  rest of the workspace can still hold derived outputs).

If the user wants to start from scratch, point them at the format
documented in `classify-tools` SKILL.md and offer to scaffold a stub.

### 2. Get input corpus from user

Ask: **what corpus to classify?** Need a CSV / JSON / JSONL path with at
least a text column. Confirm:
- File path
- Text column name (default `text`)
- ID column name (default `id`). If the corpus has no ID column, the user
  must say so explicitly — pass `--no-id` to `classify.py` for a row-index
  fallback. A typoed `--id-col` value is a fatal error, not a silent
  fallback.

### 3. Choose execution mode

Ask the user (or pick a default):
- **`async`** — real-time with concurrency cap (~20 in parallel). Use for
  small corpora (< 1000 texts) or when you want fast turnaround.
- **`batch`** — provider Batch API. **~50% cheaper** but takes minutes to
  hours. Use for full-corpus runs.

For corpora over ~1000 texts, default to `batch` and tell the user why
(cost saving). Confirm before submitting.

### 4. Run classification

```bash
RUN_NAME=run_$(date -u +%Y%m%dT%H%M%SZ)

# Header override: classify-tune writes its winner to header.md when the
# user accepts the recommendation. Picked up automatically here.
HEADER_ARG=""
if [ -f "$CLASSIFY_WORKSPACE/classification/header.md" ]; then
  HEADER_ARG="--header $CLASSIFY_WORKSPACE/classification/header.md"
fi

uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/classify.py \
  --input <corpus_path> \
  --text-col <text_col> --id-col <id_col> \
  --categories $CLASSIFY_WORKSPACE/categories.json \
  $HEADER_ARG \
  --prompt-output $CLASSIFY_WORKSPACE/classification/prompt.md \
  --output $CLASSIFY_WORKSPACE/classification/classifications/${RUN_NAME}.csv \
  --provider openai --model gpt-5-mini \
  --mode async --concurrency 20
```

For large-corpus runs (the default above ~1000 texts), swap
`--mode async --concurrency 20` for `--mode batch` — same script, ~50%
cheaper, ≤24h SLA.

### 5. Report

Read the script's stderr summary (totals, errors, token usage). Surface to
the user:
- Number classified, errors
- Cache hit rate (large = caching is working; near 0% means the corpus is
  too small to cross the per-model cache threshold, or the prompt is
  changing between runs)
- Output path

Show a small sample (first 10 rows) so the user can sanity-check labels.

## Notes

- **Prompt caching is on by default** — the system prompt is sent with
  `cache_control: ephemeral` on every call, which cuts cost ~10× for the
  cached portion after the first request. Verify it's working via the
  `cache_read_tokens` column.
- **Structured outputs are enforced** — the JSON schema constrains the
  `label` field to the IDs defined in `categories.json`, so the output is
  always parseable and assigns a valid category.
- **Force-assign is implicit.** If `categories.json` includes a `"none"`
  entry, the classifier may pick it; if not, every text gets a real
  category. No separate flag — the categories list is the only source of
  truth.
- **Re-runs are non-destructive** — each run gets its own timestamped
  output file under `classifications/`. The script refuses to overwrite an
  existing `--output` unless `--overwrite` is passed, so a fat-finger can't
  silently clobber a multi-hour batch result.

## When something goes wrong

If `classify.py` exits with errors you can't diagnose (auth failures aside
— those are the user's to fix), the provider returns malformed structured
output repeatedly, or batch retrieval hangs / fails, ask the user once
whether to file a GitHub issue with the workspace context attached. On
yes, invoke `/classify-report-issue` (or call
`$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/report_issue.py`
directly). Skip the offer for missing API keys, unsupported file formats,
or anything the error message itself tells the user how to fix.
