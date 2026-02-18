import json
import logging
import shutil
import subprocess
from urllib.parse import urlparse

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
