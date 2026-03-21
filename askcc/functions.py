import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from importlib.resources import files as package_files
from pathlib import Path
from string import Template
from urllib.parse import urlparse

from .definitions import AGENT_CONFIGS, AgentAction, AgentConfig
from .settings import (
    BLOCKING_LABELS,
    DEVELOP_LABEL,
    ENABLE_ISSUE_LABEL_PREFIX_VALIDATION,
    PLANNING_STATUS_OPTIONS,
    REQUIRED_ISSUE_LABEL_PREFIXES,
    REVIEW_LABEL,
    REVIEW_STATUS_OPTIONS,
    TEMPLATES_DIR,
)

logger = logging.getLogger(__name__)

MIN_ISSUE_URL_PARTS = 4


def _parse_issue_url(github_issue_url: str) -> tuple[str, str, int]:
    """Parse a GitHub issue URL into (owner, repo, issue_number)."""
    parsed = urlparse(github_issue_url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < MIN_ISSUE_URL_PARTS or parts[2] != "issues":
        msg = f"Invalid GitHub issue URL: {github_issue_url}"
        raise ValueError(msg)
    owner = parts[0]
    repo = parts[1]
    issue_number = int(parts[3])
    return owner, repo, issue_number


def _require_gh_cli() -> str:
    """Return the path to the gh CLI, raising if not found."""
    gh_path = shutil.which("gh")
    if not gh_path:
        msg = "'gh' CLI is not installed or not on PATH. Install it from https://cli.github.com/"
        raise FileNotFoundError(msg)
    return gh_path


def fetch_github_issue(github_issue_url: str) -> str:
    """Fetch a GitHub issue description and all comments, combined into a single string."""
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"
    logger.info("Fetching issue #%d from %s ...", issue_number, repo_nwo)

    # Fetch issue body
    issue_result = subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/issues/{issue_number}", "--jq", ".title, .body"],
        capture_output=True,
        text=True,
        check=True,
    )
    issue_text = issue_result.stdout.strip()

    logger.info("Fetching comments for issue #%d ...", issue_number)
    comments_result = subprocess.run(  # noqa: S603
        [gh, "api", "--paginate", f"repos/{repo_nwo}/issues/{issue_number}/comments"],
        capture_output=True,
        text=True,
        check=True,
    )
    comments_data = json.loads(comments_result.stdout)
    comment_texts = [f"Comment by @{c['user']['login']}:\n{c['body']}" for c in comments_data]

    sections = [f"Issue URL: {github_issue_url}\n\nIssue #{issue_number}:\n{issue_text}"]
    if comment_texts:
        sections.append("Comments:\n" + "\n---\n".join(comment_texts))
    logger.info("Fetched issue with %d comment(s)", len(comment_texts))

    return "\n\n".join(sections)


def _find_linked_pr_number(gh: str, owner: str, repo: str, issue_number: int) -> int | None:
    """Find the most recent open PR linked to the given issue number.

    Searches by branch naming convention (e.g. feature/N-*, add/N-*) first,
    then falls back to matching issue references in PR body text.
    """
    repo_nwo = f"{owner}/{repo}"
    try:
        result = subprocess.run(  # noqa: S603
            [gh, "pr", "list", "-R", repo_nwo, "--json", "number,headRefName,body", "--limit", "50"],
            capture_output=True,
            text=True,
            check=True,
        )
        prs = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.warning("Failed to list PRs for %s: %s", repo_nwo, exc)
        return None

    # Match branch names like "type/N-description" (askcc convention)
    branch_pattern = re.compile(rf"^[^/]+/{issue_number}-")
    for pr in prs:
        if branch_pattern.match(pr.get("headRefName", "")):
            return pr["number"]

    # Fallback: check PR body for issue reference
    for pr in prs:
        body = pr.get("body", "") or ""
        if f"#{issue_number}" in body or f"/issues/{issue_number}" in body:
            return pr["number"]

    return None


