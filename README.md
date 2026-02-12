# HalfLifeBench PoC

HalfLifeBench is a Python CLI benchmark for measuring retention of SOC policy
directives as context depth increases.

This PoC currently runs with:

- 10 directives (`D1`-`D10`)
- 100 probes (10 per directive)
- 13 depth checkpoints (`0, 4k, 8k, 16k, 32k, 48k, 50k, 64k, 80k, 100k, 128k, 200k, 256k`)
- 20 repetitions per `(directive, depth)` cell
- Baselines + sweep only (no near-probe refresh in the default run profile)

Models:

- **Model-under-test**: `gpt-4.1-nano` (OpenAI)
- **Judge**: `claude-sonnet-4-5` (Anthropic)

## Important Context

`deep-research-report.md` is the full research design and reference.
The PoC is scoped and may differ from that report in implementation details.

When report text and implementation differ, treat the implementation as
authoritative for benchmark runs.

## Setup

1. Create and activate a virtual environment (Python 3.11+ recommended).
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` from `.env.example` and set:
   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`

## Project Structure

```text
halflifebench/
  config.py
  providers/
  filler.py
  runner.py
  judge.py
  judge_comparison.py
  scorer.py
  report.py
data/
  directives.json
  probes.json
  system_prompt.txt
  judge_prompts/
  golden_set.json
run.py
```

## Pipeline and CLI Commands

HalfLifeBench runs as a six-stage pipeline. Each stage is an independent CLI
subcommand, and `all` executes the pipeline end-to-end.

All commands support:

- `-v` / `--verbose` for debug logging
- `-w` / `--workers` to override max parallel workers (default 30)
- `-r` / `--repetitions` to override repetitions per cell (default 20)
- `--batch` / `--no-batch` to enable or disable Batch API mode (50% cheaper,
  async up to 24h; default from `USE_BATCH` env var)

All stages support crash-safe resume: if a run is interrupted, rerunning the
same command skips already-completed API calls and continues from where it
left off.

```text
generate-filler -> validate-judge -> run --baselines -> run --sweep -> judge -> report
```

---

### 1. `generate-filler` -- Build filler conversations

```bash
python run.py generate-filler
```

Generates deterministic filler conversations for each configured depth target.
Filler simulates routine SOC monitoring context (large log blocks + JSON
payloads) inserted between system prompt and probe.

Filler is validated against blacklists covering all directives to prevent
accidental policy-triggering content. Existing depth files are skipped
(idempotent).

**Outputs:** `data/filler/depth_{N}.json` for each depth target.

---

### 2. `validate-judge` -- Verify judge accuracy on golden set

```bash
python run.py validate-judge
```

Runs the LLM judge against 100 hand-labelled examples (10 per directive) and
computes agreement. If agreement is below threshold (default 80%), `all`
aborts.

**Outputs:** `results/validate_judge.json`.

---

### 3. `run --baselines` -- Establish depth-0 compliance rates

```bash
python run.py run --baselines
```

Sends probes in two conditions:

- **B0**: with system prompt
- **B_null**: without system prompt

Also calibrates API overhead used in `depth_tokens_measured`.

With defaults, this produces 200 records per condition:

- `10 directives x 20 repetitions`

**Outputs:** `results/raw/baseline_system.jsonl`,
`results/raw/baseline_no_system.jsonl`, `results/calibration.json`.

---

### 4. `run --sweep` -- Measure compliance across depth targets

```bash
python run.py run --sweep
```

For each depth target, loads filler and evaluates probe responses at that
depth. Records planned and API-measured token depths.

With defaults, this produces 2600 records:

- `10 directives x 13 depths x 20 repetitions`

**Outputs:** `results/raw/sweep.jsonl`.

---

### 5. `judge` -- Score all stored model responses

```bash
python run.py judge
```

Scores responses for PASS/FAIL per directive using:

- Rule-based pre-check for `D1` canary leaks (with audit sample)
- LLM judge scoring for all other cases
- Cross-judge spot-check sample for agreement monitoring

Empty responses are tagged and excluded from compliance scoring.

**Outputs:** `results/judged.jsonl`, `results/judge_summary.json`,
`results/cross_judge_results.jsonl`.

---

### 6. `report` -- Compute scores and generate HTML report

```bash
python run.py report
```

Computes baseline/sweep metrics, half-life outputs (logistic regression with
inferential statistics), and judge quality metrics, then generates a
self-contained HTML report with summary cards, per-directive charts, a
consolidated compliance line, and empty-response quality tables.

**Outputs:** `results/scores.json`, `results/report.html`.

---

### Optional: `compare-judges` -- Compare two Anthropic judge models

```bash
python run.py compare-judges --model-a claude-sonnet-4-5 --model-b claude-opus-4-6
```

Runs side-by-side judging comparison on golden-set examples (and optionally a
sample of sweep rows) to evaluate agreement, kappa, and relative reliability.

**Outputs:** `results/judge_comparison.json`.

---

### `all` -- Run the full pipeline

```bash
python run.py all
```

Executes all six stages in sequence:

1. `generate-filler`
2. `validate-judge` (quality gate)
3. `run --baselines`
4. `run --sweep`
5. `judge`
6. `report`

## Outputs

All generated files go under `results/`:

| File | Stage | Description |
|------|-------|-------------|
| `calibration.json` | 3 | API token overhead calibration value |
| `baselines.json` | 3 | Metadata for baseline runs |
| `raw/baseline_system.jsonl` | 3 | 200 records: with system prompt at depth 0 |
| `raw/baseline_no_system.jsonl` | 3 | 200 records: without system prompt at depth 0 |
| `raw/sweep.jsonl` | 4 | 2600 sweep response records |
| `validate_judge.json` | 2 | Golden-set agreement breakdown |
| `judged.jsonl` | 5 | All responses with PASS/FAIL verdicts |
| `judge_summary.json` | 5 | Pre-check accuracy and cross-judge agreement |
| `cross_judge_results.jsonl` | 5 | Spot-check re-judging details |
| `scores.json` | 6 | Full scoring outputs (includes `run_config` metadata) |
| `report.html` | 6 | Self-contained HTML report with charts |

## Key Metrics

- **`depth_tokens_measured`**: authoritative depth metric derived from API token
  accounting (not tiktoken estimates).
- **Half-life**: first depth where pass rate drops below `0.5 * B`, where `B`
  is the baseline pass rate at depth ~0. Fitted via `statsmodels.Logit` with
  inferential outputs (slope, p-values, confidence intervals, LR test).
- **Rule-based shortcut**: `D1` canary-substring auto-fail with sampled LLM
  audit for quality control.
- **Empty-response exclusion**: empty outputs are tracked as a data-quality
  signal and excluded from pass-rate and half-life calculations.
