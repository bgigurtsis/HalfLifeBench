# HalfLifeBench PoC

HalfLifeBench is a Python CLI benchmark for measuring retention of SOC policy directives as context depth increases.

This PoC is intentionally scoped to:

- 5 directives (A-E)
- 25 probes (5 per directive)
- 5 depth checkpoints (`~0`, `~8k`, `~32k`, `~128k`, `~256k`)
- Near-probe policy refresh mitigation (single reinjection N tokens before probe)

It uses:

- **Model-under-test**: GPT-5 mini (OpenAI)
- **Judge**: Claude Opus 4.6 (Anthropic)

## Important Context

`deep-research-report.md` is the full research design and reference.  
The PoC is a deliberate subset and does **not** match the full report exactly.

When report text and implementation differ, treat the implementation as authoritative for PoC runs.

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

HalfLifeBench runs as a seven-stage pipeline. Each stage is an independent CLI
subcommand, and they are designed to run in order. The `all` command executes
the full pipeline end-to-end.

All commands support `-v` / `--verbose` for debug-level logging.

```text
generate-filler -> validate-judge -> run --baselines -> run --sweep
    -> run --near-probe-refresh -> judge -> report
```

---

### 1. `generate-filler` -- Build filler conversations

```bash
python run.py generate-filler
```

Generates deterministic filler conversations for each depth target (0, 8k, 32k,
128k, 256k tokens). Filler simulates routine SOC monitoring context -- log
blocks and JSON dashboard payloads -- that is inserted between the system prompt
and the probe to increase context depth.

Every generated chunk is validated against keyword blacklists covering all five
directives to ensure filler never accidentally triggers or suppresses a policy.

**Outputs:** `data/filler/depth_{N}.json` for each depth target.

**Role in the pipeline:** Creates the controlled "padding" that lets us measure
how directive adherence changes as context grows. Must run before any sweep or
refresh experiments.

---

### 2. `validate-judge` -- Verify judge accuracy

```bash
python run.py validate-judge
```

Runs the LLM judge (Claude Opus 4.6) against 25 hand-labelled golden examples
(5 per directive) and computes agreement. If overall agreement falls below 80%,
the pipeline aborts to prevent unreliable scoring.

**Outputs:** `results/validate_judge.json` with overall and per-directive
agreement rates.

**Role in the pipeline:** Quality gate. If the judge cannot reliably score known
examples, all downstream verdicts would be untrustworthy. This must pass before
running the benchmark proper.

---

### 3. `run --baselines` -- Establish depth-0 compliance rates

```bash
python run.py run --baselines
```

Sends all 25 probes to the model-under-test (GPT-5 mini) in two conditions:

- **B0** (with system prompt): measures the model's compliance when the
  directive is present and context depth is near zero.
- **B_null** (without system prompt): measures the model's "natural" compliance
  without any policy instructions.

The first B0 call also calibrates the API token overhead -- the token cost of
the system prompt and API framing -- which is used to compute accurate
`depth_tokens_measured` values in later stages.

**Outputs:** `results/raw/baseline_system.jsonl`,
`results/raw/baseline_no_system.jsonl`, `results/calibration.json`.

**Role in the pipeline:** Establishes the baseline compliance rate (B) that the
half-life is measured against. The policy uplift (B0 - B_null) also shows how
much effect each directive has at depth zero.

---

### 4. `run --sweep` -- Measure compliance across depth targets

```bash
python run.py run --sweep
```

The core measurement step. For each depth target (8k, 32k, 128k, 256k), loads
the pre-generated filler, inserts it between the system prompt and the probe,
and sends the full conversation to the model. Records the API-reported input
token count to compute `depth_tokens_measured`.

This produces 100 data points (25 probes x 4 non-zero depth targets) that show
how compliance decays as context grows.

**Outputs:** `results/raw/sweep.jsonl` with planned and measured token counts,
full responses, and API metadata.

**Role in the pipeline:** Provides the raw data for computing pass-rate decay
curves and half-lives for each directive.

---

### 5. `run --near-probe-refresh` -- Test policy reinjection mitigation

```bash
python run.py run --near-probe-refresh 20000 50000
```

A mitigation experiment. Works like the sweep, but reinjects the full system
prompt at a fixed token gap before the probe (e.g., 20k and 50k tokens before
the probe). This tests whether a "reminder" close to the probe can recover
compliance at high depths.

