from __future__ import annotations

import json
import subprocess
from pathlib import Path
from string import Template
from unittest.mock import patch

import pytest

from askcc.cli import _run_claude, main
from askcc.definitions import AGENT_CONFIGS, AgentAction, AgentConfig, SupportedLanguage
from askcc.functions import (
    _add_issue_label,
    _find_linked_pr_number,
    _find_option_id,
    _has_acceptance_criteria,
    _has_dependencies_section,
    _parse_issue_url,
    _swap_issue_labels,
    _transition_project_fields,
    append_usage_to_last_comment,
    bootstrap_templates,
    fetch_pr_content,
    install_skills,
    load_agent_config,
    load_template,
    transition_issue_to_planning,
    transition_issue_to_review,
    validate_issue_readiness,
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
    "PREPARE_SYSTEM_PROMPT.md",
    "PREPARE_USER_PROMPT.md",
    "PLAN_SYSTEM_PROMPT.md",
    "PLAN_USER_PROMPT.md",
    "DEVELOP_SYSTEM_PROMPT.md",
    "DEVELOP_USER_PROMPT.md",
    "REVIEW_SYSTEM_PROMPT.md",
    "REVIEW_USER_PROMPT.md",
    "REVIEWPR_SYSTEM_PROMPT.md",
    "REVIEWPR_USER_PROMPT.md",
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
                "model": "claude-sonnet-4-6-20250514",
                "usage": {"input_tokens": 1500, "output_tokens": 300},
            }
        )
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=claude_response, stderr="")

        with (
            caplog.at_level("INFO", logger="askcc.cli"),
            patch("askcc.cli.subprocess.run", return_value=mock_result) as mock_run,
        ):
            exit_code, usage = _run_claude("test prompt", config=agent_config, issue_id="test/repo#1", cwd=Path.cwd())

        assert exit_code == 0
        assert usage == {"input_tokens": 1500, "output_tokens": 300, "model": "claude-sonnet-4-6-20250514"}
        captured = capfd.readouterr()
        assert "Here is the plan." in captured.out
        assert "model: claude-sonnet-4-6-20250514" in caplog.text
        assert "input: 1500" in caplog.text
        assert "output: 300" in caplog.text
        mock_run.assert_called_once()

    def test_handles_invalid_json(
        self, agent_config: AgentConfig, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

        with patch("askcc.cli.subprocess.run", return_value=mock_result):
            exit_code, usage = _run_claude("test prompt", config=agent_config, issue_id="test/repo#1", cwd=Path.cwd())

        assert exit_code == 0
        assert usage is None
        captured = capfd.readouterr()
        assert "not json" in captured.out
        assert "Failed to parse Claude JSON output" in caplog.text

    def test_returns_nonzero_exit_code(self, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error occurred")

        with patch("askcc.cli.subprocess.run", return_value=mock_result):
            exit_code, usage = _run_claude("test prompt", config=agent_config, issue_id="test/repo#1", cwd=Path.cwd())

        assert exit_code == 1
        assert usage is None

    def test_returns_none_usage_when_no_usage_key(self, agent_config: AgentConfig):
        claude_response = json.dumps({"result": "Done."})
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=claude_response, stderr="")

        with patch("askcc.cli.subprocess.run", return_value=mock_result):
            exit_code, usage = _run_claude("test prompt", config=agent_config, issue_id="test/repo#1", cwd=Path.cwd())

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
            append_usage_to_last_comment(
                self.ISSUE_URL, {"model": "claude-sonnet-4-6-20250514", "input_tokens": 1500, "output_tokens": 300}
            )

        assert mock_run.call_count == 2
        patch_call = mock_run.call_args_list[1]
        body_arg = patch_call[0][0][-1]
        assert ":tokens-used: model: claude-sonnet-4-6-20250514, input: 1500, output: 300" in body_arg

    def test_noop_when_no_comments(self, caplog: pytest.LogCaptureFixture):
        get_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="null", stderr="")

        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=get_result) as mock_run,
        ):
            append_usage_to_last_comment(
                self.ISSUE_URL, {"model": "claude-sonnet-4-6-20250514", "input_tokens": 100, "output_tokens": 50}
            )

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
            append_usage_to_last_comment(
                self.ISSUE_URL, {"model": "claude-sonnet-4-6-20250514", "input_tokens": 200, "output_tokens": 100}
            )

        patch_call = mock_run.call_args_list[1]
        body_arg = patch_call[0][0][-1]
        expected = f"body={original_body}\n\n:tokens-used: model: claude-sonnet-4-6-20250514, input: 200, output: 100"
        assert body_arg == expected


