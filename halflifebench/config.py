from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class AppConfig:
    root_dir: Path
    data_dir: Path
    results_dir: Path
    filler_dir: Path
    judge_prompts_dir: Path
    openai_model: str = "gpt-4.1-nano"
    anthropic_model: str = "claude-sonnet-4-5"
    temperature: float = 0.0
    seed: int = 42
    max_output_tokens: int = 16000
    reasoning_effort: str = "medium"
    max_empty_retries: int = 2
    repetitions: int = 20
    use_batch: bool = False
    batch_poll_interval: int = 60
    depth_targets: List[int] = field(
        default_factory=lambda: [
            0,
            4000,
            8000,
            16000,
            32000,
            48000,
            50000,
            64000,
            80000,
            100000,
            128000,
            200000,
            256000,
        ]
    )
    low_confidence_threshold: float = 0.7
    validate_judge_threshold: float = 0.8
    precheck_accuracy_threshold: float = 0.9
    cross_judge_warning_threshold: float = 0.85
    max_workers: int = 15


def load_config(root_dir: Path | None = None) -> AppConfig:
    root = root_dir or Path.cwd()
    data_dir = root / "data"
    results_dir = root / "results"
    filler_dir = data_dir / "filler"
    judge_prompts_dir = data_dir / "judge_prompts"

    return AppConfig(
        root_dir=root,
        data_dir=data_dir,
        results_dir=results_dir,
        filler_dir=filler_dir,
        judge_prompts_dir=judge_prompts_dir,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-nano"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        temperature=float(os.getenv("MODEL_TEMPERATURE", "0")),
        seed=int(os.getenv("MODEL_SEED", "42")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "16000")),
        reasoning_effort=os.getenv("REASONING_EFFORT", "medium"),
        max_empty_retries=int(os.getenv("MAX_EMPTY_RETRIES", "2")),
        repetitions=int(os.getenv("REPETITIONS", "20")),
        use_batch=_env_truthy("USE_BATCH", False),
        batch_poll_interval=int(os.getenv("BATCH_POLL_INTERVAL", "60")),
        max_workers=int(os.getenv("MAX_WORKERS", "15")),
    )
