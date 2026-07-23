# Evaluation and Quality Targets

**Version:** 2026-07-23
**Applies to:** BidScope P0 deterministic evaluation runner
**Design reference:** `docs/superpowers/specs/2026-07-18-bidscope-design.md` sections 13 and 16

---

## Methodology

BidScope's evaluation system runs **fully offline** over committed, versioned JSONL datasets. There is no network access during evaluation, and no live model provider is invoked.

The runner (`bidscope.evaluation.runner.run_deterministic`) executes a fixed pipeline:

1. **Intent parsing** — natural-language requests are parsed by a deterministic fake model (`FakeIntentModel`) that reproduces the same structured output for a given input every time.
2. **Retrieval** — queries run against the committed synthetic corpus using a deterministic keyword-ranking function with exact-match boosting.
3. **Deduplication** — notice pairs run through the production `classify_duplicate` decision function.
4. **Citation verification** — every claim in the claims dataset is checked for citation coverage (presence of at least one citation) and citation correctness (all citations resolve to known evidence IDs).
5. **End-to-end scenarios** — fixed scenarios assert task success, citation validity, latency bounds, and token usage.

All metrics are computed by pure functions in `bidscope.evaluation.metrics` — no randomness, no network, no live model. The result is a `target_pass` boolean that is `True` only when every metric meets its documented target.

The clock is fixed to `2026-07-18T09:00:00+00:00` for intent cases so that time-relative parsing is reproducible. The working tree dirty flag and git commit are recorded in every result for provenance.

---

## Datasets

All datasets live in `backend/src/bidscope/evaluation/data/` (and `corpus/` for the retrieval corpus). They are immutable committed fixtures — the runner validates SHA-256 hashes against `EXPECTED_DATASET_HASHES` and refuses to run if any file has been modified.

| Dataset | File | Count | Metrics |
|---|---|---|---|
| `corpus` | `corpus/synthetic-notices-v1.jsonl` | 120 | Retrieval corpus (used by retrieval-v1, claims-v1, e2e-v1) |
| `intent-v1` | `data/intent-v1.jsonl` | 120 | Field exact match, field macro F1, error accuracy |
| `retrieval-v1` | `data/retrieval-v1.jsonl` | 30 | Recall@10, nDCG@10 |
| `dedup-v1` | `data/dedup-v1.jsonl` | 120 | Binary precision/recall/F1, multiclass macro F1 |
| `claims-v1` | `data/claims-v1.jsonl` | 60 | Citation coverage, citation correctness, support accuracy |
| `e2e-v1` | `data/e2e-v1.jsonl` | 30 | Task success rate, P50/P95 latency, token totals, cost (CNY) |

Every record carries `source=synthetic_demo`, uses `example.invalid` URLs, and has an `eval-*` prefixed ID. These are not real tender notices.

---

## Targets vs Measured

All P0 metrics are **fixture consistency** measurements. They answer: "does the deterministic pipeline reproduce its committed expected outputs?" They do **not** measure production-quality performance against real tender data or a live model.

| Metric | Target | Measurement mode |
|---|---|---|
| Intent Macro F1 | >= 90% | `fixture_consistency` |
| Retrieval Recall@10 | >= 85% | `fixture_consistency` |
| Retrieval nDCG@10 | >= 85% | `fixture_consistency` |
| Dedup F1 | >= 90% | `fixture_consistency` |
| Citation coverage | 100% | `fixture_consistency` |
| Citation correctness | >= 95% | `fixture_consistency` |
| Task success rate | >= 95% | `fixture_consistency` |
| P95 latency | <= 15,000 ms | `fixture_consistency` |
| Cost | <= CNY 0.10 | `fixture_consistency` |

> **Design principle (section 16, criterion 9):** The fixed evaluation command publishes all agreed metrics and clearly distinguishes targets from measured values. The table above states targets. Measured values come from running the command in the documented environment, and they must be reported alongside the dataset version, model name, pricing snapshot date, and environment.

### Current measured result

The most recent deterministic run was produced with:

- `provider=offline`, `model=fake-deterministic`
- Pricing snapshot date: `2026-07-18` (prompt: CNY 0.00 / million, completion: CNY 0.00 / million)
- `network=disabled`, `app_mode=demo`
- `target_pass=true`
- Dataset counts: corpus 120, intent-v1 120, retrieval-v1 30, dedup-v1 120, claims-v1 60, e2e-v1 30

The zero-cost result is an artifact of the offline pricing fixture (CNY 0.00 per million tokens), not a live cost measurement.

---

## Reproducibility

Run the full deterministic evaluation with:

```bash
uv run --offline bidscope eval run --mode deterministic --output eval/results/deterministic.json
```

This requires no database, no network, and no external service. The command:

1. Loads and hash-validates the committed datasets.
2. Runs the deterministic pipeline.
3. Writes a machine-readable JSON result to `eval/results/deterministic.json`.
4. Prints the same JSON to stdout.

The result includes `git_commit`, `working_tree_dirty`, `dataset_hashes`, `elapsed_ms`, and per-metric `target_results` with `value`, `target`, and `passed` fields.

The `--offline` flag to `uv` ensures no external package resolution occurs. The evaluation code itself never makes network requests regardless of flag.

---

## Live-Model Evaluation

The codebase supports a live-model path for intent parsing and report generation (`bidscope.llm.deepseek.DeepSeekReportModel` and siblings). Live-model evaluation is **opt-in** and disabled by default.

To enable live-model execution (not part of the deterministic P0 evaluation):

```bash
export BIDSCOPE_REAL_MODEL_ENABLED=true
export BIDSCOPE_MODEL_BASE_URL=https://api.deepseek.com
export BIDSCOPE_MODEL_NAME=deepseek-chat
export BIDSCOPE_MODEL_API_KEY=sk-...
```

Live-model runs are **not** reproducible: they depend on provider availability, model version, network latency, and prompt drift. They are excluded from the `run_deterministic` runner and from the `target_pass` gate.

Running live-model evaluation requires:
- A reachable PostgreSQL instance (for checkpoint persistence)
- A valid model provider API key
- Network access to the model provider

Results from live runs must be reported separately from the deterministic fixture-consistency metrics, with the exact model name, provider, and pricing date named explicitly.

---

## Disclosure

- All P0 metrics are **fixture consistency** measurements, not live production quality measurements.
- The retrieval corpus is synthetic (`source=synthetic_demo`, `example.invalid` URLs). No real tender notices are used in evaluation.
- The model is a deterministic fake (`fake-deterministic`). No live LLM is invoked.
- The zero-cost result comes from an offline pricing fixture (CNY 0.00 / million tokens).
- Latency figures come from committed `latency_ms` values in the e2e dataset, not from live timing.
- The `target_pass=true` result means the deterministic pipeline reproduced its committed expected outputs. It does not mean the system has been validated against real-world tender data.
- Any future claim about production performance must be backed by a separately documented live evaluation with named datasets, models, pricing, and environment.

This document follows design section 16 criterion 9: targets are stated separately from measured values, and the evaluation method is named explicitly.
