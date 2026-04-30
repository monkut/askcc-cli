from __future__ import annotations

import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from .settings import CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING, CLAUDE_ENV_DISABLE_THINKING, CLAUDE_ENV_MAX_THINKING_TOKENS

if TYPE_CHECKING:
    from .definitions import AgentConfig

logger = logging.getLogger(__name__)

DEBUG_OUTPUT_MAX_CHARS = 2000

OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105
OAUTH_TOKEN_FILE_ENV = "CLAUDE_OAUTH_TOKEN_FILE"  # noqa: S105
CONVENTIONAL_TOKEN_FILE: Path = Path.home() / ".tokens" / ".claude-oauth-token"
CREDENTIALS_JSON_FILE: Path = Path.home() / ".claude" / ".credentials.json"


class OAuthTokenNotFoundError(RuntimeError):
    """Raised when no Claude OAuth token can be resolved from any source."""


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


def _frontmatter_cli_flags(config: AgentConfig) -> list[str]:
    """Translate AgentConfig frontmatter fields into claude CLI flags."""
    flags: list[str] = []
    if config.model:
        flags.extend(["--model", config.model])
    if config.tools:
        flags.extend(["--allowedTools", ",".join(config.tools)])
    if config.disallowed_tools:
        flags.extend(["--disallowedTools", ",".join(config.disallowed_tools)])
    if config.max_turns is not None:
        flags.extend(["--max-turns", str(config.max_turns)])
    return flags


class ClaudeRunner(Runner):
    """Runs tasks via the Claude Code CLI."""

    def _read_token_file(self, path: Path) -> str | None:
        """Read and strip a token file. Returns None on FileNotFoundError or empty content.

        Logs a WARNING and returns None on PermissionError.
        """
        try:
            content = path.read_text()
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            logger.warning("auth: cannot read token file %s: %s", path, exc)
            return None
        token = content.strip()
        return token or None

    def _read_credentials_json(self, path: Path) -> str | None:
        """Read ``claudeAiOauth.accessToken`` from the Claude Code credentials file.

        Schema observed: ``{"claudeAiOauth": {"accessToken": "..."}}``.
        Returns None if missing/empty; logs a WARNING and returns None on parse or schema errors.
        """
        try:
            content = path.read_text()
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            logger.warning("auth: cannot read credentials file %s: %s", path, exc)
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("auth: failed to parse %s as JSON: %s", path, exc)
            return None
        try:
            token = data["claudeAiOauth"]["accessToken"]
        except (KeyError, TypeError) as exc:
            logger.warning("auth: %s missing claudeAiOauth.accessToken: %s", path, exc)
            return None
        if not isinstance(token, str):
            logger.warning("auth: %s claudeAiOauth.accessToken is not a string", path)
            return None
        token = token.strip()
        return token or None

    def _xdg_token_path(self) -> Path:
        """Compute the XDG-compliant Claude OAuth token path at call time."""
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
        return base / "claude" / "oauth-token"

    def _resolve_oauth_token(self) -> tuple[str, str]:
        """Resolve a Claude OAuth token from the discovery chain.

        Returns ``(token, source_label)`` where ``source_label`` names the source that
        produced the token. Raises :class:`OAuthTokenNotFoundError` when no source
        yields a non-empty token.
        """
        env_token = os.environ.get(OAUTH_TOKEN_ENV, "").strip()
        if env_token:
            return env_token, f"env {OAUTH_TOKEN_ENV}"

        checked: list[str] = [f"env {OAUTH_TOKEN_ENV}"]

        custom_path_str = os.environ.get(OAUTH_TOKEN_FILE_ENV, "").strip()
        if custom_path_str:
            custom_path = Path(custom_path_str).expanduser()
            checked.append(f"env {OAUTH_TOKEN_FILE_ENV}={custom_path}")
            token = self._read_token_file(custom_path)
            if token:
                return token, f"file {custom_path} (via {OAUTH_TOKEN_FILE_ENV})"
        else:
            checked.append(f"env {OAUTH_TOKEN_FILE_ENV} (unset)")

        checked.append(f"file {CONVENTIONAL_TOKEN_FILE}")
        conventional = self._read_token_file(CONVENTIONAL_TOKEN_FILE)
        if conventional:
            return conventional, f"file {CONVENTIONAL_TOKEN_FILE}"

        xdg_path = self._xdg_token_path()
        checked.append(f"file {xdg_path}")
        xdg_token = self._read_token_file(xdg_path)
        if xdg_token:
            return xdg_token, f"file {xdg_path}"

        checked.append(f"file {CREDENTIALS_JSON_FILE}")
        credentials_token = self._read_credentials_json(CREDENTIALS_JSON_FILE)
        if credentials_token:
            return credentials_token, f"file {CREDENTIALS_JSON_FILE}"

        locations = ", ".join(checked)
        msg = f"no Claude credentials found in any of: {locations}"
        raise OAuthTokenNotFoundError(msg)

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
        cmd.extend(_frontmatter_cli_flags(config))

        # Remove CLAUDECODE env var so the child claude process doesn't think it's nested inside Claude Code
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        token, source = self._resolve_oauth_token()
        env[OAUTH_TOKEN_ENV] = token
        if source != f"env {OAUTH_TOKEN_ENV}":
            logger.info("[%s] auth: loaded %s from %s", issue_url, OAUTH_TOKEN_ENV, source)
            if str(CREDENTIALS_JSON_FILE) in source:
                logger.warning(
                    "[%s] auth: %s can be stale (Claude Code refreshes in RAM and may not write back)",
                    issue_url,
                    CREDENTIALS_JSON_FILE,
                )

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