class TestLanguageOption:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def _run_main(self, extra_args: list[str]) -> str:
        """Run main() with given args and return the prompt passed to _run_claude."""
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(0, None)) as mock_claude,
            patch("sys.argv", ["askcc", *extra_args, AgentAction.PLAN, "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()
        return mock_claude.call_args[0][0]

    def test_japanese_appends_instruction(self):
        prompt = self._run_main(["--language", SupportedLanguage.JAPANESE])
        assert prompt.endswith(f"\nOutput all comments in {SupportedLanguage.JAPANESE}.")

    def test_english_no_append(self):
        prompt = self._run_main(["--language", SupportedLanguage.ENGLISH])
        assert "Output all comments in" not in prompt

    def test_default_no_append(self):
        prompt = self._run_main([])
        assert "Output all comments in" not in prompt


class TestInstallSkills:
    def test_explicit_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        target = tmp_path / "custom-skills"
        target.mkdir()
        install_skills(directory=target)

        installed = [d.name for d in target.iterdir() if d.is_dir()]
        assert "request-askcc" in installed

    def test_auto_detect_claude_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        monkeypatch.setattr("askcc.functions.CLAUDE_HOME", claude_home)
        monkeypatch.setattr("askcc.functions.CLAUDE_SKILLS_DIR", claude_home / "skills")
        monkeypatch.setattr("askcc.functions.OPENCLAW_HOME", tmp_path / ".openclaw")

        install_skills()

        installed = [d.name for d in (claude_home / "skills").iterdir() if d.is_dir()]
        assert "request-askcc" in installed

    def test_auto_detect_openclaw_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        openclaw_home = tmp_path / ".openclaw"
        openclaw_home.mkdir()
        config_path = openclaw_home / "openclaw.json"
        config_path.write_text("{}")
        skills_dir = openclaw_home / "workspace" / "skills"

        monkeypatch.setattr("askcc.functions.CLAUDE_HOME", tmp_path / ".claude")
        monkeypatch.setattr("askcc.functions.OPENCLAW_HOME", openclaw_home)
        monkeypatch.setattr("askcc.functions.OPENCLAW_SKILLS_DIR", skills_dir)
        monkeypatch.setattr("askcc.functions.OPENCLAW_CONFIG_PATH", config_path)

        install_skills()

        installed = [d.name for d in skills_dir.iterdir() if d.is_dir()]
        assert "request-askcc" in installed
        config = json.loads(config_path.read_text())
        assert config["skills"]["entries"]["request-askcc"]["enabled"] is True

    def test_auto_detect_both(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        openclaw_home = tmp_path / ".openclaw"
        openclaw_home.mkdir()
        config_path = openclaw_home / "openclaw.json"
        config_path.write_text("{}")
        skills_dir = openclaw_home / "workspace" / "skills"

        monkeypatch.setattr("askcc.functions.CLAUDE_HOME", claude_home)
        monkeypatch.setattr("askcc.functions.CLAUDE_SKILLS_DIR", claude_home / "skills")
        monkeypatch.setattr("askcc.functions.OPENCLAW_HOME", openclaw_home)
        monkeypatch.setattr("askcc.functions.OPENCLAW_SKILLS_DIR", skills_dir)
        monkeypatch.setattr("askcc.functions.OPENCLAW_CONFIG_PATH", config_path)

        install_skills()

        assert (claude_home / "skills" / "request-askcc").is_dir()
        assert (skills_dir / "request-askcc").is_dir()

    def test_auto_detect_neither(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setattr("askcc.functions.CLAUDE_HOME", tmp_path / ".claude")
        monkeypatch.setattr("askcc.functions.OPENCLAW_HOME", tmp_path / ".openclaw")

        with caplog.at_level("WARNING", logger="askcc.functions"):
            install_skills()

        assert caplog.text.count("Target directory not found") == 2


class TestHasAcceptanceCriteria:
    def test_with_heading_and_checklist(self):
        body = "## Summary\nSome text\n\n## Acceptance Criteria\n- [ ] First item\n- [ ] Second item\n"
        assert _has_acceptance_criteria(body) is True

    def test_with_checked_items(self):
        body = "## Acceptance Criteria\n- [x] Done item\n- [ ] Pending item\n"
        assert _has_acceptance_criteria(body) is True

    def test_missing_heading(self):
        body = "## Summary\nSome text\n- [ ] A checklist item\n"
        assert _has_acceptance_criteria(body) is False

    def test_heading_without_checklist(self):
        body = "## Acceptance Criteria\nJust plain text, no checklist.\n"
        assert _has_acceptance_criteria(body) is False

    def test_empty_body(self):
        assert _has_acceptance_criteria("") is False

    def test_checklist_in_different_section(self):
        body = "## Acceptance Criteria\nNo checklist here.\n\n## Other\n- [ ] Wrong section\n"
        assert _has_acceptance_criteria(body) is False

    def test_h3_heading(self):
        body = "### Acceptance Criteria\n- [ ] Works with h3\n"
        assert _has_acceptance_criteria(body) is True


class TestHasDependenciesSection:
    def test_dependencies_heading(self):
        assert _has_dependencies_section("## Dependencies\n- None\n") is True

    def test_context_heading(self):
        assert _has_dependencies_section("## Context\nSee docs.\n") is True

    def test_prerequisites_heading(self):
        assert _has_dependencies_section("## Prerequisites\n- Python 3.14\n") is True

    def test_blockers_heading(self):
        assert _has_dependencies_section("## Blockers\n- Waiting on API\n") is True

    def test_no_matching_heading(self):
        assert _has_dependencies_section("## Summary\nSome text\n") is False

    def test_empty_body(self):
        assert _has_dependencies_section("") is False


class TestValidateIssueReadiness:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/33"

    def _make_issue_json(
        self,
        *,
        body: str = "",
        assignees: list[dict] | None = None,
        labels: list[dict] | None = None,
    ) -> str:
        return json.dumps(
            {
                "body": body,
                "assignees": assignees or [],
                "labels": labels or [],
            }
        )

    def test_all_checks_pass(self):
        body = "## Acceptance Criteria\n- [ ] Item\n\n## Context\nSee docs.\n"
        issue_json = self._make_issue_json(
            body=body,
            assignees=[{"login": "dev1"}],
            labels=[{"name": "action:develop"}],
        )
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
        ):
            checks = validate_issue_readiness(self.ISSUE_URL)

        assert all(c.passed for c in checks)
        assert len(checks) == 4

    def test_all_checks_fail(self):
        issue_json = self._make_issue_json(
            body="## Summary\nNo criteria here.\n",
            assignees=[],
            labels=[{"name": "needs:decision"}, {"name": "blocked"}],
        )
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
        ):
            checks = validate_issue_readiness(self.ISSUE_URL)

        assert not any(c.passed for c in checks)
        assert len(checks) == 4

    def test_blocking_label_detected(self):
        issue_json = self._make_issue_json(
            body="## Acceptance Criteria\n- [ ] Item\n\n## Context\nSee docs.\n",
            assignees=[{"login": "dev1"}],
            labels=[{"name": "needs:decision"}],
        )
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
        ):
            checks = validate_issue_readiness(self.ISSUE_URL)

        blocking_check = next(c for c in checks if c.name == "No blocking labels")
        assert blocking_check.passed is False
        assert "needs:decision" in blocking_check.message

    def test_null_body_handled(self):
        issue_json = json.dumps({"body": None, "assignees": [], "labels": []})
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
        ):
            checks = validate_issue_readiness(self.ISSUE_URL)

        assert len(checks) == 4
        assert not checks[0].passed  # acceptance criteria
        assert not checks[1].passed  # dependencies


