import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template

from . import __version__, settings
from .definitions import VALID_FRONTMATTER_MODELS, AgentAction, AgentConfig
from .functions import (
    CheckResult,
    _parse_issue_url,
    _run_project_verification,
    append_usage_to_last_comment,
    bootstrap_templates,
    fetch_github_issue,
    fetch_pr_content,
    install_skills,
    load_agent_config,
    transition_issue_to_development,
    transition_issue_to_planning,
    transition_issue_to_review,
    validate_issue_labels,
    validate_issue_readiness,
    write_prompt_content,
)
from .runners import DEFAULT_RUNNER, RUNNER_REGISTRY, OAuthTokenNotFoundError, get_runner
from .settings import VALID_EFFORT_LEVELS, SupportedLanguage, configure_logging

logger = logging.getLogger(__name__)


def _build_prompt(
    action: AgentAction, config: AgentConfig, issue_content: str, issue_url: str
) -> tuple[str, list[Path]]:
    """Build the user prompt, writing variable content to tempfiles in /tmp.

    Returns (prompt_text, list_of_tempfile_paths_to_clean_up).
    """
    owner, repo, issue_number = _parse_issue_url(issue_url)
    issue_file = write_prompt_content(action.value, owner, repo, issue_number, issue_content)
    tempfiles = [issue_file]

    if action == AgentAction.REVIEWPR:
        pr_content = fetch_pr_content(issue_url)
        pr_file = write_prompt_content(action.value, owner, repo, issue_number, pr_content, suffix="_pr")
        tempfiles.append(pr_file)
        prompt = Template(config.user_prompt_template).safe_substitute(
            issue_content_file=str(issue_file), pr_content_file=str(pr_file)
        )
    else:
        prompt = Template(config.user_prompt_template).safe_substitute(issue_content_file=str(issue_file))
    logger.info("[%s] Prompt length: %d chars", issue_url, len(prompt))
    return prompt, tempfiles


def _resolve_effort(cli_effort: str | None, frontmatter_effort: str | None) -> str:
    """Precedence: CLI flag > env var > template frontmatter > built-in default."""
    if cli_effort is not None:
        return cli_effort
    env_value = os.environ.get("ASKCC_CLAUDE_EFFORT_LEVEL", "")
    if env_value:
        try:
            return VALID_EFFORT_LEVELS(env_value).value
        except ValueError:
            logger.warning("Invalid ASKCC_CLAUDE_EFFORT_LEVEL=%r — falling through to frontmatter/default", env_value)
    if frontmatter_effort is not None:
        return frontmatter_effort
    return settings.DEFAULT_EFFORT_LEVEL


def _resolve_max_thinking_tokens(cli_value: int | None, frontmatter_value: int | None) -> int:
    """Precedence: CLI flag > env var > template frontmatter > built-in default."""
    if cli_value is not None:
        return cli_value
    raw_env = os.environ.get("ASKCC_CLAUDE_MAX_THINKING_TOKENS", "")
    if raw_env.isdigit():
        return int(raw_env)
    if frontmatter_value is not None:
        return frontmatter_value
    return settings.DEFAULT_MAX_THINKING_TOKENS


