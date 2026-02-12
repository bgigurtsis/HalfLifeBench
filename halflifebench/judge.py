from __future__ import annotations

import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import AppConfig
from .providers.base import BatchRequest, CompletionResult, Message, ModelProvider
from .utils import read_json, read_jsonl, read_text, write_json, write_jsonl

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prompt_path(config: AppConfig, directive_id: str) -> Path:
    return config.judge_prompts_dir / f"{directive_id.lower()}.txt"


def build_judge_prompt(
    config: AppConfig, directive_id: str, user_probe: str, assistant_response: str
) -> str:
    header = read_text(config.judge_prompts_dir / "header.txt").strip()
    directive_prompt = read_text(_prompt_path(config, directive_id)).strip()
    prompt = (
        f"{header}\n\n{directive_prompt}\n\n"
        f"Return JSON now.\n"
        f"User probe: {user_probe}\n"
        f"Assistant response: {assistant_response}\n"
    )
    logger.debug(
        "Built judge prompt directive=%s chars=%d probe_chars=%d response_chars=%d",
        directive_id,
        len(prompt),
        len(user_probe),
        len(assistant_response),
    )
    return prompt


def extract_last_json_object(text: str) -> Optional[Dict]:
    candidates: List[str] = []
    stack = 0
    start_idx: Optional[int] = None
    for i, ch in enumerate(text):
        if ch == "{":
            if stack == 0:
                start_idx = i
            stack += 1
        elif ch == "}":
            if stack > 0:
                stack -= 1
                if stack == 0 and start_idx is not None:
                    candidates.append(text[start_idx : i + 1])
                    start_idx = None

    logger.debug("Extracting last JSON object: candidates_found=%d", len(candidates))
    for cand in reversed(candidates):
        try:
            parsed = json.loads(cand)
        except Exception:
            continue
        if isinstance(parsed, dict):
            required = {"directive_id", "verdict", "confidence", "rationale"}
            if required.issubset(parsed.keys()):
                logger.debug("Judge JSON parse succeeded with required keys")
                return parsed
    logger.debug("Judge JSON parse failed to find valid object")
    return None


def _judge_result_from_completion(
    *,
    directive_id: str,
    completion: CompletionResult,
) -> Tuple[Dict, str]:
    raw = completion.content
    parsed = extract_last_json_object(raw)
    parse_success = parsed is not None
    if parsed is None:
        logger.warning(
            "Judge output unparseable for directive=%s; defaulting verdict to FAIL",
            directive_id,
        )
        parsed = {
            "directive_id": directive_id,
            "verdict": "FAIL",
            "confidence": 0.0,
            "rationale": "Judge output unparseable; defaulting to FAIL.",
        }

    verdict = parsed.get("verdict", "FAIL")
    if verdict not in {"PASS", "FAIL"}:
        verdict = "FAIL"

    confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0

    result = {
        "directive_id": directive_id,
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, confidence)),
        "rationale": str(parsed.get("rationale", "")),
        "judge_model": completion.model,
        "judge_input_tokens": completion.input_tokens,
        "judge_output_tokens": completion.output_tokens,
        "judge_parse_success": parse_success,
    }
    logger.debug(
        "Judge call complete directive=%s verdict=%s confidence=%.3f input_tokens=%d output_tokens=%d",
        directive_id,
        result["verdict"],
        float(result["confidence"]),
        completion.input_tokens,
        completion.output_tokens,
    )
    return result, raw


def judge_once(
    *,
    provider: ModelProvider,
    config: AppConfig,
    directive_id: str,
    user_probe: str,
    assistant_response: str,
    temperature: float = 0.0,
    model: str | None = None,
) -> Tuple[Dict, str]:
    prompt = build_judge_prompt(config, directive_id, user_probe, assistant_response)
    messages: List[Message] = [{"role": "user", "content": prompt}]
    logger.debug(
        "Judge call starting directive=%s temperature=%.1f message_count=%d",
        directive_id,
        temperature,
        len(messages),
    )
    judge_model = model or config.anthropic_model
    completion = provider.complete(
        model=judge_model,
        messages=messages,
        temperature=temperature,
        seed=config.seed,
        max_output_tokens=600,
        max_empty_retries=config.max_empty_retries,
    )
    return _judge_result_from_completion(directive_id=directive_id, completion=completion)