class TestValidateCommand:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/33"

    def test_validate_exits_zero_on_pass(self, capfd: pytest.CaptureFixture[str]):
        body = "## Acceptance Criteria\n- [ ] Item\n\n## Context\nSee docs.\n"
        issue_json = json.dumps(
            {
                "body": body,
                "assignees": [{"login": "dev1"}],
                "labels": [],
            }
        )
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
            patch("sys.argv", ["askcc", "validate", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit, match="0"),
        ):
            main()

        captured = capfd.readouterr()
        assert "Result: PASS" in captured.out

    def test_validate_exits_one_on_fail(self, capfd: pytest.CaptureFixture[str]):
        issue_json = json.dumps({"body": "", "assignees": [], "labels": []})
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
            patch("sys.argv", ["askcc", "validate", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

        captured = capfd.readouterr()
        assert "Result: FAIL" in captured.out


class TestDevelopSkipValidation:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_develop_fails_on_validation_failure(self, capfd: pytest.CaptureFixture[str]):
        """Develop exits 1 when readiness validation fails."""
        issue_json = json.dumps({"body": "", "assignees": [], "labels": []})
        gh_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=issue_json, stderr="")

        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=gh_result),
            patch("sys.argv", ["askcc", "develop", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

        captured = capfd.readouterr()
        assert "Result: FAIL" in captured.out

    def test_develop_skip_validation_bypasses_check(self):
        """Develop --skip-validation skips readiness validation and runs Claude."""
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness") as mock_validate,
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(0, None)),
            patch("askcc.cli.transition_issue_to_review"),
            patch("sys.argv", ["askcc", "develop", "--skip-validation", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_validate.assert_not_called()


class TestFindOptionId:
    def test_finds_matching_option(self):
        options = [{"id": "opt1", "name": "Todo"}, {"id": "opt2", "name": "in-review"}]
        assert _find_option_id(options, ("in-internal-review", "in-review")) == "opt2"

    def test_case_insensitive(self):
        options = [{"id": "opt1", "name": "In-Review"}]
        assert _find_option_id(options, ("in-review",)) == "opt1"

    def test_returns_first_match(self):
        options = [
            {"id": "opt1", "name": "in-internal-review"},
            {"id": "opt2", "name": "in-review"},
        ]
        assert _find_option_id(options, ("in-internal-review", "in-review")) == "opt1"

    def test_no_match_returns_none(self):
        options = [{"id": "opt1", "name": "Todo"}, {"id": "opt2", "name": "Done"}]
        assert _find_option_id(options, ("in-review",)) is None

    def test_empty_options(self):
        assert _find_option_id([], ("in-review",)) is None


class TestSwapIssueLabels:
    def test_success(self, caplog: pytest.LogCaptureFixture):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            caplog.at_level("INFO", logger="askcc.functions"),
            patch("askcc.functions.subprocess.run", return_value=ok) as mock_run,
        ):
            _swap_issue_labels("/usr/bin/gh", "owner/repo", 42, remove="action:develop", add="action:review")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd
        assert "--add-label" in cmd
        assert "Transitioned labels" in caplog.text

    def test_failure_warns(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch(
                "askcc.functions.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh", stderr="label not found"),
            ),
        ):
            _swap_issue_labels("/usr/bin/gh", "owner/repo", 42, remove="action:develop", add="action:review")

        assert "Failed to transition labels" in caplog.text


class TestTransitionProjectFields:
    def _graphql_response(self, nodes: list[dict]) -> str:
        return json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}})

    def test_no_project_items(self, caplog: pytest.LogCaptureFixture):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=self._graphql_response([]), stderr="")
        with (
            caplog.at_level("INFO", logger="askcc.functions"),
            patch("askcc.functions.subprocess.run", return_value=result),
        ):
            _transition_project_fields("/usr/bin/gh", "owner", "repo", 42)

        assert "not in any project" in caplog.text

    def test_updates_status_and_action_fields(self, caplog: pytest.LogCaptureFixture):
        nodes = [
            {
                "id": "item-1",
                "project": {
                    "id": "proj-1",
                    "title": "My Board",
                    "statusField": {
                        "id": "field-status",
                        "options": [
                            {"id": "opt-todo", "name": "Todo"},
                            {"id": "opt-review", "name": "in-review"},
                        ],
                    },
                    "actionField": {
                        "id": "field-action",
                        "options": [
                            {"id": "opt-dev", "name": "DEVELOPER"},
                            {"id": "opt-rev", "name": "REVIEWER"},
                        ],
                    },
                },
            }
        ]
        query_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._graphql_response(nodes), stderr=""
        )
        mutation_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with (
            caplog.at_level("INFO", logger="askcc.functions"),
            patch("askcc.functions.subprocess.run", side_effect=[query_result, mutation_result, mutation_result]),
        ):
            _transition_project_fields("/usr/bin/gh", "owner", "repo", 42)

        assert "Updated 'Status' in project 'My Board'" in caplog.text
        assert "Updated 'Needs Action From' in project 'My Board'" in caplog.text

    def test_skips_missing_fields(self, caplog: pytest.LogCaptureFixture):
        nodes = [
            {
                "id": "item-1",
                "project": {
                    "id": "proj-1",
                    "title": "Simple Board",
                    "statusField": None,
                    "actionField": None,
                },
            }
        ]
        query_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._graphql_response(nodes), stderr=""
        )
        with (
            caplog.at_level("INFO", logger="askcc.functions"),
            patch("askcc.functions.subprocess.run", return_value=query_result) as mock_run,
        ):
            _transition_project_fields("/usr/bin/gh", "owner", "repo", 42)

        # Only the query call, no mutation calls
        mock_run.assert_called_once()

    def test_skips_unmatched_status_options(self):
        nodes = [
            {
                "id": "item-1",
                "project": {
                    "id": "proj-1",
                    "title": "Board",
                    "statusField": {
                        "id": "field-status",
                        "options": [{"id": "opt-todo", "name": "Todo"}, {"id": "opt-done", "name": "Done"}],
                    },
                    "actionField": None,
                },
            }
        ]
        query_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._graphql_response(nodes), stderr=""
        )
        with patch("askcc.functions.subprocess.run", return_value=query_result) as mock_run:
            _transition_project_fields("/usr/bin/gh", "owner", "repo", 42)

        mock_run.assert_called_once()

    def test_query_failure_warns(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch(
                "askcc.functions.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh", stderr="graphql error"),
            ),
        ):
            _transition_project_fields("/usr/bin/gh", "owner", "repo", 42)

        assert "Failed to query project items" in caplog.text


