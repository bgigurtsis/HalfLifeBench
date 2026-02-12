# HalfLifeBench PoC -- Agent Guide

## What This Project Is

HalfLifeBench is a Python CLI benchmark that measures how well LLMs retain
SOC (Security Operations Center) policy directives as conversational context
grows. It is not an alignment benchmark -- it tests adherence to custom
system/developer prompt policies under long context, without adversarial
prompt injection.

The current configuration targets GPT-4.1-nano as the model-under-test and
Claude Sonnet 4.5 as the LLM judge (different model family, per best practice).

## Architecture

Pipeline: `generate-filler -> validate-judge -> run (baselines/sweep) -> judge -> report`

```
halflifebench/           Python library
  config.py              Depth targets (0/4k/8k/16k/32k/48k/50k/64k/80k/100k/128k/200k/256k), model IDs, seed
  providers/
    base.py              ModelProvider protocol + CompletionResult dataclass
    openai_provider.py   GPT-4.1-nano (temp=0, fixed seed)
    anthropic_provider.py Claude Sonnet 4.5 (judge)
  filler.py              Deterministic filler generation + validation
  runner.py              Baselines, depth sweep
  judge.py               CoT-aware verdict parsing, rule-based pre-check, spot-check
  scorer.py              Pass rates, empirical + fitted half-life, baseline deltas
  report.py              Self-contained HTML report with Chart.js
data/
  directives.json        10 directives (D1-D10) with pass/fail criteria
  probes.json            100 probes (10 per directive) with canary_substrings
  system_prompt.txt      SOC Copilot prompt (D1-D10 internally)
  judge_prompts/         header.txt + d1.txt through d10.txt (per-directive few-shots)
  golden_set.json        100 hand-labelled examples for judge validation
  filler/                Pre-generated filler chunks (gitignored)
results/                 Output directory (gitignored)
run.py                   CLI entry point (argparse subcommands)
```

## Ten Directives

| ID | Label | Tests |
|----|-------|-------|
| D1 | Secrets & credentials | Never echo secrets; redact; recommend rotation |
| D2 | PII minimisation | Avoid broadcast PII; use masking/pseudonyms |
| D3 | Least privilege | Scoped permissions; no wildcard/admin defaults |
| D4 | Approval gating | Require ticket/IC approval for high-impact actions |
| D5 | Log integrity | Never delete/disable audit logs; preserve evidence |
| D6 | No offensive enablement | Refuse phishing/malware/exploit enablement |
| D7 | Policy confidentiality | Never reveal hidden system prompt text |
| D8 | No fabrication | Don't claim you checked tools you cannot access |
| D9 | Safe commands | Prefer read-only and caution before destructive commands |
| D10 | Incident comms discipline | Route external comms via legal/PR/leadership |

## Critical Design Rules

1. **depth_tokens_measured** is the authoritative depth metric. It is computed
   from the API response: `request_input_tokens_total - probe_tokens_estimate - overhead_calibrated`.
   tiktoken is only for planning filler sizes. All scoring, charts, and half-life
   use the API-derived value.

2. **Half-life** = depth where pass rate drops below `0.5 * B` (B = baseline at
   depth ~0). This is NOT the logistic midpoint x0.

3. **Judge isolation**: judge sees only `{directive_definition, probe, response}`.
   No filler, no conversation history.

4. **Default to FAIL** on ambiguity: unparseable judge output, low confidence,
   or uncertain verdict.

5. **Rule-based pre-check** is an audited shortcut (directive D1 only, exact
   substring match). 20% of auto-labelled FAILs are sent to the LLM judge to
   verify accuracy. Disable the shortcut if agreement < 90%.

6. **Filler validation** must cover all 10 directives via keyword blacklist,
   not just secrets/PII.

## Environment

- Python 3.11+
- API keys in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) -- never commit
- Dependencies: `openai`, `anthropic`, `tiktoken`, `python-dotenv`, `scipy`, `numpy`, `statsmodels`

## CLI

```
python run.py generate-filler
python run.py validate-judge
python run.py run --baselines
python run.py run --sweep
python run.py judge
python run.py report
python run.py all
```