def _load_all_run_records(config: AppConfig) -> List[Dict]:
    raw_dir = config.results_dir / "raw"
    records: List[Dict] = []
    for name in ("baseline_system.jsonl", "baseline_no_system.jsonl", "sweep.jsonl"):
        path = raw_dir / name
        loaded = read_jsonl(path)
        records.extend(loaded)
        logger.debug("Loaded run records from %s: count=%d", path, len(loaded))
    logger.info("Loaded all run records: total=%d", len(records))
    return records


def _load_probe_map(config: AppConfig) -> Dict[str, Dict]:
    probes = read_json(config.data_dir / "probes.json")
    probe_map = {p["probe_id"]: p for p in probes}
    logger.debug("Loaded probe map: count=%d", len(probe_map))
    return probe_map


def _directive_d1_autofail(record: Dict, probe_map: Dict[str, Dict]) -> bool:
    if record.get("directive_id") != "D1":
        return False
    probe = probe_map.get(record.get("probe_id", ""))
    if not probe:
        return False
    response = (record.get("response") or "").lower()
    canaries = [c.lower() for c in probe.get("canary_substrings", []) if c]
    matched = any(c in response for c in canaries)
    if matched:
        logger.debug(
            "Directive D1 auto-fail canary matched run_id=%s probe_id=%s",
            record.get("run_id"),
            record.get("probe_id"),
        )
    return matched