class TestTransitionIssueToReview:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/42"

    def test_calls_label_swap_and_project_transition(self):
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions._swap_issue_labels") as mock_swap,
            patch("askcc.functions._transition_project_fields") as mock_project,
        ):
            transition_issue_to_review(self.ISSUE_URL)

        mock_swap.assert_called_once_with(
            "/usr/bin/gh", "monkut/askcc-cli", 42, remove="action:develop", add="action:review"
        )
        mock_project.assert_called_once_with("/usr/bin/gh", "monkut", "askcc-cli", 42)


class TestDevelopTransitionIntegration:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_develop_success_triggers_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(0, None)),
            patch("askcc.cli.transition_issue_to_review") as mock_transition,
            patch("sys.argv", ["askcc", "develop", "--skip-validation", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_called_once_with(self.ISSUE_URL)

    def test_develop_failure_skips_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(1, None)),
            patch("askcc.cli.transition_issue_to_review") as mock_transition,
            patch("sys.argv", ["askcc", "develop", "--skip-validation", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_not_called()

    def test_plan_success_skips_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(0, None)),
            patch("askcc.cli.transition_issue_to_review") as mock_transition,
            patch("sys.argv", ["askcc", "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_not_called()


class TestLoadPrepareConfig:
    def test_load_prepare_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.PREPARE)
        assert config.action_name == "prepare"
        assert config.description == "Analyzes a backlog issue for development readiness and suggests improvements"


class TestAddIssueLabel:
    def test_success(self, caplog: pytest.LogCaptureFixture):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            caplog.at_level("INFO", logger="askcc.functions"),
            patch("askcc.functions.subprocess.run", return_value=ok) as mock_run,
        ):
            _add_issue_label("/usr/bin/gh", "owner/repo", 42, "action:develop")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd
        assert "action:develop" in cmd
        assert "Added label" in caplog.text

    def test_failure_warns(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch(
                "askcc.functions.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh", stderr="label not found"),
            ),
        ):
            _add_issue_label("/usr/bin/gh", "owner/repo", 42, "action:develop")

        assert "Failed to add label" in caplog.text


class TestTransitionIssueToPlanningIntegration:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/42"

    def test_calls_add_label_and_project_transition(self):
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions._add_issue_label") as mock_add,
            patch("askcc.functions._transition_project_fields") as mock_project,
        ):
            transition_issue_to_planning(self.ISSUE_URL)

        mock_add.assert_called_once_with("/usr/bin/gh", "monkut/askcc-cli", 42, "action:develop")
        mock_project.assert_called_once_with(
            "/usr/bin/gh",
            "monkut",
            "askcc-cli",
            42,
            status_options=("planning",),
            action_field_value="PLANNER",
            action_field_name="Needs Action From",
        )