def fetch_pr_content(github_issue_url: str) -> str:
    """Fetch the linked PR's metadata, diff, and review comments for code review."""
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    pr_number = _find_linked_pr_number(gh, owner, repo, issue_number)
    if pr_number is None:
        msg = f"No linked pull request found for issue #{issue_number} in {repo_nwo}"
        raise ValueError(msg)

    logger.info("Found linked PR #%d for issue #%d", pr_number, issue_number)

    # Fetch PR metadata
    pr_result = subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/pulls/{pr_number}"],
        capture_output=True,
        text=True,
        check=True,
    )
    pr_data = json.loads(pr_result.stdout)
    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body", "") or ""
    pr_url = pr_data.get("html_url", "")

    # Fetch PR diff
    diff_result = subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/pulls/{pr_number}", "-H", "Accept: application/vnd.github.v3.diff"],
        capture_output=True,
        text=True,
        check=True,
    )
    diff = diff_result.stdout

    # Fetch PR review comments
    comments_result = subprocess.run(  # noqa: S603
        [gh, "api", "--paginate", f"repos/{repo_nwo}/pulls/{pr_number}/comments"],
        capture_output=True,
        text=True,
        check=True,
    )
    comments = json.loads(comments_result.stdout)
    comment_texts = [
        f"Review comment by @{c['user']['login']} on {c.get('path', 'unknown')}:\n{c['body']}" for c in comments
    ]

    sections = [
        f"Pull Request #{pr_number}\nURL: {pr_url}\nTitle: {pr_title}\n\n{pr_body}",
        f"PR Diff:\n{diff}",
    ]
    if comment_texts:
        sections.append("Existing Review Comments:\n" + "\n---\n".join(comment_texts))

    logger.info("Fetched PR #%d with %d review comment(s)", pr_number, len(comment_texts))
    return "\n\n".join(sections)


def validate_issue_labels(github_issue_url: str) -> list[str]:
    """Validate that the issue has at least one label matching each required prefix.

    Returns a list of error messages (empty if all prefixes are satisfied).
    """
    if not ENABLE_ISSUE_LABEL_PREFIX_VALIDATION or not REQUIRED_ISSUE_LABEL_PREFIXES:
        return []

    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    result = subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/issues/{issue_number}", "--jq", "[.labels[].name]"],
        capture_output=True,
        text=True,
        check=True,
    )
    labels: list[str] = json.loads(result.stdout)

    errors = []
    for prefix in REQUIRED_ISSUE_LABEL_PREFIXES:
        if not any(label.startswith(prefix) for label in labels):
            errors.append(f"Missing required label with prefix '{prefix}' (found: {labels})")
    return errors


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str


def _has_acceptance_criteria(body: str) -> bool:
    """Check for an acceptance criteria section with checklist items."""
    match = re.search(r"#{2,}\s+acceptance\s+criteria", body, re.IGNORECASE)
    if not match:
        return False
    section_start = match.end()
    next_heading = re.search(r"\n#{2,}\s+", body[section_start:])
    section = body[section_start : section_start + next_heading.start()] if next_heading else body[section_start:]
    return bool(re.search(r"-\s*\[[\sx]\]", section))


def _has_dependencies_section(body: str) -> bool:
    """Check for a dependencies, prerequisites, or context section heading."""
    return bool(re.search(r"#{2,}\s+(?:dependenc|prerequisit|context|blocker)", body, re.IGNORECASE))


