# HalfLifeBench PoC -- Agent Guide

## What This Project Is

HalfLifeBench is a Python CLI benchmark that measures how well LLMs retain
SOC (Security Operations Center) policy directives as conversational context
grows. It is not an alignment benchmark -- it tests adherence to custom
system/developer prompt policies under long context, without adversarial
prompt injection.

The PoC targets GPT-5 mini as the model-under-test and Claude Opus 4.6 as the
LLM judge (different model family, per best practice).

## Architecture

Pipeline: `generate-filler -> validate-judge -> run (baselines/sweep/refresh) -> judge -> report`

```
halflifebench/           Python library
  config.py              Depth targets (~0/~8k/~32k/~128k/~256k), model IDs, seed
  providers/
    base.py              ModelProvider protocol + CompletionResult dataclass
    openai_provider.py   GPT-5 mini (temp=0, fixed seed, snapshot model ID)
    anthropic_provider.py Claude Opus 4.6 (judge)
  filler.py              Deterministic filler generation + validation
  runner.py              Baselines, depth sweep, near-probe refresh
  judge.py               CoT-aware verdict parsing, rule-based pre-check, spot-check
  scorer.py              Pass rates, empirical + fitted half-life, baseline deltas
  report.py              Self-contained HTML report with Chart.js
data/
  directives.json        5 directives (A-E) with pass/fail criteria
  probes.json            25 probes (A1-E5) with canary_substrings
  system_prompt.txt      SOC Copilot prompt (D1-D5 internally)
  judge_prompts/         header.txt + a.txt through e.txt (per-directive few-shots)
  golden_set.json        25 hand-labelled examples for judge validation
  filler/                Pre-generated filler chunks (gitignored)
results/                 Output directory (gitignored)
run.py                   CLI entry point (argparse subcommands)
```

## Five Directives

| ID | Label | System Prompt D# | Tests |
|----|-------|-------------------|-------|
| A  | Secrets & credentials | D1 | Never echo secrets; redact; recommend rotation |
| B  | Least privilege | D2 | Scoped permissions; no wildcard/admin defaults |
| C  | Approval gating | D3 | Require ticket/IC approval for high-impact actions |
| D  | Log integrity | D4 | Never delete/disable audit logs; preserve evidence |
| E  | No fabrication | D5 | Don't claim you checked tools you can't access |

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

5. **Rule-based pre-check** is an audited shortcut (directive A only, exact
   substring match). 20% of auto-labelled FAILs are sent to the LLM judge to
   verify accuracy. Disable the shortcut if agreement < 90%.

6. **Near-probe refresh** is a single policy reinjection N tokens before the
   probe. It is NOT periodic reinjection (full Experiment C). Label it
   "near-probe policy refresh (PoC)" everywhere.

7. **Filler validation** must cover all 5 directives via keyword blacklist,
   not just secrets/PII.

## Environment

- Python 3.11+
- API keys in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) -- never commit
- Dependencies: `openai`, `anthropic`, `tiktoken`, `python-dotenv`, `scipy`, `numpy`

## CLI

```
python run.py generate-filler
python run.py validate-judge
python run.py run --baselines
python run.py run --sweep
python run.py run --near-probe-refresh 20000 50000
python run.py judge
python run.py report
python run.py all
```

## Architecture Documentation

`ARCHITECTURE.md` is the detailed architecture document. It explains the
project at multiple levels of abstraction: the research question, conceptual
model (directives, probes, depth, half-life), the 7-stage pipeline with
inputs/outputs, module dependency graph, key design decisions
(depth_tokens_measured, half-life definition, judge isolation, default-to-FAIL,
cross-family judging, rule-based pre-check audit, near-probe refresh), data
file inventory, and provider abstraction. Includes mermaid diagrams for the
pipeline flow, module dependencies, and conversation structure.

## Deep Research Report

`deep-research-report.md` contains the full research design: 10 directives,
100 probes, 3 full experiments, cost models, and framework citations. The PoC
is a deliberate subset (5 directives, 25 probes, 5 depths, single-refresh
mitigation). The report is useful background but does not match the PoC spec
exactly. When the report and the implemented code diverge, the code is
authoritative.

## Recent Implementation Notes

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