class TestPrepareCommand:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_prepare_skips_label_validation(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels") as mock_labels,
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(0, None)),
            patch("askcc.cli.transition_issue_to_planning"),
            patch("sys.argv", ["askcc", "prepare", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_labels.assert_not_called()

    def test_prepare_success_triggers_planning_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(0, None)),
            patch("askcc.cli.transition_issue_to_planning") as mock_transition,
            patch("sys.argv", ["askcc", "prepare", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_called_once_with(self.ISSUE_URL)

    def test_prepare_failure_skips_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli._run_claude", return_value=(1, None)),
            patch("askcc.cli.transition_issue_to_planning") as mock_transition,
            patch("sys.argv", ["askcc", "prepare", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_not_called()


class TestFindLinkedPrNumber:
    def test_matches_branch_convention(self):
        prs = json.dumps(
            [
                {"number": 10, "headRefName": "feature/42-add-feature", "body": "unrelated"},
                {"number": 11, "headRefName": "main", "body": ""},
            ]
        )
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) == 10

    def test_matches_add_prefix(self):
        prs = json.dumps([{"number": 5, "headRefName": "add/42-new-thing", "body": ""}])
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) == 5

    def test_falls_back_to_body_reference(self):
        prs = json.dumps([{"number": 7, "headRefName": "my-branch", "body": "Closes #42"}])
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) == 7

    def test_matches_issue_url_in_body(self):
        prs = json.dumps(
            [
                {"number": 8, "headRefName": "topic", "body": "See /issues/42 for details"},
            ]
        )
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) == 8

    def test_prefers_branch_over_body(self):
        prs = json.dumps(
            [
                {"number": 1, "headRefName": "other", "body": "Fixes #42"},
                {"number": 2, "headRefName": "feature/42-impl", "body": ""},
            ]
        )
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) == 2

    def test_returns_none_when_no_match(self):
        prs = json.dumps([{"number": 1, "headRefName": "main", "body": "unrelated changes"}])
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) is None

    def test_returns_none_on_empty_list(self):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) is None

    def test_returns_none_on_subprocess_error(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch(
                "askcc.functions.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh", stderr="api error"),
            ),
        ):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) is None
        assert "Failed to list PRs" in caplog.text

    def test_null_body_handled(self):
        prs = json.dumps([{"number": 3, "headRefName": "feature/42-fix", "body": None}])
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with patch("askcc.functions.subprocess.run", return_value=result):
            assert _find_linked_pr_number("/usr/bin/gh", "owner", "repo", 42) == 3


