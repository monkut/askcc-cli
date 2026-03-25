import argparse
import logging
import subprocess
import sys
from pathlib import Path
from string import Template

from . import __version__
from .definitions import AgentAction, AgentConfig, SupportedLanguage
from .functions import (
    CheckResult,
    _parse_issue_url,
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
    write_prompt_content,
)
from .runners import DEFAULT_RUNNER, RUNNER_CHOICES, get_runner
from .settings import configure_logging

logger = logging.getLogger(__name__)


def _build_prompt(
    action: AgentAction, config: AgentConfig, issue_content: str, issue_url: str
) -> tuple[str, list[Path]]:
    """Build the user prompt, writing variable content to tempfiles.

    Returns (prompt_text, list_of_tempfile_paths).
    """
    owner, repo, issue_number = _parse_issue_url(issue_url)
    issue_file = write_prompt_content(action.value, owner, repo, issue_number, issue_content)
    prompt_files = [issue_file]

    if action == AgentAction.REVIEWPR:
        pr_content = fetch_pr_content(issue_url)
        pr_file = write_prompt_content(action.value, owner, repo, issue_number, pr_content, suffix="_pr")
        prompt_files.append(pr_file)
        prompt = Template(config.user_prompt_template).safe_substitute(
            issue_content_file=str(issue_file), pr_content_file=str(pr_file)
        )
    else:
        prompt = Template(config.user_prompt_template).safe_substitute(issue_content_file=str(issue_file))
    logger.info("[%s] Prompt length: %d chars", issue_url, len(prompt))
    return prompt, prompt_files


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
    parser.add_argument(
        "-r",
        "--runner",
        choices=RUNNER_CHOICES,
        default=DEFAULT_RUNNER,
        help=f"Runner to execute the task (default: {DEFAULT_RUNNER}).",
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
        prompt, prompt_files = _build_prompt(action, config, issue_content, issue_url)
    except ValueError:
        logger.exception("[%s] Failed to build prompt for '%s'", issue_url, action.value)
        sys.exit(1)
    if args.language != SupportedLanguage.ENGLISH:
        prompt += f"\nOutput all comments in {args.language}."
    runner = get_runner(args.runner)
    return_code, usage = runner.run(prompt, config=config, issue_url=issue_url, cwd=cwd, prompt_files=prompt_files)

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
