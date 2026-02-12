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


def _directive_name(d: str) -> str:
    return {
        "A": "Secrets",
        "B": "Least privilege",
        "C": "Approval gating",
        "D": "Log integrity",
        "E": "No fabrication",
    }.get(d, d)


def _build_baseline_rows(scores: Dict) -> str:
    rows = []
    for d in ["A", "B", "C", "D", "E"]:
        b0 = scores["baseline_system"].get(d, 0.0)
        bnull = scores["baseline_no_system"].get(d, 0.0)
        uplift = scores["policy_uplift"].get(d, 0.0)
        rows.append(
            f"<tr><td>{d} - {_directive_name(d)}</td>"
            f"<td>{b0:.2%}</td><td>{bnull:.2%}</td><td>{uplift:+.2%}</td></tr>"
        )
    return "\n".join(rows)


def _build_half_life_rows(scores: Dict) -> str:
    rows = []
    for d in ["A", "B", "C", "D", "E"]:
        item = scores["half_life"].get(d, {})
        empirical = item.get("x_half_empirical")
        fitted = item.get("x_half_fitted")
        status = item.get("fit_status", "n/a")
        empirical_txt = "No decay detected" if empirical is None else f"{empirical:,.0f}"
        fitted_txt = "n/a" if fitted is None else f"{fitted:,.0f}"
        rows.append(
            f"<tr><td>{d} - {_directive_name(d)}</td>"
            f"<td>{item.get('baseline_b', 0.0):.2%}</td>"
            f"<td>{item.get('target_half_b', 0.0):.2%}</td>"
            f"<td>{empirical_txt}</td><td>{fitted_txt}</td><td>{status}</td></tr>"
        )
    return "\n".join(rows)


def _build_sweep_grid_rows(scores: Dict) -> str:
    rows = []
    for d in ["A", "B", "C", "D", "E"]:
        for cell in scores["sweep_grid"].get(d, []):
            rows.append(
                f"<tr><td>{d}</td><td>{_directive_name(d)}</td>"
                f"<td>{cell['depth_target_tokens']:,}</td>"
                f"<td>{cell['depth_tokens_measured_mean']:.0f}</td>"
                f"<td>{cell['pass_rate']:.2%}</td><td>{cell['count']}</td></tr>"
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

    warning_block = ""
    if show_warning:
        warning_block = (
            "<div class='warn'><strong>Quality warning:</strong><ul>"
            + "".join([f"<li>{m}</li>" for m in warning_msgs])
            + "</ul></div>"
        )

    data_json = json.dumps(scores)
    baseline_row_count = sum(1 for d in ["A", "B", "C", "D", "E"] if d in scores.get("baseline_system", {}))
    sweep_row_count = sum(len(scores.get("sweep_grid", {}).get(d, [])) for d in ["A", "B", "C", "D", "E"])
    refresh_dataset_count = sum(
        len(scores.get("near_probe_refresh", {}).get(gap, {}).get(d, []))
        for gap in scores.get("near_probe_refresh", {})
        for d in ["A", "B", "C", "D", "E"]
    )
    logger.debug(
        "Report table/chart counts baseline_rows=%d sweep_rows=%d refresh_series_points=%d",
        baseline_row_count,
        sweep_row_count,
        refresh_dataset_count,
    )
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
    Judge: {scores.get("judge_model")}
  </div>
  {warning_block}

  <h2>Baselines</h2>
  <table>
    <thead>
      <tr>
        <th>Directive</th>
        <th>Token-zero baseline (B0)</th>
        <th>No-system baseline (B_null)</th>
        <th>Policy uplift (B0 - B_null)</th>
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
      <h2>Near-probe policy refresh (PoC)</h2>
      <div>Single policy refresh inserted before probe. This is not periodic reinjection.</div>
      <canvas id="refreshChart"></canvas>
    </div>
  </div>

  <h2>Sweep Grid</h2>
  <table>
    <thead>
      <tr>
        <th>Directive ID</th><th>Directive</th><th>Depth target</th><th>Mean depth_tokens_measured</th><th>Pass rate</th><th>N</th>
      </tr>
    </thead>
    <tbody>
      {_build_sweep_grid_rows(scores)}
    </tbody>
  </table>

  <h2>Half-life Readout</h2>
  <table>
    <thead>
      <tr>
        <th>Directive</th><th>Baseline B</th><th>Target 0.5*B</th><th>Empirical x_half</th><th>Fitted x_half</th><th>Fit status</th>
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
    const directives = ["A","B","C","D","E"];
    const colors = {{
      "A": "#3366cc",
      "B": "#dc3912",
      "C": "#ff9900",
      "D": "#109618",
      "E": "#990099"
    }};

    const sweepDatasets = directives.map((d) => {{
      const pts = (SCORES.sweep_grid[d] || []).map((x) => ({{x: x.depth_tokens_measured_mean, y: x.pass_rate}}));
      return {{
        label: d,
        data: pts,
        parsing: false,
        borderColor: colors[d],
        backgroundColor: colors[d],
        tension: 0.2
      }};
    }});
    const labels = {{
      "A": "Secrets",
      "B": "Least privilege",
      "C": "Approval gating",
      "D": "Log integrity",
      "E": "No fabrication"
    }};
    sweepDatasets.forEach((ds) => {{
      const d = ds.label.charAt(0);
      ds.label = d + " - " + labels[d];
    }});
    new Chart(document.getElementById("sweepChart"), {{
      type: "line",
      data: {{ datasets: sweepDatasets }},
      options: {{
        scales: {{
          x: {{ type: "linear", title: {{ display: true, text: "depth_tokens_measured" }} }},
          y: {{ min: 0, max: 1, title: {{ display: true, text: "pass rate" }} }}
        }}
      }}
    }});

    const refreshDatasets = [];
    const refresh = SCORES.near_probe_refresh || {{}};
    Object.keys(refresh).forEach((gap) => {{
      directives.forEach((d) => {{
        const pts = (refresh[gap][d] || []).map((x) => ({{x: x.depth_tokens_measured_mean, y: x.pass_rate}}));
        if (pts.length > 0) {{
          refreshDatasets.push({{
            label: d + " gap=" + gap,
            data: pts,
            parsing: false,
            borderColor: colors[d],
            backgroundColor: colors[d],
            borderDash: [4, 4],
            tension: 0.2
          }});
        }}
      }});
    }});
    new Chart(document.getElementById("refreshChart"), {{
      type: "line",
      data: {{ datasets: refreshDatasets }},
      options: {{
        scales: {{
          x: {{ type: "linear", title: {{ display: true, text: "depth_tokens_measured" }} }},
          y: {{ min: 0, max: 1, title: {{ display: true, text: "pass rate" }} }}
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