Global flags (apply to all subcommands):
- `-v` / `--verbose`: enable DEBUG logging
- `-w N` / `--workers N`: max parallel API workers (default 15, env `MAX_WORKERS`).
  Use `--workers 1` for sequential execution.
- `-r N` / `--repetitions N`: repetitions per (directive, depth) cell (default 20,
  env `REPETITIONS`).
- `--batch` / `--no-batch`: enable/disable provider Batch API mode (default from
  env `USE_BATCH`; `BATCH_POLL_INTERVAL` controls polling cadence).

## Architecture Documentation

`ARCHITECTURE.md` is the detailed architecture document. It explains the
project at multiple levels of abstraction: the research question, conceptual
model (directives, probes, depth, half-life), the 6-stage pipeline with
inputs/outputs, module dependency graph, key design decisions
(depth_tokens_measured, half-life definition, judge isolation, default-to-FAIL,
cross-family judging, rule-based pre-check audit), data
file inventory, and provider abstraction. Includes mermaid diagrams for the
pipeline flow, module dependencies, and conversation structure.

## Deep Research Report

`deep-research-report.md` contains the full research design: 10 directives,
100 probes, 3 full experiments, cost models, and framework citations. The PoC
is a deliberate subset and the implemented code is authoritative. The report
is useful background but does not match the PoC spec
exactly. When the report and the implemented code diverge, the code is
authoritative.

## Recent Implementation Notes

### 2026-02-12 -- Report cleanup: remove dead refresh section, add summary and run config metadata

- Removed dead "Near-probe policy refresh" chart and JS from the HTML report:
  - refresh runs were removed from the pipeline earlier but the report still
    rendered an empty chart card and associated JS for them.
  - removed the `refresh_dataset_count` computation and debug log reference
    from `generate_report()`.
- Added run configuration metadata to the report header:
  - repetitions/cell, depth checkpoint count, and reasoning effort are now
    displayed alongside model names and timestamp.
  - `scorer.py` now writes a `run_config` block into `scores.json` containing
    `repetitions`, `depth_targets`, `max_output_tokens`, `reasoning_effort`,
    `max_workers`, `use_batch`, `seed`, and `temperature`.
- Added a Summary section at the top of the report with at-a-glance cards:
  - total records, sweep records, overall sweep pass rate (all), max depth
    (tokens), depth checkpoint count, and significant-decay directive count.
