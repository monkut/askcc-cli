import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from string import Template

from . import __version__
from .definitions import AgentConfig, AgentType
from .functions import bootstrap_templates, fetch_github_issue, install_skills, load_agent_config
from .settings import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_PERMISSION_MODE = "acceptEdits"


def _run_claude(prompt: str, config: AgentConfig, *, cwd: Path | None = None) -> int:
    """Run claude CLI with the given prompt, streaming output to stdout/stderr."""
    agent_definition = {config.agent_name: {"description": config.description, "prompt": config.system_prompt}}

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
        "--agents",
        json.dumps(agent_definition),
    ]

    logger.info("Requesting '%s' from Claude Code ...", config.agent_name)
    result = subprocess.run(  # noqa: S603
        cmd,
        text=True,
        check=False,
        cwd=cwd,
    )
    logger.info("Claude Code finished (exit code: %d)", result.returncode)
    return result.returncode


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

    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Run Claude in plan mode (read-only analysis).")
    plan_parser.add_argument("--github-issue-url", required=True, help="GitHub issue URL to plan.")

    develop_parser = subparsers.add_parser("develop", help="Run Claude in development mode.")
    develop_parser.add_argument("--github-issue-url", required=True, help="GitHub issue URL to develop.")

    review_parser = subparsers.add_parser("review", help="Run Claude in review mode (issue quality review).")
    review_parser.add_argument("--github-issue-url", required=True, help="GitHub issue URL to review.")

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

    agent = AgentType(args.command)
    config = load_agent_config(agent)
    issue_content = fetch_github_issue(args.github_issue_url)
    prompt = Template(config.user_prompt_template).safe_substitute(issue_content=issue_content)
    logger.info("Prompt prepared for '%s' command", agent.value)
    return_code = _run_claude(prompt, config=config, cwd=args.cwd)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