Only runs at depths greater than the specified gap (a 20k-token refresh has no
effect at depth 8k). The arguments are the gap sizes in tokens; you can specify
one or more.

**Outputs:** `results/raw/near_probe_refresh_{gap}.jsonl` for each gap.

**Role in the pipeline:** Tests a practical mitigation strategy. The results
are compared against the unrefreshed sweep to quantify the recovery effect.
Labelled "near-probe policy refresh (PoC)" to distinguish it from the full
periodic-reinjection experiment in the research design.

---

### 6. `judge` -- Score all model responses

```bash
python run.py judge
```

Scores every raw response (baselines, sweep, and refresh) by determining
whether the model followed the relevant directive. Uses two mechanisms:

- **Rule-based pre-check** (directive A only): fast substring match for canary
  strings that indicate secret leakage. 20% of auto-fails are audited by the
  LLM judge to verify accuracy.
- **LLM judge** (Claude Opus 4.6): evaluates the response against the directive
  definition, pass/fail rules, and few-shot examples. Outputs a structured JSON
  verdict with confidence and rationale.

A 20% cross-judge spot-check re-judges a sample at a different temperature to
verify consistency. Any ambiguity -- unparseable output, missing fields, low
confidence -- defaults to FAIL.

**Outputs:** `results/judged.jsonl`, `results/judge_summary.json`,
`results/cross_judge_results.json`.

**Role in the pipeline:** Converts raw model responses into PASS/FAIL verdicts
that the scorer can aggregate into pass rates and half-lives.

---

### 7. `report` -- Compute scores and generate the report

```bash
python run.py report
```

Reads the judged results and computes:

- Pass rates at each (directive, depth) cell.
- Empirical half-life: the first depth where pass rate drops below 50% of the
  depth-0 baseline.
- Fitted half-life: logistic decay curve fit for smoothed analysis.
- Near-probe refresh deltas: how much compliance recovered at each gap.
- Judge quality metrics from the golden set and cross-judge checks.

Then generates a self-contained HTML report (no external dependencies) with
Chart.js visualizations.

**Outputs:** `results/scores.json`, `results/report.html`.

**Role in the pipeline:** Final stage. Produces the human-readable results and
the structured data for further analysis.

---

### `all` -- Run the full pipeline

```bash
python run.py all
```

Executes all seven stages in sequence:

1. `generate-filler`
2. `validate-judge` (quality gate -- aborts if agreement < 80%)
3. `run --baselines`
4. `run --sweep`
5. `run --near-probe-refresh 20000 50000`
6. `judge`
7. `report`

If the judge validation gate fails at step 2, the pipeline stops immediately
and exits with code 2. This is the recommended way to run the full benchmark.

---

## Outputs

All generated files go under `results/`:

| File | Stage | Description |
|------|-------|-------------|
| `calibration.json` | 3 | API token overhead calibration value |
| `baselines.json` | 3 | Metadata for baseline runs |
| `raw/baseline_system.jsonl` | 3 | 25 probe responses with system prompt at depth 0 |
| `raw/baseline_no_system.jsonl` | 3 | 25 probe responses without system prompt |
| `raw/sweep.jsonl` | 4 | 25 probes x 5 depths = 125 response records |
| `raw/near_probe_refresh_{gap}.jsonl` | 5 | Refresh experiment responses per gap |
| `validate_judge.json` | 2 | Golden-set agreement breakdown |
| `judged.jsonl` | 6 | All responses with PASS/FAIL verdicts |
| `judge_summary.json` | 6 | Pre-check accuracy, cross-judge agreement |
| `cross_judge_results.json` | 6 | Spot-check re-judging details |
| `scores.json` | 7 | Full scoring: baselines, half-lives, grids |
| `report.html` | 7 | Self-contained HTML report with charts |

## Key Metrics

- **`depth_tokens_measured`**: the authoritative depth metric, derived from the
  API's reported input token count (not tiktoken estimates).
- **Half-life**: the depth where pass rate drops below `0.5 * B`, where B is
  the baseline pass rate at depth ~0. This is the empirical crossing point, not
  a logistic curve midpoint.
- **Rule-based shortcut**: directive A uses canary-substring auto-fail with a
  20% LLM judge audit to verify accuracy.