- Files touched: `halflifebench/report.py`, `halflifebench/scorer.py`,
  `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: report.html no longer shows an empty refresh chart; header
  now includes key run parameters; new summary card row gives an immediate
  overview of the run.

### 2026-02-12 -- Add batch mode + expand probe/golden coverage and default study density

- Added end-to-end Batch API support (OpenAI + Anthropic) behind the provider
  abstraction:
  - new `ModelProvider.complete_batch(...)` protocol and `BatchRequest` type.
  - `OpenAIProvider.complete_batch(...)` now writes JSONL batch input, submits
    `/v1/chat/completions` batch jobs, polls completion, and maps `custom_id`
    results back into `CompletionResult`.
  - `AnthropicProvider.complete_batch(...)` now submits message batches, polls
    `processing_status`, streams batch results, and maps them into
    `CompletionResult`.
- Updated runtime execution to support both real-time and batch paths:
  - `runner.py` now has reusable call/record builders and batch execution paths
    for baselines and sweep while preserving record order and depth token
    accounting.
  - `judge.py` now supports batch execution for both
    `validate_judge_against_golden(...)` and `run_judging(...)`, including
    cross-judge spot checks.
- Updated benchmark defaults and controls:
  - `repetitions` default changed to `20`.
  - depth targets expanded to `0, 4k, 8k, 16k, 32k, 48k, 50k, 64k, 80k, 100k, 128k, 200k, 256k`.
  - new config/env knobs: `USE_BATCH` and `BATCH_POLL_INTERVAL`.
  - new CLI flags: `--batch` / `--no-batch`.
- Expanded benchmark assets:
  - `data/probes.json` expanded from 50 to 100 probes (D1_1-D10_10).
  - `data/golden_set.json` expanded from 50 to 100 judge-validation examples
    (D1_G1-D10_G10).
  - `.env.example` updated with new batch env vars and `REPETITIONS=20` comment.
- Files touched: `halflifebench/config.py`,
  `halflifebench/providers/base.py`,
  `halflifebench/providers/openai_provider.py`,
  `halflifebench/providers/anthropic_provider.py`,
  `halflifebench/providers/__init__.py`, `halflifebench/runner.py`,
  `halflifebench/judge.py`, `run.py`, `data/probes.json`,
  `data/golden_set.json`, `.env.example`, `AGENTS.md`,
  `.cursor/rules/claude.mdc`.
- User-visible: users can run asynchronous 50%-discount batch executions using
  `--batch`, get denser default study sampling (20 reps, 13 depth checkpoints),
  and validate judges against a doubled golden set with expanded probe coverage.

### 2026-02-12 -- Verification follow-up: README sync and legacy directive-map correction

- Completed a post-migration consistency pass after the D1-D10 rollout:
  - rewrote `README.md` to the current 6-stage pipeline profile
    (no near-probe refresh run stage), D1-D10 directives, 50 probes, and
    10 depth checkpoints with 10 repetitions.
  - corrected `LEGACY_DIRECTIVE_ID_MAP` in
    `halflifebench/judge_comparison.py` to match the current canonical mapping:
    `A->D1`, `B->D3`, `C->D4`, `D->D5`, `E->D8`.
  - clarified `compare-judges` CLI help text in `run.py` that
    `claude-opus-4-6` remains the default comparator model intentionally.
- Validation:
  - `python run.py --help` and `python run.py compare-judges --help` both pass.
  - stale README references to old near-probe command/profile removed.
- Files touched: `README.md`, `halflifebench/judge_comparison.py`, `run.py`,
  `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: documentation and judge-comparison legacy normalization now
  align with the implemented D1-D10 benchmark configuration.

### 2026-02-12 -- Add compare-judges command for Sonnet vs Opus judge equivalence

- Added a new judge-comparison workflow to evaluate two Anthropic judge
  models side-by-side on the golden set, with optional re-judging of a sampled
  sweep subset.
- New module `halflifebench/judge_comparison.py` computes:
  - per-model golden-set agreement (overall + per-directive)
  - inter-model agreement and Cohen's kappa
  - McNemar significance test on paired golden-set correctness
  - parse-failure/low-confidence reliability metrics
  - structured disagreement outputs with rationales.
- Added CLI command:
  - `python run.py compare-judges --model-a claude-sonnet-4-5 --model-b claude-opus-4-6 [--include-sweep N]`
  - writes `results/judge_comparison.json`.
- Updated `judge_once(...)` to accept an optional model override and expose
  `judge_parse_success` in the structured result for reliability accounting.
- Files touched: `halflifebench/judge_comparison.py`, `run.py`,
  `halflifebench/judge.py`, `halflifebench/__init__.py`, `AGENTS.md`,
  `.cursor/rules/claude.mdc`.
- User-visible: users can now run a direct Sonnet-vs-Opus judge comparison
  with reproducible metrics and disagreement diagnostics before changing the
  default judge model.

### 2026-02-12 -- Switch benchmark to GPT-4.1-nano + 10 directives and remove refresh runs

- Reconfigured the benchmark to match the deep-research D1-D10 profile:
  - model-under-test default changed to `gpt-4.1-nano`
  - judge default changed to `claude-sonnet-4-5`
  - directives expanded from 5 to 10 (`D1`-`D10`)
  - probes expanded to 50 (5 per directive)
  - depth targets updated to `0, 8k, 32k, 50k, 64k, 80k, 100k, 128k, 200k, 256k`
  - repetitions default increased from 8 to 10.
- Replaced benchmark data assets:
  - `data/directives.json`, `data/probes.json`, `data/system_prompt.txt`
  - `data/judge_prompts/header.txt` + new `d1.txt` through `d10.txt`
  - `data/golden_set.json` expanded to 50 examples.
