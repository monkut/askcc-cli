from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from string import Template
from unittest.mock import MagicMock, patch

import pytest

from askcc.cli import main
from askcc.definitions import (
    AGENT_CONFIGS,
    DEVELOP_AGENT_PROMPT,
    FIXCI_AGENT_PROMPT,
    REVIEWPR_AGENT_PROMPT,
    AgentAction,
    AgentConfig,
)
from askcc.functions import (
    CheckResult,
    VerificationResult,
    _add_issue_label,
    _detect_verification_commands,
    _find_linked_pr_number,
    _find_option_id,
    _has_acceptance_criteria,
    _has_dependencies_section,
    _parse_issue_url,
    _run_project_verification,
    _swap_issue_labels,
    _transition_project_fields,
    append_usage_to_last_comment,
    bootstrap_templates,
    fetch_pr_content,
    install_skills,
    load_agent_config,
    load_template,
    parse_frontmatter,
    transition_issue_to_development,
    transition_issue_to_planning,
    transition_issue_to_review,
    validate_issue_readiness,
    validate_template,
    write_prompt_content,
)
from askcc.runners import (
    OAUTH_TOKEN_ENV,
    OAUTH_TOKEN_FILE_ENV,
    ClaudeRunner,
    OAuthTokenNotFoundError,
    _read_credentials_json,
    _read_token_file,
    _resolve_oauth_token,
    get_runner,
)
from askcc.settings import (
    ASKCC_HOME,
    CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING,
    CLAUDE_ENV_DISABLE_THINKING,
    CLAUDE_ENV_MAX_THINKING_TOKENS,
    DEFAULT_EFFORT_LEVEL,
    DEFAULT_MAX_THINKING_TOKENS,
    USER_CONFIG_PATH,
    VALID_EFFORT_LEVELS,
    SupportedLanguage,
    _load_user_config,
    _resolve_default_language,
    _resolve_effort_level,
)


def _mock_runner(return_code: int = 0, usage: dict | None = None) -> MagicMock:
    """Create a mock runner with the given return values."""
    return MagicMock(run=MagicMock(return_value=(return_code, usage)))


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
    "FIXCI_SYSTEM_PROMPT.md",
    "FIXCI_USER_PROMPT.md",
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
        validate_template("Hello $issue_content_file", ("issue_content_file",), "test.md")

    def test_missing_variable_raises(self):
        with pytest.raises(ValueError, match=r"missing required variable '\$issue_content_file'"):
            validate_template("Hello world, no variable here", ("issue_content_file",), "test.md")

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
        assert config.action_name == "issue-review"
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

        # Overwrite user prompt with text that omits $issue_content_file
        (templates_dir / "PLAN_USER_PROMPT.md").write_text("No variable here")

        with pytest.raises(ValueError, match="missing required variable"):
            load_agent_config(AgentAction.PLAN)


class TestDevelopPromptTddContent:
    def test_includes_red_green_tdd_block(self):
        assert "Testing methodology — red/green TDD (non-negotiable):" in DEVELOP_AGENT_PROMPT

    def test_includes_red_phase_with_failure_confirmation(self):
        assert "RED:" in DEVELOP_AGENT_PROMPT
        assert "confirm it fails" in DEVELOP_AGENT_PROMPT

    def test_includes_green_phase_with_minimum_code(self):
        assert "GREEN:" in DEVELOP_AGENT_PROMPT
        assert "minimum code" in DEVELOP_AGENT_PROMPT

    def test_includes_refactor_phase(self):
        assert "REFACTOR:" in DEVELOP_AGENT_PROMPT

    def test_includes_phase_gate_blocking_red_to_green(self):
        assert "Do NOT proceed from RED to GREEN without a confirmed failing run." in DEVELOP_AGENT_PROMPT

    def test_includes_behavioral_assertion_rule(self):
        assert "observable inputs/outputs or side effects" in DEVELOP_AGENT_PROMPT

    def test_requires_committing_tests_with_implementation(self):
        assert "Commit tests and implementation together." in DEVELOP_AGENT_PROMPT

    def test_anti_rationalization_includes_tests_after_entry(self):
        assert "I'll write the test after, it's faster" in DEVELOP_AGENT_PROMPT

    def test_removes_legacy_write_tests_line(self):
        assert "Write tests for every new or changed behavior" not in DEVELOP_AGENT_PROMPT


class TestDevelopPromptTestPlanUpdate:
    """The develop prompt must update the PR test plan after PR creation (issue #88).

    Assertions are literal-substring matches: prompt-wording changes will surface
    here intentionally. Don't "fix" them away — update both the prompt and the
    asserted substrings together, mirroring the TestReviewprPromptMergeGuard precedent.
    """

    def test_includes_test_plan_update_section_heading(self):
        assert "Test plan update:" in DEVELOP_AGENT_PROMPT

    def test_includes_pr_body_read_command(self):
        assert "gh pr view <number> --json body -q .body" in DEVELOP_AGENT_PROMPT

    def test_includes_pr_body_edit_command(self):
        assert 'gh pr edit <number> --body "<updated body>"' in DEVELOP_AGENT_PROMPT

    def test_includes_manual_verification_carveout(self):
        assert "manual/external verification" in DEVELOP_AGENT_PROMPT
        assert "unchecked" in DEVELOP_AGENT_PROMPT

    def test_includes_noop_when_no_test_plan_section(self):
        assert "If no `## Test plan` section exists, skip this step" in DEVELOP_AGENT_PROMPT

    def test_completion_step_references_test_plan_update(self):
        assert 'Update the test plan checklist (see "Test plan update")' in DEVELOP_AGENT_PROMPT


class TestDevelopPromptPrDescriptionUpdate:
    """The develop prompt must review/update the PR description on changes to an existing PR.

    Assertions are literal-substring matches: prompt-wording changes will surface
    here intentionally — update both the prompt and the asserted substrings together.
    """

    def test_includes_pr_description_update_section_heading(self):
        assert "PR description update (when pushing changes to an existing PR):" in DEVELOP_AGENT_PROMPT

    def test_completion_step_references_existing_pr_review(self):
        assert "If a PR exists" in DEVELOP_AGENT_PROMPT
        assert "review and update its description" in DEVELOP_AGENT_PROMPT

    def test_includes_pr_body_read_command(self):
        assert "Read current PR body: `gh pr view <number> --json body -q .body`" in DEVELOP_AGENT_PROMPT

    def test_includes_pr_body_edit_command(self):
        assert 'Apply: `gh pr edit <number> --body "<updated body>"`' in DEVELOP_AGENT_PROMPT

    def test_includes_verification_refresh_guidance(self):
        assert "Refresh `## Verification`" in DEVELOP_AGENT_PROMPT

    def test_includes_preserve_unrelated_content_rule(self):
        assert "Preserve unrelated content — only edit affected sections." in DEVELOP_AGENT_PROMPT


class TestDevelopPromptIssueReference:
    """The develop prompt must require a GitHub auto-close keyword in the PR body."""

    def test_includes_close_keyword_guidance(self):
        assert "Closes #<issue-number>" in DEVELOP_AGENT_PROMPT
        assert "Fixes #<issue-number>" in DEVELOP_AGENT_PROMPT

    def test_close_keyword_guidance_lives_in_pr_description_section(self):
        pr_description_index = DEVELOP_AGENT_PROMPT.index("PR description:")
        on_completion_index = DEVELOP_AGENT_PROMPT.index("On completion:")
        close_keyword_index = DEVELOP_AGENT_PROMPT.index("Closes #<issue-number>")
        assert pr_description_index < close_keyword_index < on_completion_index


