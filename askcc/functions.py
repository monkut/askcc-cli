import json
import logging
import shutil
import subprocess
from dataclasses import replace
from importlib.resources import files as package_files
from pathlib import Path
from string import Template
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


DEFAULT_SKILLS_DIR = Path.home() / ".openclaw" / "workspace" / "skills"
OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"


def install_skills(directory: Path | None = None) -> None:
    """Copy bundled skills to the target directory and register them in openclaw.json."""
    target_dir = directory or DEFAULT_SKILLS_DIR
    skills_source = package_files("askcc") / "skills"

    # Copy each skill directory
    for skill_dir in sorted(skills_source.iterdir(), key=lambda p: p.name):
        if not skill_dir.is_dir() or skill_dir.name.startswith("__"):
            continue
        dest = target_dir / skill_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(str(skill_dir), dest)
        logger.info("Installed skill '%s' to %s", skill_dir.name, dest)

        # Register in openclaw.json
        _register_skill(skill_dir.name)


def _register_skill(skill_name: str) -> None:
    """Add a skill entry to ~/.openclaw/openclaw.json."""
    config_path = OPENCLAW_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        config = {}

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


def load_agent_config(agent: AgentType) -> AgentConfig:
    """Load an AgentConfig with templates read from disk, falling back to built-in defaults."""
    base = AGENT_CONFIGS[agent]
    user_prompt_template = load_template(base.user_prompt_file, base.user_prompt_template)
    validate_template(user_prompt_template, base.required_variables, base.user_prompt_file)
    return replace(
        base,
        system_prompt=load_template(base.system_prompt_file, base.system_prompt),
        user_prompt_template=user_prompt_template,
    )
