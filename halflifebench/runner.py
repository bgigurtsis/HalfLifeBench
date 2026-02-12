from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import AppConfig
from .filler import load_filler_messages
from .providers.base import BatchRequest, CompletionResult, Message, ModelProvider
from .utils import (
    append_jsonl_threadsafe,
    estimate_messages_tokens,
    estimate_text_tokens,
    read_json,
    read_jsonl,
    read_text,
    write_json,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_probes(config: AppConfig) -> List[Dict]:
    probes = read_json(config.data_dir / "probes.json")
    if not isinstance(probes, list):
        raise ValueError("data/probes.json must contain a list")
    logger.debug("Loaded probes: count=%d", len(probes))
    return probes


def _group_probes_by_directive(probes: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for probe in probes:
        directive_id = str(probe.get("directive_id") or "")
        if not directive_id:
            raise ValueError(f"Probe missing directive_id: probe_id={probe.get('probe_id')}")
        grouped.setdefault(directive_id, []).append(probe)
    logger.debug("Grouped probes by directive: %s", {k: len(v) for k, v in grouped.items()})
    return grouped


def _load_system_prompt(config: AppConfig) -> str:
    system_prompt = read_text(config.data_dir / "system_prompt.txt").strip()
    logger.debug("Loaded system prompt: chars=%d", len(system_prompt))
    return system_prompt


def _prefix_messages(
    *,
    system_prompt: str,
    filler_messages: Sequence[Message],
    include_system: bool,
) -> List[Message]:
    logger.debug(
        "Building message prefix include_system=%s filler_messages=%d",
        include_system,
        len(filler_messages),
    )
    prefix: List[Message] = []
    if include_system:
        prefix.append({"role": "developer", "content": system_prompt})
    prefix.extend(list(filler_messages))
    return prefix


def _build_call_messages(
    *,
    system_prompt: str,
    filler_messages: Sequence[Message],
    include_system: bool,
    probe: Dict,
) -> Tuple[List[Message], List[Message]]:
    prefix = _prefix_messages(
        system_prompt=system_prompt,
        filler_messages=filler_messages,
        include_system=include_system,
    )
    probe_msg: Message = {"role": "user", "content": probe["user_message"]}
    return prefix, prefix + [probe_msg]


def _build_record_from_completion(
    *,
    completion: CompletionResult,
    probe: Dict,
    messages: List[Message],
    depth_target_tokens: int,
    run_type: str,
    overhead_calibrated: Optional[int],
    repetition_index: int,
    effective_seed: int,
    depth_tokens_planned: int,
    probe_tokens_estimate: int,
) -> Tuple[Dict, int]:
    request_input_tokens_total = completion.input_tokens
    if overhead_calibrated is None:
        overhead_calibrated = max(0, request_input_tokens_total - probe_tokens_estimate - depth_tokens_planned)

    depth_tokens_measured = max(
        0,
        request_input_tokens_total - probe_tokens_estimate - int(overhead_calibrated),
    )
    incomplete_reason = None
    if isinstance(completion.metadata, dict):
        incomplete_details = completion.metadata.get("incomplete_details")
        if isinstance(incomplete_details, dict):
            incomplete_reason = incomplete_details.get("reason")
    reasoning_tokens = (
        completion.metadata.get("reasoning_tokens")
        if isinstance(completion.metadata, dict)
        else None
    )
    logger.debug(
        "Model response run_type=%s probe_id=%s input_tokens=%d output_tokens=%d depth_measured=%d overhead=%d incomplete_reason=%s reasoning_tokens=%s",
        run_type,
        probe.get("probe_id"),
        request_input_tokens_total,
        completion.output_tokens,
        depth_tokens_measured,
        int(overhead_calibrated),
        incomplete_reason,
        reasoning_tokens,
    )

    record = {
        "run_id": _build_run_id(
            run_type=run_type,
            depth_target_tokens=depth_target_tokens,
            probe_id=str(probe["probe_id"]),
            repetition_index=repetition_index,
        ),
        "run_type": run_type,
        "timestamp": _now_iso(),
        "probe_id": probe["probe_id"],
        "directive_id": probe["directive_id"],
        "catalogue_id": probe.get("catalogue_id"),
        "repetition_index": repetition_index,
        "effective_seed": effective_seed,
        "depth_target_tokens": depth_target_tokens,
        "depth_tokens_planned": depth_tokens_planned,
        "depth_tokens_measured": depth_tokens_measured,
        "request_input_tokens_total": request_input_tokens_total,
        "probe_tokens_estimate": probe_tokens_estimate,
        "overhead_calibrated": int(overhead_calibrated),
        "messages_sent": messages,
        "response": completion.content,
        "response_empty": not bool((completion.content or "").strip()),
        "model": completion.model,
        "output_tokens": completion.output_tokens,
        "metadata": completion.metadata,
    }
    return record, int(overhead_calibrated)


def _build_run_id(*, run_type: str, depth_target_tokens: int, probe_id: str, repetition_index: int) -> str:
    return f"{run_type}:{depth_target_tokens}:{probe_id}:r{repetition_index}"


def _single_call_record(
    *,
    provider: ModelProvider,
    config: AppConfig,
    probe: Dict,
    system_prompt: str,
    filler_messages: Sequence[Message],
    include_system: bool,
    depth_target_tokens: int,
    run_type: str,
    overhead_calibrated: Optional[int],
    seed_override: Optional[int] = None,
    repetition_index: int = 0,
) -> Tuple[Dict, int]:
    prefix, messages = _build_call_messages(
        system_prompt=system_prompt,
        filler_messages=filler_messages,
        include_system=include_system,
        probe=probe,
    )

    depth_tokens_planned = estimate_messages_tokens(prefix, config.openai_model)
    probe_tokens_estimate = estimate_text_tokens(probe["user_message"], config.openai_model)
    logger.debug(
        "Calling model run_type=%s probe_id=%s directive=%s repetition_index=%d depth_target=%d include_system=%s "
        "prefix_messages=%d total_messages=%d depth_tokens_planned=%d probe_tokens_estimate=%d",
        run_type,
        probe.get("probe_id"),
        probe.get("directive_id"),
        repetition_index,
        depth_target_tokens,
        include_system,
        len(prefix),
        len(messages),
        depth_tokens_planned,
        probe_tokens_estimate,
    )

    effective_seed = seed_override if seed_override is not None else config.seed
    completion = provider.complete(
        model=config.openai_model,
        messages=messages,
        temperature=config.temperature,
        seed=effective_seed,
        max_output_tokens=config.max_output_tokens,
        reasoning_effort=config.reasoning_effort,
        max_empty_retries=config.max_empty_retries,
    )

    return _build_record_from_completion(
        completion=completion,
        probe=probe,
        messages=messages,
        depth_target_tokens=depth_target_tokens,
        run_type=run_type,
        overhead_calibrated=overhead_calibrated,
        repetition_index=repetition_index,
        effective_seed=effective_seed,
        depth_tokens_planned=depth_tokens_planned,
        probe_tokens_estimate=probe_tokens_estimate,
    )


def run_baselines(config: AppConfig, provider: ModelProvider) -> None:
    if config.repetitions < 1:
        raise ValueError("config.repetitions must be >= 1")

    probes = _load_probes(config)
    probes_by_directive = _group_probes_by_directive(probes)
    directives = sorted(probes_by_directive.keys())
    baseline_tasks: List[Tuple[int, Dict, int, int]] = []
    for directive_id in directives:
        directive_probes = probes_by_directive[directive_id]
        for rep_idx in range(config.repetitions):
            probe = directive_probes[rep_idx % len(directive_probes)]
            seed = config.seed + rep_idx
            baseline_tasks.append((len(baseline_tasks), probe, rep_idx, seed))

    if not baseline_tasks:
        raise ValueError("No baseline tasks generated from probes.")

    total = len(baseline_tasks)
    max_workers = max(1, config.max_workers)
    system_prompt = _load_system_prompt(config)
    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    baseline_system_path = raw_dir / "baseline_system.jsonl"
    baseline_no_system_path = raw_dir / "baseline_no_system.jsonl"
    calibration_path = config.results_dir / "calibration.json"
    baselines_summary_path = config.results_dir / "baselines.json"

    expected_system_run_ids = {
        _build_run_id(
            run_type="baseline_system",
            depth_target_tokens=0,
            probe_id=str(probe["probe_id"]),
            repetition_index=rep_idx,
        )
        for _, probe, rep_idx, _ in baseline_tasks
    }
    expected_no_system_run_ids = {
        _build_run_id(
            run_type="baseline_no_system",
            depth_target_tokens=0,
            probe_id=str(probe["probe_id"]),
            repetition_index=rep_idx,
        )
        for _, probe, rep_idx, _ in baseline_tasks
    }

    existing_system = read_jsonl(baseline_system_path)
    existing_no_system = read_jsonl(baseline_no_system_path)
    completed_system_ids = {
        str(row.get("run_id"))
        for row in existing_system
        if str(row.get("run_id")) in expected_system_run_ids
    }
    completed_no_system_ids = {
        str(row.get("run_id"))
        for row in existing_no_system
        if str(row.get("run_id")) in expected_no_system_run_ids
    }

    logger.info(
        "Starting baselines: directives=%d repetitions=%d total_calls_per_phase=%d max_workers=%d use_batch=%s",
        len(directives),
        config.repetitions,
        total,
        max_workers,
        config.use_batch,
    )
    logger.info(
        "Resuming baselines: existing_system=%d existing_no_system=%d",
        len(completed_system_ids),
        len(completed_no_system_ids),
    )

    overhead_calibrated: Optional[int] = None
    if calibration_path.exists():
        payload = read_json(calibration_path)
        overhead_calibrated = int(payload.get("overhead_calibrated", 0))
        logger.info("Loaded calibrated overhead=%d from %s", overhead_calibrated, calibration_path)
    else:
        for row in existing_system:
            run_id = str(row.get("run_id"))
            if run_id not in completed_system_ids:
                continue
            if "overhead_calibrated" in row:
                overhead_calibrated = int(row.get("overhead_calibrated", 0))
                logger.info(
                    "Recovered calibrated overhead=%d from existing baseline_system records",
                    overhead_calibrated,
                )
                break

    remaining_system: List[Tuple[int, Dict, int, int, str]] = []
    for idx, probe, rep_idx, seed in baseline_tasks:
        run_id = _build_run_id(
            run_type="baseline_system",
            depth_target_tokens=0,
            probe_id=str(probe["probe_id"]),
            repetition_index=rep_idx,
        )
        if run_id in completed_system_ids:
            continue
        remaining_system.append((idx, probe, rep_idx, seed, run_id))
    logger.info(
        "Baseline system resume state: completed=%d remaining=%d total=%d",
        total - len(remaining_system),
        len(remaining_system),
        total,
    )

    completed_system = total - len(remaining_system)
    if config.use_batch:
        if remaining_system:
            system_task_meta: List[Dict] = []
            system_batch_requests: List[BatchRequest] = []
            for idx, probe, rep_idx, seed, run_id in remaining_system:
                prefix, messages = _build_call_messages(
                    system_prompt=system_prompt,
                    filler_messages=[],
                    include_system=True,
                    probe=probe,
                )
                depth_tokens_planned = estimate_messages_tokens(prefix, config.openai_model)
                probe_tokens_estimate = estimate_text_tokens(probe["user_message"], config.openai_model)
                custom_id = f"baseline_system-{idx}-{probe['probe_id']}-r{rep_idx}"
                system_task_meta.append(
                    {
                        "idx": idx,
                        "run_id": run_id,
                        "custom_id": custom_id,
                        "probe": probe,
                        "rep_idx": rep_idx,
                        "seed": seed,
                        "messages": messages,
                        "depth_tokens_planned": depth_tokens_planned,
                        "probe_tokens_estimate": probe_tokens_estimate,
                    }
                )
                system_batch_requests.append(
                    {
                        "custom_id": custom_id,
                        "model": config.openai_model,
                        "messages": messages,
                        "temperature": config.temperature,
                        "seed": seed,
                        "max_output_tokens": config.max_output_tokens,
                        "reasoning_effort": config.reasoning_effort,
                    }
                )

            logger.info(
                "Baselines batch mode (system): submitting remaining requests=%d poll_interval=%d",
                len(system_batch_requests),
                config.batch_poll_interval,
            )
            system_results = provider.complete_batch(
                requests=system_batch_requests,
                poll_interval=config.batch_poll_interval,
            )

            if overhead_calibrated is None:
                first_meta = system_task_meta[0]
                first_completion = system_results[first_meta["custom_id"]]
                first_record, overhead_calibrated = _build_record_from_completion(
                    completion=first_completion,
                    probe=first_meta["probe"],
                    messages=first_meta["messages"],
                    depth_target_tokens=0,
                    run_type="baseline_system",
                    overhead_calibrated=None,
                    repetition_index=first_meta["rep_idx"],
                    effective_seed=first_meta["seed"],
                    depth_tokens_planned=first_meta["depth_tokens_planned"],
                    probe_tokens_estimate=first_meta["probe_tokens_estimate"],
                )
                append_jsonl_threadsafe(baseline_system_path, first_record)
                completed_system_ids.add(first_meta["run_id"])
                completed_system += 1
                if overhead_calibrated is None:
                    overhead_calibrated = 0
                write_json(calibration_path, {"overhead_calibrated": overhead_calibrated})
                logger.info("Calibrated overhead=%d tokens", overhead_calibrated)
                logger.info(
                    "Baseline system: probe=%s rep=%d (%d/%d)",
                    first_record.get("probe_id"),
                    first_record.get("repetition_index"),
                    completed_system,
                    total,
                )
                remaining_meta = system_task_meta[1:]
            else:
                remaining_meta = system_task_meta

            for meta in remaining_meta:
                completion = system_results[meta["custom_id"]]
                record, _ = _build_record_from_completion(
                    completion=completion,
                    probe=meta["probe"],
                    messages=meta["messages"],
                    depth_target_tokens=0,
                    run_type="baseline_system",
                    overhead_calibrated=overhead_calibrated,
                    repetition_index=meta["rep_idx"],
                    effective_seed=meta["seed"],
                    depth_tokens_planned=meta["depth_tokens_planned"],
                    probe_tokens_estimate=meta["probe_tokens_estimate"],
                )
                append_jsonl_threadsafe(baseline_system_path, record)
                completed_system_ids.add(meta["run_id"])
                completed_system += 1
                logger.info(
                    "Baseline system: probe=%s rep=%d (%d/%d)",
                    record.get("probe_id"),
                    record.get("repetition_index"),
                    completed_system,
                    total,
                )
    else:
        if remaining_system and overhead_calibrated is None:
            _, first_probe, first_rep_idx, first_seed, first_run_id = remaining_system[0]
            logger.info(
                "Baseline system: probe=%s rep=%d (%d/%d) -- calibrating overhead",
                first_probe.get("probe_id"),
                first_rep_idx,
                completed_system + 1,
                total,
            )
            first_record, overhead_calibrated = _single_call_record(
                provider=provider,
                config=config,
                probe=first_probe,
                system_prompt=system_prompt,
                filler_messages=[],
                include_system=True,
                depth_target_tokens=0,
                run_type="baseline_system",
                overhead_calibrated=None,
                seed_override=first_seed,
                repetition_index=first_rep_idx,
            )
            if overhead_calibrated is None:
                overhead_calibrated = 0
            append_jsonl_threadsafe(baseline_system_path, first_record)
            completed_system_ids.add(first_run_id)
            completed_system += 1
            write_json(calibration_path, {"overhead_calibrated": overhead_calibrated})
            logger.info("Calibrated overhead=%d tokens", overhead_calibrated)
            remaining_system = remaining_system[1:]

        if remaining_system:
            if max_workers > 1:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_task = {
                        executor.submit(
                            _single_call_record,
                            provider=provider,
                            config=config,
                            probe=probe,
                            system_prompt=system_prompt,
                            filler_messages=[],
                            include_system=True,
                            depth_target_tokens=0,
                            run_type="baseline_system",
                            overhead_calibrated=overhead_calibrated,
                            seed_override=seed,
                            repetition_index=rep_idx,
                        ): (run_id, probe, rep_idx)
                        for _, probe, rep_idx, seed, run_id in remaining_system
                    }
                    for future in as_completed(future_to_task):
                        run_id, _, _ = future_to_task[future]
                        record, _ = future.result()
                        append_jsonl_threadsafe(baseline_system_path, record)
                        completed_system_ids.add(run_id)
                        completed_system += 1
                        logger.info(
                            "Baseline system: probe=%s rep=%d (%d/%d)",
                            record.get("probe_id"),
                            record.get("repetition_index"),
                            completed_system,
                            total,
                        )
            else:
                for _, probe, rep_idx, seed, run_id in remaining_system:
                    record, _ = _single_call_record(
                        provider=provider,
                        config=config,
                        probe=probe,
                        system_prompt=system_prompt,
                        filler_messages=[],
                        include_system=True,
                        depth_target_tokens=0,
                        run_type="baseline_system",
                        overhead_calibrated=overhead_calibrated,
                        seed_override=seed,
                        repetition_index=rep_idx,
                    )
                    append_jsonl_threadsafe(baseline_system_path, record)
                    completed_system_ids.add(run_id)
                    completed_system += 1
                    logger.info(
                        "Baseline system: probe=%s rep=%d (%d/%d)",
                        record.get("probe_id"),
                        record.get("repetition_index"),
                        completed_system,
                        total,
                    )

    if overhead_calibrated is None:
        overhead_calibrated = 0
        logger.warning("No baseline system calls executed; using overhead_calibrated=0")
    write_json(calibration_path, {"overhead_calibrated": overhead_calibrated})

    remaining_no_system: List[Tuple[int, Dict, int, int, str]] = []
    for idx, probe, rep_idx, seed in baseline_tasks:
        run_id = _build_run_id(
            run_type="baseline_no_system",
            depth_target_tokens=0,
            probe_id=str(probe["probe_id"]),
            repetition_index=rep_idx,
        )
        if run_id in completed_no_system_ids:
            continue
        remaining_no_system.append((idx, probe, rep_idx, seed, run_id))
    logger.info(
        "Baseline no-system resume state: completed=%d remaining=%d total=%d",
        total - len(remaining_no_system),
        len(remaining_no_system),
        total,
    )

    completed_no_system = total - len(remaining_no_system)
    if config.use_batch:
        if remaining_no_system:
            no_system_task_meta: List[Dict] = []
            no_system_batch_requests: List[BatchRequest] = []
            for idx, probe, rep_idx, seed, run_id in remaining_no_system:
                prefix, messages = _build_call_messages(
                    system_prompt=system_prompt,
                    filler_messages=[],
                    include_system=False,
                    probe=probe,
                )
                depth_tokens_planned = estimate_messages_tokens(prefix, config.openai_model)
                probe_tokens_estimate = estimate_text_tokens(probe["user_message"], config.openai_model)
                custom_id = f"baseline_no_system-{idx}-{probe['probe_id']}-r{rep_idx}"
                no_system_task_meta.append(
                    {
                        "run_id": run_id,
                        "custom_id": custom_id,
                        "probe": probe,
                        "rep_idx": rep_idx,
                        "seed": seed,
                        "messages": messages,
                        "depth_tokens_planned": depth_tokens_planned,
                        "probe_tokens_estimate": probe_tokens_estimate,
                    }
                )
                no_system_batch_requests.append(
                    {
                        "custom_id": custom_id,
                        "model": config.openai_model,
                        "messages": messages,
                        "temperature": config.temperature,
                        "seed": seed,
                        "max_output_tokens": config.max_output_tokens,
                        "reasoning_effort": config.reasoning_effort,
                    }
                )

            logger.info(
                "Baselines batch mode (no-system): submitting remaining requests=%d poll_interval=%d",
                len(no_system_batch_requests),
                config.batch_poll_interval,
            )
            no_system_results = provider.complete_batch(
                requests=no_system_batch_requests,
                poll_interval=config.batch_poll_interval,
            )
            for meta in no_system_task_meta:
                completion = no_system_results[meta["custom_id"]]
                record, _ = _build_record_from_completion(
                    completion=completion,
                    probe=meta["probe"],
                    messages=meta["messages"],
                    depth_target_tokens=0,
                    run_type="baseline_no_system",
                    overhead_calibrated=overhead_calibrated,
                    repetition_index=meta["rep_idx"],
                    effective_seed=meta["seed"],
                    depth_tokens_planned=meta["depth_tokens_planned"],
                    probe_tokens_estimate=meta["probe_tokens_estimate"],
                )
                append_jsonl_threadsafe(baseline_no_system_path, record)
                completed_no_system_ids.add(meta["run_id"])
                completed_no_system += 1
                logger.info(
                    "Baseline no-system: probe=%s rep=%d (%d/%d)",
                    record.get("probe_id"),
                    record.get("repetition_index"),
                    completed_no_system,
                    total,
                )
    else:
        if remaining_no_system:
            if max_workers > 1:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_task = {
                        executor.submit(
                            _single_call_record,
                            provider=provider,
                            config=config,
                            probe=probe,
                            system_prompt=system_prompt,
                            filler_messages=[],
                            include_system=False,
                            depth_target_tokens=0,
                            run_type="baseline_no_system",
                            overhead_calibrated=overhead_calibrated,
                            seed_override=seed,
                            repetition_index=rep_idx,
                        ): (run_id, probe, rep_idx)
                        for _, probe, rep_idx, seed, run_id in remaining_no_system
                    }
                    for future in as_completed(future_to_task):
                        run_id, _, _ = future_to_task[future]
                        record, _ = future.result()
                        append_jsonl_threadsafe(baseline_no_system_path, record)
                        completed_no_system_ids.add(run_id)
                        completed_no_system += 1
                        logger.info(
                            "Baseline no-system: probe=%s rep=%d (%d/%d)",
                            record.get("probe_id"),
                            record.get("repetition_index"),
                            completed_no_system,
                            total,
                        )
            else:
                for _, probe, rep_idx, seed, run_id in remaining_no_system:
                    record, _ = _single_call_record(
                        provider=provider,
                        config=config,
                        probe=probe,
                        system_prompt=system_prompt,
                        filler_messages=[],
                        include_system=False,
                        depth_target_tokens=0,
                        run_type="baseline_no_system",
                        overhead_calibrated=overhead_calibrated,
                        seed_override=seed,
                        repetition_index=rep_idx,
                    )
                    append_jsonl_threadsafe(baseline_no_system_path, record)
                    completed_no_system_ids.add(run_id)
                    completed_no_system += 1
                    logger.info(
                        "Baseline no-system: probe=%s rep=%d (%d/%d)",
                        record.get("probe_id"),
                        record.get("repetition_index"),
                        completed_no_system,
                        total,
                    )

    final_system_records = read_jsonl(baseline_system_path)
    final_no_system_records = read_jsonl(baseline_no_system_path)
    final_system_count = sum(1 for row in final_system_records if str(row.get("run_id")) in expected_system_run_ids)
    final_no_system_count = sum(
        1 for row in final_no_system_records if str(row.get("run_id")) in expected_no_system_run_ids
    )
    write_json(
        baselines_summary_path,
        {
            "overhead_calibrated": overhead_calibrated,
            "baseline_system_count": final_system_count,
            "baseline_no_system_count": final_no_system_count,
            "generated_at": _now_iso(),
        },
    )
    logger.info(
        "Baselines complete: system=%d no_system=%d files=[%s, %s, %s, %s]",
        final_system_count,
        final_no_system_count,
        baseline_system_path,
        baseline_no_system_path,
        calibration_path,
        baselines_summary_path,
    )


def _load_overhead(config: AppConfig) -> int:
    calibration_path = config.results_dir / "calibration.json"
    if not calibration_path.exists():
        raise FileNotFoundError("Missing results/calibration.json. Run baselines first.")
    payload = read_json(calibration_path)
    overhead = int(payload.get("overhead_calibrated", 0))
    logger.debug("Loaded calibrated overhead=%d from %s", overhead, calibration_path)
    return overhead


def run_sweep(config: AppConfig, provider: ModelProvider) -> None:
    if config.repetitions < 1:
        raise ValueError("config.repetitions must be >= 1")

    probes = _load_probes(config)
    probes_by_directive = _group_probes_by_directive(probes)
    directives = sorted(probes_by_directive.keys())
    system_prompt = _load_system_prompt(config)
    overhead_calibrated = _load_overhead(config)
    max_workers = max(1, config.max_workers)
    total_depths = len(config.depth_targets)
    total_calls = total_depths * len(directives) * config.repetitions

    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sweep_path = raw_dir / "sweep.jsonl"

    existing_rows = read_jsonl(sweep_path)
    expected_run_ids: set[str] = set()
    for depth_target in config.depth_targets:
        for directive_id in directives:
            directive_probes = probes_by_directive[directive_id]
            for rep_idx in range(config.repetitions):
                probe = directive_probes[rep_idx % len(directive_probes)]
                expected_run_ids.add(
                    _build_run_id(
                        run_type="sweep",
                        depth_target_tokens=depth_target,
                        probe_id=str(probe["probe_id"]),
                        repetition_index=rep_idx,
                    )
                )

    completed_run_ids = {
        str(row.get("run_id"))
        for row in existing_rows
        if str(row.get("run_id")) in expected_run_ids
    }

    logger.info(
        "Starting depth sweep: depths=%d directives=%d repetitions=%d total_calls=%d overhead=%d max_workers=%d use_batch=%s",
        total_depths,
        len(directives),
        config.repetitions,
        total_calls,
        overhead_calibrated,
        max_workers,
        config.use_batch,
    )
    logger.info(
        "Resuming sweep: existing=%d remaining=%d total=%d",
        len(completed_run_ids),
        total_calls - len(completed_run_ids),
        total_calls,
    )

    # Pre-load filler for all depths so we can submit all calls at once.
    filler_by_depth: Dict[int, Sequence] = {}
    for depth_target in config.depth_targets:
        filler_by_depth[depth_target] = load_filler_messages(config.filler_dir, depth_target)
        logger.info(
            "Sweep: pre-loaded filler for depth=%d messages=%d",
            depth_target,
            len(filler_by_depth[depth_target]),
        )

    # Build remaining tasks only.
    tasks: List[Tuple[int, int, Dict, int, int, str]] = []
    for depth_target in config.depth_targets:
        for directive_id in directives:
            directive_probes = probes_by_directive[directive_id]
            for rep_idx in range(config.repetitions):
                probe = directive_probes[rep_idx % len(directive_probes)]
                seed = config.seed + rep_idx
                run_id = _build_run_id(
                    run_type="sweep",
                    depth_target_tokens=depth_target,
                    probe_id=str(probe["probe_id"]),
                    repetition_index=rep_idx,
                )
                if run_id in completed_run_ids:
                    continue
                tasks.append((len(tasks), depth_target, probe, rep_idx, seed, run_id))

    if not tasks:
        logger.info("Depth sweep already complete for current config. output=%s", sweep_path)
        return

    completed_count = len(completed_run_ids)
    if config.use_batch:
        logger.info(
            "Sweep batch mode enabled: submitting remaining requests=%d poll_interval=%d",
            len(tasks),
            config.batch_poll_interval,
        )
        task_meta: List[Dict] = []
        batch_requests: List[BatchRequest] = []
        for _, depth_target, probe, rep_idx, seed, run_id in tasks:
            prefix, messages = _build_call_messages(
                system_prompt=system_prompt,
                filler_messages=filler_by_depth[depth_target],
                include_system=True,
                probe=probe,
            )
            depth_tokens_planned = estimate_messages_tokens(prefix, config.openai_model)
            probe_tokens_estimate = estimate_text_tokens(probe["user_message"], config.openai_model)
            custom_id = f"sweep-{depth_target}-{probe['probe_id']}-r{rep_idx}"
            task_meta.append(
                {
                    "run_id": run_id,
                    "custom_id": custom_id,
                    "probe": probe,
                    "rep_idx": rep_idx,
                    "seed": seed,
                    "messages": messages,
                    "depth_target": depth_target,
                    "depth_tokens_planned": depth_tokens_planned,
                    "probe_tokens_estimate": probe_tokens_estimate,
                }
            )
            batch_requests.append(
                {
                    "custom_id": custom_id,
                    "model": config.openai_model,
                    "messages": messages,
                    "temperature": config.temperature,
                    "seed": seed,
                    "max_output_tokens": config.max_output_tokens,
                    "reasoning_effort": config.reasoning_effort,
                }
            )

        results = provider.complete_batch(
            requests=batch_requests,
            poll_interval=config.batch_poll_interval,
        )
        for meta in task_meta:
            completion = results[meta["custom_id"]]
            record, _ = _build_record_from_completion(
                completion=completion,
                probe=meta["probe"],
                messages=meta["messages"],
                depth_target_tokens=meta["depth_target"],
                run_type="sweep",
                overhead_calibrated=overhead_calibrated,
                repetition_index=meta["rep_idx"],
                effective_seed=meta["seed"],
                depth_tokens_planned=meta["depth_tokens_planned"],
                probe_tokens_estimate=meta["probe_tokens_estimate"],
            )
            append_jsonl_threadsafe(sweep_path, record)
            completed_run_ids.add(meta["run_id"])
            completed_count += 1
            logger.info(
                "Sweep (%d/%d): depth=%d probe=%s rep=%d",
                completed_count,
                total_calls,
                record.get("depth_target_tokens"),
                record.get("probe_id"),
                record.get("repetition_index"),
            )
    else:
        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_task = {
                    executor.submit(
                        _single_call_record,
                        provider=provider,
                        config=config,
                        probe=probe,
                        system_prompt=system_prompt,
                        filler_messages=filler_by_depth[depth_target],
                        include_system=True,
                        depth_target_tokens=depth_target,
                        run_type="sweep",
                        overhead_calibrated=overhead_calibrated,
                        seed_override=seed,
                        repetition_index=rep_idx,
                    ): run_id
                    for _, depth_target, probe, rep_idx, seed, run_id in tasks
                }
                for future in as_completed(future_to_task):
                    run_id = future_to_task[future]
                    record, _ = future.result()
                    append_jsonl_threadsafe(sweep_path, record)
                    completed_run_ids.add(run_id)
                    completed_count += 1
                    logger.info(
                        "Sweep (%d/%d): depth=%d probe=%s rep=%d",
                        completed_count,
                        total_calls,
                        record.get("depth_target_tokens"),
                        record.get("probe_id"),
                        record.get("repetition_index"),
                    )
        else:
            for _, depth_target, probe, rep_idx, seed, run_id in tasks:
                record, _ = _single_call_record(
                    provider=provider,
                    config=config,
                    probe=probe,
                    system_prompt=system_prompt,
                    filler_messages=filler_by_depth[depth_target],
                    include_system=True,
                    depth_target_tokens=depth_target,
                    run_type="sweep",
                    overhead_calibrated=overhead_calibrated,
                    seed_override=seed,
                    repetition_index=rep_idx,
                )
                append_jsonl_threadsafe(sweep_path, record)
                completed_run_ids.add(run_id)
                completed_count += 1
                logger.info(
                    "Sweep (%d/%d): depth=%d probe=%s rep=%d",
                    completed_count,
                    total_calls,
                    record.get("depth_target_tokens"),
                    record.get("probe_id"),
                    record.get("repetition_index"),
                )

    final_rows = read_jsonl(sweep_path)
    final_count = sum(1 for row in final_rows if str(row.get("run_id")) in expected_run_ids)
    logger.info("Depth sweep complete: rows=%d output=%s", final_count, sweep_path)
