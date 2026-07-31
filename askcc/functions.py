import json
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, replace
from importlib.resources import files as package_files
from pathlib import Path
from string import Template
from urllib.parse import urlparse

from .definitions import (
    AGENT_CONFIGS,
    KNOWN_FRONTMATTER_KEYS,
    VALID_FRONTMATTER_MODELS,
    AgentAction,
    AgentConfig,
)
from .settings import (
    BLOCKING_LABELS,
    DEVELOP_LABEL,
    ENABLE_ISSUE_LABEL_PREFIX_VALIDATION,
    PLAN_LABEL,
    PLANNING_STATUS_OPTIONS,
    READY_STATUS_OPTIONS,
    REQUIRED_ISSUE_LABEL_PREFIXES,
    REVIEW_LABEL,
    REVIEW_STATUS_OPTIONS,
    TEMPLATES_DIR,
    VALID_EFFORT_LEVELS,
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


CLOSE_KEYWORD_RE = re.compile(
    r"\b(?:closes|closed|close|fixes|fixed|fix|resolves|resolved|resolve)\s+"
    r"(?:(?P<qualified>[\w.-]+/[\w.-]+))?#(?P<num>\d+)(?!\d)",
    re.IGNORECASE,
)


def _resolve_transferred_predecessors(gh: str, owner: str, repo: str, issue_number: int) -> list[tuple[str, str, int]]:
    """Return prior ``(owner, repo, number)`` coordinates of a transferred issue.

    Queries the GitHub Timeline API for ``transferred`` events and extracts
    each predecessor from ``source.issue``. GitHub currently exposes only the
    immediately prior hop; the list shape is kept for forward compatibility.

    All subprocess/JSON errors are logged and yield an empty list — never raised.
    """
    repo_nwo = f"{owner}/{repo}"
    try:
        result = subprocess.run(  # noqa: S603
            [gh, "api", "--paginate", f"repos/{repo_nwo}/issues/{issue_number}/timeline"],
            capture_output=True,
            text=True,
            check=True,
        )
        events = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch timeline for %s#%d: %s", repo_nwo, issue_number, exc)
        return []

    predecessors: list[tuple[str, str, int]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "transferred":
            continue
        source_issue = (event.get("source") or {}).get("issue") or {}
        prior_repo = source_issue.get("repository") or {}
        prior_owner = (prior_repo.get("owner") or {}).get("login")
        prior_repo_name = prior_repo.get("name")
        prior_number = source_issue.get("number")
        if prior_owner and prior_repo_name and prior_number is not None:
            predecessors.append((prior_owner, prior_repo_name, int(prior_number)))
    return predecessors


def _body_references_target(
    body: str,
    targets: set[tuple[str, str, int]],
    current_owner: str,
    current_repo: str,
) -> bool:
    """Return True if ``body`` positively references one of ``targets``.

    Accepts close-keyword references (``Closes #N`` / ``Fixes owner/repo#N`` /
    ``Resolves …``) and word-bounded ``/issues/N`` URL references. Repo
    qualifiers are compared case-insensitively.
    """
    targets_lower = {(o.lower(), r.lower(), n) for o, r, n in targets}
    target_numbers = {n for *_, n in targets}
    current_key = (current_owner.lower(), current_repo.lower())

    for match in CLOSE_KEYWORD_RE.finditer(body):
        num = int(match.group("num"))
        qualified = match.group("qualified")
        if qualified is None:
            if (*current_key, num) in targets_lower:
                return True
        else:
            qowner, _, qrepo = qualified.partition("/")
            if (qowner.lower(), qrepo.lower(), num) in targets_lower:
                return True

    return any(re.search(rf"/issues/{num}(?!\d)", body) for num in target_numbers)


def _find_linked_pr_number(gh: str, owner: str, repo: str, issue_number: int) -> int | None:
    """Find the most recent open PR positively linked to ``issue_number``.

    Matching rules (in priority order):
      1. PR branch name matches ``^[^/]+/{N}-`` for any ``N`` in
         ``{issue_number} ∪ predecessor_numbers``.
      2. PR body contains a close keyword (Closes/Fixes/Resolves and
         inflections) referencing the issue or a predecessor — bare ``#N``
         (current repo) or qualified ``owner/repo#N``.
      3. PR body contains ``/issues/N`` with a digit boundary for any target.

    Predecessor coordinates come from ``_resolve_transferred_predecessors`` so
    PRs created against a pre-transfer issue can still be paired after the
    transfer. Returns ``None`` if no PR positively references the issue or any
    predecessor — callers (``fetch_pr_content``) treat this as "no link".
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

    predecessors = _resolve_transferred_predecessors(gh, owner, repo, issue_number)
    targets: set[tuple[str, str, int]] = {(owner, repo, issue_number), *predecessors}
    target_numbers = {n for *_, n in targets}

    branch_patterns = [re.compile(rf"^[^/]+/{n}-") for n in target_numbers]
    for pr in prs:
        head = pr.get("headRefName", "") or ""
        if any(p.match(head) for p in branch_patterns):
            return pr["number"]

    for pr in prs:
        body = pr.get("body", "") or ""
        if _body_references_target(body, targets, owner, repo):
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
    advisory: bool = False  # reported but never blocks `develop` or the `validate` exit code


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    message: str
    checks: list[CheckResult]


VERIFICATION_TIMEOUT = 300  # 5 minutes per command


def _detect_verification_commands(cwd: Path) -> list[tuple[str, list[str]]]:
    """Load user-configured verification commands from project config.

    Checks (in order):
    1. ``[tool.askcc.verify]`` in ``pyproject.toml``
    2. ``[[verify]]`` in ``.askcc.toml``

    Returns a list of (check_name, command_args) tuples.
    If no config is found, returns an empty list (verification is skipped).
    """
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text())
            entries = data.get("tool", {}).get("askcc", {}).get("verify", [])
            if entries:
                return [(e["name"], shlex.split(e["cmd"])) for e in entries if "name" in e and "cmd" in e]
        except (tomllib.TOMLDecodeError, KeyError):
            logger.warning("Failed to parse [tool.askcc.verify] from %s", pyproject)

    askcc_toml = cwd / ".askcc.toml"
    if askcc_toml.exists():
        try:
            data = tomllib.loads(askcc_toml.read_text())
            entries = data.get("verify", [])
            if entries:
                return [(e["name"], shlex.split(e["cmd"])) for e in entries if "name" in e and "cmd" in e]
        except (tomllib.TOMLDecodeError, KeyError):
            logger.warning("Failed to parse [[verify]] from %s", askcc_toml)

    return []


def _run_project_verification(cwd: Path) -> VerificationResult:
    """Run detected project verification commands and return aggregate result."""
    commands = _detect_verification_commands(cwd)
    if not commands:
        logger.info("No verification commands detected in %s, skipping verification", cwd)
        return VerificationResult(passed=True, message="No verification commands detected, skipping", checks=[])

    checks: list[CheckResult] = []
    for name, cmd in commands:
        logger.info("Running verification: %s (%s)", name, " ".join(cmd))
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                check=False,
                cwd=cwd,
                timeout=VERIFICATION_TIMEOUT,
            )
            if result.returncode == 0:
                checks.append(CheckResult(name=name, passed=True, message="passed"))
            else:
                stderr_snippet = result.stderr.strip()[:200] if result.stderr else result.stdout.strip()[:200]
                checks.append(CheckResult(name=name, passed=False, message=f"failed: {stderr_snippet}"))
        except FileNotFoundError:
            checks.append(CheckResult(name=name, passed=False, message=f"command not found: {cmd[0]}"))
        except subprocess.TimeoutExpired:
            checks.append(CheckResult(name=name, passed=False, message=f"timed out after {VERIFICATION_TIMEOUT}s"))

    all_passed = all(c.passed for c in checks)
    passed_count = sum(1 for c in checks if c.passed)
    total = len(checks)
    message = f"{passed_count}/{total} checks passed"
    return VerificationResult(passed=all_passed, message=message, checks=checks)


def _has_acceptance_criteria(body: str) -> bool:
    """Check for an acceptance criteria section with checklist items.

    The section ends at the next heading at the same level or higher (fewer #s),
    so deeper sub-headings (e.g. ### inside an ## section) stay part of it.
    """
    match = re.search(r"(#{2,})\s+acceptance\s+criteria", body, re.IGNORECASE)
    if not match:
        return False
    heading_level = len(match.group(1))
    section_start = match.end()
    next_heading = re.search(rf"\n#{{1,{heading_level}}}\s+", body[section_start:])
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

    # 2. Dependencies identified (advisory — an issue with no dependencies is still development-ready)
    has_deps = _has_dependencies_section(body)
    checks.append(
        CheckResult(
            name="Dependencies identified",
            passed=has_deps,
            message="Dependencies section found" if has_deps else "No dependencies/context section found",
            advisory=True,
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


def transition_issue_to_development(github_issue_url: str) -> None:
    """Transition issue labels and project state after successful planning.

    Swaps action:plan -> action:develop and moves project status to ready/todo.
    If action:plan is not present, action:develop is still added (the remove
    step warns but does not raise).
    All failures are logged as warnings, never raised.
    """
    gh = _require_gh_cli()
    owner, repo, issue_number = _parse_issue_url(github_issue_url)
    repo_nwo = f"{owner}/{repo}"

    _swap_issue_labels(gh, repo_nwo, issue_number, remove=PLAN_LABEL, add=DEVELOP_LABEL)
    _transition_project_fields(gh, owner, repo, issue_number, status_options=READY_STATUS_OPTIONS)


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


FRONTMATTER_DELIMITER = "---"
_LIST_FRONTMATTER_FIELDS: frozenset[str] = frozenset({"tools", "disallowed_tools"})
_INT_FRONTMATTER_FIELDS: frozenset[str] = frozenset({"max_thinking_tokens", "max_turns"})


def _coerce_frontmatter_value(key: str, value: str, source: str) -> object:
    """Coerce a raw frontmatter value to its typed form (list/int/string)."""
    if key in _LIST_FRONTMATTER_FIELDS:
        return tuple(item.strip() for item in value.split(",") if item.strip()) if value else ()
    if key in _INT_FRONTMATTER_FIELDS:
        try:
            return int(value)
        except ValueError as exc:
            msg = f"Frontmatter field '{key}' in {source} must be an integer, got {value!r}"
            raise ValueError(msg) from exc
    return value


def _validate_frontmatter_enums(fields: dict, source: str) -> None:
    """Raise ValueError when enum-valued fields contain unsupported values."""
    if "model" in fields and fields["model"] not in VALID_FRONTMATTER_MODELS:
        msg = (
            f"Frontmatter field 'model' in {source} has invalid value {fields['model']!r}"
            f" (allowed: {', '.join(VALID_FRONTMATTER_MODELS)})"
        )
        raise ValueError(msg)
    if "effort" in fields:
        try:
            VALID_EFFORT_LEVELS(fields["effort"])
        except ValueError as exc:
            msg = (
                f"Frontmatter field 'effort' in {source} has invalid value {fields['effort']!r}"
                f" (allowed: {', '.join(VALID_EFFORT_LEVELS)})"
            )
            raise ValueError(msg) from exc


def parse_frontmatter(text: str, *, source: str = "<inline>") -> tuple[dict, str]:
    r"""Split a Claude Code subagent-style YAML frontmatter block from its body.

    Recognizes a leading `---\n...\n---\n` block. Supports flat `key: value`
    lines only — no nested mappings or multi-line values. List fields are
    comma-separated; int fields are parsed as integers.

    Returns (parsed_fields, body_without_frontmatter). When no frontmatter is
    present, returns ({}, text) unchanged for back-compat.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != FRONTMATTER_DELIMITER:
        return {}, text

    end_idx = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == FRONTMATTER_DELIMITER),
        None,
    )
    if end_idx is None:
        msg = f"Frontmatter in {source} is missing the closing '---' delimiter"
        raise ValueError(msg)

    fields: dict = {}
    for raw_line in lines[1:end_idx]:
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            msg = f"Frontmatter in {source} has malformed line (no ':'): {line!r}"
            raise ValueError(msg)
        key, _, value = line.partition(":")
        key = key.strip()
        if key not in KNOWN_FRONTMATTER_KEYS:
            logger.warning("Unknown frontmatter key in %s: %r — ignoring", source, key)
            continue
        fields[key] = _coerce_frontmatter_value(key, value.strip(), source)

    _validate_frontmatter_enums(fields, source)
    body = "".join(lines[end_idx + 1 :])
    return fields, body


def load_agent_config(agent: AgentAction) -> AgentConfig:
    """Load an AgentConfig with templates read from disk, falling back to built-in defaults.

    If the loaded system_prompt begins with a `---`-delimited frontmatter block,
    its fields override the AgentConfig's defaults (model, effort, tools, etc.)
    and the body becomes the system_prompt. Templates without frontmatter are
    returned unchanged.
    """
    base = AGENT_CONFIGS[agent]
    user_prompt_template = load_template(base.user_prompt_file, base.user_prompt_template)
    validate_template(user_prompt_template, base.required_variables, base.user_prompt_file)
    raw_system_prompt = load_template(base.system_prompt_file, base.system_prompt)
    fields, body = parse_frontmatter(raw_system_prompt, source=base.system_prompt_file)
    overrides: dict = {"system_prompt": body, "user_prompt_template": user_prompt_template}
    for key in ("tools", "disallowed_tools", "model", "effort", "max_thinking_tokens", "max_turns"):
        if key in fields:
            overrides[key] = fields[key]
    return replace(base, **overrides)
