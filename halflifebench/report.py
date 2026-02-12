from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Dict, List

from .config import AppConfig
from .utils import read_json, read_jsonl, write_text

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DIRECTIVE_NAMES = {
    "D1": "Secrets and credentials",
    "D2": "PII minimisation",
    "D3": "Least privilege",
    "D4": "Approval gating",
    "D5": "Evidence and log integrity",
    "D6": "No offensive enablement",
    "D7": "Policy confidentiality",
    "D8": "No fabrication",
    "D9": "Safe commands",
    "D10": "Incident comms discipline",
}


def _directive_sort_key(directive_id: str) -> tuple[int, int | str]:
    if directive_id.startswith("D") and directive_id[1:].isdigit():
        return (0, int(directive_id[1:]))
    return (1, directive_id)


def _directive_ids(scores: Dict) -> List[str]:
    candidates = set()
    for key in (
        "baseline_system",
        "baseline_no_system",
        "baseline_system_non_empty",
        "baseline_no_system_non_empty",
        "policy_uplift",
        "sweep_grid",
        "half_life",
    ):
        block = scores.get(key, {})
        if isinstance(block, dict):
            candidates.update(str(k) for k in block.keys() if k)
    by_directive = scores.get("empty_response_stats", {}).get("by_directive", {})
    if isinstance(by_directive, dict):
        candidates.update(str(k) for k in by_directive.keys() if k)
    return sorted(candidates, key=_directive_sort_key)


def _directive_name(d: str) -> str:
    return DIRECTIVE_NAMES.get(d, d)


def _fmt_rate(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2%}"
    return "n/a"


def _fmt_sci(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.3e}"
    return "n/a"


