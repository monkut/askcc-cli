import json
import logging
import shutil
import subprocess
from dataclasses import replace
from urllib.parse import urlparse

from .definitions import AGENT_CONFIGS, AgentConfig, AgentType
from .settings import TEMPLATES_DIR

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

    sections = [f"Issue #{issue_number}:\n{issue_text}"]
    if comment_texts:
        sections.append("Comments:\n" + "\n---\n".join(comment_texts))
    logger.info("Fetched issue with %d comment(s)", len(comment_texts))

    return "\n\n".join(sections)


def bootstrap_templates() -> None:
    """Create ~/.askcc/templates/ with default template files if the directory doesn't exist."""
    if TEMPLATES_DIR.exists():
        return
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    for config in AGENT_CONFIGS.values():
        (TEMPLATES_DIR / config.system_prompt_file).write_text(config.system_prompt)
        (TEMPLATES_DIR / config.user_prompt_file).write_text(config.user_prompt_template)
    logger.info("Created default templates in %s", TEMPLATES_DIR)


def load_template(file_name: str, default: str) -> str:
    """Read a template file from TEMPLATES_DIR, falling back to the default on missing file."""
    path = TEMPLATES_DIR / file_name
    try:
        return path.read_text()
    except FileNotFoundError:
        logger.warning("Template file not found: %s — using built-in default", path)
        return default


def load_agent_config(agent: AgentType) -> AgentConfig:
    """Load an AgentConfig with templates read from disk, falling back to built-in defaults."""
    base = AGENT_CONFIGS[agent]
    return replace(
        base,
        system_prompt=load_template(base.system_prompt_file, base.system_prompt),
        user_prompt_template=load_template(base.user_prompt_file, base.user_prompt_template),
    )