def validate_issue_readiness(github_issue_url: str) -> list[CheckResult]:
    """Validate that a GitHub issue meets readiness criteria for development."""
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    result = subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/issues/{issue_number}"],
        capture_output=True,
        text=True,
        check=True,
    )
    issue = json.loads(result.stdout)
    body = issue.get("body") or ""

    checks: list[CheckResult] = []

    # 1. Acceptance criteria
    has_ac = _has_acceptance_criteria(body)
    checks.append(
        CheckResult(
            name="Acceptance criteria",
            passed=has_ac,
            message="Clear acceptance criteria found"
            if has_ac
            else "No acceptance criteria with checklist items found",
        )
    )

    # 2. Dependencies identified
    has_deps = _has_dependencies_section(body)
    checks.append(
        CheckResult(
            name="Dependencies identified",
            passed=has_deps,
            message="Dependencies section found" if has_deps else "No dependencies/context section found",
        )
    )

    # 3. Assignee confirmed
    assignees = issue.get("assignees") or []
    has_assignee = len(assignees) > 0
    assignee_names = ", ".join(a["login"] for a in assignees)
    checks.append(
        CheckResult(
            name="Assignee confirmed",
            passed=has_assignee,
            message=f"Assigned to: {assignee_names}" if has_assignee else "No assignee",
        )
    )

    # 4. No blocking labels
    label_names = [label["name"] for label in issue.get("labels") or []]
    blocking_found = [name for name in label_names if name in BLOCKING_LABELS]
    no_blockers = len(blocking_found) == 0
    checks.append(
        CheckResult(
            name="No blocking labels",
            passed=no_blockers,
            message="No blocking labels found"
            if no_blockers
            else f"Blocking labels present: {', '.join(blocking_found)}",
        )
    )

    return checks


CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_SKILLS_DIR = CLAUDE_HOME / "skills"
OPENCLAW_HOME = Path.home() / ".openclaw"
OPENCLAW_SKILLS_DIR = OPENCLAW_HOME / "workspace" / "skills"
OPENCLAW_CONFIG_PATH = OPENCLAW_HOME / "openclaw.json"


def _copy_skills(target_dir: Path) -> list[str]:
    """Copy bundled skill directories to target_dir. Returns list of installed skill names."""
    skills_source = package_files("askcc") / "skills"
    installed = []
    for skill_dir in sorted(skills_source.iterdir(), key=lambda p: p.name):
        if not skill_dir.is_dir() or skill_dir.name.startswith("__"):
            continue
        dest = target_dir / skill_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(str(skill_dir), dest)
        logger.info("Installed skill '%s' to %s", skill_dir.name, dest)
        installed.append(skill_dir.name)
    return installed


def install_skills(directory: Path | None = None) -> None:
    """Copy bundled skills to detected target directories.

    When --directory is given, install only there.
    Otherwise auto-detect ~/.claude and ~/.openclaw and install to whichever exist.
    """
    if directory:
        _copy_skills(directory.expanduser())
        return

    if CLAUDE_HOME.is_dir():
        CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _copy_skills(CLAUDE_SKILLS_DIR)
    else:
        logger.warning("Target directory not found: %s. Skipping skill installation.", CLAUDE_HOME)

    if OPENCLAW_HOME.is_dir():
        OPENCLAW_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        for name in _copy_skills(OPENCLAW_SKILLS_DIR):
            _register_skill(name)
    else:
        logger.warning("Target directory not found: %s. Skipping skill installation.", OPENCLAW_HOME)


def _register_skill(skill_name: str) -> None:
    """Add a skill entry to ~/.openclaw/openclaw.json."""
    config_path = OPENCLAW_CONFIG_PATH
    if not config_path.exists():
        msg = f"{config_path} not found, could not install"
        raise ValueError(msg)

    config = json.loads(config_path.read_text())

    skills = config.setdefault("skills", {})
    entries = skills.setdefault("entries", {})
    entries[skill_name] = {"enabled": True}

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    logger.info("Registered skill '%s' in %s", skill_name, config_path)


def bootstrap_templates() -> None:
    """Create TEMPLATES_DIR with default template files if they are missing."""
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    created_any = False
    for config in AGENT_CONFIGS.values():
        for path, content in (
            (TEMPLATES_DIR / config.system_prompt_file, config.system_prompt),
            (TEMPLATES_DIR / config.user_prompt_file, config.user_prompt_template),
        ):
            if not path.exists():
                path.write_text(content)
                created_any = True
    if created_any:
        logger.info("Created default templates in %s", TEMPLATES_DIR)