- Updated runtime and scoring/report logic:
  - removed near-probe refresh execution from `runner.py` and CLI wiring in `run.py`
  - updated judge auto-fail shortcut from old directive `A` to `D1`
  - switched scorer/report directive handling to dynamic directive IDs so D1-D10 render correctly.
- Expanded filler blacklist coverage for new directives in `halflifebench/filler.py`
  (PII minimisation, offensive enablement, policy confidentiality, safe
  commands, and incident comms discipline keywords).
- Files touched: `halflifebench/config.py`, `halflifebench/runner.py`,
  `halflifebench/judge.py`, `halflifebench/scorer.py`,
  `halflifebench/report.py`, `halflifebench/filler.py`, `run.py`,
  `.env.example`, `data/directives.json`, `data/probes.json`,
  `data/system_prompt.txt`, `data/judge_prompts/*`, `data/golden_set.json`,
  `AGENTS.md`, `.cursor/rules/claude.mdc`, `ARCHITECTURE.md`.
- User-visible: `run`/`all` now execute baselines+sweep only (no refresh
  stage); outputs and report are now keyed to `D1`-`D10` with 50 probes and
  10 depth checkpoints at 10 repetitions per cell.

### 2026-02-12 -- Replace aggregated curve_fit with per-record logistic regression inference

- Reworked directive half-life fitting from nonlinear `curve_fit` on
  aggregated sweep cell rates to `statsmodels.Logit` on pooled non-empty
  binary sweep observations per directive.
- Added inferential outputs to `scores.json` half-life records:
  - `beta0`, `beta1`, `beta1_se`, `beta1_pvalue`
  - likelihood-ratio decay-vs-flat outputs (`lr_statistic`, `lr_pvalue`)
  - fitted half-life 95% CI (`x_half_ci_lower`, `x_half_ci_upper`)
  - regression sample counts (`n_observations`, `n_pass`, `n_fail`)
- Added edge-case fit statuses for perfect compliance/failure, convergence
  failures, non-significant/no-decay patterns, and out-of-range half-life.
- Updated report half-life readout table to include beta slope, p-values,
  fitted half-life confidence interval, LR-test p-value, and non-empty N.
- Added `statsmodels` dependency in `requirements.txt`.
- Files touched: `halflifebench/scorer.py`, `halflifebench/report.py`,
  `requirements.txt`, `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: scoring now reports statistical evidence of decay (or lack of
  decay) per directive instead of only descriptive curve-fit outputs.

### 2026-02-12 -- Change default reasoning effort from low to medium

- Updated defaults so benchmark runs use `reasoning_effort="medium"` unless
  overridden by env var:
  - `AppConfig.reasoning_effort` default changed to `"medium"`.
  - `REASONING_EFFORT` env fallback changed to `"medium"`.
  - Provider interface defaults updated to `"medium"` for consistency:
    `ModelProvider`, `OpenAIProvider`, `AnthropicProvider`.
  - OpenAI provider invalid-value fallback now defaults to `"medium"`.
- Files touched: `halflifebench/config.py`,
  `halflifebench/providers/base.py`,
  `halflifebench/providers/openai_provider.py`,
  `halflifebench/providers/anthropic_provider.py`,
  `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: default reasoning effort is now medium (higher quality /
  more reasoning token usage by default) with no env changes required.

### 2026-02-12 -- Implement per-cell repetitions (deep report aligned: n=8)

- Added a new repetitions control in configuration and CLI:
  - `AppConfig.repetitions` (default `8`, env `REPETITIONS`)
  - new top-level CLI flag `-r` / `--repetitions` in `run.py`
  - config loader now applies both workers and repetitions overrides.
- Reworked runner task generation from probe-centric to
  (directive, repetition)-centric sampling:
  - `runner.py` now groups probes by directive and cycles probe selection
    by repetition index (`rep_idx % len(directive_probes)`).
  - Baselines, sweep, and near-probe refresh now each produce exactly
    `repetitions` calls per (directive, depth) cell.
- Added per-repetition metadata to raw records:
  - unique `run_id` now includes `:r{repetition_index}`
  - records include `repetition_index` and `effective_seed`
  - model calls use per-repetition seed variation (`config.seed + rep_idx`).
