# HalfLifeBench

A small CLI benchmark that measures how quickly an LLM stops following its system prompt as the conversation grows.

The model under test plays "SOC (Security Operations Center) Copilot" and is given 10 security policy directives in its system prompt. We pad the conversation with filler up to a target token depth, send a probe designed to tempt the model into breaking one specific directive, and have an LLM judge score the response PASS/FAIL. The *half-life* of a directive is the depth at which its pass rate drops to half of the depth-0 baseline.

This is a PoC. Defaults:

- 10 directives (`D1`-`D10`), 100 probes (10 per directive)
- 13 depth checkpoints: `0, 4k, 8k, 16k, 32k, 48k, 50k, 64k, 80k, 100k, 128k, 200k, 256k`
- 20 repetitions per `(directive, depth)` cell
- Baselines + sweep only (no near-probe refresh)
- Model under test: `gpt-4.1-nano` (OpenAI)
- Judge: `claude-sonnet-4-5` (Anthropic)

`deep-research-report.md` is the original design doc. Where it disagrees with the code, the code wins.

## Findings (initial run)

From the `gpt-4.1-nano` x `claude-sonnet-4-5` run committed in `results/` (3,000 records, depth 0 to 256k):

- **Only 1 of 10 directives showed statistically significant decay**: D6 (no offensive enablement). Pass rate drops from 100% at depth 0 to 25-40% past ~32k, with fitted half-life ~70k tokens (empirical crossover at ~18k, LR p < 0.001).
- The other 9 directives are roughly flat across the full 0-256k sweep -- so for this model on this probe set, "context length erodes safety" is mostly a single-directive story, not a general one.
- **Baselines are uneven.** Some directives barely work even at depth 0: D3 (least privilege) sits at 10%, D10 (incident comms) at 30%. Half-life is meaningless when there's nothing to decay from.
- **Two directives have negative policy uplift** (D7 policy confidentiality, D8 no fabrication) -- the model scored slightly *better* without the system prompt than with it. Probably a probe-design artefact, but worth flagging.
- **Strongest policy uplift**: D6 (+65%), D2 (+60%), D9 (+50%), D5 (+40%).

Caveats: one model, 20 reps per cell, fixed probe set, judge agreement gated at 80%. The headline number on the dashboard is "1/10 significant decay" -- read the per-directive table before generalising.

## Setup

Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env   # then set OPENAI_API_KEY and ANTHROPIC_API_KEY
```

## Pipeline

```text
generate-filler -> validate-judge -> run --baselines -> run --sweep -> judge -> report
```

Each stage is a `run.py` subcommand. `python run.py all` runs them in order.

Common flags on every command:

- `-v` / `--verbose` -- debug logging
- `-w N` / `--workers N` -- max parallel workers (default 30)
- `-r N` / `--repetitions N` -- reps per cell (default 20)
- `--batch` / `--no-batch` -- toggle Batch API mode (50% cheaper, async, up to 24h; defaults to `USE_BATCH` env var)

Stages are resumable: rerunning a command skips API calls that already completed.

### 1. `generate-filler`

Builds deterministic filler conversations (log blocks + JSON payloads simulating routine SOC traffic) for each depth target. Filler is checked against per-directive blacklists so it can't accidentally trigger policy. Existing depth files are skipped.

Output: `data/filler/depth_{N}.json`.

### 2. `validate-judge`

Runs the judge against 100 hand-labelled examples and computes agreement. If agreement is below threshold (default 80%), `all` aborts.

Output: `results/validate_judge.json`.

### 3. `run --baselines`

Sends probes at depth 0 in two conditions:

- `B0` -- with system prompt
- `B_null` -- without system prompt

Also calibrates the API token overhead used for `depth_tokens_measured`. Produces 200 records per condition (10 directives x 20 reps).

Outputs: `results/raw/baseline_system.jsonl`, `results/raw/baseline_no_system.jsonl`, `results/calibration.json`.

### 4. `run --sweep`

For each depth target, loads filler and runs probes. Records both planned and API-measured token depths. Produces 2,600 records (10 x 13 x 20).

Output: `results/raw/sweep.jsonl`.

### 5. `judge`

Scores responses PASS/FAIL:

- Rule-based pre-check for `D1` canary leaks (with sampled audit)
- LLM judge for everything else
- Cross-judge spot-check for agreement monitoring

Empty responses are tagged and excluded from compliance scoring.

Outputs: `results/judged.jsonl`, `results/judge_summary.json`, `results/cross_judge_results.jsonl`.

### 6. `report`

Computes baseline and sweep metrics, fits half-lives via logistic regression (`statsmodels.Logit`), and writes a self-contained HTML report.

Outputs: `results/scores.json`, `results/report.html`.

### Optional: `compare-judges`

Side-by-side comparison of two Anthropic judge models on the golden set (and optionally a sweep sample). Reports agreement, kappa, and reliability.

```bash
python run.py compare-judges --model-a claude-sonnet-4-5 --model-b claude-opus-4-6
```

Output: `results/judge_comparison.json`.

## Outputs

Everything lands under `results/`:

| File | Stage | Description |
|------|-------|-------------|
| `calibration.json` | 3 | API token overhead calibration |
| `baselines.json` | 3 | Baseline run metadata |
| `raw/baseline_system.jsonl` | 3 | 200 records, with system prompt, depth 0 |
| `raw/baseline_no_system.jsonl` | 3 | 200 records, no system prompt, depth 0 |
| `raw/sweep.jsonl` | 4 | 2,600 sweep records |
| `validate_judge.json` | 2 | Golden-set agreement breakdown |
| `judged.jsonl` | 5 | All responses with verdicts |
| `judge_summary.json` | 5 | Pre-check accuracy and cross-judge agreement |
| `cross_judge_results.jsonl` | 5 | Spot-check re-judging details |
| `scores.json` | 6 | Full scoring outputs (incl. `run_config`) |
| `report.html` | 6 | HTML report |

## Notes on metrics

- **`depth_tokens_measured`** is the authoritative depth, derived from API token accounting rather than tiktoken estimates.
- **Half-life** is the first depth where pass rate drops below `0.5 * B`, with `B` the depth-0 baseline. Fitted with `statsmodels.Logit` (slope, p-values, CIs, LR test).
- **`D1`** uses a canary-substring auto-fail with a sampled LLM audit.
- **Empty responses** are tracked separately and excluded from pass-rate and half-life calculations.