def load_template(file_name: str, default: str) -> str:
    """Read a template file from TEMPLATES_DIR, falling back to the default on missing file."""
    path = TEMPLATES_DIR / file_name
    try:
        return path.read_text()
    except FileNotFoundError:
        logger.warning("Template file not found: %s — using built-in default", path)
        return default


def validate_template(template_text: str, required_variables: tuple[str, ...], file_name: str) -> None:
    """Validate that a template contains all required $variables."""
    # Template identifiers are words preceded by $ (or ${...})
    # We check by attempting substitution with sentinel values
    tpl = Template(template_text)
    for var in required_variables:
        sentinel = f"__SENTINEL_{var}__"
        result = tpl.safe_substitute({var: sentinel})
        if sentinel not in result:
            msg = f"Template '{file_name}' is missing required variable '${var}'"
            raise ValueError(msg)


def append_usage_to_last_comment(github_issue_url: str, usage: dict) -> None:
    """Append a :tokens-used: line to the last comment on a GitHub issue."""
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    result = subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/issues/{issue_number}/comments", "--jq", ".[-1]"],
        capture_output=True,
        text=True,
        check=True,
    )

    comment_json = result.stdout.strip()
    if not comment_json or comment_json == "null":
        logger.warning("No comments found on issue #%d, skipping usage append", issue_number)
        return

    comment = json.loads(comment_json)
    comment_id = comment["id"]
    existing_body = comment["body"]

    model = usage.get("model", "N/A")
    input_tokens = usage.get("input_tokens", "N/A")
    output_tokens = usage.get("output_tokens", "N/A")
    usage_line = f"\n\n:tokens-used: model: {model}, input: {input_tokens}, output: {output_tokens}"
    updated_body = existing_body + usage_line

    subprocess.run(  # noqa: S603
        [gh, "api", f"repos/{repo_nwo}/issues/comments/{comment_id}", "-X", "PATCH", "-f", f"body={updated_body}"],
        capture_output=True,
        text=True,
        check=True,
    )
    logger.info("Appended token usage to comment %d on issue #%d", comment_id, issue_number)


def _swap_issue_labels(gh: str, repo_nwo: str, issue_number: int, *, remove: str, add: str) -> None:
    """Remove one label and add another on a GitHub issue.

    Adds the new label first, then removes the old one only on success.
    This prevents the issue from being left with no label if the target label doesn't exist.
    """
    try:
        subprocess.run(  # noqa: S603
            [gh, "issue", "edit", str(issue_number), "-R", repo_nwo, "--add-label", add],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to add label '%s' on issue #%d: %s", add, issue_number, exc.stderr)
        return
    try:
        subprocess.run(  # noqa: S603
            [gh, "issue", "edit", str(issue_number), "-R", repo_nwo, "--remove-label", remove],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to remove label '%s' on issue #%d: %s", remove, issue_number, exc.stderr)
        return
    logger.info("Transitioned labels: -%s +%s on issue #%d", remove, add, issue_number)


def _find_option_id(options: list[dict], target_names: tuple[str, ...]) -> str | None:
    """Find the first matching option ID from a list of single-select options (case-insensitive)."""
    lower_targets = {n.lower() for n in target_names}
    for option in options:
        if option.get("name", "").lower() in lower_targets:
            return option["id"]
    return None


_PROJECT_ITEMS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 10) {
        nodes {
          id
          project {
            id
            title
            statusField: field(name: "Status") {
              ... on ProjectV2SingleSelectField {
                id
                options { id name }
              }
            }
          }
        }
      }
    }
  }
}
"""

_UPDATE_FIELD_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) {
    projectV2Item { id }
  }
}
"""