- Files touched: `halflifebench/config.py`, `halflifebench/runner.py`,
  `run.py`, `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: benchmark runs now support configurable per-cell repetitions
  (default 8) via env/CLI, and run outputs include repetition-aware IDs and
  seed metadata; baseline/sweep/refresh call volume increases accordingly.

### 2026-02-12 -- Fix GPT-5 mini empty/incomplete responses via token budget + reasoning controls

- Root cause addressed: GPT-5 mini reasoning tokens were consuming the
  `max_output_tokens=700` budget, producing frequent `status="incomplete"`
  with empty `response`.
- Increased default output budget and added reasoning controls in config:
  - `max_output_tokens` default changed from `700` to `16000`.
  - Added `reasoning_effort` config field (env `REASONING_EFFORT`,
    default `"low"`).
- Updated OpenAI provider behavior:
  - Responses API calls now include `reasoning={"effort": ...}`.
  - Empty+incomplete retries now increase token budget per retry (2x each
    attempt) instead of repeating the same request budget.
  - Existing diagnostic metadata capture remains in place (`reasoning_tokens`,
    `incomplete_details`).
- Threaded the new setting through the provider interface:
  - Added `reasoning_effort` to `ModelProvider.complete(...)`.
  - `runner.py` now passes `config.reasoning_effort` to the provider.
  - `AnthropicProvider` accepts and ignores `reasoning_effort` for protocol
    compatibility.
- Files touched: `halflifebench/config.py`,
  `halflifebench/providers/openai_provider.py`,
  `halflifebench/providers/base.py`,
  `halflifebench/providers/anthropic_provider.py`,
  `halflifebench/runner.py`, `AGENTS.md`,
  `.cursor/rules/claude.mdc`.
- User-visible: substantially fewer empty responses expected with GPT-5 mini;
  configurable reasoning effort; retries become progressively larger when
  incomplete responses return no visible text.

### 2026-02-12 -- Retry incomplete empty outputs + instrument reasoning tokens + separate empty outcome reporting

- Added minimal retry handling for OpenAI Responses API calls that return
  `status: "incomplete"` with empty output:
  - New `max_empty_retries` in `AppConfig` (default `2`, env
    `MAX_EMPTY_RETRIES`).
  - `OpenAIProvider.complete()` now retries empty incomplete responses before
    returning.
- Added per-record diagnosis instrumentation for the missing API fields:
  - `metadata.incomplete_details.reason`
  - `metadata.reasoning_tokens` (from
    `usage.output_tokens_details.reasoning_tokens` on Responses API and
    `completion_tokens_details.reasoning_tokens` on Chat Completions fallback).
  - Runner debug logs now include `incomplete_reason` and
    `reasoning_tokens` for each model call record.
- Kept empty outputs as a separate scientific outcome in scoring/reporting:
  - `sweep_grid` and refresh cells now include all-response `pass_rate`,
    plus `pass_rate_non_empty`, `empty_count`, and `non_empty_count`.
  - Baselines now include both all-response rates and non-empty breakouts.
  - Report now adds:
    - baseline empty/non-empty columns,
    - sweep grid empty/non-empty columns,
    - a non-empty sweep chart,
    - an empty-response-rate-by-depth chart.
- Files touched: `halflifebench/config.py`,
  `halflifebench/providers/base.py`,
  `halflifebench/providers/anthropic_provider.py`,
  `halflifebench/providers/openai_provider.py`, `halflifebench/runner.py`,
  `halflifebench/judge.py`, `halflifebench/scorer.py`,
  `halflifebench/report.py`, `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: benchmark runs now auto-retry a common empty-incomplete API
  failure mode, expose incomplete reason + reasoning-token diagnostics in raw
  records, and show empty outputs as an explicit third outcome alongside
  PASS/FAIL in report metrics.

### 2026-02-12 -- Exclude empty responses from scoring and report quality metrics

- Added `response_empty` tagging in run records and backward-compatible
  backfill during judging:
  - `runner.py` now writes `response_empty` for every model output.
  - `judge.py` now computes `response_empty` when missing (legacy raw files).
