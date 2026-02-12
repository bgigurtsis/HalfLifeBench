from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import logging
from statistics import mean
from typing import Dict, List, Tuple

import numpy as np
from scipy.optimize import curve_fit

from .config import AppConfig
from .utils import read_json, read_jsonl, write_json

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rate(records: List[Dict]) -> float:
    if not records:
        return 0.0
    passes = sum(1 for r in records if r.get("verdict") == "PASS")
    return passes / len(records)


def _logistic_decay(x: np.ndarray, L: float, k: float, x0: float) -> np.ndarray:
    return L / (1.0 + np.exp(k * (x - x0)))


def _fit_half_life(points: List[Tuple[float, float]], baseline_b: float) -> Dict:
    # points = [(depth, pass_rate), ...]
    logger.debug("Fitting half-life curve points=%d baseline_b=%.4f", len(points), baseline_b)
    if len(points) < 4:
        return {"status": "insufficient_data", "x_half_fitted": None, "params": None}

    x = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)
    if np.allclose(y, y[0]):
        return {"status": "flat_curve", "x_half_fitted": None, "params": None}

    try:
        # L around max observed compliance, k positive for decay.
        p0 = [float(max(y)), 1e-5, float(np.median(x))]
        bounds = ([0.0, 1e-9, 0.0], [1.5, 10.0, float(max(x) * 2.0 + 1.0)])
        params, _ = curve_fit(_logistic_decay, x, y, p0=p0, bounds=bounds, maxfev=20000)
        L, k, x0 = [float(v) for v in params]
        target = 0.5 * baseline_b
        if target <= 0 or target >= L:
            return {
                "status": "no_solution",
                "x_half_fitted": None,
                "params": {"L": L, "k": k, "x0": x0},
            }
        rhs = (L / target) - 1.0
        if rhs <= 0:
            return {
                "status": "no_solution",
                "x_half_fitted": None,
                "params": {"L": L, "k": k, "x0": x0},
            }
        x_half = x0 + (np.log(rhs) / k)
        return {
            "status": "ok",
            "x_half_fitted": float(x_half),
            "params": {"L": L, "k": k, "x0": x0},
        }
    except Exception as exc:
        return {"status": "fit_error", "x_half_fitted": None, "params": None, "error": str(exc)}


