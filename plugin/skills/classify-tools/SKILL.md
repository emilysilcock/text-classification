---
name: classify-tools
description: >
  Data access scripts for working with a classification workspace
  (categories.json, labels.json, prediction CSVs). Preloaded into the
  classify skills. Scripts handle pure data operations and the LLM calls
  — no orchestration.
allowed-tools: Bash
---

# Classify Tools

All scripts live at `$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/`.
`$CLAUDE_PLUGIN_ROOT` is set automatically by Claude Code and expands when
you run Bash commands — use it as-is.

## Workspace resolution

The classify skills write outputs under a workspace directory. Resolve it
once at the top of each skill invocation:

```bash
if [ -z "$CLASSIFY_WORKSPACE" ]; then
  if [ -f .claude/clustering/categories.json ]; then
    # Integrated mode — agentic-clustering produced a categories.json; share the
    # workspace so labels.json, tuning artifacts and run CSVs land alongside the
    # clustering artifacts.
    export CLASSIFY_WORKSPACE=.claude/clustering
  else
    export CLASSIFY_WORKSPACE=.claude/text-classification
  fi
fi
mkdir -p "$CLASSIFY_WORKSPACE/classification"
```

`CLASSIFY_WORKSPACE` always points to the directory **containing**
`categories.json`. All derived outputs live under
`$CLASSIFY_WORKSPACE/classification/`:

| Path | Written by | Purpose |
|---|---|---|
| `$CLASSIFY_WORKSPACE/categories.json` | user / cluster-finalize | The category definitions. Source of truth for both the prompt body and the structured-output enum. |
| `classification/header.md` | user / classify-tune | Optional instructions paragraph prepended to the prompt. Classify-run picks it up automatically when present. |
| `classification/prompt.md` | classify-run | The assembled prompt (categories + header/footer), written each run for inspection. |
| `classification/labels.json` | classify-label | Hand-labelled validation set. |
| `classification/classifications/run_<ts>.csv` | classify-run | One CSV per run, timestamped, never overwritten. |
| `classification/tuning/` | classify-tune | Variant headers, per-variant runs, eval JSONs. |

## categories.json format

A non-empty JSON list of `{id, name, description}` objects. The `id`s form
the structured-output enum (one of these must be returned per text). The
`description`s render into the system prompt as the body of each category.

```json
[
  {"id": "spam", "name": "Spam",         "description": "Unsolicited bulk messages, scams, mass marketing."},
  {"id": "ham",  "name": "Legitimate",   "description": "Genuine personal or transactional messages."},
  {"id": "none", "name": "Out of scope", "description": "Doesn't fit either — empty, garbled, or off-topic."}
]
```

Including a `"none"`-id category makes "none" a legal classifier output and
switches `classify.py`'s default header/footer to OOS-aware language.
Omitting it makes every text get a real category (the old `--force-assign`
behaviour, now implicit). Duplicate ids are rejected at load.

## Classification

```bash
# Apply categories to a corpus. Supports openai (default, gpt-5-mini) and
# anthropic (claude-haiku-4-5). Mode batch is ~50% cheaper but takes ≤24h;
# implemented for both providers and auto-chunked under each provider's
# input-file cap (Anthropic 256 MB / 100k req; OpenAI 200 MB).
uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/classify.py \
  --input <corpus.csv|json|jsonl> --text-col <text_col> --id-col <id_col> \
  --categories $CLASSIFY_WORKSPACE/categories.json \
  --output $CLASSIFY_WORKSPACE/classification/classifications/<run>.csv \
  --provider openai --model gpt-5-mini \
  --mode async --concurrency 20
# Optional: --header <path>  prepend an instructions paragraph (default builtin)
# Optional: --footer <path>  append output guidance (default builtin)
# Optional: --prompt-output <path>  save the assembled prompt for inspection
# Optional: --no-id  use row indexes (explicit; a typo in --id-col is fatal,
#                    not a silent index fallback)
# Optional: --overwrite  allow clobbering an existing --output file
# Requires OPENAI_API_KEY (or ANTHROPIC_API_KEY for --provider anthropic).
# Prompt caching is on by default — verify via the cache_read_tokens column.
# OpenAI caches automatically once the cacheable prefix is ≥1024 tokens.
# Anthropic Haiku 4.5 needs ≥~4096 tokens to cache; small category sets don't qualify.
# Output CSV columns: id, label, confidence, reasoning, error,
# input_tokens, cache_read_tokens, output_tokens.
```

## Tuning support

```bash
# Convert labels.json (from /classify-label) to a {id, text} corpus for
# classify.py. Used by /classify-tune.
uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/labels_to_corpus.py \
  --labels $CLASSIFY_WORKSPACE/classification/labels.json \
  --corpus <path/to/corpus.json>  # optional, supplies text bodies when missing
  --output $CLASSIFY_WORKSPACE/classification/tuning/labelled_corpus.json
# Pass --corpus when labels.json was written in dict-form ({id: label}) so
# the script can recover text bodies from the original corpus.

# Evaluate predictions against human labels.
uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/evaluate_prompt.py \
  --predictions <classifications.csv> \
  --labels      $CLASSIFY_WORKSPACE/classification/labels.json \
  --output      <eval.json>
# Output: accuracy, per-label precision/recall/F1, disagreement list, written
# to --output (no stdout dump). Labels JSON accepts {id: label} or
# [{"id": ..., "label": ...}, ...]. The "cluster" key is also accepted as a
# back-compat alias.
```

## Issue reporting

```bash
# File a GitHub issue against emilysilcock/text-classification with workspace
# context attached. Used by /classify-report-issue and the "when something
# goes wrong" escalation paths in the other skills.
uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/report_issue.py \
  --title "<one-line title>" \
  --body  "<short description>"
# Optional: --no-include-categories, --no-include-run-errors,
#           --include-log-tail 0 to opt out of any attached context.
# Optional: --prefer-url to skip gh and always emit a pre-filled web URL.
```