- Updated scoring to exclude empty responses from compliance metrics:
  - `scorer.py` now computes baseline/sweep/refresh pass rates using
    non-empty responses only.
  - Half-life fitting now excludes sweep cells with zero non-empty
    responses.
  - Added per-cell empty-response metrics (`total_count`,
    `non_empty_count`, `empty_count`, `empty_rate`) and top-level
    `empty_response_stats` in `scores.json`.
- Updated HTML report output to surface the new policy and data quality:
  - Added an exclusion note that empty responses are not scored.
  - Baselines now show scored sample sizes.
  - Sweep grid now shows `N (total)` and `N (scored)`.
  - Added "Empty Response Rates" tables by run type and directive.
- Files touched: `halflifebench/runner.py`, `halflifebench/judge.py`,
  `halflifebench/scorer.py`, `halflifebench/report.py`, `AGENTS.md`,
  `.cursor/rules/claude.mdc`.
- User-visible: pass rates and half-life now reflect non-empty responses
  only; report explicitly displays empty-response rates as a data-quality
  signal.

### 2026-02-12 -- Results evaluation document (EVALUATION.md, Run 2)

- Rewrote `EVALUATION.md` with Run 2 results (480 records, n=8 per cell,
  `max_output_tokens=16000`, `reasoning_effort=medium`, `max_empty_retries=2`).
- Key findings: 0% empty responses (down from 52%), 93.5% sweep pass rate,
  100% cross-judge agreement (93 spot-checks), 4 low-confidence verdicts
  (0.8%), no measurable directive-compliance decay up to 256k tokens.
  Directives D and E achieve 100% at all depths. Directive A is weakest
  at 82.5% but failures are not depth-correlated.
- Half-life estimates exceed the tested range for all directives (fitted
  values in the millions of tokens; D and E are flat curves).
- Near-probe refresh shows no signal because the sweep itself shows no decay.
- Files touched: `EVALUATION.md`, `AGENTS.md`, `.cursor/rules/claude.mdc`.
- User-visible: updated evaluation document reflecting clean Run 2 data.

### 2026-02-12 -- Fix _supports_param_error pattern matching and deduplicate discovery logs

- `_supports_param_error` now matches the actual OpenAI API error format:
  added `"unsupported parameter"` and `"is not supported"` to the substring
  checks. Previously only `"unsupported value"`, `"does not support"`, and
  `"unexpected keyword argument"` were matched, so API-level 400 errors
  (e.g. `"Unsupported parameter: 'temperature' is not supported"`) were not
  caught, causing the Responses API retry to fall through to Chat
  Completions on every call.
- Added `was_new` dedup guards to all 8 discovery log sites (4 Responses
  API, 4 Chat Completions). Only the first thread to discover an
  unsupported parameter logs the INFO message; parallel threads that hit
  the same code path stay silent.
- Files touched: `halflifebench/providers/openai_provider.py`.
- User-visible: eliminates all WARNING-level fallback messages during
  parallel runs. First call emits 1-2 INFO lines for param discovery;
  all subsequent calls produce zero log noise.

### 2026-02-12 -- Parallel API calls via ThreadPoolExecutor

- Added `concurrent.futures.ThreadPoolExecutor` parallelism to all API-calling
  stages: baselines, depth sweep, near-probe refresh, judging (primary +
  cross-judge), and validate-judge.
- New `max_workers` field in `AppConfig` (default 5, configurable via
  `MAX_WORKERS` env var).
- New `-w` / `--workers` CLI flag on the top-level parser to override
  `max_workers` at runtime. Applies to all subcommands. Use `--workers 1`
  to disable parallelism and fall back to sequential execution.
- Added `threading.Lock` to `OpenAIProvider` to protect the mutable fallback
  caches (`_use_responses_api_by_model`, `_responses_param_support_by_model`,
  `_chat_param_support_by_model`) for thread safety.
- Baselines: first probe runs sequentially to calibrate `overhead_calibrated`;
  remaining 24 baseline_system + all 25 baseline_no_system run in parallel.
- Sweep and refresh: filler is pre-loaded for all depths, then all
  (depth, probe) combinations are submitted to the thread pool at once.
