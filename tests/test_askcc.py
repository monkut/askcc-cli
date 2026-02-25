from __future__ import annotations

import json
import subprocess
from string import Template
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from askcc.cli import _run_claude
from askcc.definitions import AGENT_CONFIGS, AgentAction, AgentConfig
from askcc.functions import (
    _parse_issue_url,
    append_usage_to_last_comment,
    bootstrap_templates,
    load_agent_config,
    load_template,
    validate_template,
)


class TestParseIssueUrl:
    def test_valid_issue_url(self):
        owner, repo, issue_number = _parse_issue_url("https://github.com/monkut/askcc-cli/issues/42")
        assert owner == "monkut"
        assert repo == "askcc-cli"
        assert issue_number == 42

    def test_invalid_url_missing_issues_segment(self):
        with pytest.raises(ValueError):
            _parse_issue_url("https://github.com/monkut/askcc-cli/pull/1")

    def test_invalid_url_too_few_parts(self):
        with pytest.raises(ValueError):
            _parse_issue_url("https://github.com/monkut")

    def test_valid_url_with_trailing_slash(self):
        owner, repo, issue_number = _parse_issue_url("https://github.com/monkut/askcc-cli/issues/7/")
        assert owner == "monkut"
        assert repo == "askcc-cli"
        assert issue_number == 7


EXPECTED_TEMPLATE_FILES = {
    "PLAN_SYSTEM_PROMPT.md",
    "PLAN_USER_PROMPT.md",
    "DEVELOP_SYSTEM_PROMPT.md",
    "DEVELOP_USER_PROMPT.md",
    "REVIEW_SYSTEM_PROMPT.md",
    "REVIEW_USER_PROMPT.md",
    "EXPLORE_SYSTEM_PROMPT.md",
    "EXPLORE_USER_PROMPT.md",
    "DIAGNOSE_SYSTEM_PROMPT.md",
    "DIAGNOSE_USER_PROMPT.md",
}


class TestBootstrapTemplates:
    def test_bootstrap_creates_directory_and_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bootstrap_templates()

        assert templates_dir.is_dir()
        actual_files = {f.name for f in templates_dir.iterdir()}
        assert actual_files == EXPECTED_TEMPLATE_FILES

    def test_bootstrap_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bootstrap_templates()
        first_run_contents = {f.name: f.read_text() for f in templates_dir.iterdir()}

        bootstrap_templates()
        second_run_contents = {f.name: f.read_text() for f in templates_dir.iterdir()}

        assert first_run_contents == second_run_contents

    def test_bootstrap_writes_correct_default_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bootstrap_templates()

        plan_config = AGENT_CONFIGS[AgentAction.PLAN]
        assert (templates_dir / "PLAN_SYSTEM_PROMPT.md").read_text() == plan_config.system_prompt
        assert (templates_dir / "PLAN_USER_PROMPT.md").read_text() == plan_config.user_prompt_template

    def test_bootstrap_fills_missing_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        # Create only one file — bootstrap should fill the rest
        (templates_dir / "PLAN_SYSTEM_PROMPT.md").write_text("custom")

        bootstrap_templates()

        actual_files = {f.name for f in templates_dir.iterdir()}
        assert actual_files == EXPECTED_TEMPLATE_FILES
        # The pre-existing file should not be overwritten
        assert (templates_dir / "PLAN_SYSTEM_PROMPT.md").read_text() == "custom"


class TestLoadTemplate:
    def test_reads_custom_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        custom_text = "My custom system prompt"
        (templates_dir / "PLAN_SYSTEM_PROMPT.md").write_text(custom_text)

        result = load_template("PLAN_SYSTEM_PROMPT.md", "fallback")
        assert result == custom_text

    def test_falls_back_on_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        result = load_template("nonexistent.txt", "default value")
        assert result == "default value"


class TestValidateTemplate:
    def test_valid_template_passes(self):
        validate_template("Hello $issue_content", ("issue_content",), "test.md")

    def test_missing_variable_raises(self):
        with pytest.raises(ValueError, match=r"missing required variable '\$issue_content'"):
            validate_template("Hello world, no variable here", ("issue_content",), "test.md")

    def test_no_required_variables_always_passes(self):
        validate_template("Anything goes", (), "test.md")


class TestLoadAgentConfig:
    def test_returns_disk_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bootstrap_templates()

        custom_system = "Custom system prompt for plan"
        (templates_dir / "PLAN_SYSTEM_PROMPT.md").write_text(custom_system)

        config = load_agent_config(AgentAction.PLAN)
        assert config.system_prompt == custom_system
        assert config.action_name == "plan"

    def test_preserves_non_template_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.DEVELOP)
        assert config.action_name == "develop"
        assert config.description == "Develops a planned/defined issue"

    def test_load_review_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.REVIEW)
        assert config.action_name == "review"
        assert config.description == "Reviews a GitHub issue for clarity, completeness, and feasibility"

    def test_load_explore_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.EXPLORE)
        assert config.action_name == "explore"
        assert config.description == "Investigates a GitHub issue and proposes best-practice solutions"

    def test_load_diagnose_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.DIAGNOSE)
        assert config.action_name == "diagnose"
        assert config.description == "Investigates a reported issue and identifies potential causes"

    def test_raises_on_missing_required_variable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        # Overwrite user prompt with text that omits $issue_content
        (templates_dir / "PLAN_USER_PROMPT.md").write_text("No variable here")

        with pytest.raises(ValueError, match="missing required variable"):
            load_agent_config(AgentAction.PLAN)


