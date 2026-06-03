# text-classification

A Claude Code plugin for **LLM-driven text classification**. Apply a fixed set of category labels to a corpus of texts, using Claude or GPT under the hood, with prompt caching and structured outputs so every row gets a valid label, a confidence score, and a one-sentence rationale.

The plugin is provider-agnostic and category-agnostic. Bring your own `categories.json` and your own corpus; the plugin handles the call, the schema, the caching, and the batch/async plumbing.

## Prerequisites

- **[`uv`](https://docs.astral.sh/uv/)** — the skill scripts run via `uv run` and resolve their own dependencies (PEP 723), so no manual `pip install` is needed.
- **An API key** for whichever provider you pick:
  - `OPENAI_API_KEY` — default. Uses GPT-5-mini: cheap, fast, and automatic prompt caching kicks in at ≥1024 tokens.
  - `ANTHROPIC_API_KEY` — uses Claude Haiku 4.5. Comparable cost on large prompts but caching requires ≥~4096 tokens, so small category sets don't cache.

## Quick start

```text
/classify-run
```

The skill asks for:

1. **Categories** — a path to a `categories.json` file (see [format](#categoriesjson) below).
2. **Corpus** — a CSV / JSON / JSONL with at least a text column and (ideally) an ID column.
3. **Execution mode** — `async` for small corpora (< ~1000 rows, fast turnaround) or `batch` for large ones (~50% cheaper, ≤ 24h SLA).

Output is a timestamped CSV with one row per text: `id`, `label`, `confidence`, `reasoning`, plus cache stats.

## Commands

| Command | What it does |
|---|---|
| `/classify-run` | Apply a `categories.json` to a corpus → labelled CSV |
| `/classify-label` | Walk through a sample of texts and hand-label each → `labels.json` validation set |
| `/classify-tune` | Generate prompt-header variants, score each against `labels.json`, recommend the best → `tuned_prompt.md` |
| `/classify-report-issue` | File a GitHub issue with workspace context attached |

Commands may appear namespaced in the `/` menu as `/text-classification:classify-run`, etc.

## File formats

### `categories.json`

A JSON list of category definitions. The classify skills build the system prompt from these descriptions and use the `id`s as the structured-output enum.

```json
[
  {"id": "spam",   "name": "Spam",          "description": "Unsolicited bulk messages, scams, mass marketing."},
  {"id": "ham",    "name": "Legitimate",    "description": "Genuine personal or transactional messages."},
  {"id": "none",   "name": "Out of scope",  "description": "Doesn't fit either category — empty, garbled, or off-topic."}
]
```

- `id` — short slug used as the structured-output enum value and the label in outputs. Stable across runs.
- `name` — display name (used in the prompt).
- `description` — what makes a text fit this category, distinguishing it from neighbours. The more precise, the better the classifier.

If you include a `"none"`-shaped category, the classifier is allowed to assign it. If you omit one, every text gets one of the real categories (i.e. `--force-assign` semantics is implicit in the category set).

### `labels.json`

The hand-label validation set produced by `/classify-label` and consumed by `/classify-tune`. Format:

```json
[
  {"id": "row-001", "label": "spam", "text": "FREE Rolex click..."},
  {"id": "row-002", "label": "ham",  "text": "Hey mom, I'll be home..."}
]
```

(`/classify-tune` also accepts the dict shape `{"row-001": "spam", ...}` paired with a `--corpus` lookup for the texts.)

### Output CSV from `/classify-run`

Columns: `id`, `label`, `confidence` (1–5), `reasoning`, `cache_read_tokens`, `cache_creation_tokens`, `input_tokens`, `output_tokens`. One row per input text. Errors land in a separate column rather than dropping rows.

## Where things live

Output workspace defaults to `.claude/text-classification/` in the project root. Override with the `CLASSIFY_WORKSPACE` environment variable.

When `/classify-run` is invoked inside a project that also has an [agentic-clustering](https://github.com/emilysilcock/agentic-clustering) workspace at `.claude/clustering/`, the skills auto-detect it: a `categories.json` produced by `/cluster-finalize` is the default category file, and outputs land under `.claude/clustering/classification/` so all phase-2 artifacts sit alongside the discovery artifacts.

Per-run layout (`<workspace>/`):

- `categories.json` — the category definitions (workspace root; source of truth).
- `classification/prompt.md` — the assembled system prompt (categories + optional header), written each run for inspection.
- `classification/header.md` — optional instructions paragraph; `/classify-run` picks it up automatically when present, and `/classify-tune` writes its winner here.
- `classification/labels.json` — written by `/classify-label`.
- `classification/classifications/run_<timestamp>.csv` — one CSV per run; runs never overwrite.
- `classification/tuning/` — `/classify-tune` outputs: variant headers, per-variant runs, eval JSONs.

## Caching, modes, and cost

- **Prompt caching** is on by default. The system prompt is sent with `cache_control: ephemeral` so the cached portion costs ~10× less after the first call. Verify via the `cache_read_tokens` column.
- **Async mode** (`--mode async --concurrency 20`) runs requests in parallel with a concurrency cap. Best for < ~1000 texts or rapid iteration.
- **Batch mode** (`--mode batch`) uses the provider's Batch API. ~50% cheaper, takes minutes to hours (≤ 24h SLA). Best for full-corpus runs.

For corpora over ~1000 rows the skills default to batch and explain why.

## Use with agentic-clustering

This plugin is a hard dependency of [`agentic-clustering`](https://github.com/emilysilcock/agentic-clustering). When you install `agentic-clustering` from the `econ-nlp-plugins` marketplace, `text-classification` is auto-installed alongside it. The integration is by convention — no code coupling — and goes:

1. `/cluster-run` → `/cluster-finalize`. The latter writes `categories.json` next to `taxonomy.md` in `.claude/clustering/`.
2. (Optional) `/classify-label` → `labels.json` in the clustering workspace.
3. (Optional) `/classify-tune` → `tuned_prompt.md` in the clustering workspace.
4. `/classify-run` → labelled CSV in `.claude/clustering/classification/classifications/`.

You can also use `text-classification` entirely on its own — write a `categories.json` by hand and `/classify-run` against any corpus.

## Reporting issues

If something goes wrong during a `/classify-*` run, the orchestrator will offer to file a GitHub issue for you. You can also invoke `/classify-report-issue` any time — it asks for a one-line title and a short description, then attaches workspace context (plugin commit, the run prompt, last eval/run output, optional error tail) and files the issue against `emilysilcock/text-classification`. Raw corpus text is never auto-attached.

If you have the [GitHub CLI](https://cli.github.com/) (`gh`) installed and authenticated, the issue is filed directly. Otherwise the command prints a pre-filled `github.com/.../issues/new?title=...&body=...` URL — open it in a browser and click submit.

## Authors

- Emily Silcock <emilysilcock@fas.harvard.edu>
- Simon Löwe <loewe.sim@gmail.com>

## License

MIT — see [LICENSE](LICENSE).