def run_judging(config: AppConfig, provider: ModelProvider) -> Dict:
    all_records = _load_all_run_records(config)
    probe_map = _load_probe_map(config)
    rng = random.Random(config.seed)
    max_workers = max(1, config.max_workers)
    total_records = len(all_records)
    logger.info("Starting judging for records=%d max_workers=%d", total_records, max_workers)

    # -- Identify auto-fail and audit indices (no API calls) ---------------
    auto_fail_idx: List[int] = []
    for i, rec in enumerate(all_records):
        if _directive_d1_autofail(rec, probe_map):
            auto_fail_idx.append(i)

    audit_count = int(0.2 * len(auto_fail_idx))
    if len(auto_fail_idx) > 0 and audit_count == 0:
        audit_count = 1
    audit_idx = set(rng.sample(auto_fail_idx, audit_count)) if audit_count > 0 else set()
    auto_fail_set = set(auto_fail_idx)
    logger.info(
        "Auto-fail candidates=%d audit_count=%d",
        len(auto_fail_idx),
        len(audit_idx),
    )

    # -- Prepare rows and identify which need LLM calls --------------------
    judged_rows: List[Optional[Dict]] = [None] * total_records
    llm_call_indices: List[int] = []  # indices into all_records that need LLM judge

    for idx, record in enumerate(all_records):
        row = dict(record)
        if "response_empty" not in row:
            response_text = row.get("response") or ""
            row["response_empty"] = not bool(response_text.strip())
        row["judged_at"] = _now_iso()

        if idx in auto_fail_set and idx not in audit_idx:
            row.update(
                {
                    "judge_method": "rule_based",
                    "verdict": "FAIL",
                    "confidence": 1.0,
                    "rationale": "Auto-fail: directive D1 canary substring reproduced in response.",
                    "low_confidence": False,
                    "judge_raw_output": None,
                }
            )
            logger.info(
                "Judged (rule-based auto-fail) run_id=%s verdict=FAIL",
                row.get("run_id"),
            )
            judged_rows[idx] = row
        else:
            llm_call_indices.append(idx)
            judged_rows[idx] = row  # placeholder -- will be updated with LLM results

    logger.info(
        "LLM judge calls needed=%d (rule-based=%d)",
        len(llm_call_indices),
        total_records - len(llm_call_indices),
    )

    # -- Run LLM judge calls in parallel -----------------------------------
    def _judge_record(idx: int) -> Tuple[int, Dict, str]:
        row = judged_rows[idx]
        user_probe = probe_map[row["probe_id"]]["user_message"]
        llm_result, raw_output = judge_once(
            provider=provider,
            config=config,
            directive_id=row["directive_id"],
            user_probe=user_probe,
            assistant_response=row.get("response", ""),
            temperature=0.0,
        )
        return idx, llm_result, raw_output

    audited_matches = 0
    audited_total = 0
    llm_rows_idx: List[int] = []

    if config.use_batch and llm_call_indices:
        logger.info(
            "Judging batch mode enabled: submitting primary judge batch requests=%d poll_interval=%d",
            len(llm_call_indices),
            config.batch_poll_interval,
        )
        primary_requests: List[BatchRequest] = []
        idx_to_custom: Dict[int, str] = {}
        for idx in llm_call_indices:
            row = judged_rows[idx]
            user_probe = probe_map[row["probe_id"]]["user_message"]
            prompt = build_judge_prompt(
                config=config,
                directive_id=row["directive_id"],
                user_probe=user_probe,
                assistant_response=row.get("response", ""),
            )
            custom_id = f"judge_primary-{idx}-{row['probe_id']}"
            idx_to_custom[idx] = custom_id
            primary_requests.append(
                {
                    "custom_id": custom_id,
                    "model": config.anthropic_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "seed": config.seed,
                    "max_output_tokens": 600,
                }
            )

        primary_results = provider.complete_batch(
            requests=primary_requests,
            poll_interval=config.batch_poll_interval,
        )
        for call_num, idx in enumerate(llm_call_indices, start=1):
            row = judged_rows[idx]
            completion = primary_results[idx_to_custom[idx]]
            llm_result, raw_output = _judge_result_from_completion(
                directive_id=row["directive_id"],
                completion=completion,
            )

            if idx in audit_idx:
                audited_total += 1
                if llm_result["verdict"] == "FAIL":
                    audited_matches += 1

            row.update(
                {
                    "judge_method": "rule_based_audit" if idx in audit_idx else "llm",
                    "verdict": llm_result["verdict"],
                    "confidence": llm_result["confidence"],
                    "rationale": llm_result["rationale"],
                    "judge_model": llm_result["judge_model"],
                    "judge_input_tokens": llm_result["judge_input_tokens"],
                    "judge_output_tokens": llm_result["judge_output_tokens"],
                    "low_confidence": llm_result["confidence"] < config.low_confidence_threshold,
                    "judge_raw_output": raw_output,
                }
            )
            llm_rows_idx.append(idx)
            logger.info(
                "Judged (%d/%d) run_id=%s verdict=%s confidence=%.2f method=%s",
                call_num,
                len(llm_call_indices),
                row.get("run_id"),
                row["verdict"],
                float(row["confidence"]),
                row["judge_method"],
            )
    elif max_workers > 1 and llm_call_indices:
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(_judge_record, idx): idx
                for idx in llm_call_indices
            }
            for future in as_completed(future_to_idx):
                idx, llm_result, raw_output = future.result()
                row = judged_rows[idx]

                if idx in audit_idx:
                    audited_total += 1
                    if llm_result["verdict"] == "FAIL":
                        audited_matches += 1

                row.update(
                    {
                        "judge_method": "rule_based_audit" if idx in audit_idx else "llm",
                        "verdict": llm_result["verdict"],
                        "confidence": llm_result["confidence"],
                        "rationale": llm_result["rationale"],
                        "judge_model": llm_result["judge_model"],
                        "judge_input_tokens": llm_result["judge_input_tokens"],
                        "judge_output_tokens": llm_result["judge_output_tokens"],
                        "low_confidence": llm_result["confidence"] < config.low_confidence_threshold,
                        "judge_raw_output": raw_output,
                    }
                )
                llm_rows_idx.append(idx)
                completed += 1
                logger.info(
                    "Judged (%d/%d) run_id=%s verdict=%s confidence=%.2f method=%s",
                    completed,
                    len(llm_call_indices),
                    row.get("run_id"),
                    row["verdict"],
                    float(row["confidence"]),
                    row["judge_method"],
                )
    else:
        for call_num, idx in enumerate(llm_call_indices, start=1):
            _, llm_result, raw_output = _judge_record(idx)
            row = judged_rows[idx]

            if idx in audit_idx:
                audited_total += 1
                if llm_result["verdict"] == "FAIL":
                    audited_matches += 1

            row.update(
                {
                    "judge_method": "rule_based_audit" if idx in audit_idx else "llm",
                    "verdict": llm_result["verdict"],
                    "confidence": llm_result["confidence"],
                    "rationale": llm_result["rationale"],
                    "judge_model": llm_result["judge_model"],
                    "judge_input_tokens": llm_result["judge_input_tokens"],
                    "judge_output_tokens": llm_result["judge_output_tokens"],
                    "low_confidence": llm_result["confidence"] < config.low_confidence_threshold,
                    "judge_raw_output": raw_output,
                }
            )
            llm_rows_idx.append(idx)
            logger.info(
                "Judged (%d/%d) run_id=%s verdict=%s confidence=%.2f method=%s",
                call_num,
                len(llm_call_indices),
                row.get("run_id"),
                row["verdict"],
                float(row["confidence"]),
                row["judge_method"],
            )

    # -- Cross-judge spot-check on 20% of LLM-scored items ----------------
    cross_count = int(0.2 * len(llm_rows_idx))
    if len(llm_rows_idx) > 0 and cross_count == 0:
        cross_count = 1
    cross_targets = set(rng.sample(llm_rows_idx, cross_count)) if cross_count > 0 else set()
    logger.info(
        "Cross-judge spot-check targets=%d of llm_scored=%d",
        len(cross_targets),
        len(llm_rows_idx),
    )

    def _cross_judge_record(row_idx: int) -> Tuple[int, Dict, str]:
        row = judged_rows[row_idx]
        user_probe = probe_map[row["probe_id"]]["user_message"]
        second_result, second_raw = judge_once(
            provider=provider,
            config=config,
            directive_id=row["directive_id"],
            user_probe=user_probe,
            assistant_response=row.get("response", ""),
            temperature=0.3,
        )
        return row_idx, second_result, second_raw

    cross_matches = 0
    cross_total = 0
    cross_rows: List[Dict] = []
    cross_targets_list = sorted(cross_targets)

    if config.use_batch and cross_targets_list:
        logger.info(
            "Judging batch mode enabled: submitting cross-judge batch requests=%d poll_interval=%d",
            len(cross_targets_list),
            config.batch_poll_interval,
        )
        cross_requests: List[BatchRequest] = []
        row_to_custom: Dict[int, str] = {}
        for row_idx in cross_targets_list:
            row = judged_rows[row_idx]
            user_probe = probe_map[row["probe_id"]]["user_message"]
            prompt = build_judge_prompt(
                config=config,
                directive_id=row["directive_id"],
                user_probe=user_probe,
                assistant_response=row.get("response", ""),
            )
            custom_id = f"judge_cross-{row_idx}-{row['probe_id']}"
            row_to_custom[row_idx] = custom_id
            cross_requests.append(
                {
                    "custom_id": custom_id,
                    "model": config.anthropic_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "seed": config.seed,
                    "max_output_tokens": 600,
                }
            )

        cross_results = provider.complete_batch(
            requests=cross_requests,
            poll_interval=config.batch_poll_interval,
        )
        for row_idx in cross_targets_list:
            row = judged_rows[row_idx]
            completion = cross_results[row_to_custom[row_idx]]
            second_result, second_raw = _judge_result_from_completion(
                directive_id=row["directive_id"],
                completion=completion,
            )
            match = second_result["verdict"] == row["verdict"]
            cross_total += 1
            if match:
                cross_matches += 1
            logger.info(
                "Cross-judge result run_id=%s primary=%s secondary=%s match=%s",
                row.get("run_id"),
                row.get("verdict"),
                second_result["verdict"],
                match,
            )
            row["cross_judge_verdict"] = second_result["verdict"]
            row["cross_judge_match"] = match
            cross_rows.append(
                {
                    "run_id": row["run_id"],
                    "primary_verdict": row["verdict"],
                    "secondary_verdict": second_result["verdict"],
                    "match": match,
                    "secondary_raw_output": second_raw,
                }
            )
    elif max_workers > 1 and cross_targets_list:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(_cross_judge_record, row_idx): row_idx
                for row_idx in cross_targets_list
            }
            for future in as_completed(future_to_idx):
                row_idx, second_result, second_raw = future.result()
                row = judged_rows[row_idx]
                match = second_result["verdict"] == row["verdict"]
                cross_total += 1
                if match:
                    cross_matches += 1
                logger.info(
                    "Cross-judge result run_id=%s primary=%s secondary=%s match=%s",
                    row.get("run_id"),
                    row.get("verdict"),
                    second_result["verdict"],
                    match,
                )
                row["cross_judge_verdict"] = second_result["verdict"]
                row["cross_judge_match"] = match
                cross_rows.append(
                    {
                        "run_id": row["run_id"],
                        "primary_verdict": row["verdict"],
                        "secondary_verdict": second_result["verdict"],
                        "match": match,
                        "secondary_raw_output": second_raw,
                    }
                )
    else:
        for row_idx in cross_targets_list:
            _, second_result, second_raw = _cross_judge_record(row_idx)
            row = judged_rows[row_idx]
            match = second_result["verdict"] == row["verdict"]
            cross_total += 1
            if match:
                cross_matches += 1
            logger.info(
                "Cross-judge result run_id=%s primary=%s secondary=%s match=%s",
                row.get("run_id"),
                row.get("verdict"),
                second_result["verdict"],
                match,
            )
            row["cross_judge_verdict"] = second_result["verdict"]
            row["cross_judge_match"] = match
            cross_rows.append(
                {
                    "run_id": row["run_id"],
                    "primary_verdict": row["verdict"],
                    "secondary_verdict": second_result["verdict"],
                    "match": match,
                    "secondary_raw_output": second_raw,
                }
            )

    judged_path = config.results_dir / "judged.jsonl"
    cross_results_path = config.results_dir / "cross_judge_results.json"
    write_jsonl(judged_path, judged_rows)
    write_json(cross_results_path, cross_rows)
    logger.info("Wrote judged rows: %s (count=%d)", judged_path, len(judged_rows))
    logger.info("Wrote cross-judge results: %s (count=%d)", cross_results_path, len(cross_rows))

    precheck_accuracy = (audited_matches / audited_total) if audited_total else None
    cross_agreement = (cross_matches / cross_total) if cross_total else None
    summary = {
        "generated_at": _now_iso(),
        "total_records": len(judged_rows),
        "auto_fail_candidates": len(auto_fail_idx),
        "auto_fail_audited": audited_total,
        "precheck_accuracy": precheck_accuracy,
        "disable_rule_based_shortcut_next_run": (
            precheck_accuracy is not None and precheck_accuracy < config.precheck_accuracy_threshold
        ),
        "cross_judge_total": cross_total,
        "cross_judge_agreement": cross_agreement,
    }
    summary_path = config.results_dir / "judge_summary.json"
    write_json(summary_path, summary)
    logger.info(
        "Judging summary: precheck_accuracy=%s cross_judge_agreement=%s summary=%s",
        precheck_accuracy,
        cross_agreement,
        summary_path,
    )
    return summary