class TestFixciPromptPrDescriptionUpdate:
    """The fix-ci prompt must review/update the PR description after applying fixes."""

    def test_includes_pr_description_update_step(self):
        assert "Review and update the PR description to reflect the fix:" in FIXCI_AGENT_PROMPT

    def test_includes_pr_body_read_command(self):
        assert "Read current PR body: `gh pr view <number> --json body -q .body`" in FIXCI_AGENT_PROMPT

    def test_includes_pr_body_edit_command(self):
        assert 'Apply: `gh pr edit <number> --body "<updated body>"`' in FIXCI_AGENT_PROMPT

    def test_includes_verification_refresh_guidance(self):
        assert "Refresh `## Verification`" in FIXCI_AGENT_PROMPT

    def test_step_ordering_places_update_before_summary_comment(self):
        update_index = FIXCI_AGENT_PROMPT.index("Review and update the PR description")
        comment_index = FIXCI_AGENT_PROMPT.index("Comment on the PR summarizing")
        assert update_index < comment_index


class TestReviewprPromptMergeGuard:
    """The pr-review prompt must block merging when CHANGES_REQUESTED reviews exist (issue #86)."""

    def test_includes_changes_requested_check_command(self):
        assert (
            "gh pr view <number> -R <owner/repo> --json reviews --jq '[.reviews[] "
            '| select(.state == "CHANGES_REQUESTED")]\'' in REVIEWPR_AGENT_PROMPT
        )

    def test_includes_do_not_merge_instruction(self):
        assert "DO NOT merge" in REVIEWPR_AGENT_PROMPT

    def test_includes_address_inline_comments_instruction(self):
        assert "reply to all inline comments" in REVIEWPR_AGENT_PROMPT

    def test_includes_pre_merge_guard_heading(self):
        assert "Pre-merge guard" in REVIEWPR_AGENT_PROMPT


class TestReviewprPromptDedupGuard:
    """The pr-review prompt must skip duplicate reviews when no new commits since last review (issue #100)."""

    def test_includes_pre_review_dedup_heading(self):
        assert "Pre-review dedup" in REVIEWPR_AGENT_PROMPT

    def test_includes_dedup_check_command(self):
        assert "sort_by(.committedDate)" in REVIEWPR_AGENT_PROMPT
        assert '.author.login == "ellen-goc"' in REVIEWPR_AGENT_PROMPT
        assert "(.body|length > 50)" in REVIEWPR_AGENT_PROMPT
        assert "--json reviews,commits" in REVIEWPR_AGENT_PROMPT

    def test_includes_skip_instruction(self):
        assert "skip entirely" in REVIEWPR_AGENT_PROMPT

    def test_dedup_guard_placed_before_pre_merge_guard(self):
        assert REVIEWPR_AGENT_PROMPT.index("Pre-review dedup") < REVIEWPR_AGENT_PROMPT.index("Pre-merge guard")


class TestReviewprPromptNoLinkedIssueComment:
    """The pr-review prompt must not unconditionally post a linked-issue comment (issue #100)."""

    def test_does_not_post_linked_issue_comment(self):
        assert "Also post a brief summary on the linked issue" not in REVIEWPR_AGENT_PROMPT


class TestDevelopPromptConditionalIssueComment:
    """The develop prompt must only comment on the issue when opening a new PR (issue #100)."""

    def test_does_not_unconditionally_comment_on_issue(self):
        assert "- Comment on the issue summarizing what was implemented." not in DEVELOP_AGENT_PROMPT

    def test_includes_new_pr_branch(self):
        assert "If this session opened a new PR" in DEVELOP_AGENT_PROMPT

    def test_includes_existing_pr_skip_branch(self):
        assert "PR already existed" in DEVELOP_AGENT_PROMPT
        assert "PR description update is sufficient" in DEVELOP_AGENT_PROMPT


class TestDevelopPromptMermaidLabelSafety:
    r"""The develop prompt must instruct quoting mermaid labels with shape-reserved chars.

    Without quoting, labels starting with `/` or `\` are parsed as parallelogram/trapezoid
    shape syntax (e.g. `B[/simplify]`) and break rendering with a lexical error.
    """

    def test_includes_label_safety_guidance(self):
        assert "Mermaid label safety" in DEVELOP_AGENT_PROMPT

    def test_includes_quoted_example(self):
        assert 'B["/simplify, commit, push"]' in DEVELOP_AGENT_PROMPT

    def test_warns_against_parallelogram_collision(self):
        assert "parallelogram/trapezoid shape syntax" in DEVELOP_AGENT_PROMPT


class TestConfigBoundaryConstraints:
    """Both develop and fix-ci prompts must forbid relaxing linter/type-checker config (issue #89)."""

    def test_develop_prompt_includes_config_boundaries_heading(self):
        assert "Configuration boundaries" in DEVELOP_AGENT_PROMPT

    def test_develop_prompt_forbids_modifying_linter_config(self):
        assert "pyproject.toml" in DEVELOP_AGENT_PROMPT
        assert "[tool.ruff]" in DEVELOP_AGENT_PROMPT
        assert "[tool.pyright]" in DEVELOP_AGENT_PROMPT
        assert "[tool.mypy]" in DEVELOP_AGENT_PROMPT
        assert ".ruff.toml" in DEVELOP_AGENT_PROMPT
        assert "pyrightconfig.json" in DEVELOP_AGENT_PROMPT
        assert "mypy.ini" in DEVELOP_AGENT_PROMPT
        assert ".pre-commit-config.yaml" in DEVELOP_AGENT_PROMPT
        assert "ESLint/Prettier" in DEVELOP_AGENT_PROMPT
        assert "Fix the code, not the config." in DEVELOP_AGENT_PROMPT

    def test_develop_prompt_forbids_inline_suppression_comments(self):
        assert "# noqa" in DEVELOP_AGENT_PROMPT
        assert "# type: ignore" in DEVELOP_AGENT_PROMPT
        assert "# pyright: ignore" in DEVELOP_AGENT_PROMPT
        assert "eslint-disable" in DEVELOP_AGENT_PROMPT

    def test_develop_prompt_documents_exceptions(self):
        assert "issue explicitly requests" in DEVELOP_AGENT_PROMPT
        assert "third-party bug" in DEVELOP_AGENT_PROMPT

    def test_fixci_prompt_includes_config_boundaries_heading(self):
        assert "Configuration boundaries" in FIXCI_AGENT_PROMPT

    def test_fixci_prompt_places_boundaries_before_fixing_failures(self):
        boundaries_idx = FIXCI_AGENT_PROMPT.find("Configuration boundaries")
        fixing_idx = FIXCI_AGENT_PROMPT.find("## Fixing Failures")
        assert boundaries_idx != -1
        assert fixing_idx != -1
        assert boundaries_idx < fixing_idx

    def test_fixci_prompt_forbids_modifying_linter_config(self):
        assert "pyproject.toml" in FIXCI_AGENT_PROMPT
        assert "[tool.ruff]" in FIXCI_AGENT_PROMPT
        assert "[tool.pyright]" in FIXCI_AGENT_PROMPT
        assert "[tool.mypy]" in FIXCI_AGENT_PROMPT
        assert ".ruff.toml" in FIXCI_AGENT_PROMPT
        assert "pyrightconfig.json" in FIXCI_AGENT_PROMPT
        assert "mypy.ini" in FIXCI_AGENT_PROMPT
        assert ".pre-commit-config.yaml" in FIXCI_AGENT_PROMPT
        assert "ESLint/Prettier" in FIXCI_AGENT_PROMPT
        assert "Fix the code, not the config." in FIXCI_AGENT_PROMPT

    def test_fixci_prompt_forbids_inline_suppression_comments(self):
        assert "# noqa" in FIXCI_AGENT_PROMPT
        assert "# type: ignore" in FIXCI_AGENT_PROMPT
        assert "# pyright: ignore" in FIXCI_AGENT_PROMPT
        assert "eslint-disable" in FIXCI_AGENT_PROMPT

    def test_fixci_prompt_documents_exceptions(self):
        assert "issue explicitly requests" in FIXCI_AGENT_PROMPT
        assert "third-party bug" in FIXCI_AGENT_PROMPT


