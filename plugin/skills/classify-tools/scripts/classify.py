#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   # >=0.74.1: first release able to send the structured-outputs `output_config`
#   # param (now GA server-side, no beta header). An older anthropic silently
#   # lacks the kwarg and the --provider anthropic path would break.
#   "anthropic>=0.74.1",
#   "openai>=1.50.0",
#   "truststore>=0.10 ; sys_platform == 'win32'",
# ]
# ///
"""Classify texts into a category set.

Reads a ``categories.json`` (the source of truth for both the structured-
output enum and the category bodies in the system prompt), assembles a
system prompt from those categories with an optional ``--header`` and
``--footer``, then runs each input text through the chosen provider/model
and writes per-text results to CSV.

Supports two providers (anthropic, openai) and two execution modes (async
with concurrency cap; batch via each provider's Batch API for 50% cost
reduction). Prompt caching is enabled by default.

Usage::

    uv run classify.py \\
        --input corpus.csv --text-col text --id-col id \\
        --categories categories.json \\
        --output classifications/run.csv \\
        --provider openai --model gpt-5-mini \\
        --mode async --concurrency 20
"""

from __future__ import annotations

import sys

# Force UTF-8 on stdout/stderr — Windows defaults to cp1252 and crashes on
# non-ASCII category names / corpus content. Idempotent; no-op on streams
# that aren't TextIOWrapper (e.g. captured in tests).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":
    # AVG / similar AV intercepts TLS with a root that lives in the Windows
    # cert store but is absent from certifi. Route Python's SSL through the
    # OS trust store. Must happen before any httpx / anthropic / openai
    # import.
    import truststore as _truststore
    _truststore.inject_into_ssl()

import argparse
import asyncio
import csv
import json
import os
import time
from pathlib import Path
from typing import Any

# Python's csv module defaults to a 131,072-char per-field cap. Opt out so
# long-body corpora don't trip the reader. Capped at 2**31-1 because
# Windows' C long can't hold sys.maxsize.
csv.field_size_limit(2**31 - 1)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5-mini",
}

# Both SDKs default max_retries=2, which is not enough for bursty long-input
# workloads (we saw the majority of requests fail under sustained 429s at
# the default). 10 lets the SDK's exponential backoff + Retry-After
# handling drain a rate-limit spike before giving up.
CLIENT_MAX_RETRIES = 10

# "out of scope" sentinel — when present in the categories enum, the default
# header/footer guidance includes "or 'none' if no category fits". If the
# user wants a differently-named OOS bucket (e.g. "other", "unclear"), they
# can either include it as just another category, or supply their own
# --header/--footer to set custom guidance.
NONE_ID = "none"

DEFAULT_HEADER_WITH_NONE = """\
You are a text classifier. Read the input text and assign it to exactly one of \
the categories defined below, or "none" if no category fits.

"""

DEFAULT_HEADER_NO_NONE = """\
You are a text classifier. Read the input text and assign it to exactly one of \
the categories defined below. Every text must be assigned.

"""

DEFAULT_FOOTER_WITH_NONE = """

INSTRUCTIONS:
- Assign exactly ONE label from those defined above, or "none" if no category fits.
- Use any boundary notes in the descriptions to resolve ambiguous cases.
- Confidence is an integer 1-5: 1 = very uncertain, 5 = very confident.
- Reasoning should be one short sentence explaining the assignment.
"""

DEFAULT_FOOTER_NO_NONE = """

INSTRUCTIONS:
- Assign exactly ONE label from those defined above. "none" is not an option — pick the closest match.
- Use any boundary notes in the descriptions to resolve ambiguous cases.
- Confidence is an integer 1-5: 1 = very uncertain, 5 = very confident.
- Reasoning should be one short sentence explaining the assignment.
"""


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def load_categories(path: Path) -> list[dict]:
    """Load categories.json. Validates the shape rather than silently emitting
    a degenerate schema with a one-element enum. Each entry must have ``id``
    and ``name``; ``description`` is recommended but not strictly required
    (an empty description still parses and runs, just hurts accuracy)."""
    if not path.exists():
        print(f"error: categories not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        print(
            f"error: {path} must be a non-empty JSON list of "
            f"{{id, name, description}} objects",
            file=sys.stderr,
        )
        sys.exit(1)
    seen_ids: set[str] = set()
    out: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"error: categories[{i}] is not an object", file=sys.stderr)
            sys.exit(1)
        cid = item.get("id")
        name = item.get("name")
        if not cid or not isinstance(cid, str):
            print(f"error: categories[{i}] missing 'id' (string)", file=sys.stderr)
            sys.exit(1)
        if not name or not isinstance(name, str):
            print(f"error: categories[{i}] missing 'name' (string)", file=sys.stderr)
            sys.exit(1)
        if cid in seen_ids:
            print(f"error: duplicate category id '{cid}' in {path}", file=sys.stderr)
            sys.exit(1)
        seen_ids.add(cid)
        out.append({
            "id": cid,
            "name": name,
            "description": str(item.get("description") or "").strip(),
        })
    return out