def validate_judge_against_golden(config: AppConfig, provider: ModelProvider) -> Dict:
    golden = read_json(config.data_dir / "golden_set.json")
    if not isinstance(golden, list):
        raise ValueError("data/golden_set.json must contain a list")
    max_workers = max(1, config.max_workers)
    total = len(golden)
    logger.info("Starting judge validation against golden set: count=%d max_workers=%d", total, max_workers)

    def _validate_item(idx: int, item: Dict) -> Tuple[int, Dict, Dict]:
        directive_id = item["directive_id"]
        result, raw = judge_once(
            provider=provider,
            config=config,
            directive_id=directive_id,
            user_probe=item["user_probe"],
            assistant_response=item["assistant_response"],
            temperature=0.0,
        )
        expected = item["expected_verdict"]
        match = result["verdict"] == expected
        row = {
            "example_id": item["example_id"],
            "directive_id": directive_id,
            "expected_verdict": expected,
            "predicted_verdict": result["verdict"],
            "confidence": result["confidence"],
            "rationale": result["rationale"],
            "match": match,
            "judge_raw_output": raw,
        }
        return idx, row, result

    rows: List[Optional[Dict]] = [None] * total

    if config.use_batch and total > 0:
        logger.info(
            "Golden validation batch mode enabled: submitting requests=%d poll_interval=%d",
            total,
            config.batch_poll_interval,
        )
        batch_requests: List[BatchRequest] = []
        idx_to_custom: Dict[int, str] = {}
        for idx, item in enumerate(golden):
            prompt = build_judge_prompt(
                config=config,
                directive_id=item["directive_id"],
                user_probe=item["user_probe"],
                assistant_response=item["assistant_response"],
            )
            custom_id = f"golden_validate-{idx}-{item['example_id']}"
            idx_to_custom[idx] = custom_id
            batch_requests.append(
                {
                    "custom_id": custom_id,
                    "model": config.anthropic_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "seed": config.seed,
                    "max_output_tokens": 600,
                }
            )

        results = provider.complete_batch(
            requests=batch_requests,
            poll_interval=config.batch_poll_interval,
        )
        for idx, item in enumerate(golden):
            completion = results[idx_to_custom[idx]]
            result, raw = _judge_result_from_completion(
                directive_id=item["directive_id"],
                completion=completion,
            )
            expected = item["expected_verdict"]
            match = result["verdict"] == expected
            row = {
                "example_id": item["example_id"],
                "directive_id": item["directive_id"],
                "expected_verdict": expected,
                "predicted_verdict": result["verdict"],
                "confidence": result["confidence"],
                "rationale": result["rationale"],
                "match": match,
                "judge_raw_output": raw,
            }
            rows[idx] = row
            logger.info(
                "Golden validation (%d/%d) example_id=%s expected=%s predicted=%s match=%s confidence=%.2f",
                idx + 1,
                total,
                row["example_id"],
                row["expected_verdict"],
                row["predicted_verdict"],
                row["match"],
                float(result["confidence"]),
            )
    elif max_workers > 1:
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(_validate_item, idx, item): idx
                for idx, item in enumerate(golden)
            }
            for future in as_completed(future_to_idx):
                idx, row, result = future.result()
                rows[idx] = row
                completed += 1
                logger.info(
                    "Golden validation (%d/%d) example_id=%s expected=%s predicted=%s match=%s confidence=%.2f",
                    completed,
                    total,
                    row["example_id"],
                    row["expected_verdict"],
                    row["predicted_verdict"],
                    row["match"],
                    float(result["confidence"]),
                )
    else:
        for idx, item in enumerate(golden):
            _, row, result = _validate_item(idx, item)
            rows[idx] = row
            logger.info(
                "Golden validation (%d/%d) example_id=%s expected=%s predicted=%s match=%s confidence=%.2f",
                idx + 1,
                total,
                row["example_id"],
                row["expected_verdict"],
                row["predicted_verdict"],
                row["match"],
                float(result["confidence"]),
            )

    directive_ids = sorted({str(item.get("directive_id", "")).strip() for item in golden if item.get("directive_id")})
    per_directive = {d: {"match": 0, "total": 0} for d in directive_ids}
    total_match = 0
    for row in rows:
        assert row is not None
        directive_id = row["directive_id"]
        if directive_id not in per_directive:
            per_directive[directive_id] = {"match": 0, "total": 0}
        per_directive[directive_id]["total"] += 1
        if row["match"]:
            per_directive[directive_id]["match"] += 1
            total_match += 1

    total = len(rows)
    overall_agreement = (total_match / total) if total else 0.0
    per_directive_agreement = {
        d: ((v["match"] / v["total"]) if v["total"] else 0.0) for d, v in per_directive.items()
    }

    payload = {
        "generated_at": _now_iso(),
        "overall_agreement": overall_agreement,
        "per_directive_agreement": per_directive_agreement,
        "threshold": config.validate_judge_threshold,
        "passes_threshold": overall_agreement >= config.validate_judge_threshold,
        "rows": rows,
    }
    out_path = config.results_dir / "validate_judge.json"
    write_json(out_path, payload)
    logger.info(
        "Judge validation complete overall=%.2f%% threshold=%.0f%% pass=%s output=%s",
        overall_agreement * 100.0,
        config.validate_judge_threshold * 100.0,
        payload["passes_threshold"],
        out_path,
    )
    return payload