class TestWritePromptContent:
    def test_writes_file_with_correct_name(self):
        filepath = write_prompt_content("plan", "monkut", "askcc-cli", 42, "issue content here")
        try:
            assert filepath.name == "askcc_plan_monkut-askcc-cli_42.md"
            assert filepath.read_text() == "issue content here"
        finally:
            filepath.unlink(missing_ok=True)

    def test_writes_file_with_suffix(self):
        filepath = write_prompt_content("pr-review", "owner", "repo", 7, "pr diff", suffix="_pr")
        try:
            assert filepath.name == "askcc_pr-review_owner-repo_7_pr.md"
            assert filepath.read_text() == "pr diff"
        finally:
            filepath.unlink(missing_ok=True)

    def test_overwrites_existing_file(self):
        filepath = write_prompt_content("plan", "owner", "repo", 1, "first")
        try:
            assert filepath.read_text() == "first"
            filepath2 = write_prompt_content("plan", "owner", "repo", 1, "second")
            assert filepath == filepath2
            assert filepath.read_text() == "second"
        finally:
            filepath.unlink(missing_ok=True)


class TestStringTemplateSubstitution:
    def test_issue_content_file_substituted(self):
        template_str = "Do the thing.\n\nRead the issue content from: $issue_content_file"
        filepath = write_prompt_content("plan", "owner", "repo", 42, "content")
        try:
            result = Template(template_str).safe_substitute(issue_content_file=str(filepath))
            assert str(filepath) in result
        finally:
            filepath.unlink(missing_ok=True)

    def test_file_path_substitution(self):
        template_str = "Process this:\n\nRead the issue content from: $issue_content_file"
        filepath = write_prompt_content("plan", "owner", "repo", 42, "content")
        try:
            result = Template(template_str).safe_substitute(issue_content_file=str(filepath))
            assert str(filepath) in result
            assert "$issue_content_file" not in result
        finally:
            filepath.unlink(missing_ok=True)


