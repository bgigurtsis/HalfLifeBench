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
    estimate_messages_tokens,
    estimate_text_tokens,
    read_json,
    read_text,
    write_json,
    write_jsonl,
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
        "run_id": f"{run_type}:{depth_target_tokens}:{probe['probe_id']}:r{repetition_index}",
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

    system_prompt = _load_system_prompt(config)
    total = len(baseline_tasks)
    max_workers = max(1, config.max_workers)
    logger.info(
        "Starting baselines: directives=%d repetitions=%d total_calls_per_phase=%d max_workers=%d",
        len(directives),
        config.repetitions,
        total,
        max_workers,
    )

    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    system_records: List[Optional[Dict]] = [None] * total
    no_system_records: List[Optional[Dict]] = [None] * total

    if config.use_batch:
        logger.info(
            "Baselines batch mode enabled: submitting phase requests via Batch API poll_interval=%d",
            config.batch_poll_interval,
        )

        # -- Phase 1: baseline_system batch --------------------------------
        system_task_meta: List[Dict] = []
        system_batch_requests: List[BatchRequest] = []
        for idx, probe, rep_idx, seed in baseline_tasks:
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

        system_results = provider.complete_batch(
            requests=system_batch_requests,
            poll_interval=config.batch_poll_interval,
        )

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
        system_records[first_meta["idx"]] = first_record
        logger.info("Calibrated overhead=%d tokens", overhead_calibrated)

        for meta in system_task_meta[1:]:
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
            system_records[meta["idx"]] = record

        # -- Phase 2: baseline_no_system batch -----------------------------
        no_system_task_meta: List[Dict] = []
        no_system_batch_requests: List[BatchRequest] = []
        for idx, probe, rep_idx, seed in baseline_tasks:
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
                    "idx": idx,
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
            no_system_records[meta["idx"]] = record
    else:
        # -- Phase 1: baseline_system ----------------------------------------
        # First task runs sequentially to calibrate overhead.
        _, first_probe, first_rep_idx, first_seed = baseline_tasks[0]
        logger.info(
            "Baseline system: probe=%s rep=%d (1/%d) -- calibrating overhead",
            first_probe.get("probe_id"),
            first_rep_idx,
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
        logger.info("Calibrated overhead=%d tokens", overhead_calibrated)

        # Remaining baseline_system calls run in parallel.
        system_records[0] = first_record
        remaining_system = baseline_tasks[1:]

        if remaining_system and max_workers > 1:
            completed = 1
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
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
                    ): idx
                    for idx, probe, rep_idx, seed in remaining_system
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    record, _ = future.result()
                    system_records[idx] = record
                    completed += 1
                    logger.info(
                        "Baseline system: probe=%s rep=%d (%d/%d)",
                        record.get("probe_id"),
                        record.get("repetition_index"),
                        completed,
                        total,
                    )
        else:
            for idx, probe, rep_idx, seed in remaining_system:
                logger.info("Baseline system: probe=%s rep=%d (%d/%d)", probe.get("probe_id"), rep_idx, idx + 1, total)
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
                system_records[idx] = record

        # -- Phase 2: baseline_no_system -------------------------------------
        if max_workers > 1:
            completed = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
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
                    ): idx
                    for idx, probe, rep_idx, seed in baseline_tasks
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    record, _ = future.result()
                    no_system_records[idx] = record
                    completed += 1
                    logger.info(
                        "Baseline no-system: probe=%s rep=%d (%d/%d)",
                        record.get("probe_id"),
                        record.get("repetition_index"),
                        completed,
                        total,
                    )
        else:
            for idx, probe, rep_idx, seed in baseline_tasks:
                logger.info("Baseline no-system: probe=%s rep=%d (%d/%d)", probe.get("probe_id"), rep_idx, idx + 1, total)
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
                no_system_records[idx] = record

    # -- Write outputs ----------------------------------------------------
    baseline_system_path = raw_dir / "baseline_system.jsonl"
    baseline_no_system_path = raw_dir / "baseline_no_system.jsonl"
    calibration_path = config.results_dir / "calibration.json"
    baselines_summary_path = config.results_dir / "baselines.json"
    write_jsonl(baseline_system_path, system_records)  # type: ignore[arg-type]
    write_jsonl(baseline_no_system_path, no_system_records)  # type: ignore[arg-type]
    write_json(calibration_path, {"overhead_calibrated": overhead_calibrated})
    write_json(
        baselines_summary_path,
        {
            "overhead_calibrated": overhead_calibrated,
            "baseline_system_count": len(system_records),
            "baseline_no_system_count": len(no_system_records),
            "generated_at": _now_iso(),
        },
    )
    logger.info(
        "Baselines complete: system=%d no_system=%d files=[%s, %s, %s, %s]",
        len(system_records),
        len(no_system_records),
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
    logger.info(
        "Starting depth sweep: depths=%d directives=%d repetitions=%d total_calls=%d overhead=%d max_workers=%d",
        total_depths,
        len(directives),
        config.repetitions,
        total_calls,
        overhead_calibrated,
        max_workers,
    )

    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Pre-load filler for all depths so we can submit all calls at once.
    filler_by_depth: Dict[int, Sequence] = {}
    for depth_target in config.depth_targets:
        filler_by_depth[depth_target] = load_filler_messages(config.filler_dir, depth_target)
        logger.info(
            "Sweep: pre-loaded filler for depth=%d messages=%d",
            depth_target,
            len(filler_by_depth[depth_target]),
        )

    # Build ordered task list: (global_idx, depth_target, probe, repetition_index, seed)
    tasks: List[Tuple[int, int, Dict, int, int]] = []
    for depth_target in config.depth_targets:
        for directive_id in directives:
            directive_probes = probes_by_directive[directive_id]
            for rep_idx in range(config.repetitions):
                probe = directive_probes[rep_idx % len(directive_probes)]
                seed = config.seed + rep_idx
                tasks.append((len(tasks), depth_target, probe, rep_idx, seed))

    rows: List[Optional[Dict]] = [None] * len(tasks)

    if config.use_batch:
        logger.info(
            "Sweep batch mode enabled: submitting %d requests poll_interval=%d",
            len(tasks),
            config.batch_poll_interval,
        )
        task_meta: List[Dict] = []
        batch_requests: List[BatchRequest] = []
        for global_idx, depth_target, probe, rep_idx, seed in tasks:
            prefix, messages = _build_call_messages(
                system_prompt=system_prompt,
                filler_messages=filler_by_depth[depth_target],
                include_system=True,
                probe=probe,
            )
            depth_tokens_planned = estimate_messages_tokens(prefix, config.openai_model)
            probe_tokens_estimate = estimate_text_tokens(probe["user_message"], config.openai_model)
            custom_id = f"sweep-{depth_target}-{global_idx}-{probe['probe_id']}-r{rep_idx}"
            task_meta.append(
                {
                    "global_idx": global_idx,
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
        for completed, meta in enumerate(task_meta, start=1):
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
            rows[meta["global_idx"]] = record
            logger.info(
                "Sweep (%d/%d): depth=%d probe=%s rep=%d",
                completed,
                total_calls,
                record.get("depth_target_tokens"),
                record.get("probe_id"),
                record.get("repetition_index"),
            )
    else:
        if max_workers > 1:
            completed = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
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
                    ): global_idx
                    for global_idx, depth_target, probe, rep_idx, seed in tasks
                }
                for future in as_completed(future_to_idx):
                    global_idx = future_to_idx[future]
                    record, _ = future.result()
                    rows[global_idx] = record
                    completed += 1
                    logger.info(
                        "Sweep (%d/%d): depth=%d probe=%s rep=%d",
                        completed,
                        total_calls,
                        record.get("depth_target_tokens"),
                        record.get("probe_id"),
                        record.get("repetition_index"),
                    )
        else:
            for global_idx, depth_target, probe, rep_idx, seed in tasks:
                logger.info(
                    "Sweep (%d/%d): depth=%d probe=%s rep=%d",
                    global_idx + 1,
                    total_calls,
                    depth_target,
                    probe.get("probe_id"),
                    rep_idx,
                )
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
                rows[global_idx] = record

    sweep_path = raw_dir / "sweep.jsonl"
    write_jsonl(sweep_path, rows)  # type: ignore[arg-type]
    logger.info("Depth sweep complete: rows=%d output=%s", len(rows), sweep_path)
