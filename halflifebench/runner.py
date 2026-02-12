from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import AppConfig
from .filler import load_filler_messages
from .providers.base import Message, ModelProvider
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


def _load_system_prompt(config: AppConfig) -> str:
    system_prompt = read_text(config.data_dir / "system_prompt.txt").strip()
    logger.debug("Loaded system prompt: chars=%d", len(system_prompt))
    return system_prompt


def _prefix_messages(
    *,
    system_prompt: str,
    filler_messages: Sequence[Message],
    include_system: bool,
    refresh_before_probe: bool = False,
    refresh_gap_tokens: Optional[int] = None,
    model: str,
) -> List[Message]:
    logger.debug(
        "Building message prefix include_system=%s filler_messages=%d refresh_before_probe=%s refresh_gap_tokens=%s",
        include_system,
        len(filler_messages),
        refresh_before_probe,
        refresh_gap_tokens,
    )
    prefix: List[Message] = []
    if include_system:
        prefix.append({"role": "developer", "content": system_prompt})

    filler = list(filler_messages)
    if include_system and refresh_before_probe and refresh_gap_tokens is not None and refresh_gap_tokens > 0:
        tail_tokens = 0
        split_idx = len(filler)
        for i in range(len(filler) - 1, -1, -1):
            tail_tokens += estimate_messages_tokens([filler[i]], model)
            split_idx = i
            if tail_tokens >= refresh_gap_tokens:
                break
        early = filler[:split_idx]
        tail = filler[split_idx:]
        logger.debug(
            "Near-probe refresh split computed split_idx=%d early_messages=%d tail_messages=%d tail_tokens~%d",
            split_idx,
            len(early),
            len(tail),
            tail_tokens,
        )
        prefix.extend(early)
        # Reinject full directive block close to probe.
        prefix.append({"role": "developer", "content": system_prompt})
        prefix.extend(tail)
    else:
        prefix.extend(filler)

    return prefix


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
    refresh_gap_tokens: Optional[int],
    overhead_calibrated: Optional[int],
) -> Tuple[Dict, int]:
    prefix = _prefix_messages(
        system_prompt=system_prompt,
        filler_messages=filler_messages,
        include_system=include_system,
        refresh_before_probe=refresh_gap_tokens is not None,
        refresh_gap_tokens=refresh_gap_tokens,
        model=config.openai_model,
    )
    probe_msg: Message = {"role": "user", "content": probe["user_message"]}
    messages = prefix + [probe_msg]

    depth_tokens_planned = estimate_messages_tokens(prefix, config.openai_model)
    probe_tokens_estimate = estimate_text_tokens(probe["user_message"], config.openai_model)
    logger.debug(
        "Calling model run_type=%s probe_id=%s directive=%s depth_target=%d include_system=%s refresh_gap_tokens=%s "
        "prefix_messages=%d total_messages=%d depth_tokens_planned=%d probe_tokens_estimate=%d",
        run_type,
        probe.get("probe_id"),
        probe.get("directive_id"),
        depth_target_tokens,
        include_system,
        refresh_gap_tokens,
        len(prefix),
        len(messages),
        depth_tokens_planned,
        probe_tokens_estimate,
    )

    completion = provider.complete(
        model=config.openai_model,
        messages=messages,
        temperature=config.temperature,
        seed=config.seed,
        max_output_tokens=config.max_output_tokens,
    )

    request_input_tokens_total = completion.input_tokens
    if overhead_calibrated is None:
        overhead_calibrated = max(0, request_input_tokens_total - probe_tokens_estimate - depth_tokens_planned)

    depth_tokens_measured = max(
        0,
        request_input_tokens_total - probe_tokens_estimate - int(overhead_calibrated),
    )
    logger.debug(
        "Model response run_type=%s probe_id=%s input_tokens=%d output_tokens=%d depth_measured=%d overhead=%d",
        run_type,
        probe.get("probe_id"),
        request_input_tokens_total,
        completion.output_tokens,
        depth_tokens_measured,
        int(overhead_calibrated),
    )

    record = {
        "run_id": f"{run_type}:{refresh_gap_tokens or 0}:{depth_target_tokens}:{probe['probe_id']}",
        "run_type": run_type,
        "timestamp": _now_iso(),
        "probe_id": probe["probe_id"],
        "directive_id": probe["directive_id"],
        "catalogue_id": probe.get("catalogue_id"),
        "depth_target_tokens": depth_target_tokens,
        "depth_tokens_planned": depth_tokens_planned,
        "depth_tokens_measured": depth_tokens_measured,
        "request_input_tokens_total": request_input_tokens_total,
        "probe_tokens_estimate": probe_tokens_estimate,
        "overhead_calibrated": int(overhead_calibrated),
        "refresh_gap_tokens": refresh_gap_tokens,
        "messages_sent": messages,
        "response": completion.content,
        "model": completion.model,
        "output_tokens": completion.output_tokens,
        "metadata": completion.metadata,
    }
    return record, int(overhead_calibrated)


