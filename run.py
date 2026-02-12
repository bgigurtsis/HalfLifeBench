from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from halflifebench.config import load_config
from halflifebench.filler import generate_filler_files
from halflifebench.judge import run_judging, validate_judge_against_golden
from halflifebench.providers import AnthropicProvider, OpenAIProvider
from halflifebench.report import generate_report
from halflifebench.runner import run_baselines, run_near_probe_refresh, run_sweep
from halflifebench.scorer import score_results

logger = logging.getLogger(__name__)


def _configure_noisy_loggers(verbose: bool) -> None:
    # Keep our app logs detailed while suppressing extremely verbose transport traces.
    noisy_level = logging.WARNING
    for name in ("httpx", "httpcore", "openai._base_client"):
        logging.getLogger(name).setLevel(noisy_level)
    # In verbose mode, still allow OpenAI package info/warnings.
    logging.getLogger("openai").setLevel(logging.INFO if verbose else logging.WARNING)

def cmd_generate_filler() -> int:
    config = load_config(Path.cwd())
    generate_filler_files(config)
    logger.info("Generated filler files.")
    return 0


def cmd_validate_judge() -> int:
    config = load_config(Path.cwd())
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
    config = load_config(Path.cwd())
    provider = OpenAIProvider()

    if not (args.baselines or args.sweep or args.near_probe_refresh):
        logger.info("Nothing to run. Choose at least one of --baselines, --sweep, --near-probe-refresh.")
        return 1

    if args.baselines:
        logger.info("Running baselines...")
        run_baselines(config, provider)
        logger.info("Baselines complete.")
    if args.sweep:
        logger.info("Running depth sweep...")
        run_sweep(config, provider)
        logger.info("Depth sweep complete.")
    if args.near_probe_refresh:
        logger.info("Running near-probe refresh for gaps: %s", args.near_probe_refresh)
        run_near_probe_refresh(config, provider, args.near_probe_refresh)
        logger.info("Near-probe refresh complete.")
    return 0


def cmd_judge() -> int:
    config = load_config(Path.cwd())
    provider = AnthropicProvider()
    summary = run_judging(config, provider)
    logger.info(
        "Judging complete. Cross-judge agreement=%s Pre-check accuracy=%s",
        summary.get("cross_judge_agreement"),
        summary.get("precheck_accuracy"),
    )
    return 0


def cmd_report() -> int:
    config = load_config(Path.cwd())
    score_results(config)
    report_path = generate_report(config)
    logger.info("Report generated: %s", report_path)
    return 0


def cmd_all() -> int:
    config = load_config(Path.cwd())
    logger.info("Step 1/7: generate-filler")
    generate_filler_files(config)

    logger.info("Step 2/7: validate-judge")
    judge_provider = AnthropicProvider()
    validation = validate_judge_against_golden(config, judge_provider)
    if not bool(validation.get("passes_threshold", False)):
        logger.info(
            "Aborting: validate-judge below threshold (%.2f%% < %.0f%%).",
            float(validation.get("overall_agreement", 0.0)) * 100.0,
            float(validation.get("threshold", 0.0)) * 100.0,
        )
        return 2

    logger.info("Step 3/7: run --baselines")
    mut_provider = OpenAIProvider()
    run_baselines(config, mut_provider)

    logger.info("Step 4/7: run --sweep")
    run_sweep(config, mut_provider)

    logger.info("Step 5/7: run --near-probe-refresh 20000 50000")
    run_near_probe_refresh(config, mut_provider, config.near_probe_refresh_defaults)

    logger.info("Step 6/7: judge")
    run_judging(config, judge_provider)

    logger.info("Step 7/7: report")
    score_results(config)
    report_path = generate_report(config)
    logger.info("All steps complete. Report: %s", report_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HalfLifeBench PoC CLI")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-filler", help="Generate filler chunks")
    sub.add_parser("validate-judge", help="Validate judge against golden set")

    run_parser = sub.add_parser("run", help="Run benchmark calls")
    run_parser.add_argument("--baselines", action="store_true", help="Run token-zero and no-system baselines")
    run_parser.add_argument("--sweep", action="store_true", help="Run depth sweep")
    run_parser.add_argument(
        "--near-probe-refresh",
        type=int,
        nargs="+",
        help="Near-probe policy refresh (PoC) with one refresh inserted N tokens before probe",
    )

    sub.add_parser("judge", help="Judge stored model responses")
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
        return cmd_generate_filler()
    if args.command == "validate-judge":
        return cmd_validate_judge()
    if args.command == "run":
        return cmd_run(args)
    if args.command == "judge":
        return cmd_judge()
    if args.command == "report":
        return cmd_report()
    if args.command == "all":
        return cmd_all()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
