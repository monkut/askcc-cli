import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from string import Template

from . import __version__
from .definitions import AgentAction, AgentConfig
from .functions import (
    append_usage_to_last_comment,
    bootstrap_templates,
    fetch_github_issue,
    install_skills,
    load_agent_config,
    validate_issue_labels,
)
from .settings import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_PERMISSION_MODE = "acceptEdits"


def _run_claude(prompt: str, config: AgentConfig, *, cwd: Path | None = None) -> tuple[int, dict | None]:
    """Run claude CLI with the given prompt, capturing JSON output for token usage reporting."""
    agent_definition = {config.action_name: {"description": config.description, "prompt": config.system_prompt}}

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--agents",
        json.dumps(agent_definition),
    ]

    logger.info("Requesting '%s' action from Claude Code ...", config.action_name)
    result = subprocess.run(  # noqa: S603
        cmd,
        text=True,
        check=False,
        capture_output=True,
        cwd=cwd,
    )
    logger.info("Claude Code finished (exit code: %d)", result.returncode)

    usage = None
    if result.stdout:
        try:
            data = json.loads(result.stdout)
            response_text = data.get("result", "")
            if response_text:
                print(response_text)  # noqa: T201
            usage = data.get("usage")
            if usage:
                logger.info(
                    "Token usage — input: %s, output: %s",
                    usage.get("input_tokens", "N/A"),
                    usage.get("output_tokens", "N/A"),
                )
        except json.JSONDecodeError:
            logger.warning("Failed to parse Claude JSON output")
            print(result.stdout)  # noqa: T201

    if result.stderr:
        print(result.stderr, file=sys.stderr)  # noqa: T201

    return result.returncode, usage


def main() -> None:
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
        choices=["english", "japanese"],
        default="english",
        help="Language for agent output comments (default: english).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Run Claude in plan mode (read-only analysis).")
    plan_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to plan.")

    develop_parser = subparsers.add_parser("develop", help="Run Claude in development mode.")
    develop_parser.add_argument("-g", "--github-issue-url", required=True, help="GitHub issue URL to develop.")

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
        help="Target directory for skills (defaults to ~/.openclaw/workspace/skills).",
    )

    args = parser.parse_args()

    if args.command == "install":
        install_skills(directory=args.directory)
        return

    bootstrap_templates()

    if not args.ignore_labels:
        label_errors = validate_issue_labels(args.github_issue_url)
        if label_errors:
            for error in label_errors:
                logger.error(error)
            sys.exit(1)

    agent = AgentAction(args.command)
    config = load_agent_config(agent)
    issue_content = fetch_github_issue(args.github_issue_url)
    prompt = Template(config.user_prompt_template).safe_substitute(issue_content=issue_content)
    if args.language == "japanese":
        prompt += "\nOutput all comments in Japanese."
    logger.info("Prompt prepared for '%s' command", agent.value)
    return_code, usage = _run_claude(prompt, config=config, cwd=args.cwd)

    if usage:
        append_usage_to_last_comment(args.github_issue_url, usage)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