def run_baselines(config: AppConfig, provider: ModelProvider) -> None:
    probes = _load_probes(config)
    system_prompt = _load_system_prompt(config)
    logger.info("Starting baselines: probe_count=%d", len(probes))

    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    overhead_calibrated: Optional[int] = None
    system_records: List[Dict] = []
    no_system_records: List[Dict] = []

    total = len(probes)
    for idx, probe in enumerate(probes, start=1):
        logger.info("Baseline system: probe %s (%d/%d)", probe.get("probe_id"), idx, total)
        record, overhead_calibrated = _single_call_record(
            provider=provider,
            config=config,
            probe=probe,
            system_prompt=system_prompt,
            filler_messages=[],
            include_system=True,
            depth_target_tokens=0,
            run_type="baseline_system",
            refresh_gap_tokens=None,
            overhead_calibrated=overhead_calibrated,
        )
        system_records.append(record)
        logger.debug(
            "Baseline system result probe=%s input=%d output=%d depth_measured=%d",
            probe.get("probe_id"),
            int(record.get("request_input_tokens_total", 0)),
            int(record.get("output_tokens", 0)),
            int(record.get("depth_tokens_measured", 0)),
        )

    if overhead_calibrated is None:
        overhead_calibrated = 0
    logger.info("Calibrated overhead=%d tokens", overhead_calibrated)

    for idx, probe in enumerate(probes, start=1):
        logger.info("Baseline no-system: probe %s (%d/%d)", probe.get("probe_id"), idx, total)
        record, _ = _single_call_record(
            provider=provider,
            config=config,
            probe=probe,
            system_prompt=system_prompt,
            filler_messages=[],
            include_system=False,
            depth_target_tokens=0,
            run_type="baseline_no_system",
            refresh_gap_tokens=None,
            overhead_calibrated=overhead_calibrated,
        )
        no_system_records.append(record)
        logger.debug(
            "Baseline no-system result probe=%s input=%d output=%d depth_measured=%d",
            probe.get("probe_id"),
            int(record.get("request_input_tokens_total", 0)),
            int(record.get("output_tokens", 0)),
            int(record.get("depth_tokens_measured", 0)),
        )

    baseline_system_path = raw_dir / "baseline_system.jsonl"
    baseline_no_system_path = raw_dir / "baseline_no_system.jsonl"
    calibration_path = config.results_dir / "calibration.json"
    baselines_summary_path = config.results_dir / "baselines.json"
    write_jsonl(baseline_system_path, system_records)
    write_jsonl(baseline_no_system_path, no_system_records)
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
    probes = _load_probes(config)
    system_prompt = _load_system_prompt(config)
    overhead_calibrated = _load_overhead(config)
    logger.info(
        "Starting depth sweep: depths=%d probes=%d overhead=%d",
        len(config.depth_targets),
        len(probes),
        overhead_calibrated,
    )

    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict] = []
    total_depths = len(config.depth_targets)
    total_probes = len(probes)
    for depth_idx, depth_target in enumerate(config.depth_targets, start=1):
        filler_messages = load_filler_messages(config.filler_dir, depth_target)
        logger.info(
            "Sweep depth %d/%d: target=%d filler_messages=%d",
            depth_idx,
            total_depths,
            depth_target,
            len(filler_messages),
        )
        for probe_idx, probe in enumerate(probes, start=1):
            logger.info(
                "Sweep depth=%d: probe %s (%d/%d)",
                depth_target,
                probe.get("probe_id"),
                probe_idx,
                total_probes,
            )
            record, _ = _single_call_record(
                provider=provider,
                config=config,
                probe=probe,
                system_prompt=system_prompt,
                filler_messages=filler_messages,
                include_system=True,
                depth_target_tokens=depth_target,
                run_type="sweep",
                refresh_gap_tokens=None,
                overhead_calibrated=overhead_calibrated,
            )
            rows.append(record)
            logger.debug(
                "Sweep result depth=%d probe=%s input=%d output=%d depth_planned=%d depth_measured=%d",
                depth_target,
                probe.get("probe_id"),
                int(record.get("request_input_tokens_total", 0)),
                int(record.get("output_tokens", 0)),
                int(record.get("depth_tokens_planned", 0)),
                int(record.get("depth_tokens_measured", 0)),
            )

    sweep_path = raw_dir / "sweep.jsonl"
    write_jsonl(sweep_path, rows)
    logger.info("Depth sweep complete: rows=%d output=%s", len(rows), sweep_path)


