# Token benchmark: kikai vs an MLflow MCP server

A small, reproducible measurement of how many tokens an agent spends to do the
same ML-ops task through kikai's HTTP API versus through a Model Context Protocol
(MCP) server for a comparable tracking system.

> This is one task at one scale with one tokenizer. It measures a real, narrow
> thing — not a universal "N× faster" claim. Read the caveats.

## Task

An "MNIST-scale" workload: train a small classifier (`sklearn` digits, 8×8) at
three learning rates, then have an agent **resume the project and identify the
best of the three runs by accuracy** — the everyday resume→inspect loop.

## Setup

- **MLflow side:** MLflow 3.14 (sqlite backend) + the community
  [`mlflow-mcp`](https://github.com/kkruglik/mlflow-mcp) server (40 tools),
  introspected via its real `list_tools()` and called against the live db.
- **kikai side:** the same three runs recorded in a kikai registry, served by a
  local `kikai server`; task driven with plain HTTP.
- **Tokenizer:** `tiktoken` `cl100k_base` — a proxy for Claude's tokenizer, used
  identically on both sides.

## Result (measured)

| | MLflow MCP | kikai |
|---|---:|---:|
| Resident tool schema (in context **every session**) | **5,601** | **0** |
| One-time API guide (fetch-once, droppable) | — | 4,084 (`skill.md`) |
| Dynamic task cost (request + response) | 542 (3 tool calls) | 187 (`compare`, 1 call) – 591 (`brief` + 3 metric reads) |
| **First-session total** | **6,143** | **4,271 – 4,675** |

**First-session ratio ≈ 1.4× fewer tokens** for kikai on this task (6,143 →
~4,300).

## What actually drives the difference

1. **No resident tool-schema tax.** An MCP server puts every tool's JSON schema
   into the model's context. The 40-tool MLflow MCP = **5,601 tokens present in
   every session**. kikai exposes one HTTP surface and ships its agent guide at
   `GET /v1/skill.md`, which the agent fetches **once and can drop** — 0 resident.
2. **One-call resume.** kikai's `brief` / `compare` return the whole run set in a
   single response; the MCP equivalent is several typed calls.
3. **The raw per-call payloads are comparable** (542 vs 187–591) — kikai's edge is
   structural, not a shrink of individual responses.

## Honest caveats

- **Prompt caching** flattens the MCP's 5,601-token resident cost *within* a
  session (it's a one-time cache write, not re-billed per turn). The resident-tax
  advantage is largest across **many short/cold sessions**, smallest within one
  long cached session — where the two converge toward the dynamic-cost parity.
- **Not the same category.** MLflow (and W&B) are *tracking* systems; their MCPs
  are read/query oriented and **cannot launch the 4th run**. kikai is a control
  plane that also submits and manages training (`submit-from`, ~13 tokens for a
  one-variable variant). For the full iterate loop kikai does strictly more with
  fewer tokens, but that part has no MCP baseline to compare against.
- **W&B not measured** — its official MCP (16 tools, verbose descriptions)
  requires an account/cloud backend; by tool count and description length its
  resident schema is the same order of magnitude.
- **One tokenizer, one task, one MCP implementation.** A leaner MCP, or a
  metric-heavy task, would move the numbers.

## Reproduce

```bash
pip install mlflow scikit-learn tiktoken mlflow-mcp
# 1. log three runs to a local sqlite MLflow backend
export MLFLOW_TRACKING_URI="sqlite:///$(pwd)/mlflow.db"
python docs/benchmark/train_mlflow.py 0.01 && python docs/benchmark/train_mlflow.py 0.1 && python docs/benchmark/train_mlflow.py 1.0
# 2. introspect the MCP tool schemas (resident cost) and call the task tools (dynamic cost)
#    tokenizing each with tiktoken cl100k_base
# 3. mirror the three runs into a kikai registry, `kikai server start`, and tokenize
#    GET /brief, /compare, /metrics for the same task
```

Bottom line: kikai used **~1.4× fewer tokens** on this task and carries **none of
the ~5,600-token per-session tool-schema tax** a 40-tool MCP imposes — while also
being able to *launch* the next run, which the tracking MCPs cannot.