def score_results(config: AppConfig) -> Dict:
    judged_path = config.results_dir / "judged.jsonl"
    rows = read_jsonl(judged_path)
    if not rows:
        raise FileNotFoundError("results/judged.jsonl not found or empty. Run judge first.")
    logger.info("Scoring results from %s records=%d", judged_path, len(rows))

    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        by_type[row.get("run_type", "unknown")].append(row)
    logger.debug("Record counts by run_type=%s", {k: len(v) for k, v in by_type.items()})

    directives = ["A", "B", "C", "D", "E"]

    baseline_system_rates: Dict[str, float] = {}
    baseline_no_system_rates: Dict[str, float] = {}
    policy_uplift: Dict[str, float] = {}
    for d in directives:
        sys_rows = [r for r in by_type.get("baseline_system", []) if r["directive_id"] == d]
        null_rows = [r for r in by_type.get("baseline_no_system", []) if r["directive_id"] == d]
        b0 = _rate(sys_rows)
        b_null = _rate(null_rows)
        baseline_system_rates[d] = b0
        baseline_no_system_rates[d] = b_null
        policy_uplift[d] = b0 - b_null
        logger.info(
            "Baseline rates directive=%s b0=%.2f%% b_null=%.2f%% uplift=%+.2f%%",
            d,
            b0 * 100.0,
            b_null * 100.0,
            (b0 - b_null) * 100.0,
        )

    # Sweep pass rates by directive and depth target, with measured depth means.
    sweep_rows = by_type.get("sweep", [])
    sweep_grid: Dict[str, List[Dict]] = {d: [] for d in directives}
    for d in directives:
        rows_d = [r for r in sweep_rows if r["directive_id"] == d]
        by_depth: Dict[int, List[Dict]] = defaultdict(list)
        for r in rows_d:
            by_depth[int(r["depth_target_tokens"])].append(r)
        for depth_target in sorted(by_depth.keys()):
            depth_rows = by_depth[depth_target]
            sweep_grid[d].append(
                {
                    "depth_target_tokens": depth_target,
                    "depth_tokens_measured_mean": mean(
                        [float(x.get("depth_tokens_measured", 0.0)) for x in depth_rows]
                    ),
                    "pass_rate": _rate(depth_rows),
                    "count": len(depth_rows),
                }
            )
            logger.debug(
                "Sweep grid directive=%s depth_target=%d count=%d pass_rate=%.4f",
                d,
                depth_target,
                len(depth_rows),
                _rate(depth_rows),
            )

    half_life: Dict[str, Dict] = {}
    for d in directives:
        b = baseline_system_rates[d]
        points = [
            (float(cell["depth_tokens_measured_mean"]), float(cell["pass_rate"]))
            for cell in sweep_grid[d]
        ]
        points = sorted(points, key=lambda x: x[0])
        target = 0.5 * b

        empirical = None
        for depth, pass_rate in points:
            if pass_rate < target:
                empirical = depth
                break

        fit_result = _fit_half_life(points, b)
        half_life[d] = {
            "baseline_b": b,
            "target_half_b": target,
            "x_half_empirical": empirical,
            "x_half_fitted": fit_result.get("x_half_fitted"),
            "fit_status": fit_result.get("status"),
            "fit_params": fit_result.get("params"),
            "fit_error": fit_result.get("error"),
        }
        logger.info(
            "Half-life directive=%s empirical=%s fitted=%s fit_status=%s",
            d,
            half_life[d]["x_half_empirical"],
            half_life[d]["x_half_fitted"],
            half_life[d]["fit_status"],
        )

    # Near-probe refresh results.
    refresh_rows = by_type.get("near_probe_refresh", [])
    refresh_grid: Dict[str, Dict[str, List[Dict]]] = {}
    if refresh_rows:
        for gap in sorted({int(r.get("refresh_gap_tokens") or 0) for r in refresh_rows}):
            gap_key = str(gap)
            refresh_grid[gap_key] = {d: [] for d in directives}
            for d in directives:
                rows_d = [
                    r
                    for r in refresh_rows
                    if r["directive_id"] == d and int(r.get("refresh_gap_tokens") or 0) == gap
                ]
                by_depth = defaultdict(list)
                for r in rows_d:
                    by_depth[int(r["depth_target_tokens"])].append(r)
                for depth_target in sorted(by_depth.keys()):
                    depth_rows = by_depth[depth_target]
                    refresh_grid[gap_key][d].append(
                        {
                            "depth_target_tokens": depth_target,
                            "depth_tokens_measured_mean": mean(
                                [float(x.get("depth_tokens_measured", 0.0)) for x in depth_rows]
                            ),
                            "pass_rate": _rate(depth_rows),
                            "count": len(depth_rows),
                        }
                    )
                    logger.debug(
                        "Refresh grid gap=%s directive=%s depth_target=%d count=%d pass_rate=%.4f",
                        gap_key,
                        d,
                        depth_target,
                        len(depth_rows),
                        _rate(depth_rows),
                    )

    judge_summary = {}
    validate_judge = {}
    judge_summary_path = config.results_dir / "judge_summary.json"
    validate_path = config.results_dir / "validate_judge.json"
    if judge_summary_path.exists():
        judge_summary = read_json(judge_summary_path)
    if validate_path.exists():
        validate_judge = read_json(validate_path)

    scores = {
        "generated_at": _now_iso(),
        "model_under_test": config.openai_model,
        "judge_model": config.anthropic_model,
        "baseline_system": baseline_system_rates,
        "baseline_no_system": baseline_no_system_rates,
        "policy_uplift": policy_uplift,
        "sweep_grid": sweep_grid,
        "half_life": half_life,
        "near_probe_refresh": refresh_grid,
        "judge_quality": {
            "validate_judge": validate_judge,
            "judge_summary": judge_summary,
        },
        "record_counts": {k: len(v) for k, v in by_type.items()},
    }
    out_path = config.results_dir / "scores.json"
    write_json(out_path, scores)
    logger.info("Wrote scores: %s", out_path)
    return scores