class TestStringTemplateSubstitution:
    def test_issue_content_substituted(self):
        template_str = "Do the thing.\n\n$issue_content"
        result = Template(template_str).safe_substitute(issue_content="Fix bug #42")
        assert result == "Do the thing.\n\nFix bug #42"

    def test_json_curly_braces_survive(self):
        template_str = "Process this:\n\n$issue_content"
        issue = '{"json": true, "nested": {"key": "value"}}'
        result = Template(template_str).safe_substitute(issue_content=issue)
        assert '{"json": true' in result
        assert "$issue_content" not in result


class TestRunClaude:
    @pytest.fixture
    def agent_config(self) -> AgentConfig:
        return AgentConfig(
            action_name="test-agent",
            description="A test agent",
            system_prompt="You are a test agent.",
            user_prompt_template="$issue_content",
            system_prompt_file="TEST_SYSTEM_PROMPT.md",
            user_prompt_file="TEST_USER_PROMPT.md",
        )

    def test_logs_token_usage(
        self, agent_config: AgentConfig, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ):
        claude_response = json.dumps(
            {
                "result": "Here is the plan.",
                "usage": {"input_tokens": 1500, "output_tokens": 300},
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=claude_response, stderr="")

        with (
            caplog.at_level("INFO", logger="askcc.cli"),
            patch("askcc.cli.subprocess.run", return_value=mock_result) as mock_run,
        ):
            exit_code, usage = _run_claude("test prompt", config=agent_config)

        assert exit_code == 0
        assert usage == {"input_tokens": 1500, "output_tokens": 300}
        captured = capfd.readouterr()
        assert "Here is the plan." in captured.out
        assert "input: 1500" in caplog.text
        assert "output: 300" in caplog.text
        mock_run.assert_called_once()

    def test_handles_invalid_json(
        self, agent_config: AgentConfig, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

        with patch("askcc.cli.subprocess.run", return_value=mock_result):
            exit_code, usage = _run_claude("test prompt", config=agent_config)

        assert exit_code == 0
        assert usage is None
        captured = capfd.readouterr()
        assert "not json" in captured.out
        assert "Failed to parse Claude JSON output" in caplog.text

    def test_returns_nonzero_exit_code(self, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error occurred")

        with patch("askcc.cli.subprocess.run", return_value=mock_result):
            exit_code, usage = _run_claude("test prompt", config=agent_config)

        assert exit_code == 1
        assert usage is None

    def test_returns_none_usage_when_no_usage_key(self, agent_config: AgentConfig):
        claude_response = json.dumps({"result": "Done."})
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=claude_response, stderr="")

        with patch("askcc.cli.subprocess.run", return_value=mock_result):
            exit_code, usage = _run_claude("test prompt", config=agent_config)

        assert exit_code == 0
        assert usage is None


class TestAppendUsageToLastComment:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/42"

    def test_appends_usage_to_last_comment(self):
        comment_json = json.dumps({"id": 123, "body": "Existing comment body"})
        get_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=comment_json, stderr="")
        patch_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", side_effect=[get_result, patch_result]) as mock_run,
        ):
            append_usage_to_last_comment(self.ISSUE_URL, {"input_tokens": 1500, "output_tokens": 300})

        assert mock_run.call_count == 2
        patch_call = mock_run.call_args_list[1]
        body_arg = patch_call[0][0][-1]
        assert ":tokens-used: input: 1500, output: 300" in body_arg

    def test_noop_when_no_comments(self, caplog: pytest.LogCaptureFixture):
        get_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="null", stderr="")

        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=get_result) as mock_run,
        ):
            append_usage_to_last_comment(self.ISSUE_URL, {"input_tokens": 100, "output_tokens": 50})

        mock_run.assert_called_once()
        assert "No comments found" in caplog.text

    def test_preserves_existing_body(self):
        original_body = "Line 1\nLine 2\n\nMore text"
        comment_json = json.dumps({"id": 456, "body": original_body})
        get_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=comment_json, stderr="")
        patch_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", side_effect=[get_result, patch_result]) as mock_run,
        ):
            append_usage_to_last_comment(self.ISSUE_URL, {"input_tokens": 200, "output_tokens": 100})

        patch_call = mock_run.call_args_list[1]
        body_arg = patch_call[0][0][-1]
        expected = f"body={original_body}\n\n:tokens-used: input: 200, output: 100"
        assert body_arg == expected
