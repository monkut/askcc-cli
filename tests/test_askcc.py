from __future__ import annotations

from string import Template
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from askcc.definitions import AGENT_CONFIGS, AgentType
from askcc.functions import _parse_issue_url, bootstrap_templates, load_agent_config, load_template


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


class TestBootstrapTemplates:
    def test_bootstrap_creates_directory_and_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bootstrap_templates()

        assert templates_dir.is_dir()
        expected_files = {"plan_system.txt", "plan_user.txt", "develop_system.txt", "develop_user.txt"}
        actual_files = {f.name for f in templates_dir.iterdir()}
        assert actual_files == expected_files

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

        plan_config = AGENT_CONFIGS[AgentType.PLAN]
        assert (templates_dir / "plan_system.txt").read_text() == plan_config.system_prompt
        assert (templates_dir / "plan_user.txt").read_text() == plan_config.user_prompt_template


class TestLoadTemplate:
    def test_reads_custom_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        custom_text = "My custom system prompt"
        (templates_dir / "plan_system.txt").write_text(custom_text)

        result = load_template("plan_system.txt", "fallback")
        assert result == custom_text

    def test_falls_back_on_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        result = load_template("nonexistent.txt", "default value")
        assert result == "default value"


class TestLoadAgentConfig:
    def test_returns_disk_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bootstrap_templates()

        custom_system = "Custom system prompt for plan"
        (templates_dir / "plan_system.txt").write_text(custom_system)

        config = load_agent_config(AgentType.PLAN)
        assert config.system_prompt == custom_system
        assert config.agent_name == "planner"

    def test_preserves_non_template_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentType.DEVELOP)
        assert config.agent_name == "developer"
        assert config.description == "Develops a planned/defined issue"


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