def _fmt_pvalue(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    numeric = float(value)
    if numeric < 0.001:
        return "&lt;0.001"
    return f"{numeric:.3f}"


def _fmt_ci(lower: object, upper: object) -> str:
    if isinstance(lower, (int, float)) and isinstance(upper, (int, float)):
        return f"[{float(lower):,.0f}, {float(upper):,.0f}]"
    return "n/a"


def _build_baseline_rows(scores: Dict) -> str:
    rows = []
    directive_ids = _directive_ids(scores)
    baseline_counts = scores.get("baseline_counts", {})
    baseline_system_non_empty = scores.get("baseline_system_non_empty", {})
    baseline_no_system_non_empty = scores.get("baseline_no_system_non_empty", {})
    for d in directive_ids:
        b0 = scores["baseline_system"].get(d, 0.0)
        bnull = scores["baseline_no_system"].get(d, 0.0)
        uplift = scores["policy_uplift"].get(d, 0.0)
        counts = baseline_counts.get(d, {})
        b0_empty = int(counts.get("baseline_system_empty_count", 0))
        bnull_empty = int(counts.get("baseline_no_system_empty_count", 0))
        b0_non_empty_rate = baseline_system_non_empty.get(d)
        bnull_non_empty_rate = baseline_no_system_non_empty.get(d)
        rows.append(
            f"<tr><td>{d} - {_directive_name(d)}</td>"
            f"<td>{_fmt_rate(b0)}</td><td>{_fmt_rate(bnull)}</td><td>{uplift:+.2%}</td>"
            f"<td>{b0_empty} / {bnull_empty}</td>"
            f"<td>{_fmt_rate(b0_non_empty_rate)} / {_fmt_rate(bnull_non_empty_rate)}</td></tr>"
        )
    return "\n".join(rows)


def _build_half_life_rows(scores: Dict) -> str:
    rows = []
    for d in _directive_ids(scores):
        item = scores["half_life"].get(d, {})
        empirical = item.get("x_half_empirical")
        fitted = item.get("x_half_fitted")
        status = item.get("fit_status", "n/a")
        beta1_pvalue = item.get("beta1_pvalue")
        lr_pvalue = item.get("lr_pvalue")
        n_observations = int(item.get("n_observations", 0) or 0)
        empirical_txt = "No decay detected" if empirical is None else f"{empirical:,.0f}"
        fitted_txt = "n/a" if fitted is None else f"{fitted:,.0f}"
        beta1_pvalue_txt = _fmt_pvalue(beta1_pvalue)
        lr_pvalue_txt = _fmt_pvalue(lr_pvalue)
        if isinstance(beta1_pvalue, (int, float)) and float(beta1_pvalue) < 0.05:
            beta1_pvalue_txt = f"<strong>{beta1_pvalue_txt}</strong>"
        if isinstance(lr_pvalue, (int, float)) and float(lr_pvalue) < 0.05:
            lr_pvalue_txt = f"<strong>{lr_pvalue_txt}</strong>"
        rows.append(
            f"<tr><td>{d} - {_directive_name(d)}</td>"
            f"<td>{item.get('baseline_b', 0.0):.2%}</td>"
            f"<td>{item.get('target_half_b', 0.0):.2%}</td>"
            f"<td>{_fmt_sci(item.get('beta1'))}</td>"
            f"<td>{beta1_pvalue_txt}</td>"
            f"<td>{empirical_txt}</td>"
            f"<td>{fitted_txt}</td>"
            f"<td>{_fmt_ci(item.get('x_half_ci_lower'), item.get('x_half_ci_upper'))}</td>"
            f"<td>{lr_pvalue_txt}</td>"
            f"<td>{status}</td>"
            f"<td>{n_observations}</td></tr>"
        )
    return "\n".join(rows)


def _build_sweep_grid_rows(scores: Dict) -> str:
    rows = []
    for d in _directive_ids(scores):
        for cell in scores["sweep_grid"].get(d, []):
            pass_rate = cell.get("pass_rate")
            pass_rate_non_empty = cell.get("pass_rate_non_empty")
            total_count = int(cell.get("count", 0))
            non_empty_count = int(cell.get("non_empty_count", 0))
            empty_count = int(cell.get("empty_count", 0))
            rows.append(
                f"<tr><td>{d}</td><td>{_directive_name(d)}</td>"
                f"<td>{cell['depth_target_tokens']:,}</td>"
                f"<td>{cell['depth_tokens_measured_mean']:.0f}</td>"
                f"<td>{_fmt_rate(pass_rate)}</td><td>{empty_count}</td>"
                f"<td>{_fmt_rate(pass_rate_non_empty)}</td>"
                f"<td>{total_count}</td><td>{non_empty_count}</td></tr>"
            )
    return "\n".join(rows)


def _build_empty_response_rows_by_run_type(scores: Dict) -> str:
    rows = []
    by_run_type = scores.get("empty_response_stats", {}).get("by_run_type", {})
    for run_type in sorted(by_run_type.keys()):
        metrics = by_run_type.get(run_type, {})
        rows.append(
            f"<tr><td>{run_type}</td>"
            f"<td>{int(metrics.get('total_count', 0))}</td>"
            f"<td>{int(metrics.get('empty_count', 0))}</td>"
            f"<td>{int(metrics.get('non_empty_count', 0))}</td>"
            f"<td>{_fmt_rate(metrics.get('empty_rate'))}</td></tr>"
        )
    return "\n".join(rows)


def _build_empty_response_rows_by_directive(scores: Dict) -> str:
    rows = []
    by_directive = scores.get("empty_response_stats", {}).get("by_directive", {})
    for directive_id in _directive_ids(scores):
        metrics = by_directive.get(directive_id, {})
        rows.append(
            f"<tr><td>{directive_id} - {_directive_name(directive_id)}</td>"
            f"<td>{int(metrics.get('total_count', 0))}</td>"
            f"<td>{int(metrics.get('empty_count', 0))}</td>"
            f"<td>{int(metrics.get('non_empty_count', 0))}</td>"
            f"<td>{_fmt_rate(metrics.get('empty_rate'))}</td></tr>"
        )
    return "\n".join(rows)


def generate_report(config: AppConfig) -> Path:
    scores_path = config.results_dir / "scores.json"
    if not scores_path.exists():
        raise FileNotFoundError("results/scores.json not found. Run scoring first.")
    logger.info("Generating report from scores file: %s", scores_path)
    scores = read_json(scores_path)

    judge_quality = scores.get("judge_quality", {})
    validate = judge_quality.get("validate_judge", {})
    judge_summary = judge_quality.get("judge_summary", {})

    validate_agreement = validate.get("overall_agreement")
    cross_agreement = judge_summary.get("cross_judge_agreement")
    precheck_accuracy = judge_summary.get("precheck_accuracy")
    judged_path = config.results_dir / "judged.jsonl"
    judged_rows = read_jsonl(judged_path)
    low_confidence_count = sum(1 for r in judged_rows if r.get("low_confidence"))
    logger.debug(
        "Loaded judged rows for report quality metrics: path=%s rows=%d low_confidence=%d",
        judged_path,
        len(judged_rows),
        low_confidence_count,
    )

    show_warning = False
    warning_msgs: List[str] = []
    if isinstance(cross_agreement, (float, int)) and cross_agreement < config.cross_judge_warning_threshold:
        show_warning = True
        warning_msgs.append(
            f"Cross-judge agreement is below threshold ({cross_agreement:.2%} < "
            f"{config.cross_judge_warning_threshold:.0%})."
        )
        logger.warning(
            "Cross-judge agreement below threshold: %.2f%% < %.0f%%",
            float(cross_agreement) * 100.0,
            config.cross_judge_warning_threshold * 100.0,
        )
    if isinstance(precheck_accuracy, (float, int)) and precheck_accuracy < config.precheck_accuracy_threshold:
        show_warning = True
        warning_msgs.append(
            f"Rule-based pre-check audit accuracy is below threshold ({precheck_accuracy:.2%} < "
            f"{config.precheck_accuracy_threshold:.0%})."
        )
        logger.warning(
            "Rule-based pre-check accuracy below threshold: %.2f%% < %.0f%%",
            float(precheck_accuracy) * 100.0,
            config.precheck_accuracy_threshold * 100.0,
        )

    scoring_policy = scores.get("scoring_policy", {})
    exclusion_note = ""
    if scoring_policy.get("empty_outcome_tracked_separately"):
        exclusion_note = (
            "<div class='note'>"
            "Pass rates show all responses, with a separate empty-output outcome and non-empty pass-rate breakout."
            "</div>"
        )

    warning_block = ""
    if show_warning:
        warning_block = (
            "<div class='warn'><strong>Quality warning:</strong><ul>"
            + "".join([f"<li>{m}</li>" for m in warning_msgs])
            + "</ul></div>"
        )

    directive_ids = _directive_ids(scores)
    directive_labels = {d: _directive_name(d) for d in directive_ids}
    data_json = json.dumps(scores)
    directives_json = json.dumps(directive_ids)
    labels_json = json.dumps(directive_labels)
    baseline_row_count = sum(1 for d in directive_ids if d in scores.get("baseline_system", {}))
    sweep_row_count = sum(len(scores.get("sweep_grid", {}).get(d, [])) for d in directive_ids)
    logger.debug(
        "Report table/chart counts baseline_rows=%d sweep_rows=%d",
        baseline_row_count,
        sweep_row_count,
    )

    # Run configuration and summary metrics
    run_config = scores.get("run_config", {})
    record_counts = scores.get("record_counts", {})
    total_records = sum(record_counts.values())
    sweep_record_count = record_counts.get("sweep", 0)

    _sweep_passes = 0
    _sweep_total = 0
    for _d_cells in scores.get("sweep_grid", {}).values():
        for _cell in _d_cells:
            _c = int(_cell.get("count", 0))
            _sweep_total += _c
            _sweep_passes += round(float(_cell.get("pass_rate", 0)) * _c)
    overall_sweep_pass_rate = _sweep_passes / _sweep_total if _sweep_total > 0 else 0.0

    _all_depths: set[int] = set()
    for _d_cells in scores.get("sweep_grid", {}).values():
        for _cell in _d_cells:
            _all_depths.add(int(_cell.get("depth_target_tokens", 0)))
    depth_count = len(_all_depths)
    depth_max = max(_all_depths) if _all_depths else 0

    half_life_data = scores.get("half_life", {})
    decay_detected_count = sum(
        1 for hl in half_life_data.values() if hl.get("fit_status") == "ok"
    )
    total_directives_fitted = len(half_life_data)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HalfLifeBench PoC Report</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111; }}
    h1, h2 {{ margin: 0 0 8px 0; }}
    .meta {{ color: #555; margin-bottom: 16px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 14px; }}
    th {{ background: #f7f7f7; }}
    .note {{ border: 1px solid #9ec5fe; background: #e7f1ff; padding: 12px; margin: 0 0 16px; }}
    .warn {{ border: 1px solid #e67e22; background: #fff3e6; padding: 12px; margin: 12px 0 18px; }}
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 14px; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px; }}
  </style>
</head>
<body>
  <h1>HalfLifeBench PoC</h1>
  <div class="meta">
    Generated: {_now_iso()} |
    Model-under-test: {scores.get("model_under_test")} |
    Judge: {scores.get("judge_model")} |
    Repetitions/cell: {run_config.get("repetitions", "n/a")} |
    Depth checkpoints: {depth_count} |
    Reasoning effort: {run_config.get("reasoning_effort", "n/a")}
  </div>
  {exclusion_note}
  {warning_block}

  <h2>Summary</h2>
  <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));">
    <div class="card" style="text-align:center;"><strong>{total_records:,}</strong><br>Total records</div>
    <div class="card" style="text-align:center;"><strong>{sweep_record_count:,}</strong><br>Sweep records</div>
    <div class="card" style="text-align:center;"><strong>{overall_sweep_pass_rate:.1%}</strong><br>Sweep pass rate (all)</div>
    <div class="card" style="text-align:center;"><strong>{depth_max:,}</strong><br>Max depth (tokens)</div>
    <div class="card" style="text-align:center;"><strong>{depth_count}</strong><br>Depth checkpoints</div>
    <div class="card" style="text-align:center;"><strong>{decay_detected_count} / {total_directives_fitted}</strong><br>Significant decay detected</div>
  </div>

  <h2>Baselines</h2>
  <table>
    <thead>
      <tr>
        <th>Directive</th>
        <th>Token-zero baseline (B0)</th>
        <th>No-system baseline (B_null)</th>
        <th>Policy uplift (B0 - B_null)</th>
        <th>Empty count (B0 / B_null)</th>
        <th>Non-empty pass rate (B0 / B_null)</th>
      </tr>
    </thead>
    <tbody>
      {_build_baseline_rows(scores)}
    </tbody>
  </table>

  <div class="grid">
    <div class="card">
      <h2>Sweep Compliance vs depth_tokens_measured</h2>
      <canvas id="sweepChart"></canvas>
    </div>
    <div class="card">
      <h2>Sweep Compliance (Non-empty only)</h2>
      <canvas id="sweepNonEmptyChart"></canvas>
    </div>
    <div class="card">
      <h2>Empty Response Rate by Depth</h2>
      <canvas id="emptyRateChart"></canvas>
    </div>
  </div>

  <h2>Sweep Grid</h2>
  <table>
    <thead>
      <tr>
        <th>Directive ID</th><th>Directive</th><th>Depth target</th><th>Mean depth_tokens_measured</th><th>Pass rate (all)</th><th>Empty</th><th>Pass rate (non-empty)</th><th>N (total)</th><th>N (non-empty)</th>
      </tr>
    </thead>
    <tbody>
      {_build_sweep_grid_rows(scores)}
    </tbody>
  </table>

  <h2>Empty Response Rates</h2>
  <div class="grid">
    <div class="card">
      <h2>By Run Type</h2>
      <table>
        <thead>
          <tr>
            <th>Run type</th><th>Total</th><th>Empty</th><th>Non-empty</th><th>Empty rate</th>
          </tr>
        </thead>
        <tbody>
          {_build_empty_response_rows_by_run_type(scores)}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>By Directive</h2>
      <table>
        <thead>
          <tr>
            <th>Directive</th><th>Total</th><th>Empty</th><th>Non-empty</th><th>Empty rate</th>
          </tr>
        </thead>
        <tbody>
          {_build_empty_response_rows_by_directive(scores)}
        </tbody>
      </table>
    </div>
  </div>

  <h2>Half-life Readout</h2>
  <table>
    <thead>
      <tr>
        <th>Directive</th><th>Baseline B</th><th>Target 0.5*B</th><th>beta1</th><th>beta1 p-value</th><th>Empirical x_half</th><th>Fitted x_half</th><th>95% CI (fitted x_half)</th><th>LR p-value (decay vs flat)</th><th>Fit status</th><th>N (non-empty)</th>
      </tr>
    </thead>
    <tbody>
      {_build_half_life_rows(scores)}
    </tbody>
  </table>

  <h2>Judge Quality</h2>
  <table>
    <thead>
      <tr>
        <th>Metric</th><th>Value</th>
      </tr>
    </thead>
    <tbody>
      <tr><td>Golden set agreement</td><td>{(validate_agreement if isinstance(validate_agreement, (float, int)) else 0.0):.2%}</td></tr>
      <tr><td>Cross-judge agreement</td><td>{(cross_agreement if isinstance(cross_agreement, (float, int)) else 0.0):.2%}</td></tr>
      <tr><td>Rule-based pre-check accuracy</td><td>{(precheck_accuracy if isinstance(precheck_accuracy, (float, int)) else 0.0):.2%}</td></tr>
      <tr><td>Low-confidence verdicts</td><td>{low_confidence_count}</td></tr>
    </tbody>
  </table>

  <script>
    const SCORES = {data_json};
    const directives = {directives_json};
    const labels = {labels_json};
    const colorPalette = [
      "#3366cc", "#dc3912", "#ff9900", "#109618", "#990099",
      "#0099c6", "#dd4477", "#66aa00", "#b82e2e", "#316395",
      "#994499", "#22aa99"
    ];
    const colors = {{}};
    directives.forEach((d, idx) => {{
      colors[d] = colorPalette[idx % colorPalette.length];
    }});
    const decorateDirectiveLabel = (dataset) => {{
      const d = dataset.label;
      dataset.label = d + " - " + (labels[d] || d);
      return dataset;
    }};

    // -- Compute aggregate "Overall" line across all directives per depth ----
    function computeOverallLine(rateKey, countKey) {{
      // Collect all depth targets and aggregate pass counts / totals.
      const byDepth = {{}};
      directives.forEach((d) => {{
        (SCORES.sweep_grid[d] || []).forEach((cell) => {{
          const dt = cell.depth_target_tokens;
          if (!byDepth[dt]) byDepth[dt] = {{ depthMean: 0, depthCount: 0, passes: 0, total: 0 }};
          const n = cell[countKey] || cell.count || 0;
          const rate = cell[rateKey];
          if (typeof rate === "number" && n > 0) {{
            byDepth[dt].passes += Math.round(rate * n);
            byDepth[dt].total += n;
          }}
          byDepth[dt].depthMean += cell.depth_tokens_measured_mean;
          byDepth[dt].depthCount += 1;
        }});
      }});
      return Object.keys(byDepth)
        .map(Number)
        .sort((a, b) => a - b)
        .filter((dt) => byDepth[dt].total > 0)
        .map((dt) => ({{
          x: byDepth[dt].depthMean / byDepth[dt].depthCount,
          y: byDepth[dt].passes / byDepth[dt].total,
        }}));
    }}

    const overallStyle = {{
      borderColor: "#000",
      backgroundColor: "#000",
      borderWidth: 3,
      borderDash: [8, 4],
      pointRadius: 4,
      tension: 0.2,
      parsing: false,
    }};

    // -- Sweep (all) chart ---------------------------------------------------
    const sweepDatasets = directives.map((d) => {{
      return decorateDirectiveLabel({{
        label: d,
        data: (SCORES.sweep_grid[d] || []).map((x) => ({{x: x.depth_tokens_measured_mean, y: x.pass_rate}})),
        parsing: false,
        borderColor: colors[d],
        backgroundColor: colors[d],
        tension: 0.2
      }});
    }});
    sweepDatasets.unshift(Object.assign({{
      label: "Overall (all directives)",
      data: computeOverallLine("pass_rate", "count"),
    }}, overallStyle));
    new Chart(document.getElementById("sweepChart"), {{
      type: "line",
      data: {{ datasets: sweepDatasets }},
      options: {{
        scales: {{
          x: {{ type: "linear", title: {{ display: true, text: "depth_tokens_measured" }} }},
          y: {{ min: 0, max: 1, title: {{ display: true, text: "pass rate (all)" }} }}
        }}
      }}
    }});

    // -- Sweep (non-empty) chart ---------------------------------------------
    const sweepNonEmptyDatasets = directives.map((d) => {{
      return decorateDirectiveLabel({{
        label: d,
        data: (SCORES.sweep_grid[d] || [])
          .filter((x) => typeof x.pass_rate_non_empty === "number")
          .map((x) => ({{x: x.depth_tokens_measured_mean, y: x.pass_rate_non_empty}})),
        parsing: false,
        borderColor: colors[d],
        backgroundColor: colors[d],
        tension: 0.2
      }});
    }});
    sweepNonEmptyDatasets.unshift(Object.assign({{
      label: "Overall (all directives)",
      data: computeOverallLine("pass_rate_non_empty", "non_empty_count"),
    }}, overallStyle));
    new Chart(document.getElementById("sweepNonEmptyChart"), {{
      type: "line",
      data: {{ datasets: sweepNonEmptyDatasets }},
      options: {{
        scales: {{
          x: {{ type: "linear", title: {{ display: true, text: "depth_tokens_measured" }} }},
          y: {{ min: 0, max: 1, title: {{ display: true, text: "pass rate (non-empty)" }} }}
        }}
      }}
    }});

    const emptyRateDatasets = directives.map((d) => {{
      return decorateDirectiveLabel({{
        label: d,
        data: (SCORES.sweep_grid[d] || []).map((x) => ({{
          x: x.depth_tokens_measured_mean,
          y: x.count > 0 ? (x.empty_count / x.count) : 0
        }})),
        parsing: false,
        borderColor: colors[d],
        backgroundColor: colors[d],
        tension: 0.2
      }});
    }});
    new Chart(document.getElementById("emptyRateChart"), {{
      type: "line",
      data: {{ datasets: emptyRateDatasets }},
      options: {{
        scales: {{
          x: {{ type: "linear", title: {{ display: true, text: "depth_tokens_measured" }} }},
          y: {{ min: 0, max: 1, title: {{ display: true, text: "empty response rate" }} }}
        }}
      }}
    }});

  </script>
</body>
</html>
"""

    out_path = config.results_dir / "report.html"
    write_text(out_path, html)
    logger.info("Report generated: %s", out_path)
    return out_path