def build_prompt(
    categories: list[dict],
    header: str | None = None,
    footer: str | None = None,
) -> str:
    """Assemble the system prompt from category bodies + optional header/footer.

    Each category renders as ``## <name> (`<id>`)`` followed by the
    description on a blank line. Header (instructions) precedes them; footer
    (output guidance) trails. Both default based on whether ``"none"`` is
    one of the category ids, so the OOS language matches the schema.

    The output is deterministic given the same inputs, so prompt caching at
    the provider can amortise the cost of the body across many calls.
    """
    has_none = any(c["id"] == NONE_ID for c in categories)
    if header is None:
        header = DEFAULT_HEADER_WITH_NONE if has_none else DEFAULT_HEADER_NO_NONE
    if footer is None:
        footer = DEFAULT_FOOTER_WITH_NONE if has_none else DEFAULT_FOOTER_NO_NONE

    parts = [header]
    for cat in categories:
        parts.append(f"## {cat['name']} (`{cat['id']}`)\n")
        if cat["description"]:
            parts.append(f"{cat['description']}\n")
        parts.append("\n")
    parts.append(footer)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def build_schema(categories: list[dict]) -> dict:
    """Strict JSON schema enforced at decode time on both providers. The
    ``label`` enum is exactly the category ids — no implicit injection of
    "none". If the user wants "none" allowed, they include it in
    ``categories.json``."""
    label_enum = [c["id"] for c in categories]
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": label_enum},
            "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "reasoning": {"type": "string"},
        },
        "required": ["label", "confidence", "reasoning"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Corpus I/O
# ---------------------------------------------------------------------------

def load_corpus(path: Path, text_col: str, id_col: str | None) -> list[dict]:
    """Load a corpus. ``id_col`` of None means "use row index" — opt-in via
    the CLI's --no-id flag. A missing id_col is an error, not a silent
    fallback, so a typo in the column name doesn't quietly produce a CSV
    keyed by row indexes.
    """
    if not path.exists():
        print(f"error: input not found: {path}", file=sys.stderr)
        sys.exit(1)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path, text_col, id_col)
    if suffix == ".json":
        return _load_json(path, text_col, id_col)
    if suffix == ".jsonl":
        return _load_jsonl(path, text_col, id_col)
    print(f"error: unsupported format {suffix} (use .csv, .json, or .jsonl)", file=sys.stderr)
    sys.exit(1)


def _load_csv(path: Path, text_col: str, id_col: str | None) -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if text_col not in fields:
            print(f"error: column '{text_col}' not in CSV. Available: {fields}", file=sys.stderr)
            sys.exit(1)
        if id_col is not None and id_col not in fields:
            print(
                f"error: id column '{id_col}' not in CSV. Available: {fields}. "
                f"Pass --no-id to fall back to row indexes.",
                file=sys.stderr,
            )
            sys.exit(1)
        for i, row in enumerate(reader):
            text = (row[text_col] or "").strip()
            if not text:
                continue
            tid = str(row[id_col]).strip() if id_col is not None else str(i)
            out.append({"id": tid, "text": text})
    return out