def run_near_probe_refresh(config: AppConfig, provider: ModelProvider, gaps: Sequence[int]) -> None:
    probes = _load_probes(config)
    system_prompt = _load_system_prompt(config)
    overhead_calibrated = _load_overhead(config)
    raw_dir = config.results_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Starting near-probe refresh: gaps=%s depths=%d probes=%d overhead=%d",
        list(gaps),
        len(config.depth_targets),
        len(probes),
        overhead_calibrated,
    )

    for gap in gaps:
        logger.info("Near-probe refresh gap=%d: starting", gap)
        rows: List[Dict] = []
        eligible_depths = [depth for depth in config.depth_targets if depth > gap]
        for depth_idx, depth_target in enumerate(config.depth_targets, start=1):
            if depth_target <= gap:
                logger.debug(
                    "Skipping depth target=%d for gap=%d because depth<=gap",
                    depth_target,
                    gap,
                )
                continue
            filler_messages = load_filler_messages(config.filler_dir, depth_target)
            logger.info(
                "Near-probe refresh gap=%d depth=%d (depth %d/%d eligible=%d): filler_messages=%d",
                gap,
                depth_target,
                depth_idx,
                len(config.depth_targets),
                len(eligible_depths),
                len(filler_messages),
            )
            for probe_idx, probe in enumerate(probes, start=1):
                logger.info(
                    "Refresh gap=%d depth=%d: probe %s (%d/%d)",
                    gap,
                    depth_target,
                    probe.get("probe_id"),
                    probe_idx,
                    len(probes),
                )
                record, _ = _single_call_record(
                    provider=provider,
                    config=config,
                    probe=probe,
                    system_prompt=system_prompt,
                    filler_messages=filler_messages,
                    include_system=True,
                    depth_target_tokens=depth_target,
                    run_type="near_probe_refresh",
                    refresh_gap_tokens=gap,
                    overhead_calibrated=overhead_calibrated,
                )
                rows.append(record)
                logger.debug(
                    "Refresh result gap=%d depth=%d probe=%s input=%d output=%d depth_measured=%d",
                    gap,
                    depth_target,
                    probe.get("probe_id"),
                    int(record.get("request_input_tokens_total", 0)),
                    int(record.get("output_tokens", 0)),
                    int(record.get("depth_tokens_measured", 0)),
                )
        out_path = raw_dir / f"near_probe_refresh_{gap}.jsonl"
        write_jsonl(out_path, rows)
        logger.info("Near-probe refresh gap=%d complete: rows=%d output=%s", gap, len(rows), out_path)
