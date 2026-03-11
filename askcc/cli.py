import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from string import Template

from . import __version__
from .definitions import AgentAction, AgentConfig, SupportedLanguage
from .functions import (
    CheckResult,
    append_usage_to_last_comment,
    bootstrap_templates,
    fetch_github_issue,
    install_skills,
    load_agent_config,
    transition_issue_to_planning,
    transition_issue_to_review,
    validate_issue_labels,
    validate_issue_readiness,
)
from .settings import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_PERMISSION_MODE = "acceptEdits"


def _run_claude(
    prompt: str, config: AgentConfig, *, issue_url: str, cwd: Path | None = None
) -> tuple[int, dict | None]:
    """Run claude CLI with the given prompt, capturing JSON output for token usage reporting."""
    agent_definition = {config.action_name: {"description": config.description, "prompt": config.system_prompt}}

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--worktree",
        "--agents",
        json.dumps(agent_definition),
    ]

    # Remove CLAUDECODE env var so the child claude process doesn't think it's nested inside Claude Code
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    logger.info("Requesting '%s' action for %s from Claude Code ...", config.action_name, issue_url)
    result = subprocess.run(  # noqa: S603
        cmd,
        text=True,
        check=False,
        capture_output=True,
        cwd=cwd,
        env=env,
    )
    logger.info("Claude Code finished %s (exit code: %d)", issue_url, result.returncode)

    usage = None
    if result.stdout:
        try:
            data = json.loads(result.stdout)
            response_text = data.get("result", "")
            if response_text:
                print(response_text)  # noqa: T201
            usage = data.get("usage")
            if usage:
                model = data.get("model")
                if model:
                    usage["model"] = model
                logger.info(
                    "Token usage — model: %s, input: %s, output: %s",
                    usage.get("model", "N/A"),
                    usage.get("input_tokens", "N/A"),
                    usage.get("output_tokens", "N/A"),
                )
        except json.JSONDecodeError:
            logger.warning("Failed to parse Claude JSON output")
            print(result.stdout)  # noqa: T201

    if result.stderr:
        if result.returncode != 0:
            logger.error("Claude Code stderr:\n%s", result.stderr)
        else:
            logger.warning("Claude Code stderr:\n%s", result.stderr)

    return result.returncode, usage


def _print_validation_report(issue_url: str, checks: list[CheckResult]) -> None:
    """Print a structured pass/fail validation report."""
    passed_count = sum(1 for c in checks if c.passed)
    total = len(checks)
    print(f"Validation Report: {issue_url}")  # noqa: T201
    print("-" * 60)  # noqa: T201
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"  [{status}] {check.name}: {check.message}")  # noqa: T201
    print("-" * 60)  # noqa: T201
    result = "PASS" if passed_count == total else "FAIL"
    print(f"Result: {result} ({passed_count}/{total} checks passed)")  # noqa: T201


def main() -> None:  # noqa: PLR0915
    configure_logging()
    parser = argparse.ArgumentParser(description="A one-shot Claude Code CLI executor.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"askcc {__version__}",
    )

    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Working directory for the claude subprocess (defaults to current directory).",
    )
    parser.add_argument(
        "-i",
        "--ignore-labels",
        action="store_true",
        default=False,
        help="Bypass issue label verification.",
    )
    parser.add_argument(
        "-l",
        "--language",
        choices=[lang.value for lang in SupportedLanguage],
        default=SupportedLanguage.ENGLISH,
        help="Language for agent output comments (default: english).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Analyze a backlog issue for development readiness.")
    prepare_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to prepare.")

    plan_parser = subparsers.add_parser("plan", help="Run Claude in plan mode (read-only analysis).")
    plan_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to plan.")

    validate_parser = subparsers.add_parser("validate", help="Check issue readiness for development.")
    validate_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to validate.")

    develop_parser = subparsers.add_parser("develop", help="Run Claude in development mode.")
    develop_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to develop.")
    develop_parser.add_argument(
        "--skip-validation", action="store_true", default=False, help="Skip readiness validation before development."
    )

    review_parser = subparsers.add_parser("review", help="Run Claude in review mode (issue quality review).")
    review_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to review.")

    explore_parser = subparsers.add_parser(
        "explore", help="Run Claude in explore mode (investigate and propose solutions)."
    )
    explore_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to explore.")

    diagnose_parser = subparsers.add_parser("diagnose", help="Run Claude in diagnose mode (root cause analysis).")
    diagnose_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to diagnose.")

    install_parser = subparsers.add_parser("install", help="Install bundled skills to the agent workspace.")
    install_parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Target directory for skills (overrides auto-detection of ~/.claude and ~/.openclaw).",
    )

    args = parser.parse_args()

    if args.command == "install":
        install_skills(directory=args.directory)
        return

    if args.command == "validate":
        checks = validate_issue_readiness(args.github_issue_url)
        _print_validation_report(args.github_issue_url, checks)
        sys.exit(0 if all(c.passed for c in checks) else 1)

    bootstrap_templates()

    if not args.ignore_labels and args.command != "prepare":
        label_errors = validate_issue_labels(args.github_issue_url)
        if label_errors:
            for error in label_errors:
                logger.error(error)
            sys.exit(1)

    if args.command == "develop" and not args.skip_validation:
        checks = validate_issue_readiness(args.github_issue_url)
        if not all(c.passed for c in checks):
            _print_validation_report(args.github_issue_url, checks)
            logger.error("Issue readiness validation failed. Use --skip-validation to bypass.")
            sys.exit(1)

    agent = AgentAction(args.command)
    config = load_agent_config(agent)
    issue_content = fetch_github_issue(args.github_issue_url)
    prompt = Template(config.user_prompt_template).safe_substitute(issue_content=issue_content)
    if args.language != SupportedLanguage.ENGLISH:
        prompt += f"\nOutput all comments in {args.language}."
    logger.info("Prompt prepared for '%s' command", agent.value)
    return_code, usage = _run_claude(prompt, config=config, issue_url=args.github_issue_url, cwd=args.cwd)

    if usage:
        append_usage_to_last_comment(args.github_issue_url, usage)

    if args.command == "prepare" and return_code == 0:
        transition_issue_to_planning(args.github_issue_url)

    if args.command == "develop" and return_code == 0:
        transition_issue_to_review(args.github_issue_url)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