class TestFetchPrContent:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/42"

    def _make_pr_responses(
        self,
        *,
        pr_number: int = 10,
        pr_title: str = "Add feature",
        pr_body: str = "Implements #42",
        pr_url: str = "https://github.com/monkut/askcc-cli/pull/10",
        diff: str = "diff --git a/file.py\n+new line",
        review_comments: list[dict] | None = None,
    ) -> list[subprocess.CompletedProcess]:
        # PR list (for _find_linked_pr_number)
        prs = json.dumps([{"number": pr_number, "headRefName": "feature/42-add", "body": pr_body}])
        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")

        # PR metadata
        pr_data = json.dumps({"title": pr_title, "body": pr_body, "html_url": pr_url})
        meta_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=pr_data, stderr="")

        # PR diff
        diff_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=diff, stderr="")

        # PR review comments
        comments = json.dumps(review_comments or [])
        comments_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=comments, stderr="")

        return [list_result, meta_result, diff_result, comments_result]

    def test_fetches_pr_content(self):
        responses = self._make_pr_responses()
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", side_effect=responses),
        ):
            content = fetch_pr_content(self.ISSUE_URL)

        assert "Pull Request #10" in content
        assert "Add feature" in content
        assert "diff --git" in content

    def test_includes_review_comments(self):
        comments = [{"user": {"login": "reviewer1"}, "path": "file.py", "body": "Looks good"}]
        responses = self._make_pr_responses(review_comments=comments)
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", side_effect=responses),
        ):
            content = fetch_pr_content(self.ISSUE_URL)

        assert "Review comment by @reviewer1" in content
        assert "Looks good" in content

    def test_raises_when_no_linked_pr(self):
        prs = json.dumps([])
        list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=prs, stderr="")
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", return_value=list_result),
            pytest.raises(ValueError, match="No linked pull request found"),
        ):
            fetch_pr_content(self.ISSUE_URL)