def _update_project_field(
    gh: str,
    project_id: str,
    item_id: str,
    field_id: str,
    option_id: str,
    *,
    issue_number: int,
    project_title: str,
    field_name: str,
) -> None:
    """Update a single-select field on a project item. Warns on failure."""
    try:
        subprocess.run(  # noqa: S603
            [
                gh,
                "api",
                "graphql",
                "-f",
                f"query={_UPDATE_FIELD_MUTATION}",
                "-F",
                f"projectId={project_id}",
                "-F",
                f"itemId={item_id}",
                "-F",
                f"fieldId={field_id}",
                "-F",
                f"optionId={option_id}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info("Updated '%s' in project '%s' for issue #%d", field_name, project_title, issue_number)
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Failed to update '%s' in project '%s' for issue #%d: %s",
            field_name,
            project_title,
            issue_number,
            exc.stderr,
        )


def _transition_project_fields(
    gh: str,
    owner: str,
    repo: str,
    issue_number: int,
    *,
    status_options: tuple[str, ...] = REVIEW_STATUS_OPTIONS,
) -> None:
    """Move issue to a target status in project boards. Best-effort, warns on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            [
                gh,
                "api",
                "graphql",
                "-f",
                f"query={_PROJECT_ITEMS_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo}",
                "-F",
                f"number={issue_number}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to query project items for issue #%d: %s", issue_number, exc.stderr)
        return

    data = json.loads(result.stdout)
    items = data.get("data", {}).get("repository", {}).get("issue", {}).get("projectItems", {}).get("nodes") or []

    if not items:
        logger.info("Issue #%d is not in any project, skipping project transitions", issue_number)
        return

    for item in items:
        project = item.get("project", {})
        project_id = project.get("id")
        item_id = item.get("id")
        project_title = project.get("title", "unknown")

        # Update Status field
        status_field = project.get("statusField")
        if status_field and status_field.get("options"):
            option_id = _find_option_id(status_field["options"], status_options)
            if option_id:
                _update_project_field(
                    gh,
                    project_id,
                    item_id,
                    status_field["id"],
                    option_id,
                    issue_number=issue_number,
                    project_title=project_title,
                    field_name="Status",
                )


def _add_issue_label(gh: str, repo_nwo: str, issue_number: int, label: str) -> None:
    """Add a label to a GitHub issue. Warns on failure."""
    try:
        subprocess.run(  # noqa: S603
            [gh, "issue", "edit", str(issue_number), "-R", repo_nwo, "--add-label", label],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info("Added label '%s' to issue #%d", label, issue_number)
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to add label '%s' to issue #%d: %s", label, issue_number, exc.stderr)


def transition_issue_to_planning(github_issue_url: str) -> None:
    """Transition issue to planning state after successful preparation.

    Adds action:develop label and moves to planning column.
    All failures are logged as warnings, never raised.
    """
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    _add_issue_label(gh, repo_nwo, issue_number, DEVELOP_LABEL)
    _transition_project_fields(
        gh,
        owner,
        repo,
        issue_number,
        status_options=PLANNING_STATUS_OPTIONS,
    )


def transition_issue_to_review(github_issue_url: str) -> None:
    """Transition issue labels and project state after successful PR creation.

    All failures are logged as warnings, never raised.
    """
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    _swap_issue_labels(gh, repo_nwo, issue_number, remove=DEVELOP_LABEL, add=REVIEW_LABEL)
    _transition_project_fields(gh, owner, repo, issue_number)


def write_prompt_content(
    command: str, owner: str, repo: str, issue_number: int, content: str, *, suffix: str = ""
) -> Path:
    """Write variable prompt content to a named tempfile in /tmp.

    Returns the path to the written file.
    """
    filename = f"askcc_{command}_{owner}-{repo}_{issue_number}{suffix}.md"
    filepath = Path(tempfile.gettempdir()) / filename
    filepath.write_text(content)
    logger.info("Wrote prompt content to %s (%d chars)", filepath, len(content))
    return filepath


def load_agent_config(agent: AgentAction) -> AgentConfig:
    """Load an AgentConfig with templates read from disk, falling back to built-in defaults."""
    base = AGENT_CONFIGS[agent]
    user_prompt_template = load_template(base.user_prompt_file, base.user_prompt_template)
    validate_template(user_prompt_template, base.required_variables, base.user_prompt_file)
    return replace(
        base,
        system_prompt=load_template(base.system_prompt_file, base.system_prompt),
        user_prompt_template=user_prompt_template,
    )