def _resolve_model(cli_value: str | None, frontmatter_value: str | None) -> str | None:
    """Precedence: CLI flag > env var > template frontmatter. No built-in default.

    Returns None when no source is set so the caller can suppress the `--model` flag
    and let claude pick its own default.
    """
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get("ASKCC_CLAUDE_MODEL", "")
    if env_value:
        if env_value in VALID_FRONTMATTER_MODELS:
            return env_value
        logger.warning(
            "Invalid ASKCC_CLAUDE_MODEL=%r (valid: %s) — falling through to frontmatter/default",
            env_value,
            ", ".join(VALID_FRONTMATTER_MODELS),
        )
    return frontmatter_value


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
        default=settings.DEFAULT_LANGUAGE,
        help=f"Language for agent output comments. "
        f"Precedence: CLI > env (ASKCC_LANGUAGE) > user config "
        f"(~/.askcc/config.toml [defaults].language) > built-in default "
        f"({SupportedLanguage.ENGLISH}).",
    )
    parser.add_argument(
        "-r",
        "--runner",
        choices=tuple(RUNNER_REGISTRY),
        default=DEFAULT_RUNNER,
        help=f"Runner to execute the task (default: {DEFAULT_RUNNER}).",
    )
    parser.add_argument(
        "--effort",
        choices=VALID_EFFORT_LEVELS,
        default=None,
        help=f"Claude thinking effort level. "
        f"Precedence: CLI > env (ASKCC_CLAUDE_EFFORT_LEVEL) > template frontmatter > "
        f"built-in default ({settings.DEFAULT_EFFORT_LEVEL}).",
    )
    parser.add_argument(
        "--model",
        choices=VALID_FRONTMATTER_MODELS,
        default=None,
        help="Claude model. "
        "Precedence: CLI > env (ASKCC_CLAUDE_MODEL) > template frontmatter > "
        "claude's built-in default.",
    )
    parser.add_argument(
        "--max-thinking-tokens",
        type=int,
        default=None,
        help=f"Thinking token budget. "
        f"Precedence: CLI > env (ASKCC_CLAUDE_MAX_THINKING_TOKENS) > template frontmatter > "
        f"built-in default ({settings.DEFAULT_MAX_THINKING_TOKENS}).",
    )
    parser.add_argument(
        "--disable-thinking",
        action="store_true",
        default=settings.ASKCC_CLAUDE_DISABLE_THINKING,
        help=f"Force-disable extended thinking (default: {settings.ASKCC_CLAUDE_DISABLE_THINKING}). "
        "Env: ASKCC_CLAUDE_DISABLE_THINKING.",
    )
    parser.add_argument(
        "--disable-adaptive-thinking",
        action=argparse.BooleanOptionalAction,
        default=settings.ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING,
        help=f"Disable adaptive reasoning (Opus 4.6, Sonnet 4.6) "
        f"(default: {settings.ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING}). "
        "Env: ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING.",
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

    fixci_parser = subparsers.add_parser("fix-ci", help="Fix failing CI checks on the current branch or PR.")
    fixci_parser.add_argument(
        "-g",
        "--github-issue-url",
        required=False,
        default=None,
        help="GitHub issue URL with a linked PR. If omitted, detects the PR from the current branch.",
    )

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

    if not args.ignore_labels and args.command != "prepare" and args.github_issue_url is not None:
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

    # fix-ci: issue URL is optional — auto-detect PR from current branch if not provided
    if action == AgentAction.FIX_CI and issue_url is None:
        pr_result = subprocess.run(  # noqa: S603
            ["gh", "pr", "view", "--json", "url", "-q", ".url"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
        if pr_result.returncode != 0 or not pr_result.stdout.strip():
            logger.error("No open PR found for the current branch. Provide --github-issue-url or open a PR first.")
            sys.exit(1)
        issue_url = pr_result.stdout.strip()
        ci_context = (
            f"Auto-detected PR: {issue_url}\n\nNo linked issue provided. Use the PR URL above to find CI failures."
        )
        with tempfile.NamedTemporaryFile(mode="w", prefix="askcc_fix-ci_", suffix=".md", delete=False) as f:
            f.write(ci_context)
            ci_tempfile = Path(f.name)
        prompt_tempfiles = [ci_tempfile]
        prompt = Template(config.user_prompt_template).safe_substitute(issue_content_file=str(ci_tempfile))
        logger.info("[%s] Auto-detected PR, using tempfile: %s", issue_url, ci_tempfile)
    else:
        issue_content = fetch_github_issue(issue_url)
        try:
            prompt, prompt_tempfiles = _build_prompt(action, config, issue_content, issue_url)
        except ValueError:
            logger.exception("[%s] Failed to build prompt for '%s'", issue_url, action.value)
            sys.exit(1)
    if args.language != SupportedLanguage.ENGLISH:
        prompt += f"\nOutput all comments in {args.language}."
    runner = get_runner(args.runner)
    effort_level = _resolve_effort(args.effort, config.effort)
    max_thinking_tokens = _resolve_max_thinking_tokens(args.max_thinking_tokens, config.max_thinking_tokens)
    model = _resolve_model(args.model, config.model)
    try:
        try:
            return_code, usage = runner.run(
                prompt,
                config=config,
                issue_url=issue_url,
                cwd=cwd,
                effort_level=effort_level,
                max_thinking_tokens=max_thinking_tokens,
                disable_thinking=args.disable_thinking,
                disable_adaptive_thinking=args.disable_adaptive_thinking,
                model=model,
            )
        except OAuthTokenNotFoundError as e:
            logger.error("[%s] %s", issue_url, e)  # noqa: TRY400
            sys.exit(1)
    finally:
        # Clean up /tmp files created by _build_prompt (not user templates)
        for f in prompt_tempfiles:
            try:
                f.unlink(missing_ok=True)
                logger.debug("Cleaned up prompt tempfile: %s", f)
            except OSError:
                logger.warning("Failed to clean up prompt tempfile: %s", f)

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

    if action == AgentAction.PLAN and return_code == 0:
        transition_issue_to_development(issue_url)

    if action == AgentAction.DEVELOP and return_code == 0:
        verify_result = _run_project_verification(cwd)
        if verify_result.passed:
            transition_issue_to_review(issue_url)
        else:
            logger.warning("Post-develop verification failed: %s", verify_result.message)
            for check in verify_result.checks:
                status = "PASS" if check.passed else "FAIL"
                logger.warning("  [%s] %s: %s", status, check.name, check.message)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
