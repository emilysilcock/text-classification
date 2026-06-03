---
name: classify-report-issue
description: >
  File a GitHub issue against the text-classification repo with workspace
  context attached. Use when a /classify-* run is stuck, the classifier or
  evaluator failed in an unexpected way, or the user wants to flag a bug.
allowed-tools: Bash, Read
---

# Report a classification issue

You are filing a GitHub issue on behalf of the user against
`emilysilcock/text-classification`. The heavy lifting is done by
`report_issue.py` — your job is to collect a one-line title and a short
free-text description from the user, then invoke the script.

## Environment

Scripts live at `$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/`.
Workspace resolution mirrors the other classify skills:

```bash
if [ -z "$CLASSIFY_WORKSPACE" ]; then
  if [ -f .claude/clustering/categories.json ]; then
    export CLASSIFY_WORKSPACE=.claude/clustering
  else
    export CLASSIFY_WORKSPACE=.claude/text-classification
  fi
fi
```

It is fine to run this skill outside a configured workspace — the script
will simply omit any context that isn't there.

## Flow

1. **Ask for the title.** One sentence, present tense, like a git commit
   subject ("classify.py async hangs on empty rows", "tune recommends a
   header that drops accuracy"). If you already know the problem from the
   surrounding conversation, propose a title and let the user edit.

2. **Ask what went wrong.** Free-text description. If the failure is one
   you just saw in this conversation, summarize it first (1-3 sentences,
   including the command that failed and the error excerpt), then let the
   user add to or correct it.

3. **Ask whether to attach the workspace context.** Defaults to yes. The
   attached context includes:
   - Plugin commit hash (if discoverable)
   - Workspace path, platform, Python version
   - A `categories.json` summary — count + ids + whether "none" is in the
     enum (no descriptions or category names; those can include sensitive
     domain language)
   - A summary of the most recent classification run's errors — counts
     and a small sample of error messages (no raw text bodies)
   - The tail of `log.jsonl` (last 40 entries) if it exists

   It does **not** include raw corpus text or labels.json content. If the
   user wants to include a specific text or rationale, ask them to paste
   it into the description.

4. **File the issue.** Call:

   ```bash
   uv run "$CLAUDE_PLUGIN_ROOT/skills/classify-tools/scripts/report_issue.py" \
     --title "<title>" \
     --body "<description>"
   ```

   Add `--no-include-categories`, `--no-include-run-errors`, or
   `--include-log-tail 0` if the user opted out of any of those. Use
   `--prefer-url` if the user explicitly wants to review the issue in the
   browser before submitting.

5. **Report back.** The script prints one URL on stdout:
   - If `gh` was available and authed, that's the URL of the freshly-filed
     issue — share it with the user.
   - Otherwise it's a pre-filled `issues/new?title=…&body=…` URL — tell
     the user to open it in a browser to submit. Also mention they may
     need to run `gh auth login` once if they want one-step filing next
     time.

## When to escalate vs. fix

If the user's complaint is something you can plausibly fix yourself (a
typo, a wrong argument, an obvious local config mistake), say so and
offer to fix it instead of filing. A GitHub issue is the right call when
the problem is in the plugin itself, when the user wants the maintainers
to see it, or when you can't reproduce or explain it.