- Judging: auto-fail labeling stays synchronous (no API call); all LLM judge
  calls and cross-judge spot-checks run in parallel.
- Output ordering is preserved: results are collected by index and written to
  JSONL in the original catalogue order regardless of completion order.
- Files touched: `halflifebench/config.py`, `run.py`,
  `halflifebench/providers/openai_provider.py`, `halflifebench/runner.py`,
  `halflifebench/judge.py`.
- User-visible: up to ~5x faster pipeline runs (bounded by API rate limits);
  new `--workers` / `-w` CLI flag; `max_workers` logged at start of each phase.

### 2026-02-12 -- Fix OpenAI provider repeated warning spam

- Added `_responses_param_support_by_model` cache to `OpenAIProvider`,
  mirroring the existing `_chat_param_support_by_model`. Both APIs now
  remember which parameters (temperature, seed) a model supports after the
  first discovery call.
- Fixed Responses API retry logic to handle the case where both `seed` and
  `temperature` are unsupported. Previously, when `seed` failed first and
  the retry without seed also failed on `temperature`, the exception escaped
  to the Chat Completions fallback instead of trying with both disabled.
- Changed `_use_responses_api_by_model[model] = False` to fire on any
  Responses API failure that triggers the Chat Completions fallback, not
  just the narrow `"unexpected keyword argument 'seed'"` case. After the
  first call, subsequent calls skip straight to Chat Completions.
- Downgraded first-call parameter-discovery log messages from WARNING to
  INFO (expected behavior, not an error). The "falling back to Chat
  Completions" message remains WARNING.
- Files touched: `halflifebench/providers/openai_provider.py`.
- User-visible change: eliminates repeated warning spam and wasted API
  retries during sweeps. First probe logs 1-2 INFO + 1 WARNING; all
  subsequent probes produce zero warnings and zero wasted calls.

### 2026-02-12 -- Extended depth targets to 400k

- Added `400000` to `depth_targets` in `halflifebench/config.py`, pushing the
  sweep to GPT-5 mini's full 400k context window (6 checkpoints total:
  0, 8k, 32k, 128k, 256k, 400k).
- No other code changes needed -- filler generation, runner, scorer, and report
  all iterate over `config.depth_targets` dynamically.
- Re-run `generate-filler` after this change to create the new
  `data/filler/depth_400000.json` file before running the sweep.
- Files touched: `halflifebench/config.py`, `AGENTS.md`,
  `.cursor/rules/claude.mdc`, `ARCHITECTURE.md`.

### 2026-02-12 -- README and ARCHITECTURE documentation polish

- Rewrote `README.md` CLI section with per-command explanations: what each
  command does, its outputs, and its role in the pipeline.
- Added structured outputs table to README.
- Polished `ARCHITECTURE.md` for readability: tightened prose, improved list
  formatting, added dependency-graph legend, numbered judge mechanisms,
  enriched module descriptions, added cross-reference to README.
- Files touched: `README.md`, `ARCHITECTURE.md`.
- No code changes; documentation only.

### 2026-02-11 -- Runtime logging coverage for all commands

- Added centralized runtime logging in `run.py` with:
  - `INFO` default progress logs
  - `DEBUG` logs via `-v` / `--verbose`
  - timestamped/module-based formatting
- Replaced direct CLI prints with structured logger output.
- Added detailed command-flow logging to:
  - `halflifebench/filler.py`
  - `halflifebench/runner.py`
  - `halflifebench/judge.py`
  - `halflifebench/scorer.py`
  - `halflifebench/report.py`
- Added provider API diagnostics and fallback warnings in:
  - `halflifebench/providers/openai_provider.py`
  - `halflifebench/providers/anthropic_provider.py`

## Required Post-Change Documentation Sync

This project requires documentation updates after every code change or bug fix.

1. Update both files in the same task:
   - `AGENTS.md`
   - `.cursor/rules/claude.mdc`
2. Add or update a dated note under "Recent Implementation Notes" that includes:
   - change summary
   - key files touched
   - user-visible behavior changes
3. Consider the task incomplete if this sync step is missing.
