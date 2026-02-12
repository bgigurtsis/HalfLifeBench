from __future__ import annotations

import logging
import random
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from scipy.stats import chi2

from .config import AppConfig
from .judge import judge_once
from .providers.base import ModelProvider
from .utils import read_json, read_jsonl, write_json

logger = logging.getLogger(__name__)

LEGACY_DIRECTIVE_ID_MAP = {
    # Pre-D1..D10 legacy IDs used in historical datasets.
    "A": "D1",
    "B": "D3",
    "C": "D4",
    "D": "D5",
    "E": "D8",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _normalize_directive_id(directive_id: str) -> str:
    raw = str(directive_id or "").strip()
    if not raw:
        return "UNKNOWN"
    upper = raw.upper()
    return LEGACY_DIRECTIVE_ID_MAP.get(upper, upper)


def _normalize_verdict(verdict: str) -> str:
    return "PASS" if str(verdict).strip().upper() == "PASS" else "FAIL"


def _cohens_kappa(verdict_pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    total = len(verdict_pairs)
    if total == 0:
        return None
    a_pass = 0
    a_fail = 0
    b_pass = 0
    b_fail = 0
    agree = 0
    for a_verdict, b_verdict in verdict_pairs:
        a_label = _normalize_verdict(a_verdict)
        b_label = _normalize_verdict(b_verdict)
        if a_label == "PASS":
            a_pass += 1
        else:
            a_fail += 1
        if b_label == "PASS":
            b_pass += 1
        else:
            b_fail += 1
        if a_label == b_label:
            agree += 1
    po = agree / total
    pe = ((a_pass / total) * (b_pass / total)) + ((a_fail / total) * (b_fail / total))
    denominator = 1.0 - pe
    if abs(denominator) < 1e-12:
        return 1.0 if abs(po - 1.0) < 1e-12 else 0.0
    return (po - pe) / denominator


def _mcnemar_from_golden(golden_rows: Sequence[Dict]) -> Dict:
    # b = model_a correct, model_b incorrect
    # c = model_a incorrect, model_b correct
    b = 0
    c = 0
    for row in golden_rows:
        human = _normalize_verdict(row.get("human_label", "FAIL"))
        a_correct = _normalize_verdict(row["model_a"]["verdict"]) == human
        b_correct = _normalize_verdict(row["model_b"]["verdict"]) == human
        if a_correct and not b_correct:
            b += 1
        elif not a_correct and b_correct:
            c += 1
    discordant = b + c
    if discordant == 0:
        return {
            "b_model_a_correct_only": b,
            "c_model_b_correct_only": c,
            "statistic": None,
            "p_value": None,
        }
    statistic = ((abs(b - c) - 1.0) ** 2) / discordant
    return {
        "b_model_a_correct_only": b,
        "c_model_b_correct_only": c,
        "statistic": statistic,
        "p_value": float(chi2.sf(statistic, df=1)),
    }


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.fmean(values))


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _build_probe_map(config: AppConfig) -> Dict[str, Dict]:
    probes = read_json(config.data_dir / "probes.json")
    if not isinstance(probes, list):
        raise ValueError("data/probes.json must contain a list")
    probe_map: Dict[str, Dict] = {}
    for probe in probes:
        probe_id = str(probe.get("probe_id", "")).strip()
        if probe_id:
            probe_map[probe_id] = probe
    return probe_map


def _extract_user_probe(record: Dict, probe_map: Dict[str, Dict]) -> Tuple[str, str]:
    probe_id = str(record.get("probe_id", "")).strip()
    if probe_id and probe_id in probe_map:
        probe = probe_map[probe_id]
        user_message = str(probe.get("user_message", "")).strip()
        if user_message:
            return user_message, "probe_map"

    messages_sent = record.get("messages_sent", [])
    if isinstance(messages_sent, list):
        for message in reversed(messages_sent):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip().lower()
            if role == "user":
                content = str(message.get("content", "")).strip()
                if content:
                    return content, "messages_sent"
    return "", "missing"


def _load_sweep_records(config: AppConfig) -> Tuple[List[Dict], Path]:
    candidates = [
        config.results_dir / "raw" / "sweep.jsonl",
        config.results_dir / "raw_sweep.jsonl",
    ]
    for path in candidates:
        if path.exists():
            return read_jsonl(path), path
    raise FileNotFoundError(
        f"Sweep records not found. Expected one of: {candidates[0]} or {candidates[1]}"
    )


def _collect_reliability(rows: Sequence[Dict], model_key: str, threshold: float) -> Dict:
    total = 0
    parse_failures = 0
    low_confidence_count = 0
    confidences: List[float] = []
    for row in rows:
        model_data = row[model_key]
        total += 1
        parse_success = bool(model_data.get("judge_parse_success", True))
        if not parse_success:
            parse_failures += 1
        confidence = float(model_data.get("confidence", 0.0) or 0.0)
        confidences.append(confidence)
        if confidence < threshold:
            low_confidence_count += 1
    return {
        "total_calls": total,
        "parse_failures": parse_failures,
        "parse_failure_rate": _safe_rate(parse_failures, total),
        "low_confidence_count": low_confidence_count,
        "low_confidence_rate": _safe_rate(low_confidence_count, total),
        "mean_confidence": _mean(confidences),
        "median_confidence": _median(confidences),
    }


def compare_judges(
    *,
    config: AppConfig,
    provider: ModelProvider,
    model_a: str = "claude-sonnet-4-5",
    # Intentionally keep Opus as comparator default for side-by-side analysis.
    model_b: str = "claude-opus-4-6",
    sweep_sample_size: int = 0,
) -> Dict:
    logger.info(
        "Starting judge comparison model_a=%s model_b=%s sweep_sample_size=%d",
        model_a,
        model_b,
        sweep_sample_size,
    )
    max_workers = max(1, config.max_workers)
    golden = read_json(config.data_dir / "golden_set.json")
    if not isinstance(golden, list):
        raise ValueError("data/golden_set.json must contain a list")
    logger.info("Loaded golden set rows=%d", len(golden))

    def _judge_golden_item(index: int, item: Dict) -> Tuple[int, Dict]:
        directive_id = _normalize_directive_id(str(item.get("directive_id", "")))
        result_a, _ = judge_once(
            provider=provider,
            config=config,
            directive_id=directive_id,
            user_probe=str(item.get("user_probe", "")),
            assistant_response=str(item.get("assistant_response", "")),
            temperature=0.0,
            model=model_a,
        )
        result_b, _ = judge_once(
            provider=provider,
            config=config,
            directive_id=directive_id,
            user_probe=str(item.get("user_probe", "")),
            assistant_response=str(item.get("assistant_response", "")),
            temperature=0.0,
            model=model_b,
        )
        row = {
            "example_id": str(item.get("example_id", f"golden_{index}")),
            "directive_id": directive_id,
            "human_label": _normalize_verdict(str(item.get("expected_verdict", "FAIL"))),
            "model_a": result_a,
            "model_b": result_b,
        }
        return index, row

    golden_rows: List[Optional[Dict]] = [None] * len(golden)
    if max_workers > 1 and golden:
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_judge_golden_item, idx, item): idx
                for idx, item in enumerate(golden)
            }
            for future in as_completed(futures):
                idx, row = future.result()
                golden_rows[idx] = row
                completed += 1
                logger.info(
                    "Golden comparison (%d/%d) example_id=%s model_a=%s model_b=%s expected=%s",
                    completed,
                    len(golden),
                    row["example_id"],
                    row["model_a"]["verdict"],
                    row["model_b"]["verdict"],
                    row["human_label"],
                )
    else:
        for idx, item in enumerate(golden):
            _, row = _judge_golden_item(idx, item)
            golden_rows[idx] = row
            logger.info(
                "Golden comparison (%d/%d) example_id=%s model_a=%s model_b=%s expected=%s",
                idx + 1,
                len(golden),
                row["example_id"],
                row["model_a"]["verdict"],
                row["model_b"]["verdict"],
                row["human_label"],
            )

    finalized_golden_rows: List[Dict] = [row for row in golden_rows if row is not None]
    golden_total = len(finalized_golden_rows)
    model_a_correct = 0
    model_b_correct = 0
    model_agree = 0
    golden_pairs: List[Tuple[str, str]] = []
    golden_per_directive: Dict[str, Dict[str, int]] = {}
    golden_disagreements: List[Dict] = []
    for row in finalized_golden_rows:
        directive_id = row["directive_id"]
        directive_bucket = golden_per_directive.setdefault(
            directive_id,
            {"total": 0, "model_a_correct": 0, "model_b_correct": 0, "model_agree": 0},
        )
        directive_bucket["total"] += 1
        expected = row["human_label"]
        a_verdict = _normalize_verdict(row["model_a"]["verdict"])
        b_verdict = _normalize_verdict(row["model_b"]["verdict"])
        golden_pairs.append((a_verdict, b_verdict))

        a_is_correct = a_verdict == expected
        b_is_correct = b_verdict == expected
        if a_is_correct:
            model_a_correct += 1
            directive_bucket["model_a_correct"] += 1
        if b_is_correct:
            model_b_correct += 1
            directive_bucket["model_b_correct"] += 1
        if a_verdict == b_verdict:
            model_agree += 1
            directive_bucket["model_agree"] += 1
        else:
            golden_disagreements.append(
                {
                    "example_id": row["example_id"],
                    "directive_id": directive_id,
                    "human_label": expected,
                    "model_a_verdict": a_verdict,
                    "model_b_verdict": b_verdict,
                    "model_a_confidence": row["model_a"]["confidence"],
                    "model_b_confidence": row["model_b"]["confidence"],
                    "model_a_rationale": row["model_a"]["rationale"],
                    "model_b_rationale": row["model_b"]["rationale"],
                }
            )

    golden_mcnemar = _mcnemar_from_golden(finalized_golden_rows)
    golden_payload = {
        "total_rows": golden_total,
        "model_a_agreement": _safe_rate(model_a_correct, golden_total),
        "model_b_agreement": _safe_rate(model_b_correct, golden_total),
        "model_a_per_directive": {
            d: _safe_rate(v["model_a_correct"], v["total"]) for d, v in sorted(golden_per_directive.items())
        },
        "model_b_per_directive": {
            d: _safe_rate(v["model_b_correct"], v["total"]) for d, v in sorted(golden_per_directive.items())
        },
        "inter_model_agreement": _safe_rate(model_agree, golden_total),
        "inter_model_per_directive_agreement": {
            d: _safe_rate(v["model_agree"], v["total"]) for d, v in sorted(golden_per_directive.items())
        },
        "cohens_kappa": _cohens_kappa(golden_pairs),
        "mcnemar_statistic": golden_mcnemar["statistic"],
        "mcnemar_p_value": golden_mcnemar["p_value"],
        "mcnemar_counts": {
            "model_a_correct_only": golden_mcnemar["b_model_a_correct_only"],
            "model_b_correct_only": golden_mcnemar["c_model_b_correct_only"],
        },
        "disagreements": golden_disagreements,
        "rows": finalized_golden_rows,
    }

    sweep_payload: Optional[Dict] = None
    sweep_rows: List[Dict] = []
    if sweep_sample_size > 0:
        sweep_records, sweep_path = _load_sweep_records(config)
        logger.info("Loaded sweep records path=%s count=%d", sweep_path, len(sweep_records))
        if not sweep_records:
            raise ValueError(f"Sweep file is empty: {sweep_path}")
        sample_size = min(sweep_sample_size, len(sweep_records))
        rng = random.Random(config.seed)
        sampled_indices = sorted(rng.sample(range(len(sweep_records)), sample_size))
        sampled_records = [sweep_records[idx] for idx in sampled_indices]
        probe_map = _build_probe_map(config)

        def _judge_sweep_item(index: int, record: Dict) -> Tuple[int, Dict]:
            directive_id = _normalize_directive_id(str(record.get("directive_id", "")))
            assistant_response = str(record.get("response", "") or "")
            run_id = str(record.get("run_id", f"sweep_{index}"))
            probe_id = str(record.get("probe_id", ""))
            user_probe, user_probe_source = _extract_user_probe(record, probe_map)
            result_a, _ = judge_once(
                provider=provider,
                config=config,
                directive_id=directive_id,
                user_probe=user_probe,
                assistant_response=assistant_response,
                temperature=0.0,
                model=model_a,
            )
            result_b, _ = judge_once(
                provider=provider,
                config=config,
                directive_id=directive_id,
                user_probe=user_probe,
                assistant_response=assistant_response,
                temperature=0.0,
                model=model_b,
            )
            row = {
                "run_id": run_id,
                "probe_id": probe_id,
                "directive_id": directive_id,
                "user_probe_source": user_probe_source,
                "model_a": result_a,
                "model_b": result_b,
            }
            return index, row

        sampled_rows: List[Optional[Dict]] = [None] * sample_size
        if max_workers > 1 and sampled_records:
            completed = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_judge_sweep_item, idx, record): idx
                    for idx, record in enumerate(sampled_records)
                }
                for future in as_completed(futures):
                    idx, row = future.result()
                    sampled_rows[idx] = row
                    completed += 1
                    logger.info(
                        "Sweep comparison (%d/%d) run_id=%s model_a=%s model_b=%s",
                        completed,
                        sample_size,
                        row["run_id"],
                        row["model_a"]["verdict"],
                        row["model_b"]["verdict"],
                    )
        else:
            for idx, record in enumerate(sampled_records):
                _, row = _judge_sweep_item(idx, record)
                sampled_rows[idx] = row
                logger.info(
                    "Sweep comparison (%d/%d) run_id=%s model_a=%s model_b=%s",
                    idx + 1,
                    sample_size,
                    row["run_id"],
                    row["model_a"]["verdict"],
                    row["model_b"]["verdict"],
                )

        sweep_rows = [row for row in sampled_rows if row is not None]
        sweep_pairs: List[Tuple[str, str]] = []
        sweep_agree = 0
        sweep_per_directive: Dict[str, Dict[str, int]] = {}
        sweep_disagreements: List[Dict] = []
        for row in sweep_rows:
            directive_id = row["directive_id"]
            bucket = sweep_per_directive.setdefault(directive_id, {"agree": 0, "total": 0})
            bucket["total"] += 1
            a_verdict = _normalize_verdict(row["model_a"]["verdict"])
            b_verdict = _normalize_verdict(row["model_b"]["verdict"])
            sweep_pairs.append((a_verdict, b_verdict))
            if a_verdict == b_verdict:
                sweep_agree += 1
                bucket["agree"] += 1
            else:
                sweep_disagreements.append(
                    {
                        "run_id": row["run_id"],
                        "probe_id": row["probe_id"],
                        "directive_id": directive_id,
                        "model_a_verdict": a_verdict,
                        "model_b_verdict": b_verdict,
                        "model_a_confidence": row["model_a"]["confidence"],
                        "model_b_confidence": row["model_b"]["confidence"],
                        "model_a_rationale": row["model_a"]["rationale"],
                        "model_b_rationale": row["model_b"]["rationale"],
                    }
                )
        sweep_payload = {
            "source_file": str(sweep_path),
            "sample_size_requested": sweep_sample_size,
            "sample_size_used": len(sweep_rows),
            "inter_model_agreement": _safe_rate(sweep_agree, len(sweep_rows)),
            "cohens_kappa": _cohens_kappa(sweep_pairs),
            "per_directive_agreement": {
                d: _safe_rate(v["agree"], v["total"]) for d, v in sorted(sweep_per_directive.items())
            },
            "disagreements": sweep_disagreements,
            "rows": sweep_rows,
        }

    all_rows = finalized_golden_rows + sweep_rows
    reliability_payload = {
        "model_a": _collect_reliability(all_rows, "model_a", config.low_confidence_threshold),
        "model_b": _collect_reliability(all_rows, "model_b", config.low_confidence_threshold),
    }

    threshold_ok_a = (
        golden_payload["model_a_agreement"] is not None
        and golden_payload["model_a_agreement"] >= config.validate_judge_threshold
    )
    threshold_ok_b = (
        golden_payload["model_b_agreement"] is not None
        and golden_payload["model_b_agreement"] >= config.validate_judge_threshold
    )
    kappa_ok = (
        golden_payload["cohens_kappa"] is not None
        and golden_payload["cohens_kappa"] >= 0.8
    )
    mcnemar_p_value = golden_payload["mcnemar_p_value"]
    mcnemar_ok = (mcnemar_p_value is None) or (mcnemar_p_value > 0.05)
    recommendation = {
        "criteria": {
            "model_a_agreement_at_or_above_threshold": bool(threshold_ok_a),
            "model_b_agreement_at_or_above_threshold": bool(threshold_ok_b),
            "cohens_kappa_at_or_above_0_80": bool(kappa_ok),
            "mcnemar_p_value_above_0_05_or_no_discordant_pairs": bool(mcnemar_ok),
        },
        "is_statistically_defensible_substitute": bool(
            threshold_ok_a and threshold_ok_b and kappa_ok and mcnemar_ok
        ),
    }

    payload = {
        "generated_at": _now_iso(),
        "model_a": model_a,
        "model_b": model_b,
        "config_thresholds": {
            "validate_judge_threshold": config.validate_judge_threshold,
            "low_confidence_threshold": config.low_confidence_threshold,
        },
        "golden_set": golden_payload,
        "sweep_sample": sweep_payload,
        "reliability": reliability_payload,
        "recommendation": recommendation,
    }
    out_path = config.results_dir / "judge_comparison.json"
    write_json(out_path, payload)
    logger.info(
        "Judge comparison complete output=%s golden_kappa=%s model_a_agreement=%s model_b_agreement=%s",
        out_path,
        golden_payload["cohens_kappa"],
        golden_payload["model_a_agreement"],
        golden_payload["model_b_agreement"],
    )
    payload["output_path"] = str(out_path)
    return payload