def _load_json(path: Path, text_col: str, id_col: str | None) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("error: JSON must be a list of objects", file=sys.stderr)
        sys.exit(1)
    out = []
    for i, item in enumerate(data):
        if isinstance(item, dict) and text_col in item:
            text = str(item[text_col]).strip()
            if not text:
                continue
            if id_col is not None:
                if id_col not in item:
                    print(
                        f"error: id field '{id_col}' missing on item {i}. "
                        f"Pass --no-id to fall back to row indexes.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                tid = str(item[id_col])
            else:
                tid = str(i)
            out.append({"id": tid, "text": text})
        elif isinstance(item, str):
            out.append({"id": str(i), "text": item.strip()})
    return out


def _load_jsonl(path: Path, text_col: str, id_col: str | None) -> list[dict]:
    """JSON-lines: one object per line. Matches the canonical
    ``documents.jsonl`` layout (one ``{id, text, ...}`` per line)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict) or text_col not in item:
                continue
            text = str(item[text_col]).strip()
            if not text:
                continue
            if id_col is not None:
                if id_col not in item:
                    print(
                        f"error: id field '{id_col}' missing on line {i+1}. "
                        f"Pass --no-id to fall back to row indexes.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                tid = str(item[id_col])
            else:
                tid = str(i)
            out.append({"id": tid, "text": text})
    return out


# ---------------------------------------------------------------------------
# Anthropic — async
# ---------------------------------------------------------------------------

async def classify_anthropic_async(
    model: str,
    prompt: str,
    schema: dict,
    records: list[dict],
    concurrency: int,
) -> dict[str, dict]:
    import anthropic

    client = anthropic.AsyncAnthropic(max_retries=CLIENT_MAX_RETRIES)
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def one(rec: dict) -> None:
        async with sem:
            # Split API-error and parse-error handling so a refusal / tool-only
            # response (next() over an empty generator) yields a useful
            # "ParseError: StopIteration: " message instead of a bare empty
            # one. Mirrors the batch path's narrow-except below.
            try:
                resp = await client.messages.create(
                    model=model,
                    max_tokens=512,
                    system=[{
                        "type": "text",
                        "text": prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": rec["text"]}],
                    output_config={
                        "format": {"type": "json_schema", "schema": schema},
                    },
                )
            except Exception as e:
                results[rec["id"]] = {
                    "label": None, "confidence": None, "reasoning": None,
                    "error": f"{type(e).__name__}: {e}",
                    "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                }
                return
            try:
                text = next(b.text for b in resp.content if b.type == "text")
                parsed = json.loads(text)
                results[rec["id"]] = {
                    **parsed,
                    "error": None,
                    "input_tokens": resp.usage.input_tokens,
                    "cache_read_tokens": resp.usage.cache_read_input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                }
            except (StopIteration, json.JSONDecodeError, AttributeError) as e:
                results[rec["id"]] = {
                    "label": None, "confidence": None, "reasoning": None,
                    "error": f"ParseError: {type(e).__name__}: {e}",
                    "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                }

    total = len(records)
    done = 0
    tasks = [asyncio.create_task(one(r)) for r in records]
    for t in asyncio.as_completed(tasks):
        await t
        done += 1
        if done % 50 == 0 or done == total:
            print(f"  classified {done}/{total}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Anthropic — batch
# ---------------------------------------------------------------------------

# Anthropic Messages Batches API caps: 100,000 requests per batch, 256 MB
# input size. Target 200 MB to leave headroom for JSON-encoding overhead.
ANTHROPIC_BATCH_MAX_REQUESTS = 100_000
ANTHROPIC_BATCH_MAX_BYTES = 200 * 1024 * 1024


def _chunk_anthropic_requests(
    requests: list[dict], max_bytes: int, max_count: int,
) -> tuple[list[list[dict]], list[int]]:
    """Group batch requests into chunks under both the byte and count caps.

    Returns ``(chunks, chunk_bytes)`` — the per-chunk byte totals are returned
    alongside so the caller can log sizes without re-encoding every request.

    Single requests that exceed max_bytes on their own are placed in their own
    chunk so the caller fails loudly on Anthropic's side rather than silently
    dropping them.
    """
    chunks: list[list[dict]] = []
    chunk_bytes: list[int] = []
    current: list[dict] = []
    current_bytes = 0
    for req in requests:
        req_bytes = len(json.dumps(req, ensure_ascii=False).encode("utf-8"))
        if current and (
            current_bytes + req_bytes > max_bytes or len(current) >= max_count
        ):
            chunks.append(current)
            chunk_bytes.append(current_bytes)
            current = []
            current_bytes = 0
        current.append(req)
        current_bytes += req_bytes
    if current:
        chunks.append(current)
        chunk_bytes.append(current_bytes)
    return chunks, chunk_bytes


async def _submit_and_collect_anthropic_batch(
    client,
    requests: list[dict],
    label: str,
) -> dict[str, dict]:
    """Submit one chunk of anthropic batch requests, poll, parse results.

    Returns results_by_custom_id. The caller marks ids missing from the merged
    results across all chunks as errors.
    """
    print(f"  [{label}] submitting {len(requests)} requests...", file=sys.stderr)
    batch = await client.messages.batches.create(requests=requests)
    print(f"  [{label}] batch id: {batch.id}", file=sys.stderr)

    while True:
        b = await client.messages.batches.retrieve(batch.id)
        counts = b.request_counts
        print(
            f"  [{label}] status={b.processing_status} "
            f"processing={counts.processing} succeeded={counts.succeeded} "
            f"errored={counts.errored}",
            file=sys.stderr,
        )
        if b.processing_status == "ended":
            break
        await asyncio.sleep(60)

    results: dict[str, dict] = {}
    # Wrap the result stream so a transient network blip or SDK quirk after the
    # batch has already completed (and been billed) doesn't discard every
    # record. Per-record parse errors are handled by the narrow-except inside
    # the loop; this outer except catches the stream-level failure that would
    # otherwise propagate out of asyncio.gather in the caller and lose the
    # whole chunk. Any custom_id we hadn't seen yet gets a specific error
    # string so the caller's "missing from batch results" safety net isn't the
    # only signal the operator has.
    try:
        async for r in await client.messages.batches.results(batch.id):
            cid = getattr(r, "custom_id", None)
            if cid is None:
                print(f"warn: [{label}] anthropic batch result missing custom_id, skipping: {str(r)[:200]}", file=sys.stderr)
                continue
            if r.result.type == "succeeded":
                try:
                    msg = r.result.message
                    text = next(blk.text for blk in msg.content if blk.type == "text")
                    parsed = json.loads(text)
                    results[cid] = {
                        **parsed,
                        "error": None,
                        "input_tokens": msg.usage.input_tokens,
                        "cache_read_tokens": msg.usage.cache_read_input_tokens,
                        "output_tokens": msg.usage.output_tokens,
                    }
                except (StopIteration, json.JSONDecodeError, AttributeError) as e:
                    results[cid] = {
                        "label": None, "confidence": None, "reasoning": None,
                        "error": f"ParseError: {type(e).__name__}: {e}",
                        "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                    }
            else:
                err = getattr(r.result, "error", r.result.type)
                results[cid] = {
                    "label": None, "confidence": None, "reasoning": None,
                    "error": str(err),
                    "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                }
    except Exception as e:
        print(
            f"  [{label}] stream-level batch results fetch failed after batch "
            f"completed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        err_msg = f"BatchError: stream fetch failed: {type(e).__name__}: {e}"
        for req in requests:
            cid = req["custom_id"]
            if cid not in results:
                results[cid] = {
                    "label": None, "confidence": None, "reasoning": None,
                    "error": err_msg,
                    "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                }
    return results


async def classify_anthropic_batch(
    model: str,
    prompt: str,
    schema: dict,
    records: list[dict],
) -> dict[str, dict]:
    """Anthropic Batch API path. ~50% discount on input + output, ≤24h SLA.

    Flow: build the request list → chunk it to fit Anthropic's caps (100k
    requests / 256 MB input) → submit all chunks concurrently → poll each →
    parse → merge per-chunk results. Concurrent submission matters above
    the per-batch cap: each batch has its own ≤24h SLA, so sequential
    submission of N chunks stacks to N×24h worst-case. The `[chunk N/M]`
    label on every progress line keeps interleaved logs parseable.
    """
    import anthropic

    client = anthropic.AsyncAnthropic(max_retries=CLIENT_MAX_RETRIES)

    # 1. Build one request dict per record.
    requests = [
        {
            "custom_id": rec["id"],
            "params": {
                "model": model,
                "max_tokens": 512,
                "system": [{
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                "messages": [{"role": "user", "content": rec["text"]}],
                "output_config": {
                    "format": {"type": "json_schema", "schema": schema},
                },
            },
        }
        for rec in records
    ]

    # 2. Chunk to fit under Anthropic's caps. Byte totals come back with the
    # chunks so the summary line doesn't have to re-encode every request.
    chunks, chunk_bytes = _chunk_anthropic_requests(
        requests, ANTHROPIC_BATCH_MAX_BYTES, ANTHROPIC_BATCH_MAX_REQUESTS,
    )
    if len(chunks) > 1:
        sizes_mb = [b // (1024 * 1024) for b in chunk_bytes]
        chunk_summary = ", ".join(
            f"{len(c)}reqs/{s}MB" for c, s in zip(chunks, sizes_mb)
        )
        print(
            f"  total payload exceeds Anthropic caps "
            f"({ANTHROPIC_BATCH_MAX_REQUESTS:,} reqs or "
            f"{ANTHROPIC_BATCH_MAX_BYTES // (1024 * 1024)} MB); "
            f"split into {len(chunks)} chunks ({chunk_summary})",
            file=sys.stderr,
        )

    # 3. Submit + collect every chunk concurrently. Each batch has its own
    # ≤24h SLA, so sequential submission would stack to N×24h worst-case;
    # gathering pipelines the waits. Per-chunk logs already carry their own
    # `[chunk N/M]` prefix, so interleaved output stays parseable.
    labels = [
        f"chunk {i}/{len(chunks)}" if len(chunks) > 1 else "batch"
        for i in range(1, len(chunks) + 1)
    ]
    chunk_results = await asyncio.gather(*(
        _submit_and_collect_anthropic_batch(client, chunk, label)
        for chunk, label in zip(chunks, labels)
    ))
    results: dict[str, dict] = {}
    for cr in chunk_results:
        results.update(cr)

    # 4. Any record we didn't see in batch results — mark missing.
    for rec in records:
        if rec["id"] not in results:
            results[rec["id"]] = {
                "label": None, "confidence": None, "reasoning": None,
                "error": "BatchError: missing from batch results",
                "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
            }

    return results


# ---------------------------------------------------------------------------
# OpenAI — async
# ---------------------------------------------------------------------------

async def classify_openai_async(
    model: str,
    prompt: str,
    schema: dict,
    records: list[dict],
    concurrency: int,
) -> dict[str, dict]:
    import openai

    client = openai.AsyncOpenAI(max_retries=CLIENT_MAX_RETRIES)
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def one(rec: dict) -> None:
        async with sem:
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": rec["text"]},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "classification",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
                text = resp.choices[0].message.content
                parsed = json.loads(text or "{}")
                usage = resp.usage
                cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                # OpenAI's prompt_tokens is the TOTAL (cached + uncached); subtract
                # cached so input_tokens means the same thing as in the Anthropic
                # rows (non-cached portion only). Keeps the CSV semantic
                # provider-neutral and downstream cost math correct.
                results[rec["id"]] = {
                    **parsed,
                    "error": None,
                    "input_tokens": usage.prompt_tokens - cached,
                    "cache_read_tokens": cached,
                    "output_tokens": usage.completion_tokens,
                }
            except Exception as e:
                results[rec["id"]] = {
                    "label": None, "confidence": None, "reasoning": None,
                    "error": f"{type(e).__name__}: {e}",
                    "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                }

    total = len(records)
    done = 0
    tasks = [asyncio.create_task(one(r)) for r in records]
    for t in asyncio.as_completed(tasks):
        await t
        done += 1
        if done % 50 == 0 or done == total:
            print(f"  classified {done}/{total}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# OpenAI — batch
# ---------------------------------------------------------------------------

# OpenAI Batch API limit: input file size ≤ 200 MB. We target 150 MB per chunk
# to leave headroom for JSONL newlines and any size estimation slop.
OPENAI_BATCH_MAX_BYTES = 150 * 1024 * 1024


def _chunk_jsonl_lines(
    lines: list[str], max_bytes: int,
) -> tuple[list[list[str]], list[int]]:
    """Group JSONL lines into chunks each ≤ max_bytes (newline-inclusive).

    Returns ``(chunks, chunk_bytes)`` — the per-chunk byte totals are returned
    alongside so the caller can log sizes without re-encoding every line.

    Single lines that exceed max_bytes are placed in their own chunk so the
    caller fails loudly on OpenAI's upload rather than silently dropping them.
    """
    chunks: list[list[str]] = []
    chunk_bytes: list[int] = []
    current: list[str] = []
    current_bytes = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8")) + 1  # +1 for newline separator
        if current and current_bytes + line_bytes > max_bytes:
            chunks.append(current)
            chunk_bytes.append(current_bytes)
            current = []
            current_bytes = 0
        current.append(line)
        current_bytes += line_bytes
    if current:
        chunks.append(current)
        chunk_bytes.append(current_bytes)
    return chunks, chunk_bytes


async def _submit_and_collect_openai_batch(
    client,
    lines: list[str],
    label: str,
) -> dict[str, dict]:
    """Upload one JSONL chunk, submit a batch, poll, return parsed results.

    Returns results_by_custom_id. The caller marks ids missing from the merged
    results across all chunks as errors.
    """
    import io

    payload = ("\n".join(lines) + "\n").encode("utf-8")

    print(
        f"  [{label}] uploading {len(lines)} requests, {len(payload):,} bytes...",
        file=sys.stderr,
    )
    upload = await client.files.create(
        file=("classify_batch.jsonl", io.BytesIO(payload)),
        purpose="batch",
    )
    batch = await client.batches.create(
        input_file_id=upload.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    print(f"  [{label}] batch id: {batch.id}", file=sys.stderr)

    # Poll until terminal.
    while True:
        b = await client.batches.retrieve(batch.id)
        counts = b.request_counts
        completed = getattr(counts, "completed", 0) if counts else 0
        failed = getattr(counts, "failed", 0) if counts else 0
        total = getattr(counts, "total", len(lines)) if counts else len(lines)
        print(
            f"  [{label}] status={b.status} completed={completed}/{total} failed={failed}",
            file=sys.stderr,
        )
        if b.status in ("completed", "failed", "cancelled", "expired"):
            break
        await asyncio.sleep(60)

    results: dict[str, dict] = {}

    # Both file fetches below are wrapped: a transient network blip or a
    # malformed top-level JSONL line after the batch has already completed
    # (and been billed) would otherwise propagate out of asyncio.gather in the
    # caller and discard the whole chunk. Output-file failure fills missing
    # custom_ids with a specific error; error-file failure just logs (it's
    # supplementary — successful records are already in `results`, and any
    # custom_id absent from both files is filled by the caller's safety net).
    if b.output_file_id:
        try:
            out_content = await client.files.content(b.output_file_id)
            raw = await out_content.aread() if hasattr(out_content, "aread") else out_content.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                cid = entry.get("custom_id")
                if cid is None:
                    print(f"warn: openai batch output entry missing custom_id, skipping: {str(entry)[:200]}", file=sys.stderr)
                    continue
                response = entry.get("response") or {}
                body = response.get("body") or {}
                choices = body.get("choices") or []
                if response.get("status_code") == 200 and choices:
                    text = choices[0].get("message", {}).get("content", "")
                    try:
                        parsed = json.loads(text or "{}")
                    except json.JSONDecodeError as e:
                        parsed = {"label": None, "confidence": None, "reasoning": None}
                        err = f"JSONDecodeError: {e}"
                    else:
                        err = None
                    usage = body.get("usage") or {}
                    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                    # OpenAI's prompt_tokens is the TOTAL (cached + uncached); subtract
                    # cached so input_tokens is the non-cached portion only (matches
                    # the Anthropic semantic). Keeps downstream cost math correct.
                    results[cid] = {
                        **parsed,
                        "error": err,
                        "input_tokens": max(0, usage.get("prompt_tokens", 0) - cached),
                        "cache_read_tokens": cached,
                        "output_tokens": usage.get("completion_tokens", 0),
                    }
                else:
                    err_obj = entry.get("error") or response.get("error") or {"message": "non-200 response"}
                    results[cid] = {
                        "label": None, "confidence": None, "reasoning": None,
                        "error": f"BatchError: {err_obj}",
                        "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                    }
        except Exception as e:
            print(
                f"  [{label}] output file fetch/parse failed after batch "
                f"completed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            err_msg = f"BatchError: output fetch failed: {type(e).__name__}: {e}"
            for raw_line in lines:
                try:
                    cid = json.loads(raw_line).get("custom_id")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if cid and cid not in results:
                    results[cid] = {
                        "label": None, "confidence": None, "reasoning": None,
                        "error": err_msg,
                        "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                    }

    if b.error_file_id:
        try:
            err_content = await client.files.content(b.error_file_id)
            raw = await err_content.aread() if hasattr(err_content, "aread") else err_content.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                cid = entry.get("custom_id")
                if cid is None:
                    print(f"warn: openai batch error entry missing custom_id, skipping: {str(entry)[:200]}", file=sys.stderr)
                    continue
                results[cid] = {
                    "label": None, "confidence": None, "reasoning": None,
                    "error": f"BatchError: {entry.get('error') or entry}",
                    "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
                }
        except Exception as e:
            print(
                f"  [{label}] error file fetch/parse failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    return results


async def classify_openai_batch(
    model: str,
    prompt: str,
    schema: dict,
    records: list[dict],
) -> dict[str, dict]:
    """OpenAI Batch API path. 50% discount on input + output, ≤24h SLA.

    Flow: build the JSONL → chunk it to fit OpenAI's 200 MB input cap →
    upload + submit all chunks concurrently → poll each → parse → merge
    per-chunk results. Concurrent submission matters above the per-batch
    cap: each batch has its own ≤24h SLA, so sequential submission of N
    chunks stacks to N×24h worst-case. The `[chunk N/M]` label on every
    progress line keeps interleaved logs parseable.
    """
    import openai

    client = openai.AsyncOpenAI(max_retries=CLIENT_MAX_RETRIES)

    # 1. Build one JSONL line per request.
    lines = []
    for rec in records:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": rec["text"]},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "classification",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        lines.append(json.dumps({
            "custom_id": rec["id"],
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }, ensure_ascii=False))

    # 2. Chunk to fit under the 200 MB OpenAI input-file cap. Byte totals come
    # back with the chunks so the summary line doesn't have to re-encode.
    chunks, chunk_bytes = _chunk_jsonl_lines(lines, OPENAI_BATCH_MAX_BYTES)
    if len(chunks) > 1:
        print(
            f"  total payload exceeds {OPENAI_BATCH_MAX_BYTES // (1024*1024)} MB cap; "
            f"split into {len(chunks)} chunks "
            f"({', '.join(f'{b // (1024*1024)}MB' for b in chunk_bytes)})",
            file=sys.stderr,
        )

    # 3. Submit + collect every chunk concurrently. Each batch has its own
    # ≤24h SLA, so sequential submission would stack to N×24h worst-case;
    # gathering pipelines the waits. Per-chunk logs already carry their own
    # `[chunk N/M]` prefix, so interleaved output stays parseable.
    labels = [
        f"chunk {i}/{len(chunks)}" if len(chunks) > 1 else "batch"
        for i in range(1, len(chunks) + 1)
    ]
    chunk_results = await asyncio.gather(*(
        _submit_and_collect_openai_batch(client, chunk, label)
        for chunk, label in zip(chunks, labels)
    ))
    results: dict[str, dict] = {}
    for cr in chunk_results:
        results.update(cr)

    # 4. Any record we didn't see in either output or error files — mark missing.
    for rec in records:
        if rec["id"] not in results:
            results[rec["id"]] = {
                "label": None, "confidence": None, "reasoning": None,
                "error": "BatchError: missing from output and error files",
                "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
            }

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(records: list[dict], results: dict[str, dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id", "label", "confidence", "reasoning", "error",
        "input_tokens", "cache_read_tokens", "output_tokens",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in records:
            r = results.get(rec["id"], {})
            w.writerow({
                "id": rec["id"],
                "label": r.get("label"),
                "confidence": r.get("confidence"),
                "reasoning": r.get("reasoning"),
                "error": r.get("error"),
                "input_tokens": r.get("input_tokens", 0),
                "cache_read_tokens": r.get("cache_read_tokens", 0),
                "output_tokens": r.get("output_tokens", 0),
            })


def summarize(results: dict[str, dict]) -> None:
    n = len(results)
    n_err = sum(1 for r in results.values() if r.get("error"))
    in_t = sum(r.get("input_tokens", 0) for r in results.values())
    cache_t = sum(r.get("cache_read_tokens", 0) for r in results.values())
    out_t = sum(r.get("output_tokens", 0) for r in results.values())
    print(file=sys.stderr)
    print(f"summary: {n} classified, {n_err} errors", file=sys.stderr)
    print(
        f"tokens:  input={in_t:,} (cached={cache_t:,}, "
        f"{cache_t / max(in_t, 1):.0%}) output={out_t:,}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="Path to corpus CSV / JSON / JSONL")
    p.add_argument("--text-col", default="text", help="Text column name (default: text)")
    p.add_argument("--id-col", default="id", help="ID column name (default: id). A missing column errors; pass --no-id for a row-index fallback.")
    p.add_argument(
        "--no-id",
        action="store_true",
        help="Use row indexes as IDs instead of an id column. Use this explicitly when the corpus has no ID column; without it, a missing --id-col is a fatal error rather than a silent fallback.",
    )
    p.add_argument(
        "--categories",
        required=True,
        help=(
            "Path to categories.json — a non-empty JSON list of "
            '{id, name, description} objects. The ids form the structured-output '
            'enum; the descriptions are inserted into the system prompt.'
        ),
    )
    p.add_argument(
        "--header",
        help=(
            "Optional path to a plain-text instructions paragraph prepended to "
            "the assembled prompt. Defaults to a builtin header whose wording "
            "depends on whether 'none' is in the category set."
        ),
    )
    p.add_argument(
        "--footer",
        help=(
            "Optional path to plain-text output guidance appended after the "
            "category list. Defaults to a builtin footer."
        ),
    )
    p.add_argument(
        "--prompt-output",
        help=(
            "Optional path to write the assembled system prompt for inspection. "
            "Useful for verifying that header/footer/category bodies came together "
            "the way you expected before paying for a batch."
        ),
    )
    p.add_argument("--output", required=True, help="Path to write per-text classifications CSV")
    p.add_argument("--provider", choices=["anthropic", "openai"], default="openai")
    p.add_argument("--model", help="Model ID (defaults: anthropic=claude-haiku-4-5, openai=gpt-5-mini)")
    p.add_argument("--mode", choices=["async", "batch"], default="async",
                   help="async = real-time with concurrency cap; batch = provider Batch API (50%% cheaper, ≤24h, supports both anthropic and openai)")
    p.add_argument("--concurrency", type=int, default=20, help="Async mode only (default: 20)")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Allow --output to be overwritten if it exists. Default is to refuse "
            "and exit so a re-run can't silently clobber a prior batch's results "
            "(batch runs can be hours and not-free)."
        ),
    )
    args = p.parse_args()

    # Refuse to clobber a prior output. Checked here — before any API call,
    # corpus load, or key check — so the operator's fat-finger fails in
    # milliseconds rather than after the batch completes.
    output_path = Path(args.output)
    if output_path.exists() and not args.overwrite:
        print(
            f"error: output already exists: {output_path}. Pass --overwrite "
            "to replace it, or choose a different --output path.",
            file=sys.stderr,
        )
        return 1

    if args.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if args.provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print("error: OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    model = args.model or DEFAULT_MODELS[args.provider]

    categories = load_categories(Path(args.categories))
    header = None
    if args.header:
        header_path = Path(args.header)
        if not header_path.exists():
            print(f"error: --header file not found: {header_path}", file=sys.stderr)
            return 1
        header = header_path.read_text(encoding="utf-8")
    footer = None
    if args.footer:
        footer_path = Path(args.footer)
        if not footer_path.exists():
            print(f"error: --footer file not found: {footer_path}", file=sys.stderr)
            return 1
        footer = footer_path.read_text(encoding="utf-8")

    prompt = build_prompt(categories, header=header, footer=footer)
    schema = build_schema(categories)

    if args.prompt_output:
        prompt_out = Path(args.prompt_output)
        prompt_out.parent.mkdir(parents=True, exist_ok=True)
        prompt_out.write_text(prompt, encoding="utf-8")
        print(f"  wrote assembled prompt to {prompt_out}", file=sys.stderr)

    id_col = None if args.no_id else args.id_col
    records = load_corpus(Path(args.input), args.text_col, id_col)
    print(
        f"classifying {len(records)} texts: provider={args.provider} "
        f"model={model} mode={args.mode} categories={len(categories)}",
        file=sys.stderr,
    )

    t0 = time.time()
    if args.provider == "anthropic" and args.mode == "async":
        results = asyncio.run(classify_anthropic_async(model, prompt, schema, records, args.concurrency))
    elif args.provider == "anthropic" and args.mode == "batch":
        results = asyncio.run(classify_anthropic_batch(model, prompt, schema, records))
    elif args.provider == "openai" and args.mode == "async":
        results = asyncio.run(classify_openai_async(model, prompt, schema, records, args.concurrency))
    elif args.provider == "openai" and args.mode == "batch":
        results = asyncio.run(classify_openai_batch(model, prompt, schema, records))
    else:
        print("error: unsupported provider/mode combination", file=sys.stderr)
        return 1
    elapsed = time.time() - t0

    write_output(records, results, output_path)
    summarize(results)
    print(f"  elapsed: {elapsed:.1f}s", file=sys.stderr)
    print(f"  wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
