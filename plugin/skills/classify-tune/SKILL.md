---
name: classify-tune
description: >
  Tune the classification prompt by generating header variants, scoring each
  against human labels, and recommending the best. Requires labels.json from
  /classify-label.
allowed-tools: Bash, Read, Write
---

# Prompt Tuning

Find the best classification prompt header by generating variants tailored
to the category set and observed errors, scoring each against human labels,
and recommending the winner.

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
mkdir -p "$CLASSIFY_WORKSPACE/classification/tuning"
```

## Workflow

### 1. Verify prerequisites

Check that the workspace contains:
- `categories.json`
- `classification/labels.json` (from `/classify-label`)

If `labels.json` is missing, tell the user to run `/classify-label` first.

### 2. Run a baseline classification (no header override)

This gives you (a) accuracy of the built-in header and (b) a list of
disagreements that inform the variants.

```bash
# Build a {id, text} corpus subset from labels.json (drops the label column
# so classify.py can consume it). Pass --corpus when labels.json is in dict
# shape and needs text bodies recovered from the original corpus.
uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/labels_to_corpus.py \
  --labels $CLASSIFY_WORKSPACE/classification/labels.json \
  --output $CLASSIFY_WORKSPACE/classification/tuning/labelled_corpus.json

uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/classify.py \
  --input $CLASSIFY_WORKSPACE/classification/tuning/labelled_corpus.json \
  --text-col text --id-col id \
  --categories $CLASSIFY_WORKSPACE/categories.json \
  --output $CLASSIFY_WORKSPACE/classification/tuning/baseline.csv \
  --provider openai --model gpt-5-mini --mode async

uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/evaluate_prompt.py \
  --predictions $CLASSIFY_WORKSPACE/classification/tuning/baseline.csv \
  --labels $CLASSIFY_WORKSPACE/classification/labels.json \
  --output $CLASSIFY_WORKSPACE/classification/tuning/baseline.eval.json
```

Read the eval output. Note the accuracy and inspect the `disagreements`
list — patterns in disagreements (e.g., "the model keeps confusing c3 with
c5", "unclear cases get assigned to a category instead of `none`") are
what the variants need to address.

### 3. Generate header variants

You (the orchestrator) generate **3-4 candidate headers**, conditioned on:
- The category set (read `categories.json`)
- The baseline disagreements (read `baseline.eval.json`)

Aim for variants that target distinct hypothetical failure modes. Examples
of useful axes:
- **Strictness** — when in doubt, prefer `none` (reduces false positives on
  weak fits). Only useful when `categories.json` includes `none`.
- **Focus** — classify by core meaning, not incidental details (reduces
  spurious assignments based on a single keyword).
- **Boundary handling** — explicit instructions for distinguishing the
  category pairs that actually got confused in the baseline.

Write each variant to a file:

```
<workspace>/classification/tuning/header_<name>.txt
```

Keep variants focused — a single, additional paragraph of guidance, not a
rewrite. The body of the prompt (category definitions) is generated from
`categories.json` and stays the same across variants.

### 4. Score each variant

For each variant header:

```bash
NAME=strict

uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/classify.py \
  --input $CLASSIFY_WORKSPACE/classification/tuning/labelled_corpus.json \
  --text-col text --id-col id \
  --categories $CLASSIFY_WORKSPACE/categories.json \
  --header $CLASSIFY_WORKSPACE/classification/tuning/header_${NAME}.txt \
  --output $CLASSIFY_WORKSPACE/classification/tuning/run_${NAME}.csv \
  --provider openai --model gpt-5-mini --mode async

uv run $CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/evaluate_prompt.py \
  --predictions $CLASSIFY_WORKSPACE/classification/tuning/run_${NAME}.csv \
  --labels      $CLASSIFY_WORKSPACE/classification/labels.json \
  --output      $CLASSIFY_WORKSPACE/classification/tuning/eval_${NAME}.json
```

Run variants in parallel where possible (each `classify.py` call is
independent). Use the same provider and model for all variants for fair
comparison — provider changes prompt caching, schema enforcement, and
tokenization, so swapping it mid-experiment confounds the accuracy diff.

### 5. Compare and recommend

Read all eval JSON files, compare accuracy. Show the user a table:

```
header              accuracy   disagreements   notes
baseline            72%        14/50           keeps assigning weak fits to c3
strict              82%        9/50            ↓ false positives, ↑ none-rate
focus               78%        11/50           helps c3/c5 boundary
strict_focus        85%        7.5/50          best on this validation set
```

Recommend the best, but explicitly flag:
- Whether the difference is within noise (sample size matters — 50 labels
  means a single label's worth is 2%)
- Whether one variant is much better on a specific category the user cares
  about, even if not best overall
- That the user can override the recommendation

### 6. Save the chosen header

After the user confirms (or accepts the default recommendation), copy the
winning variant header into the location `/classify-run` looks for. Use a
Python one-liner so it works on both Git Bash and vanilla PowerShell —
`cp` is not available in stock PowerShell. Paths are passed as argv (not
interpolated into the source string) so a space or quote in
`$CLASSIFY_WORKSPACE` can't break the call:

```bash
uv run python -c "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])" \
  "$CLASSIFY_WORKSPACE/classification/tuning/header_<chosen>.txt" \
  "$CLASSIFY_WORKSPACE/classification/header.md"
```

`/classify-run` will pick up `classification/header.md` automatically on
the next run. If the user already had a hand-written `header.md`, ask
before overwriting.

## Notes

- **No majority-vote eval.** Single run per variant. Sample size is the
  trust signal — bigger labels.json = more reliable comparison.
- **Be honest about noise.** With 50 labels, accuracy differences under 5%
  are often not meaningful. Show absolute counts alongside percentages.
- **Variants don't need to win on overall accuracy** to be useful. If a
  variant resolves a specific confusion the user cares about (e.g., the
  category that downstream analysis hinges on), that may matter more than
  aggregate accuracy.
- **Cost** — N variants × M labels = N×M classifier calls per tuning run.
  With caching this is cheap, but warn the user if running > 4 variants on
  > 200 labels.

## When something goes wrong

If `evaluate_prompt.py` or `labels_to_corpus.py` fails in a way you can't
explain, or the tuning sweep produces accuracy numbers that look obviously
broken (e.g. every variant at 0% or 100% on a balanced label set), ask the
user once whether to file a GitHub issue with the workspace context
attached. On yes, invoke `/classify-report-issue` (or call
`$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/report_issue.py`
directly). Don't offer for an honest no-improvement-over-baseline result —
that's a real outcome, not a bug.