class TestLoadReviewprConfig:
    def test_load_reviewpr_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.REVIEWPR)
        assert config.action_name == "reviewpr"
        assert config.description == "Reviews a pull request against its linked issue's Definition of Done"
        assert config.required_variables == ("issue_content", "pr_content")


class TestReviewprCommand:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_reviewpr_fetches_pr_content(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.fetch_pr_content", return_value="pr diff content") as mock_pr,
            patch("askcc.cli._run_claude", return_value=(0, None)) as mock_claude,
            patch("sys.argv", ["askcc", "reviewpr", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_pr.assert_called_once_with(self.ISSUE_URL)
        prompt = mock_claude.call_args[0][0]
        assert "issue body" in prompt
        assert "pr diff content" in prompt

    def test_reviewpr_exits_on_no_linked_pr(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("ERROR", logger="askcc.cli"),
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.fetch_pr_content", side_effect=ValueError("No linked pull request found")),
            patch("sys.argv", ["askcc", "reviewpr", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

        assert "Failed to build prompt" in caplog.text

    def test_reviewpr_no_transition_on_success(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.fetch_pr_content", return_value="pr content"),
            patch("askcc.cli._run_claude", return_value=(0, None)),
            patch("askcc.cli.transition_issue_to_review") as mock_review,
            patch("askcc.cli.transition_issue_to_planning") as mock_planning,
            patch("sys.argv", ["askcc", "reviewpr", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_review.assert_not_called()
        mock_planning.assert_not_called()
