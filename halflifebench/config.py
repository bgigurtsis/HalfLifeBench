from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class AppConfig:
    root_dir: Path
    data_dir: Path
    results_dir: Path
    filler_dir: Path
    judge_prompts_dir: Path
    openai_model: str = "gpt-5-mini"
    anthropic_model: str = "claude-opus-4-6"
    temperature: float = 0.0
    seed: int = 42
    max_output_tokens: int = 700
    depth_targets: List[int] = field(default_factory=lambda: [0, 8000, 32000, 128000, 256000])
    near_probe_refresh_defaults: List[int] = field(default_factory=lambda: [20000, 50000])
    low_confidence_threshold: float = 0.7
    validate_judge_threshold: float = 0.8
    precheck_accuracy_threshold: float = 0.9
    cross_judge_warning_threshold: float = 0.85


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
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6"),
        temperature=float(os.getenv("MODEL_TEMPERATURE", "0")),
        seed=int(os.getenv("MODEL_SEED", "42")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "700")),
    )
