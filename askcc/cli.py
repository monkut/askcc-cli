import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .definitions import AGENT_CONFIGS, AgentType
from .functions import fetch_github_issue
from .settings import configure_logging

DEFAULT_PERMISSION_MODE = "acceptEdits"


def _run_claude(prompt: str, agent: AgentType = AgentType.PLAN, *, cwd: Path | None = None) -> int:
    """Run claude CLI with the given prompt, streaming output to stdout/stderr."""
    config = AGENT_CONFIGS[agent]
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

    result = subprocess.run(  # noqa: S603
        cmd,
        text=True,
        check=False,
        cwd=cwd,
    )
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

    args = parser.parse_args()

    agent = AgentType(args.command)
    config = AGENT_CONFIGS[agent]
    issue_content = fetch_github_issue(args.github_issue_url)
    prompt = config.user_prompt_template.format(issue_content=issue_content)
    return_code = _run_claude(prompt, agent=agent, cwd=args.cwd)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
