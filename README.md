# text-classification

A Claude Code plugin for **LLM-driven text classification**: apply a category set to a corpus via Claude or GPT, with prompt caching, structured outputs, and async or batch execution. Optionally hand-label a sample first and tune the prompt against it.

The plugin is the installable artifact — see [`plugin/README.md`](plugin/README.md) for the full documentation, install instructions, and usage.

## Install

Through Claude Code's marketplace mechanism. From a directory you trust:

```
/plugin marketplace add emilysilcock/econ-nlp-plugins
/plugin install text-classification@econ-nlp-plugins
```

After install, the slash commands `/classify-run`, `/classify-tune`, `/classify-label`, and `/classify-report-issue` become available (possibly namespaced as `/text-classification:classify-run`, etc.).

## Use with agentic-clustering

This plugin is a hard dependency of [`agentic-clustering`](https://github.com/emilysilcock/agentic-clustering) — installing `agentic-clustering` auto-installs `text-classification` too. After `/cluster-finalize` produces a `categories.json`, the classify skills pick it up automatically. You can also install `text-classification` on its own for any classification task.

## License

MIT — see [LICENSE](LICENSE).
