---
name: classify-label
description: >
  Interactively label sample texts with category IDs to create a validation
  set. The user reviews each text in chat and assigns a category (or "none").
  Saves labels for use by /classify-tune.
allowed-tools: Bash, Read, Write
---

# Interactive Labelling

Build a labelled validation set by walking the user through sample texts
one at a time. Output is `labels.json` in the workspace, consumed by
`/classify-tune` and `evaluate_prompt.py`.

## Environment

Scripts at `$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/`. Resolve
the workspace before any script call:

```bash
if [ -z "$CLASSIFY_WORKSPACE" ]; then
  if [ -f .claude/clustering/categories.json ]; then
    export CLASSIFY_WORKSPACE=.claude/clustering
  else
    export CLASSIFY_WORKSPACE=.claude/text-classification
  fi
fi
mkdir -p "$CLASSIFY_WORKSPACE/classification"
```

## Workflow

### 1. Verify prerequisites

`categories.json` must exist in `$CLASSIFY_WORKSPACE`. If not, tell the
user where to put it (or, in integrated mode, to run `/cluster-finalize`
first).

### 2. Get the source corpus

Ask the user for:
- **Corpus path** (CSV / JSON / JSONL) — the file holding the texts to
  sample from.
- **Text column** (default `text`).
- **ID column** (default `id`). If the corpus has no ID column, ask
  explicitly and use row indexes.

### 3. Decide the sample

Ask the user how many texts to label. Defaults:
- Minimum useful: 30
- Recommended: 50–100
- For tight tuning of header variants: 100

Read the corpus with `Read` (or a small inline Python one-liner via Bash
if the corpus is large) and draw a random sample of N texts. Seed the RNG
explicitly so the draw is reproducible — note the seed in chat for the
user, and consider writing a short marker line to
`$CLASSIFY_WORKSPACE/log.jsonl` for provenance.

### 4. Read the category definitions

Read `categories.json` so you can show definitions to the user as they
label. The user sees `name` and `description` for each category; the `id`
is what they reply with.

### 5. Walk through each text

Initialize an empty `labels = []` (or load `<workspace>/classification/labels.json`
if it already exists and the user wants to resume).

For each sampled text, present in this format:

> **Text 3 of 50** (id: `abc-123`)
>
> _[the text, possibly truncated to ~500 chars with a note if longer]_
>
> Available categories:
> - `spam` — Spam: short description
> - `ham`  — Legitimate: ...
> - `none` — Out of scope: ... *(shown only if `none` is in categories.json)*
>
> **Which category?** (reply with the ID, or `skip`, or `quit` to save and stop)

Wait for the user's reply. Validate it's one of the category IDs, `skip`,
or `quit`. Re-prompt if invalid. If `categories.json` doesn't include a
`none`-id category, the user can't reply `none` — they must pick one of
the real categories.

- On a valid label: append `{"id": ..., "label": ..., "text": ...}` to
  `labels`. Save to `<workspace>/classification/labels.json` after each
  response (incremental save — don't lose progress if the session drops).
- On `skip`: do not record, move to the next text.
- On `quit`: save and break.

### 6. Save and report

Final write to `<workspace>/classification/labels.json` (it should already
be there from incremental saves). Report:
- How many labelled, how many skipped
- Distribution across categories (catch under-represented ones)
- Path to labels.json

If any category has 0 labels, warn the user — tuning quality suffers when
categories are missing from the validation set.

## Notes

- **Save incrementally.** Long labelling sessions get interrupted; the
  user shouldn't have to start over.
- **Don't cap text length silently.** If you truncate a text for display,
  say so explicitly: *"(truncated to first 500 chars)"*. The user might
  want to see more before deciding — accept a `more` reply to show the
  full text.
- **Don't suggest a label.** This is a validation set; suggestions bias it.
- **`labels.json` schema** — the format consumed by `/classify-tune` and
  `evaluate_prompt.py` is either `{id: label_id, ...}` or
  `[{"id": ..., "label": ..., "text": ...}, ...]`. Use the list form so we
  can attach the text for later review. The `cluster` key is also accepted
  as a back-compat alias for `label` on existing files.

## When something goes wrong

If you can't read the source corpus, can't parse `categories.json`, or
hit a repeated I/O failure when saving incrementally, ask the user once
whether to file a GitHub issue. On yes, invoke `/classify-report-issue`
(or call
`$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/report_issue.py`
directly). Don't offer for a user who just wants to stop labelling
partway through — that's a `quit`, not an error.
