from __future__ import annotations

import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .settings import CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING, CLAUDE_ENV_DISABLE_THINKING, CLAUDE_ENV_MAX_THINKING_TOKENS

if TYPE_CHECKING:
    from pathlib import Path

    from .definitions import AgentConfig

logger = logging.getLogger(__name__)

DEBUG_OUTPUT_MAX_CHARS = 2000


class Runner(ABC):
    """Base class for task runners."""

    @abstractmethod
    def run(
        self,
        prompt: str,
        config: AgentConfig,
        *,
        issue_url: str,
        cwd: Path,
        effort_level: str | None = None,
        max_thinking_tokens: int | None = None,
        disable_thinking: bool = False,
        disable_adaptive_thinking: bool = False,
    ) -> tuple[int, dict | None]:
        """Execute a prompt and return (exit_code, usage_dict_or_none)."""


class ClaudeRunner(Runner):
    """Runs tasks via the Claude Code CLI."""

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        *,
        issue_url: str,
        cwd: Path,
        effort_level: str | None = None,
        max_thinking_tokens: int | None = None,
        disable_thinking: bool = False,
        disable_adaptive_thinking: bool = False,
    ) -> tuple[int, dict | None]:
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

        if effort_level:
            cmd.extend(["--effort", effort_level])

        # Remove CLAUDECODE env var so the child claude process doesn't think it's nested inside Claude Code
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        if max_thinking_tokens is not None:
            env[CLAUDE_ENV_MAX_THINKING_TOKENS] = str(max_thinking_tokens)
        if disable_thinking:
            env[CLAUDE_ENV_DISABLE_THINKING] = "1"
        if disable_adaptive_thinking:
            env[CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING] = "1"

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


RUNNER_REGISTRY: dict[str, type[Runner]] = {
    "claude": ClaudeRunner,
}

DEFAULT_RUNNER = "claude"


def get_runner(name: str) -> Runner:
    """Instantiate a runner by name."""
    cls = RUNNER_REGISTRY.get(name)
    if cls is None:
        msg = f"Unknown runner: {name!r}. Available: {', '.join(RUNNER_REGISTRY)}"
        raise ValueError(msg)
    return cls()
