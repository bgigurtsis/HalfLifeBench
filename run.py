from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from halflifebench.config import load_config
from halflifebench.filler import generate_filler_files
from halflifebench.judge import run_judging, validate_judge_against_golden
from halflifebench.judge_comparison import compare_judges
from halflifebench.providers import AnthropicProvider, OpenAIProvider
from halflifebench.report import generate_report
from halflifebench.runner import run_baselines, run_sweep
from halflifebench.scorer import score_results

logger = logging.getLogger(__name__)


def _load_config_with_overrides(
    workers_override: int | None = None,
    repetitions_override: int | None = None,
):
    """Load config and apply top-level CLI overrides if provided."""
    config = load_config(Path.cwd())
    if workers_override is not None:
        config.max_workers = workers_override
    if repetitions_override is not None:
        config.repetitions = repetitions_override
    return config


def _configure_noisy_loggers(verbose: bool) -> None:
    # Keep our app logs detailed while suppressing extremely verbose transport traces.
    noisy_level = logging.WARNING
    for name in ("httpx", "httpcore", "openai._base_client"):
        logging.getLogger(name).setLevel(noisy_level)
    # In verbose mode, still allow OpenAI package info/warnings.
    logging.getLogger("openai").setLevel(logging.INFO if verbose else logging.WARNING)

def cmd_generate_filler(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    generate_filler_files(config)
    logger.info("Generated filler files.")
    return 0


def cmd_validate_judge(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    provider = AnthropicProvider()
    result = validate_judge_against_golden(config, provider)
    overall = float(result["overall_agreement"])
    threshold = float(result["threshold"])
    logger.info("Validate judge overall agreement: %.2f%% (threshold %.0f%%)", overall * 100.0, threshold * 100.0)
    if not bool(result["passes_threshold"]):
        logger.info("Judge validation failed threshold.")
        return 2
    logger.info("Judge validation passed threshold.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    provider = OpenAIProvider()

    if not (args.baselines or args.sweep):
        logger.info("Nothing to run. Choose at least one of --baselines or --sweep.")
        return 1

    logger.info(
        "Using max_workers=%d repetitions=%d for benchmark calls",
        config.max_workers,
        config.repetitions,
    )

    if args.baselines:
        logger.info("Running baselines...")
        run_baselines(config, provider)
        logger.info("Baselines complete.")
    if args.sweep:
        logger.info("Running depth sweep...")
        run_sweep(config, provider)
        logger.info("Depth sweep complete.")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    provider = AnthropicProvider()
    summary = run_judging(config, provider)
    logger.info(
        "Judging complete. Cross-judge agreement=%s Pre-check accuracy=%s",
        summary.get("cross_judge_agreement"),
        summary.get("precheck_accuracy"),
    )
    return 0


def cmd_compare_judges(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    if args.include_sweep < 0:
        logger.error("--include-sweep must be >= 0")
        return 1
    provider = AnthropicProvider()
    result = compare_judges(
        config=config,
        provider=provider,
        model_a=args.model_a,
        model_b=args.model_b,
        sweep_sample_size=args.include_sweep,
    )
    golden = result.get("golden_set", {})
    logger.info(
        "Judge comparison complete model_a=%s model_b=%s golden_a=%.2f%% golden_b=%.2f%% kappa=%s output=%s",
        args.model_a,
        args.model_b,
        float(golden.get("model_a_agreement", 0.0) or 0.0) * 100.0,
        float(golden.get("model_b_agreement", 0.0) or 0.0) * 100.0,
        golden.get("cohens_kappa"),
        result.get("output_path"),
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    score_results(config)
    report_path = generate_report(config)
    logger.info("Report generated: %s", report_path)
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    config = _load_config_with_overrides(args.workers, args.repetitions)
    logger.info("Step 1/6: generate-filler")
    generate_filler_files(config)

    logger.info(
        "Using max_workers=%d repetitions=%d for benchmark calls",
        config.max_workers,
        config.repetitions,
    )

    logger.info("Step 2/6: validate-judge")
    judge_provider = AnthropicProvider()
    validation = validate_judge_against_golden(config, judge_provider)
    if not bool(validation.get("passes_threshold", False)):
        logger.info(
            "Aborting: validate-judge below threshold (%.2f%% < %.0f%%).",
            float(validation.get("overall_agreement", 0.0)) * 100.0,
            float(validation.get("threshold", 0.0)) * 100.0,
        )
        return 2

    logger.info("Step 3/6: run --baselines")
    mut_provider = OpenAIProvider()
    run_baselines(config, mut_provider)

    logger.info("Step 4/6: run --sweep")
    run_sweep(config, mut_provider)

    logger.info("Step 5/6: judge")
    run_judging(config, judge_provider)

    logger.info("Step 6/6: report")
    score_results(config)
    report_path = generate_report(config)
    logger.info("All steps complete. Report: %s", report_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HalfLifeBench PoC CLI")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="Max parallel API workers (default: 15, or MAX_WORKERS env var). Use 1 to disable parallelism.",
    )
    parser.add_argument(
        "-r",
        "--repetitions",
        type=int,
        default=None,
        help="Repetitions per (directive, depth) cell (default: 10, or REPETITIONS env var).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-filler", help="Generate filler chunks")
    sub.add_parser("validate-judge", help="Validate judge against golden set")

    run_parser = sub.add_parser("run", help="Run benchmark calls")
    run_parser.add_argument("--baselines", action="store_true", help="Run token-zero and no-system baselines")
    run_parser.add_argument("--sweep", action="store_true", help="Run depth sweep")

    sub.add_parser("judge", help="Judge stored model responses")
    compare_parser = sub.add_parser("compare-judges", help="Compare two Anthropic judge models")
    compare_parser.add_argument(
        "--model-a",
        default="claude-sonnet-4-5",
        help="First judge model ID (default: claude-sonnet-4-5).",
    )
    compare_parser.add_argument(
        "--model-b",
        default="claude-opus-4-6",
        help="Second judge model ID (default: claude-opus-4-6, kept for Sonnet-vs-Opus comparison).",
    )
    compare_parser.add_argument(
        "--include-sweep",
        type=int,
        default=0,
        help="Optionally re-judge N sampled rows from results/raw/sweep.jsonl with both models.",
    )
    sub.add_parser("report", help="Score and generate HTML report")
    sub.add_parser("all", help="Run full pipeline with validate-judge gating")
    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logger.debug("Logging initialized at level=%s", "DEBUG" if args.verbose else "INFO")
    _configure_noisy_loggers(args.verbose)

    if args.command == "generate-filler":
        return cmd_generate_filler(args)
    if args.command == "validate-judge":
        return cmd_validate_judge(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "judge":
        return cmd_judge(args)
    if args.command == "compare-judges":
        return cmd_compare_judges(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "all":
        return cmd_all(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
