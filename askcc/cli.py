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
    fetch_pr_content,
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
DEBUG_OUTPUT_MAX_CHARS = 2000


def _run_claude(prompt: str, config: AgentConfig, *, issue_url: str, cwd: Path) -> tuple[int, dict | None]:
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

    logger.info("[%s] Requesting '%s' from Claude Code ...", issue_url, config.action_name)
    logger.info("[%s] Working directory: %s", issue_url, cwd)
    logger.debug("[%s] Command: %s", issue_url, " ".join("<prompt>" if arg is prompt else arg for arg in cmd))
    result = subprocess.run(  # noqa: S603
        cmd,
        text=True,
        check=False,
        capture_output=True,
        cwd=cwd,
        env=env,
    )
    logger.info("[%s] Claude Code finished (exit code: %d)", issue_url, result.returncode)

    usage = None
    if result.stdout:
        logger.debug("[%s] Claude Code raw output: %s", issue_url, result.stdout[:DEBUG_OUTPUT_MAX_CHARS])
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
                    "[%s] Token usage — model: %s, input: %s, output: %s",
                    issue_url,
                    usage.get("model", "N/A"),
                    usage.get("input_tokens", "N/A"),
                    usage.get("output_tokens", "N/A"),
                )
        except json.JSONDecodeError:
            logger.warning("[%s] Failed to parse Claude JSON output", issue_url)
            print(result.stdout)  # noqa: T201

    if result.stderr:
        log_level = logging.ERROR if result.returncode != 0 else logging.DEBUG
        logger.log(log_level, "[%s] Claude Code stderr:\n%s", issue_url, result.stderr)

    return result.returncode, usage


def _build_prompt(action: AgentAction, config: AgentConfig, issue_content: str, issue_url: str) -> str:
    """Build the user prompt, fetching PR content for pr-review commands."""
    if action == AgentAction.REVIEWPR:
        pr_content = fetch_pr_content(issue_url)
        prompt = Template(config.user_prompt_template).safe_substitute(
            issue_content=issue_content, pr_content=pr_content
        )
    else:
        prompt = Template(config.user_prompt_template).safe_substitute(issue_content=issue_content)
    logger.info("[%s] Prompt length: %d chars", issue_url, len(prompt))
    return prompt


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


def main() -> None:  # noqa: PLR0912, PLR0915, C901
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

    review_parser = subparsers.add_parser(
        "issue-review", help="Review issue quality (clarity, completeness, feasibility)."
    )
    review_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to review.")

    reviewpr_parser = subparsers.add_parser("pr-review", help="Review a PR's code against its linked issue.")
    reviewpr_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL with linked PR.")

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

    action = AgentAction(args.command)

    if action == AgentAction.DEVELOP and not args.skip_validation:
        checks = validate_issue_readiness(args.github_issue_url)
        if not all(c.passed for c in checks):
            _print_validation_report(args.github_issue_url, checks)
            logger.error("Issue readiness validation failed. Use --skip-validation to bypass.")
            sys.exit(1)

    issue_url = args.github_issue_url
    cwd = (args.cwd or Path.cwd()).resolve()
    config = load_agent_config(action)
    issue_content = fetch_github_issue(issue_url)
    try:
        prompt = _build_prompt(action, config, issue_content, issue_url)
    except ValueError:
        logger.exception("[%s] Failed to build prompt for '%s'", issue_url, action.value)
        sys.exit(1)
    if args.language != SupportedLanguage.ENGLISH:
        prompt += f"\nOutput all comments in {args.language}."
    return_code, usage = _run_claude(prompt, config=config, issue_url=issue_url, cwd=cwd)

    if usage:
        append_usage_to_last_comment(issue_url, usage)

    if action == AgentAction.DEVELOP:
        # Post-develop: check if any changes were produced
        git_result = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
        if git_result.returncode != 0:
            logger.warning("[%s] git status failed: %s", issue_url, git_result.stderr.strip())
        else:
            logger.info("[%s] Post-develop git status: %s", issue_url, git_result.stdout.strip() or "(clean)")
        # Check for worktree branches left behind
        wt_result = subprocess.run(  # noqa: S603
            ["git", "worktree", "list", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
        if wt_result.returncode != 0:
            logger.warning("[%s] git worktree list failed: %s", issue_url, wt_result.stderr.strip())
        elif wt_result.stdout.strip():
            logger.debug("[%s] Git worktrees:\n%s", issue_url, wt_result.stdout.strip())

    if action == AgentAction.PREPARE and return_code == 0:
        transition_issue_to_planning(issue_url)

    if action == AgentAction.DEVELOP and return_code == 0:
        transition_issue_to_review(issue_url)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