class TestClaudeRunner:
    ISSUE_URL = "https://github.com/test/repo/issues/1"

    @pytest.fixture(autouse=True)
    def _set_oauth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OAUTH_TOKEN_ENV, "test-oauth-token")  # noqa: S105

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

    @pytest.fixture
    def runner(self) -> ClaudeRunner:
        return ClaudeRunner()

    def test_logs_token_usage(
        self,
        runner: ClaudeRunner,
        agent_config: AgentConfig,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
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
            caplog.at_level("INFO", logger="askcc.runners"),
            patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run,
        ):
            exit_code, usage = runner.run("test prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())

        assert exit_code == 0
        assert usage == {"input_tokens": 1500, "output_tokens": 300, "model": "claude-sonnet-4-6-20250514"}
        captured = capfd.readouterr()
        assert "Here is the plan." in captured.out
        assert "model: claude-sonnet-4-6-20250514" in caplog.text
        assert "input: 1500" in caplog.text
        assert "output: 300" in caplog.text
        mock_run.assert_called_once()

    def test_handles_invalid_json(
        self,
        runner: ClaudeRunner,
        agent_config: AgentConfig,
        capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

        with patch("askcc.runners.subprocess.run", return_value=mock_result):
            exit_code, usage = runner.run("test prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())

        assert exit_code == 0
        assert usage is None
        captured = capfd.readouterr()
        assert "not json" in captured.out
        assert "Failed to parse Claude JSON output" in caplog.text

    def test_returns_nonzero_exit_code(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error occurred")

        with patch("askcc.runners.subprocess.run", return_value=mock_result):
            exit_code, usage = runner.run("test prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())

        assert exit_code == 1
        assert usage is None

    def test_returns_none_usage_when_no_usage_key(self, runner: ClaudeRunner, agent_config: AgentConfig):
        claude_response = json.dumps({"result": "Done."})
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=claude_response, stderr="")

        with patch("askcc.runners.subprocess.run", return_value=mock_result):
            exit_code, usage = runner.run("test prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())

        assert exit_code == 0
        assert usage is None


class TestGetRunner:
    def test_get_claude_runner(self):
        runner = get_runner("claude")
        assert isinstance(runner, ClaudeRunner)

    def test_unknown_runner_raises(self):
        with pytest.raises(ValueError, match="Unknown runner"):
            get_runner("nonexistent")


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
        """Run main() with given args and return the prompt passed to runner.run."""
        mock_runner = _mock_runner()
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=mock_runner),
            patch("sys.argv", ["askcc", *extra_args, AgentAction.PLAN, "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()
        return mock_runner.run.call_args[0][0]

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

    def test_h2_section_with_h3_subheadings(self):
        body = (
            "## Acceptance Criteria\n\n"
            "### Core Responder Abstraction\n"
            "- [ ] Responder ABC defined\n"
            "- [ ] validate_input pass-through\n\n"
            "### Guardrail\n"
            "- [ ] LLMGuardrail class defined\n"
        )
        assert _has_acceptance_criteria(body) is True

    def test_h2_section_with_h3_subheadings_then_other_h2(self):
        body = (
            "## Acceptance Criteria\n\n### Group A\n- [ ] Item one\n\n## Other Section\n- [ ] Should not be counted\n"
        )
        assert _has_acceptance_criteria(body) is True

    def test_h3_section_with_h4_subheadings(self):
        body = "### Acceptance Criteria\n\n#### Subgroup\n- [ ] Item under h4\n"
        assert _has_acceptance_criteria(body) is True

    def test_h3_section_terminated_by_sibling_h3(self):
        body = "### Acceptance Criteria\nPlain text with no checklist.\n\n### Other\n- [ ] Wrong section\n"
        assert _has_acceptance_criteria(body) is False


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
        verification_passed = VerificationResult(passed=True, message="skipped", checks=[])
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness") as mock_validate,
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.cli._run_project_verification", return_value=verification_passed),
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

        assert mock_run.call_count == 2
        add_cmd = mock_run.call_args_list[0][0][0]
        remove_cmd = mock_run.call_args_list[1][0][0]
        assert "--add-label" in add_cmd
        assert "--remove-label" not in add_cmd
        assert "--remove-label" in remove_cmd
        assert "--add-label" not in remove_cmd
        assert "Transitioned labels" in caplog.text

    def test_add_failure_skips_remove(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch(
                "askcc.functions.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh", stderr="label not found"),
            ) as mock_run,
        ):
            _swap_issue_labels("/usr/bin/gh", "owner/repo", 42, remove="action:develop", add="action:review")

        mock_run.assert_called_once()  # only the add call, remove never attempted
        assert "Failed to add label" in caplog.text

    def test_remove_failure_warns(self, caplog: pytest.LogCaptureFixture):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            caplog.at_level("WARNING", logger="askcc.functions"),
            patch(
                "askcc.functions.subprocess.run",
                side_effect=[ok, subprocess.CalledProcessError(1, "gh", stderr="label not found")],
            ) as mock_run,
        ):
            _swap_issue_labels("/usr/bin/gh", "owner/repo", 42, remove="action:develop", add="action:review")

        assert mock_run.call_count == 2
        assert "Failed to remove label" in caplog.text


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

    def test_updates_status_field(self, caplog: pytest.LogCaptureFixture):
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
                },
            }
        ]
        query_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._graphql_response(nodes), stderr=""
        )
        mutation_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")

        with (
            caplog.at_level("INFO", logger="askcc.functions"),
            patch("askcc.functions.subprocess.run", side_effect=[query_result, mutation_result]),
        ):
            _transition_project_fields("/usr/bin/gh", "owner", "repo", 42)

        assert "Updated 'Status' in project 'My Board'" in caplog.text

    def test_skips_missing_fields(self, caplog: pytest.LogCaptureFixture):
        nodes = [
            {
                "id": "item-1",
                "project": {
                    "id": "proj-1",
                    "title": "Simple Board",
                    "statusField": None,
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


_VERIFICATION_PASSED = VerificationResult(passed=True, message="3/3 checks passed", checks=[])
_VERIFICATION_FAILED = VerificationResult(
    passed=False,
    message="1/2 checks passed",
    checks=[
        CheckResult(name="tests", passed=True, message="passed"),
        CheckResult(name="lint", passed=False, message="failed: ruff errors"),
    ],
)


class TestDevelopTransitionIntegration:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_develop_success_triggers_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.cli._run_project_verification", return_value=_VERIFICATION_PASSED),
            patch("askcc.cli.transition_issue_to_review") as mock_transition,
            patch("sys.argv", ["askcc", "develop", "--skip-validation", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_called_once_with(self.ISSUE_URL)

    def test_develop_verification_failure_blocks_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.cli._run_project_verification", return_value=_VERIFICATION_FAILED),
            patch("askcc.cli.transition_issue_to_review") as mock_transition,
            patch("sys.argv", ["askcc", "develop", "--skip-validation", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_not_called()

    def test_develop_failure_skips_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.validate_issue_readiness", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner(1)),
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
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.cli.transition_issue_to_review") as mock_transition,
            patch("sys.argv", ["askcc", "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_not_called()


class TestDetectVerificationCommands:
    def test_reads_from_pyproject_toml(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[[tool.askcc.verify]]\nname = "tests"\ncmd = "uv run poe test"\n\n'
            '[[tool.askcc.verify]]\nname = "lint"\ncmd = "uv run poe check"\n'
        )
        commands = _detect_verification_commands(tmp_path)
        assert commands == [
            ("tests", ["uv", "run", "poe", "test"]),
            ("lint", ["uv", "run", "poe", "check"]),
        ]

    def test_reads_from_askcc_toml(self, tmp_path: Path):
        askcc_toml = tmp_path / ".askcc.toml"
        askcc_toml.write_text(
            '[[verify]]\nname = "tests"\ncmd = "npm test"\n\n[[verify]]\nname = "lint"\ncmd = "npm run lint"\n'
        )
        commands = _detect_verification_commands(tmp_path)
        assert commands == [
            ("tests", ["npm", "test"]),
            ("lint", ["npm", "run", "lint"]),
        ]

    def test_pyproject_takes_precedence_over_askcc_toml(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text('[[tool.askcc.verify]]\nname = "tests"\ncmd = "make test"\n')
        (tmp_path / ".askcc.toml").write_text('[[verify]]\nname = "tests"\ncmd = "npm test"\n')
        commands = _detect_verification_commands(tmp_path)
        assert commands == [("tests", ["make", "test"])]

    def test_returns_empty_when_no_config(self, tmp_path: Path):
        commands = _detect_verification_commands(tmp_path)
        assert commands == []

    def test_returns_empty_when_pyproject_has_no_verify_section(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\n')
        commands = _detect_verification_commands(tmp_path)
        assert commands == []

    def test_skips_entries_missing_required_fields(self, tmp_path: Path):
        (tmp_path / ".askcc.toml").write_text(
            '[[verify]]\nname = "tests"\ncmd = "pytest"\n\n[[verify]]\nname = "incomplete"\n'  # missing cmd
        )
        commands = _detect_verification_commands(tmp_path)
        assert commands == [("tests", ["pytest"])]


class TestRunProjectVerification:
    def test_skips_when_no_commands_detected(self, tmp_path: Path):
        result = _run_project_verification(tmp_path)
        assert result.passed is True
        assert result.checks == []
        assert "skipping" in result.message.lower()

    def test_all_checks_pass(self, tmp_path: Path):
        (tmp_path / ".askcc.toml").write_text(
            '[[verify]]\nname = "tests"\ncmd = "pytest"\n\n[[verify]]\nname = "lint"\ncmd = "ruff check"\n'
        )
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("askcc.functions.subprocess.run", return_value=ok):
            result = _run_project_verification(tmp_path)
        assert result.passed is True
        assert all(c.passed for c in result.checks)

    def test_partial_failure(self, tmp_path: Path):
        (tmp_path / ".askcc.toml").write_text(
            '[[verify]]\nname = "tests"\ncmd = "pytest"\n\n[[verify]]\nname = "lint"\ncmd = "ruff check"\n'
        )
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lint error found")
        with patch("askcc.functions.subprocess.run", side_effect=[ok, fail]):
            result = _run_project_verification(tmp_path)
        assert result.passed is False
        assert result.message == "1/2 checks passed"

    def test_command_not_found(self, tmp_path: Path):
        (tmp_path / ".askcc.toml").write_text('[[verify]]\nname = "tests"\ncmd = "pytest"\n')
        with patch("askcc.functions.subprocess.run", side_effect=FileNotFoundError):
            result = _run_project_verification(tmp_path)
        assert result.passed is False
        assert "command not found" in result.checks[0].message

    def test_timeout(self, tmp_path: Path):
        (tmp_path / ".askcc.toml").write_text('[[verify]]\nname = "tests"\ncmd = "pytest"\n')
        with patch("askcc.functions.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 300)):
            result = _run_project_verification(tmp_path)
        assert result.passed is False
        assert "timed out" in result.checks[0].message


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
        )


class TestTransitionIssueToDevelopmentIntegration:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/42"

    def test_calls_swap_labels_and_project_transition(self):
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions._swap_issue_labels") as mock_swap,
            patch("askcc.functions._transition_project_fields") as mock_project,
        ):
            transition_issue_to_development(self.ISSUE_URL)

        mock_swap.assert_called_once_with(
            "/usr/bin/gh",
            "monkut/askcc-cli",
            42,
            remove="action:plan",
            add="action:develop",
        )
        mock_project.assert_called_once_with(
            "/usr/bin/gh",
            "monkut",
            "askcc-cli",
            42,
            status_options=("ready", "todo"),
        )

    def test_handles_no_project_cleanly(self):
        empty_items = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}}),
            stderr="",
        )
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions.subprocess.run", side_effect=[ok, ok, empty_items]),
        ):
            transition_issue_to_development(self.ISSUE_URL)


class TestPlanCommand:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_plan_success_triggers_development_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.cli.transition_issue_to_development") as mock_transition,
            patch("sys.argv", ["askcc", "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_called_once_with(self.ISSUE_URL)

    def test_plan_success_invokes_project_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.functions.shutil.which", return_value="/usr/bin/gh"),
            patch("askcc.functions._swap_issue_labels"),
            patch("askcc.functions._transition_project_fields") as mock_project,
            patch("sys.argv", ["askcc", "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_project.assert_called_once_with(
            "/usr/bin/gh",
            "monkut",
            "askcc-cli",
            1,
            status_options=("ready", "todo"),
        )

    def test_plan_failure_skips_transition(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner(1)),
            patch("askcc.cli.transition_issue_to_development") as mock_transition,
            patch("sys.argv", ["askcc", "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_transition.assert_not_called()


class TestPrepareCommand:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_prepare_skips_label_validation(self):
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels") as mock_labels,
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
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
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
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
            patch("askcc.cli.get_runner", return_value=_mock_runner(1)),
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
        assert config.action_name == "pr-review"
        assert config.description == "Reviews a pull request against its linked issue's Definition of Done"
        assert config.required_variables == ("issue_content_file", "pr_content_file")


class TestReviewprCommand:
    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_reviewpr_fetches_pr_content(self):
        mock_runner = _mock_runner()
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.fetch_pr_content", return_value="pr diff content") as mock_pr,
            patch("askcc.cli.get_runner", return_value=mock_runner),
            patch("sys.argv", ["askcc", "pr-review", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_pr.assert_called_once_with(self.ISSUE_URL)
        prompt = mock_runner.run.call_args[0][0]
        assert "askcc_pr-review_monkut-askcc-cli_1.md" in prompt
        assert "askcc_pr-review_monkut-askcc-cli_1_pr.md" in prompt

    def test_reviewpr_exits_on_no_linked_pr(self, caplog: pytest.LogCaptureFixture):
        with (
            caplog.at_level("ERROR", logger="askcc.cli"),
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.fetch_pr_content", side_effect=ValueError("No linked pull request found")),
            patch("sys.argv", ["askcc", "pr-review", "-g", self.ISSUE_URL]),
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
            patch("askcc.cli.get_runner", return_value=_mock_runner()),
            patch("askcc.cli.transition_issue_to_review") as mock_review,
            patch("askcc.cli.transition_issue_to_planning") as mock_planning,
            patch("sys.argv", ["askcc", "pr-review", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()

        mock_review.assert_not_called()
        mock_planning.assert_not_called()


class TestThinkingSettings:
    """Tests for ASKCC_CLAUDE_* thinking/reasoning env var settings."""

    def test_effort_level_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "high")
        # Re-evaluate the module-level expression
        result = os.getenv("ASKCC_CLAUDE_EFFORT_LEVEL") or None
        assert result == "high"

    def test_effort_level_empty_string_uses_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "")
        result = _resolve_effort_level()
        assert result == DEFAULT_EFFORT_LEVEL

    def test_effort_level_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_CLAUDE_EFFORT_LEVEL", raising=False)
        result = _resolve_effort_level()
        assert result == DEFAULT_EFFORT_LEVEL

    def test_invalid_effort_level_logged_and_ignored(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "turbo")
        with caplog.at_level("WARNING", logger="askcc.settings"):
            result = _resolve_effort_level()
        assert result == DEFAULT_EFFORT_LEVEL
        assert "Invalid ASKCC_CLAUDE_EFFORT_LEVEL" in caplog.text

    def test_xhigh_effort_level_resolves_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "xhigh")
        with caplog.at_level("WARNING", logger="askcc.settings"):
            result = _resolve_effort_level()
        assert result == VALID_EFFORT_LEVELS.XHIGH
        assert "Invalid ASKCC_CLAUDE_EFFORT_LEVEL" not in caplog.text

    def test_default_effort_level_is_xhigh(self):
        assert DEFAULT_EFFORT_LEVEL == VALID_EFFORT_LEVELS.XHIGH

    def test_max_thinking_tokens_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_MAX_THINKING_TOKENS", "50000")
        raw = os.getenv("ASKCC_CLAUDE_MAX_THINKING_TOKENS", "")
        assert raw.isdigit()
        assert int(raw) == 50000

    def test_max_thinking_tokens_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_CLAUDE_MAX_THINKING_TOKENS", raising=False)
        raw = os.getenv("ASKCC_CLAUDE_MAX_THINKING_TOKENS", "")
        assert not raw.isdigit()
        assert DEFAULT_MAX_THINKING_TOKENS == 21000

    def test_disable_thinking_truthy(self, monkeypatch: pytest.MonkeyPatch):
        for val in ("1", "true", "True", "TRUE"):
            monkeypatch.setenv("ASKCC_CLAUDE_DISABLE_THINKING", val)
            assert os.getenv("ASKCC_CLAUDE_DISABLE_THINKING", "").lower() in ("1", "true")

    def test_disable_thinking_falsy(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_DISABLE_THINKING", "0")
        assert os.getenv("ASKCC_CLAUDE_DISABLE_THINKING", "").lower() not in ("1", "true")

    def test_disable_adaptive_thinking_default_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING", raising=False)
        result = os.getenv("ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING", "true").lower() not in ("0", "false")
        assert result is True

    def test_disable_adaptive_thinking_explicit_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING", "false")
        result = os.getenv("ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING", "true").lower() not in ("0", "false")
        assert result is False


class TestDefaultLanguageResolution:
    """Tests for ASKCC_LANGUAGE env var and ~/.askcc/config.toml default-language resolution."""

    def test_default_language_from_env_var(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_LANGUAGE", "japanese")
        assert _resolve_default_language(user_config={}) == SupportedLanguage.JAPANESE

    def test_default_language_from_config_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.delenv("ASKCC_LANGUAGE", raising=False)
        config_path = tmp_path / "config.toml"
        config_path.write_text('[defaults]\nlanguage = "japanese"\n')
        loaded = _load_user_config(config_path)
        assert _resolve_default_language(user_config=loaded) == SupportedLanguage.JAPANESE

    def test_env_var_overrides_config_file(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_LANGUAGE", "japanese")
        assert (
            _resolve_default_language(user_config={"defaults": {"language": "english"}}) == SupportedLanguage.JAPANESE
        )

    def test_config_file_overrides_builtin_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_LANGUAGE", raising=False)
        assert (
            _resolve_default_language(user_config={"defaults": {"language": "japanese"}}) == SupportedLanguage.JAPANESE
        )

    def test_default_language_unset_returns_english(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_LANGUAGE", raising=False)
        assert _resolve_default_language(user_config={}) == SupportedLanguage.ENGLISH

    def test_invalid_env_value_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("ASKCC_LANGUAGE", "klingon")
        with caplog.at_level("WARNING", logger="askcc.settings"):
            result = _resolve_default_language(user_config={})
        assert result == SupportedLanguage.ENGLISH
        assert "Invalid ASKCC_LANGUAGE" in caplog.text

    def test_invalid_config_value_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.delenv("ASKCC_LANGUAGE", raising=False)
        with caplog.at_level("WARNING", logger="askcc.settings"):
            result = _resolve_default_language(user_config={"defaults": {"language": "klingon"}})
        assert result == SupportedLanguage.ENGLISH
        assert "Invalid [defaults].language" in caplog.text

    def test_missing_config_file_silent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="askcc.settings"):
            result = _load_user_config(tmp_path / "missing.toml")
        assert result == {}
        assert caplog.text == ""

    def test_malformed_toml_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        config_path = tmp_path / "config.toml"
        config_path.write_text("not = valid = toml\n[broken")
        with caplog.at_level("WARNING", logger="askcc.settings"):
            result = _load_user_config(config_path)
        assert result == {}
        assert "Failed to read user config" in caplog.text

    def test_user_config_path_uses_askcc_home(self):
        # USER_CONFIG_PATH must resolve relative to ASKCC_HOME (so ASKCC_HOME overrides apply).
        assert USER_CONFIG_PATH == ASKCC_HOME / "config.toml"

    def test_cli_flag_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        # CLI flag precedence is enforced by argparse — passing --language overrides
        # whatever DEFAULT_LANGUAGE was resolved to at module import.
        monkeypatch.setenv("ASKCC_LANGUAGE", "japanese")
        mock_runner = _mock_runner()
        issue_url = "https://github.com/monkut/askcc-cli/issues/1"
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=mock_runner),
            patch("sys.argv", ["askcc", "--language", "english", AgentAction.PLAN, "-g", issue_url]),
            pytest.raises(SystemExit),
        ):
            main()
        prompt = mock_runner.run.call_args[0][0]
        assert "Output all comments in" not in prompt


class TestThinkingCLIFlags:
    """Tests for --effort, --max-thinking-tokens, --disable-thinking, --disable-adaptive-thinking CLI flags."""

    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def _run_main_with_args(self, extra_args: list[str]) -> MagicMock:
        mock_runner = _mock_runner()
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=mock_runner),
            patch("sys.argv", ["askcc", *extra_args, "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()
        return mock_runner

    def test_effort_flag_passed_to_runner(self):
        mock_runner = self._run_main_with_args(["--effort", "high"])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["effort_level"] == "high"

    def test_effort_flag_invalid_rejected_by_argparse(self):
        with (
            pytest.raises(SystemExit, match="2"),
            patch("sys.argv", ["askcc", "--effort", "turbo", "plan", "-g", self.ISSUE_URL]),
        ):
            main()

    def test_max_thinking_tokens_flag_passed_to_runner(self):
        mock_runner = self._run_main_with_args(["--max-thinking-tokens", "50000"])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["max_thinking_tokens"] == 50000

    def test_disable_thinking_flag_passed_to_runner(self):
        mock_runner = self._run_main_with_args(["--disable-thinking"])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["disable_thinking"] is True

    def test_disable_adaptive_thinking_flag_passed_to_runner(self):
        mock_runner = self._run_main_with_args(["--disable-adaptive-thinking"])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["disable_adaptive_thinking"] is True

    def test_no_disable_adaptive_thinking_flag(self):
        mock_runner = self._run_main_with_args(["--no-disable-adaptive-thinking"])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["disable_adaptive_thinking"] is False

    def test_effort_env_default_used(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "low")
        mock_runner = self._run_main_with_args([])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["effort_level"] == "low"

    def test_cli_flag_overrides_env_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "low")
        mock_runner = self._run_main_with_args(["--effort", "max"])
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs["effort_level"] == "max"


class TestClaudeRunnerThinkingOptions:
    """Tests that ClaudeRunner passes thinking options to the subprocess."""

    ISSUE_URL = "https://github.com/test/repo/issues/1"

    @pytest.fixture(autouse=True)
    def _set_oauth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OAUTH_TOKEN_ENV, "test-oauth-token")  # noqa: S105

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

    @pytest.fixture
    def runner(self) -> ClaudeRunner:
        return ClaudeRunner()

    def test_effort_level_appended_to_cmd(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
                effort_level="high",
            )
        cmd = mock_run.call_args[0][0]
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_max_thinking_tokens_in_env(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
                max_thinking_tokens=50000,
            )
        env = mock_run.call_args[1]["env"]
        assert env[CLAUDE_ENV_MAX_THINKING_TOKENS] == "50000"

    def test_disable_thinking_in_env(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
                disable_thinking=True,
            )
        env = mock_run.call_args[1]["env"]
        assert env[CLAUDE_ENV_DISABLE_THINKING] == "1"

    def test_disable_adaptive_thinking_in_env(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
                disable_adaptive_thinking=True,
            )
        env = mock_run.call_args[1]["env"]
        assert env[CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING] == "1"

    def test_no_thinking_env_when_defaults(
        self, runner: ClaudeRunner, agent_config: AgentConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv(CLAUDE_ENV_DISABLE_THINKING, raising=False)
        monkeypatch.delenv(CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING, raising=False)
        monkeypatch.delenv(CLAUDE_ENV_MAX_THINKING_TOKENS, raising=False)
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
            )
        cmd = mock_run.call_args[0][0]
        env = mock_run.call_args[1]["env"]
        assert "--effort" not in cmd
        assert CLAUDE_ENV_DISABLE_THINKING not in env
        assert CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING not in env

    def test_effort_none_not_appended(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
                effort_level=None,
            )
        cmd = mock_run.call_args[0][0]
        assert "--effort" not in cmd

    def test_disable_thinking_false_not_set(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run(
                "prompt",
                config=agent_config,
                issue_url=self.ISSUE_URL,
                cwd=Path.cwd(),
                disable_thinking=False,
            )
        env = mock_run.call_args[1]["env"]
        assert CLAUDE_ENV_DISABLE_THINKING not in env


class TestResolveOAuthToken:
    """Tests for the OAuth token discovery chain."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(OAUTH_TOKEN_ENV, raising=False)
        monkeypatch.delenv(OAUTH_TOKEN_FILE_ENV, raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
        monkeypatch.setattr("askcc.runners.CONVENTIONAL_TOKEN_FILE", tmp_path / "missing-conventional")
        monkeypatch.setattr("askcc.runners.CREDENTIALS_JSON_FILE", tmp_path / "missing-credentials.json")

    def test_env_var_present_no_fallback_used(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(OAUTH_TOKEN_ENV, "env-token")  # noqa: S105
        with patch("askcc.runners._read_token_file") as mock_read:
            token, source = _resolve_oauth_token()
        assert token == "env-token"  # noqa: S105
        assert source == f"env {OAUTH_TOKEN_ENV}"
        mock_read.assert_not_called()

    def test_env_var_empty_falls_through(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv(OAUTH_TOKEN_ENV, "   ")
        token_file = tmp_path / "token"
        token_file.write_text("file-token")
        monkeypatch.setenv(OAUTH_TOKEN_FILE_ENV, str(token_file))
        token, source = _resolve_oauth_token()
        assert token == "file-token"  # noqa: S105
        assert OAUTH_TOKEN_FILE_ENV in source

    def test_token_file_env_var_used_when_main_env_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        token_file = tmp_path / "custom-token"
        token_file.write_text("custom-token-value")
        monkeypatch.setenv(OAUTH_TOKEN_FILE_ENV, str(token_file))
        token, source = _resolve_oauth_token()
        assert token == "custom-token-value"  # noqa: S105
        assert str(token_file) in source
        assert OAUTH_TOKEN_FILE_ENV in source

    def test_conventional_path_used_when_envs_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        conventional = tmp_path / "conventional"
        conventional.write_text("conventional-token")
        monkeypatch.setattr("askcc.runners.CONVENTIONAL_TOKEN_FILE", conventional)
        token, source = _resolve_oauth_token()
        assert token == "conventional-token"  # noqa: S105
        assert str(conventional) in source

    def test_xdg_path_used(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        xdg_dir = tmp_path / "xdg"
        (xdg_dir / "claude").mkdir(parents=True)
        (xdg_dir / "claude" / "oauth-token").write_text("xdg-token")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
        token, source = _resolve_oauth_token()
        assert token == "xdg-token"  # noqa: S105
        assert "oauth-token" in source

    def test_xdg_path_default_when_xdg_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "claude").mkdir(parents=True)
        (fake_home / ".config" / "claude" / "oauth-token").write_text("home-xdg-token")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr("askcc.runners.Path.home", lambda: fake_home)
        token, source = _resolve_oauth_token()
        assert token == "home-xdg-token"  # noqa: S105
        assert ".config/claude/oauth-token" in source

    def test_credentials_json_used_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "creds-token"}}))
        monkeypatch.setattr("askcc.runners.CREDENTIALS_JSON_FILE", creds)
        with caplog.at_level("INFO", logger="askcc.runners"):
            token, source = _resolve_oauth_token()
        assert token == "creds-token"  # noqa: S105
        assert str(creds) in source

    def test_trailing_newline_stripped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        token_file = tmp_path / "token"
        token_file.write_text("abc\n")
        monkeypatch.setenv(OAUTH_TOKEN_FILE_ENV, str(token_file))
        token, _ = _resolve_oauth_token()
        assert token == "abc"  # noqa: S105

    def test_unreadable_file_warns_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        unreadable = tmp_path / "unreadable"
        unreadable.write_text("ignored")
        monkeypatch.setenv(OAUTH_TOKEN_FILE_ENV, str(unreadable))

        conventional = tmp_path / "conventional"
        conventional.write_text("from-conventional")
        monkeypatch.setattr("askcc.runners.CONVENTIONAL_TOKEN_FILE", conventional)

        original_read_text = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs) -> str:
            if self == unreadable:
                raise PermissionError("denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        with caplog.at_level("WARNING", logger="askcc.runners"):
            token, source = _resolve_oauth_token()
        assert token == "from-conventional"  # noqa: S105
        assert str(conventional) in source
        assert "cannot read token file" in caplog.text
        assert str(unreadable) in caplog.text

    def test_malformed_credentials_json_warns_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        creds = tmp_path / "credentials.json"
        creds.write_text("{not json")
        monkeypatch.setattr("askcc.runners.CREDENTIALS_JSON_FILE", creds)
        with caplog.at_level("WARNING", logger="askcc.runners"), pytest.raises(OAuthTokenNotFoundError):
            _resolve_oauth_token()
        assert "failed to parse" in caplog.text
        assert str(creds) in caplog.text

    def test_credentials_json_missing_keys_warns_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"otherSchema": {"foo": "bar"}}))
        monkeypatch.setattr("askcc.runners.CREDENTIALS_JSON_FILE", creds)
        with caplog.at_level("WARNING", logger="askcc.runners"), pytest.raises(OAuthTokenNotFoundError):
            _resolve_oauth_token()
        assert "claudeAiOauth.accessToken" in caplog.text

    def test_empty_token_file_falls_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        empty_file = tmp_path / "empty"
        empty_file.write_text("   \n  \n")
        monkeypatch.setenv(OAUTH_TOKEN_FILE_ENV, str(empty_file))
        with pytest.raises(OAuthTokenNotFoundError):
            _resolve_oauth_token()

    def test_all_sources_empty_raises_with_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv(OAUTH_TOKEN_FILE_ENV, str(tmp_path / "missing-custom"))
        monkeypatch.setattr("askcc.runners.CONVENTIONAL_TOKEN_FILE", tmp_path / "missing-conventional")
        monkeypatch.setattr("askcc.runners.CREDENTIALS_JSON_FILE", tmp_path / "missing-credentials.json")
        with pytest.raises(OAuthTokenNotFoundError) as exc_info:
            _resolve_oauth_token()
        message = str(exc_info.value)
        assert OAUTH_TOKEN_ENV in message
        assert OAUTH_TOKEN_FILE_ENV in message
        assert "missing-custom" in message
        assert "missing-conventional" in message
        assert "missing-credentials.json" in message

    def test_read_credentials_json_helper_returns_none_when_missing(self, tmp_path: Path):
        result = _read_credentials_json(tmp_path / "does-not-exist")
        assert result is None

    def test_read_token_file_helper_returns_none_when_missing(self, tmp_path: Path):
        result = _read_token_file(tmp_path / "does-not-exist")
        assert result is None


class TestClaudeRunnerOAuthIntegration:
    """Tests that ClaudeRunner injects the resolved OAuth token into the subprocess env."""

    ISSUE_URL = "https://github.com/test/repo/issues/1"

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

    @pytest.fixture
    def runner(self) -> ClaudeRunner:
        return ClaudeRunner()

    def test_runner_injects_resolved_token_into_subprocess_env(self, runner: ClaudeRunner, agent_config: AgentConfig):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with (
            patch(
                "askcc.runners._resolve_oauth_token",
                return_value=("xyz", f"file /tmp/example-{OAUTH_TOKEN_ENV}"),
            ),
            patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run,
        ):
            runner.run("prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())
        env = mock_run.call_args[1]["env"]
        assert env[OAUTH_TOKEN_ENV] == "xyz"

    def test_runner_logs_source_for_non_env_resolution(
        self,
        runner: ClaudeRunner,
        agent_config: AgentConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with (
            caplog.at_level("INFO", logger="askcc.runners"),
            patch(
                "askcc.runners._resolve_oauth_token",
                return_value=("token", "file /home/user/.tokens/.claude-oauth-token"),
            ),
            patch("askcc.runners.subprocess.run", return_value=mock_result),
        ):
            runner.run("prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())
        assert "auth: loaded" in caplog.text
        assert ".claude-oauth-token" in caplog.text

    def test_runner_no_log_when_env_var_source(
        self,
        runner: ClaudeRunner,
        agent_config: AgentConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with (
            caplog.at_level("INFO", logger="askcc.runners"),
            patch(
                "askcc.runners._resolve_oauth_token",
                return_value=("token", f"env {OAUTH_TOKEN_ENV}"),
            ),
            patch("askcc.runners.subprocess.run", return_value=mock_result),
        ):
            runner.run("prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())
        assert "auth: loaded" not in caplog.text

    def test_runner_logs_warning_when_credentials_json_used(
        self,
        runner: ClaudeRunner,
        agent_config: AgentConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        creds_path = Path.home() / ".claude" / ".credentials.json"
        with (
            caplog.at_level("WARNING", logger="askcc.runners"),
            patch(
                "askcc.runners._resolve_oauth_token",
                return_value=("token", f"file {creds_path}"),
            ),
            patch("askcc.runners.subprocess.run", return_value=mock_result),
        ):
            runner.run("prompt", config=agent_config, issue_url=self.ISSUE_URL, cwd=Path.cwd())
        assert "can be stale" in caplog.text


class TestMainOAuthExitPath:
    """Tests CLI exit behavior when OAuth token resolution fails."""

    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def test_main_exits_nonzero_when_oauth_resolution_fails(self, caplog: pytest.LogCaptureFixture):
        failing_runner = MagicMock()
        failing_runner.run.side_effect = OAuthTokenNotFoundError(
            "no Claude credentials found in any of: env CLAUDE_CODE_OAUTH_TOKEN, ..."
        )
        with (
            caplog.at_level("ERROR", logger="askcc.cli"),
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.get_runner", return_value=failing_runner),
            patch("sys.argv", ["askcc", "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        assert "no Claude credentials found" in caplog.text


class TestParseFrontmatter:
    """Tests for the YAML-style subagent frontmatter parser."""

    def _parse(self, text: str) -> tuple[dict, str]:
        return parse_frontmatter(text, source="test.md")

    def test_no_frontmatter_returns_empty_and_full_text(self):
        text = "Just a plain prompt body.\nNo frontmatter here.\n"
        fields, body = self._parse(text)
        assert fields == {}
        assert body == text

    def test_basic_frontmatter_parsed(self):
        text = "---\nname: develop\nmodel: opus\neffort: max\n---\nPrompt body here.\n"
        fields, body = self._parse(text)
        assert fields["name"] == "develop"
        assert fields["model"] == "opus"
        assert fields["effort"] == "max"
        assert body == "Prompt body here.\n"

    def test_tools_parsed_as_tuple(self):
        text = "---\ntools: Read, Write, Bash(gh:*)\n---\nbody\n"
        fields, _ = self._parse(text)
        assert fields["tools"] == ("Read", "Write", "Bash(gh:*)")

    def test_int_field_parsed(self):
        text = "---\nmax_thinking_tokens: 32000\nmax_turns: 200\n---\nbody\n"
        fields, _ = self._parse(text)
        assert fields["max_thinking_tokens"] == 32000
        assert fields["max_turns"] == 200

    def test_int_field_invalid_raises(self):
        text = "---\nmax_thinking_tokens: not-a-number\n---\nbody\n"
        with pytest.raises(ValueError, match="must be an integer"):
            self._parse(text)

    def test_invalid_model_raises(self):
        text = "---\nmodel: opuz\n---\nbody\n"
        with pytest.raises(ValueError, match="model.*invalid value"):
            self._parse(text)

    def test_invalid_effort_raises(self):
        text = "---\neffort: turbo\n---\nbody\n"
        with pytest.raises(ValueError, match="effort.*invalid value"):
            self._parse(text)

    def test_unclosed_frontmatter_raises(self):
        text = "---\nname: develop\nbody never ends with closing delimiter\n"
        with pytest.raises(ValueError, match="missing the closing"):
            self._parse(text)

    def test_unknown_key_warned_and_ignored(self, caplog: pytest.LogCaptureFixture):
        text = "---\nname: develop\nmade_up_field: foo\n---\nbody\n"
        with caplog.at_level("WARNING", logger="askcc.functions"):
            fields, _ = self._parse(text)
        assert "made_up_field" not in fields
        assert "Unknown frontmatter key" in caplog.text

    def test_blank_and_comment_lines_skipped(self):
        text = "---\n# a comment\nname: develop\n\nmodel: opus\n---\nbody\n"
        fields, _ = self._parse(text)
        assert fields == {"name": "develop", "model": "opus"}


class TestLoadAgentConfigFrontmatter:
    """Tests that load_agent_config picks up frontmatter overrides from disk."""

    def test_default_template_yields_frontmatter_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)
        bootstrap_templates()

        config = load_agent_config(AgentAction.DEVELOP)
        assert config.model == "opus"
        assert config.effort == "max"
        assert "Edit" in (config.tools or ())
        # Frontmatter should be stripped from the system prompt body
        assert not config.system_prompt.startswith("---")

    def test_template_without_frontmatter_back_compat(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        # User-style template with no frontmatter
        (templates_dir / "PLAN_SYSTEM_PROMPT.md").write_text("Plain system prompt with no frontmatter.\n")
        (templates_dir / "PLAN_USER_PROMPT.md").write_text("Read $issue_content_file and plan.\n")

        config = load_agent_config(AgentAction.PLAN)
        assert config.system_prompt == "Plain system prompt with no frontmatter.\n"
        assert config.model is None
        assert config.effort is None
        assert config.tools is None

    def test_user_frontmatter_overrides_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        custom = "---\nmodel: haiku\neffort: low\n---\nCustom plan body.\n"
        (templates_dir / "PLAN_SYSTEM_PROMPT.md").write_text(custom)
        (templates_dir / "PLAN_USER_PROMPT.md").write_text("Read $issue_content_file and plan.\n")

        config = load_agent_config(AgentAction.PLAN)
        assert config.model == "haiku"
        assert config.effort == "low"
        assert config.system_prompt == "Custom plan body.\n"

    def test_invalid_frontmatter_raises_at_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir(parents=True)
        monkeypatch.setattr("askcc.functions.TEMPLATES_DIR", templates_dir)

        bad = "---\nmodel: opuz\n---\nbody\n"
        (templates_dir / "PLAN_SYSTEM_PROMPT.md").write_text(bad)
        (templates_dir / "PLAN_USER_PROMPT.md").write_text("Read $issue_content_file and plan.\n")

        with pytest.raises(ValueError, match="model.*invalid value"):
            load_agent_config(AgentAction.PLAN)


class TestEffortPrecedence:
    """Tests for CLI > env > frontmatter > built-in default precedence."""

    ISSUE_URL = "https://github.com/monkut/askcc-cli/issues/1"

    def _run_main(self, args: list[str], frontmatter_effort: str | None = "high") -> MagicMock:
        mock_runner = _mock_runner()
        # Build a fake AgentConfig that overrides the default frontmatter effort
        base = AGENT_CONFIGS[AgentAction.PLAN]
        fake_config = AgentConfig(
            action_name=base.action_name,
            description=base.description,
            system_prompt="body",
            user_prompt_template=base.user_prompt_template,
            system_prompt_file=base.system_prompt_file,
            user_prompt_file=base.user_prompt_file,
            required_variables=base.required_variables,
            effort=frontmatter_effort,
        )
        with (
            patch("askcc.cli.bootstrap_templates"),
            patch("askcc.cli.validate_issue_labels", return_value=[]),
            patch("askcc.cli.fetch_github_issue", return_value="issue body"),
            patch("askcc.cli.load_agent_config", return_value=fake_config),
            patch("askcc.cli.transition_issue_to_development"),
            patch("askcc.cli.get_runner", return_value=mock_runner),
            patch("sys.argv", ["askcc", *args, "plan", "-g", self.ISSUE_URL]),
            pytest.raises(SystemExit),
        ):
            main()
        return mock_runner

    def test_cli_overrides_frontmatter_and_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "medium")
        runner = self._run_main(["--effort", "max"], frontmatter_effort="low")
        assert runner.run.call_args.kwargs["effort_level"] == "max"

    def test_env_overrides_frontmatter(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASKCC_CLAUDE_EFFORT_LEVEL", "medium")
        runner = self._run_main([], frontmatter_effort="low")
        assert runner.run.call_args.kwargs["effort_level"] == "medium"

    def test_frontmatter_used_when_no_cli_or_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_CLAUDE_EFFORT_LEVEL", raising=False)
        runner = self._run_main([], frontmatter_effort="low")
        assert runner.run.call_args.kwargs["effort_level"] == "low"

    def test_default_used_when_no_cli_env_or_frontmatter(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASKCC_CLAUDE_EFFORT_LEVEL", raising=False)
        runner = self._run_main([], frontmatter_effort=None)
        assert runner.run.call_args.kwargs["effort_level"] == DEFAULT_EFFORT_LEVEL


class TestRunnerFrontmatterFlags:
    """Tests that ClaudeRunner translates AgentConfig frontmatter fields to CLI flags."""

    ISSUE_URL = "https://github.com/test/repo/issues/1"

    @pytest.fixture(autouse=True)
    def _set_oauth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OAUTH_TOKEN_ENV, "test-oauth-token")  # noqa: S105

    def _config(self, **overrides) -> AgentConfig:
        base = {
            "action_name": "test",
            "description": "test",
            "system_prompt": "prompt",
            "user_prompt_template": "$issue_content_file",
            "system_prompt_file": "TEST_SYSTEM_PROMPT.md",
            "user_prompt_file": "TEST_USER_PROMPT.md",
        }
        base.update(overrides)
        return AgentConfig(**base)

    def _run(self, config: AgentConfig) -> list[str]:
        runner = ClaudeRunner()
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch("askcc.runners.subprocess.run", return_value=mock_result) as mock_run:
            runner.run("prompt", config=config, issue_url=self.ISSUE_URL, cwd=Path.cwd())
        return mock_run.call_args[0][0]

    def test_model_flag_emitted(self):
        cmd = self._run(self._config(model="opus"))
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"

    def test_tools_flag_emitted_as_csv(self):
        cmd = self._run(self._config(tools=("Read", "Write", "Bash(gh:*)")))
        assert "--allowedTools" in cmd
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,Write,Bash(gh:*)"

    def test_disallowed_tools_flag_emitted(self):
        cmd = self._run(self._config(disallowed_tools=("WebFetch",)))
        assert "--disallowedTools" in cmd
        assert cmd[cmd.index("--disallowedTools") + 1] == "WebFetch"

    def test_max_turns_flag_emitted(self):
        cmd = self._run(self._config(max_turns=200))
        assert "--max-turns" in cmd
        assert cmd[cmd.index("--max-turns") + 1] == "200"

    def test_no_frontmatter_flags_when_unset(self):
        cmd = self._run(self._config())
        for flag in ("--model", "--allowedTools", "--disallowedTools", "--max-turns"):
            assert flag not in cmd
