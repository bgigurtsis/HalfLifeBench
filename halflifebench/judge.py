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
from .utils import append_jsonl_threadsafe, read_json, read_jsonl, read_text, write_json

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


def _judged_row_complete(row: Dict) -> bool:
    run_id = row.get("run_id")
    verdict = row.get("verdict")
    judge_method = row.get("judge_method")
    return isinstance(run_id, str) and bool(run_id) and verdict in {"PASS", "FAIL"} and bool(judge_method)


def run_judging(config: AppConfig, provider: ModelProvider) -> Dict:
    all_records = _load_all_run_records(config)
    probe_map = _load_probe_map(config)
    rng = random.Random(config.seed)
    max_workers = max(1, config.max_workers)
    total_records = len(all_records)
    logger.info(
        "Starting judging for records=%d max_workers=%d use_batch=%s",
        total_records,
        max_workers,
        config.use_batch,
    )

    judged_path = config.results_dir / "judged.jsonl"
    cross_results_path = config.results_dir / "cross_judge_results.json"
    cross_partial_path = config.results_dir / "cross_judge_results.jsonl"

    existing_judged = read_jsonl(judged_path)
    existing_judged_by_run_id: Dict[str, Dict] = {}
    for row in existing_judged:
        if _judged_row_complete(row):
            existing_judged_by_run_id[str(row["run_id"])] = row

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
    llm_call_indices: List[int] = []
    restored = 0
    newly_written_judged = 0

    for idx, record in enumerate(all_records):
        run_id = str(record.get("run_id") or "")
        restored_row = existing_judged_by_run_id.get(run_id)
        if restored_row is not None:
            row = dict(restored_row)
            if "response_empty" not in row:
                response_text = row.get("response") or ""
                row["response_empty"] = not bool(response_text.strip())
            judged_rows[idx] = row
            restored += 1
            continue

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
            judged_rows[idx] = row
            append_jsonl_threadsafe(judged_path, row)
            existing_judged_by_run_id[run_id] = dict(row)
            newly_written_judged += 1
            logger.info(
                "Judged (rule-based auto-fail) run_id=%s verdict=FAIL",
                row.get("run_id"),
            )
        else:
            llm_call_indices.append(idx)
            judged_rows[idx] = row

    logger.info(
        "Resuming judging: restored=%d remaining_llm_calls=%d newly_rule_based=%d",
        restored,
        len(llm_call_indices),
        newly_written_judged,
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
            append_jsonl_threadsafe(judged_path, row)
            newly_written_judged += 1
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
            future_to_idx = {executor.submit(_judge_record, idx): idx for idx in llm_call_indices}
            for future in as_completed(future_to_idx):
                idx, llm_result, raw_output = future.result()
                row = judged_rows[idx]
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
                append_jsonl_threadsafe(judged_path, row)
                newly_written_judged += 1
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
            append_jsonl_threadsafe(judged_path, row)
            newly_written_judged += 1
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
    llm_rows_idx = [
        idx
        for idx, row in enumerate(judged_rows)
        if row is not None and row.get("judge_method") in {"llm", "rule_based_audit"}
    ]
    cross_count = int(0.2 * len(llm_rows_idx))
    if len(llm_rows_idx) > 0 and cross_count == 0:
        cross_count = 1
    cross_targets = set(rng.sample(llm_rows_idx, cross_count)) if cross_count > 0 else set()
    cross_targets_list = sorted(cross_targets)
    logger.info(
        "Cross-judge spot-check targets=%d of llm_scored=%d",
        len(cross_targets_list),
        len(llm_rows_idx),
    )

    existing_cross = read_jsonl(cross_partial_path)
    cross_by_run_id: Dict[str, Dict] = {}
    for item in existing_cross:
        run_id = str(item.get("run_id") or "")
        if not run_id:
            continue
        cross_by_run_id[run_id] = item
    for row in judged_rows:
        if row is None:
            continue
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
        if "cross_judge_verdict" in row and "cross_judge_match" in row:
            cross_by_run_id[run_id] = {
                "run_id": run_id,
                "primary_verdict": row.get("verdict"),
                "secondary_verdict": row.get("cross_judge_verdict"),
                "match": row.get("cross_judge_match"),
                "secondary_raw_output": row.get("secondary_raw_output"),
            }

    for row_idx in cross_targets_list:
        row = judged_rows[row_idx]
        run_id = str(row.get("run_id") or "")
        existing_cross_row = cross_by_run_id.get(run_id)
        if existing_cross_row is None:
            continue
        row["cross_judge_verdict"] = existing_cross_row.get("secondary_verdict")
        row["cross_judge_match"] = existing_cross_row.get("match")

    pending_cross_indices = []
    for row_idx in cross_targets_list:
        row = judged_rows[row_idx]
        run_id = str(row.get("run_id") or "")
        if run_id in cross_by_run_id:
            continue
        pending_cross_indices.append(row_idx)
    logger.info(
        "Resuming cross-judge: existing=%d pending=%d",
        len(cross_targets_list) - len(pending_cross_indices),
        len(pending_cross_indices),
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

    if config.use_batch and pending_cross_indices:
        logger.info(
            "Judging batch mode enabled: submitting cross-judge batch requests=%d poll_interval=%d",
            len(pending_cross_indices),
            config.batch_poll_interval,
        )
        cross_requests: List[BatchRequest] = []
        row_to_custom: Dict[int, str] = {}
        for row_idx in pending_cross_indices:
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
        for row_idx in pending_cross_indices:
            row = judged_rows[row_idx]
            completion = cross_results[row_to_custom[row_idx]]
            second_result, second_raw = _judge_result_from_completion(
                directive_id=row["directive_id"],
                completion=completion,
            )
            match = second_result["verdict"] == row["verdict"]
            logger.info(
                "Cross-judge result run_id=%s primary=%s secondary=%s match=%s",
                row.get("run_id"),
                row.get("verdict"),
                second_result["verdict"],
                match,
            )
            row["cross_judge_verdict"] = second_result["verdict"]
            row["cross_judge_match"] = match
            cross_row = {
                "run_id": row["run_id"],
                "primary_verdict": row["verdict"],
                "secondary_verdict": second_result["verdict"],
                "match": match,
                "secondary_raw_output": second_raw,
            }
            cross_by_run_id[str(row["run_id"])] = cross_row
            append_jsonl_threadsafe(cross_partial_path, cross_row)
    elif max_workers > 1 and pending_cross_indices:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {executor.submit(_cross_judge_record, row_idx): row_idx for row_idx in pending_cross_indices}
            for future in as_completed(future_to_idx):
                row_idx, second_result, second_raw = future.result()
                row = judged_rows[row_idx]
                match = second_result["verdict"] == row["verdict"]
                logger.info(
                    "Cross-judge result run_id=%s primary=%s secondary=%s match=%s",
                    row.get("run_id"),
                    row.get("verdict"),
                    second_result["verdict"],
                    match,
                )
                row["cross_judge_verdict"] = second_result["verdict"]
                row["cross_judge_match"] = match
                cross_row = {
                    "run_id": row["run_id"],
                    "primary_verdict": row["verdict"],
                    "secondary_verdict": second_result["verdict"],
                    "match": match,
                    "secondary_raw_output": second_raw,
                }
                cross_by_run_id[str(row["run_id"])] = cross_row
                append_jsonl_threadsafe(cross_partial_path, cross_row)
    else:
        for row_idx in pending_cross_indices:
            _, second_result, second_raw = _cross_judge_record(row_idx)
            row = judged_rows[row_idx]
            match = second_result["verdict"] == row["verdict"]
            logger.info(
                "Cross-judge result run_id=%s primary=%s secondary=%s match=%s",
                row.get("run_id"),
                row.get("verdict"),
                second_result["verdict"],
                match,
            )
            row["cross_judge_verdict"] = second_result["verdict"]
            row["cross_judge_match"] = match
            cross_row = {
                "run_id": row["run_id"],
                "primary_verdict": row["verdict"],
                "secondary_verdict": second_result["verdict"],
                "match": match,
                "secondary_raw_output": second_raw,
            }
            cross_by_run_id[str(row["run_id"])] = cross_row
            append_jsonl_threadsafe(cross_partial_path, cross_row)

    final_judged_rows: List[Dict] = []
    for row in judged_rows:
        if row is None:
            raise RuntimeError("Missing judged row; judging did not complete all records.")
        final_judged_rows.append(row)

    cross_rows: List[Dict] = []
    for row_idx in cross_targets_list:
        row = final_judged_rows[row_idx]
        run_id = str(row.get("run_id") or "")
        cross_row = cross_by_run_id.get(run_id)
        if cross_row is None:
            continue
        cross_rows.append(cross_row)

    write_json(cross_results_path, cross_rows)
    logger.info("Incremental judged rows stored in %s (new=%d)", judged_path, newly_written_judged)
    logger.info("Wrote cross-judge results: %s (count=%d)", cross_results_path, len(cross_rows))

    audited_rows = [final_judged_rows[i] for i in sorted(audit_idx)]
    audited_total = len(audited_rows)
    audited_matches = sum(1 for row in audited_rows if row.get("verdict") == "FAIL")
    precheck_accuracy = (audited_matches / audited_total) if audited_total else None

    cross_total = len(cross_rows)
    cross_matches = sum(1 for row in cross_rows if bool(row.get("match")))
    cross_agreement = (cross_matches / cross_total) if cross_total else None

    summary = {
        "generated_at": _now_iso(),
        "total_records": len(final_judged_rows),
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
    logger.info(
        "Starting judge validation against golden set: count=%d max_workers=%d use_batch=%s",
        total,
        max_workers,
        config.use_batch,
    )

    validate_path = config.results_dir / "validate_judge.json"
    partial_path = config.results_dir / "validate_judge_partial.jsonl"
    existing_by_example_id: Dict[str, Dict] = {}

    if validate_path.exists():
        try:
            existing_payload = read_json(validate_path)
            rows_payload = existing_payload.get("rows", []) if isinstance(existing_payload, dict) else []
            if isinstance(rows_payload, list):
                for row in rows_payload:
                    example_id = str(row.get("example_id") or "")
                    if not example_id:
                        continue
                    existing_by_example_id[example_id] = row
        except Exception:
            logger.warning("Could not load existing %s; continuing with partial resume only.", validate_path)

    for row in read_jsonl(partial_path):
        example_id = str(row.get("example_id") or "")
        if not example_id:
            continue
        existing_by_example_id[example_id] = row

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
    pending_indices: List[int] = []
    for idx, item in enumerate(golden):
        example_id = str(item.get("example_id") or "")
        existing_row = existing_by_example_id.get(example_id)
        if existing_row is not None:
            rows[idx] = dict(existing_row)
            continue
        pending_indices.append(idx)

    logger.info(
        "Resuming judge validation: existing=%d remaining=%d total=%d",
        total - len(pending_indices),
        len(pending_indices),
        total,
    )

    if config.use_batch and pending_indices:
        logger.info(
            "Golden validation batch mode enabled: submitting requests=%d poll_interval=%d",
            len(pending_indices),
            config.batch_poll_interval,
        )
        batch_requests: List[BatchRequest] = []
        idx_to_custom: Dict[int, str] = {}
        for idx in pending_indices:
            item = golden[idx]
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
        completed_count = total - len(pending_indices)
        for idx in pending_indices:
            item = golden[idx]
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
            append_jsonl_threadsafe(partial_path, row)
            completed_count += 1
            logger.info(
                "Golden validation (%d/%d) example_id=%s expected=%s predicted=%s match=%s confidence=%.2f",
                completed_count,
                total,
                row["example_id"],
                row["expected_verdict"],
                row["predicted_verdict"],
                row["match"],
                float(result["confidence"]),
            )
    elif max_workers > 1 and pending_indices:
        completed = total - len(pending_indices)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(_validate_item, idx, golden[idx]): idx
                for idx in pending_indices
            }
            for future in as_completed(future_to_idx):
                idx, row, result = future.result()
                rows[idx] = row
                append_jsonl_threadsafe(partial_path, row)
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
        completed = total - len(pending_indices)
        for idx in pending_indices:
            item = golden[idx]
            _, row, result = _validate_item(idx, item)
            rows[idx] = row
            append_jsonl_threadsafe(partial_path, row)
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

    directive_ids = sorted({str(item.get("directive_id", "")).strip() for item in golden if item.get("directive_id")})
    per_directive = {d: {"match": 0, "total": 0} for d in directive_ids}
    total_match = 0
    for row in rows:
        if row is None:
            raise RuntimeError("Missing golden validation row; validation did not complete all examples.")
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
    write_json(validate_path, payload)
    logger.info(
        "Judge validation complete overall=%.2f%% threshold=%.0f%% pass=%s output=%s",
        overall_agreement * 100.0,
        config.validate_judge_threshold * 100.0,
        payload["passes_threshold"],
        validate_path,
    )
    return payload
