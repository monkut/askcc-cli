# askcc

A one-shot Claude Code CLI executor that fetches a GitHub issue and pipes it to [Claude Code](https://claude.ai) with a specialized agent prompt.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/guides/install-python/)
- [Claude Code CLI](https://claude.ai) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`) — authenticated

## Installation

```bash
uv tool install . --python 3.14
```

Or install directly from GitHub:

```bash
uv tool install "askcc @ https://github.com/monkut/askcc-cli/archive/refs/tags/$(gh release view --repo monkut/askcc-cli --json tagName -q .tagName).tar.gz" --python 3.14
```

Or run directly with `uvx`:

```bash
uvx --from . --python 3.14 askcc --help
```

## Usage

```
askcc [--cwd DIR] {plan,validate,develop,review,explore,diagnose} --github-issue-url URL
askcc install [--directory DIR]
```

### Commands

| Command    | Description                                                              |
|------------|--------------------------------------------------------------------------|
| `plan`     | Fetch the issue and run Claude in planning mode (architecture/design)    |
| `validate` | Check issue readiness for development (acceptance criteria, dependencies, assignee, blocking labels) |
| `develop`  | Fetch the issue and run Claude in development mode (implementation)      |
| `review`   | Fetch the issue and run Claude in review mode (issue quality review)     |
| `explore`  | Fetch the issue and run Claude in explore mode (investigate and propose solutions) |
| `diagnose` | Fetch the issue and run Claude in diagnose mode (root cause analysis)    |
| `install`  | Install bundled skills to `~/.claude/skills` and/or `~/.openclaw/workspace/skills` |

### Options

| Option               | Description                                              |
|----------------------|----------------------------------------------------------|
| `--github-issue-url` | **(required)** GitHub issue URL to process               |
| `--cwd`              | Working directory for the Claude subprocess (default: cwd) |
| `--skip-validation`  | Skip readiness validation before development (`develop` only) |
| `--directory`        | Override auto-detection and install skills to this directory (`install` only) |
| `--version`          | Show version                                             |

### Environment Variables

| Variable    | Description                                | Default |
|-------------|--------------------------------------------|---------|
| `LOG_LEVEL`  | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, etc.) | `INFO`    |
| `ASKCC_HOME` | Root directory for askcc configuration and templates   | `~/.askcc` |
| `DECISION_ISSUE_LABEL` | GitHub label applied when an agent flags a decision is needed | `needs:decision` |
| `ENABLE_ISSUE_LABEL_PREFIX_VALIDATION` | Enable/disable issue label prefix validation before agent execution | `true` |

### Customizing Prompts

On first run, askcc creates `~/.askcc/templates/` with default template files:

| File                       | Required variables | Description                          |
|----------------------------|--------------------|--------------------------------------|
| `PLAN_SYSTEM_PROMPT.md`      | —                  | System prompt for the planning agent   |
| `PLAN_USER_PROMPT.md`        | `$issue_content`   | User prompt template for planning      |
| `DEVELOP_SYSTEM_PROMPT.md`   | —                  | System prompt for the dev agent        |
| `DEVELOP_USER_PROMPT.md`     | `$issue_content`   | User prompt template for development   |
| `REVIEW_SYSTEM_PROMPT.md`    | —                  | System prompt for the review agent     |
| `REVIEW_USER_PROMPT.md`      | `$issue_content`   | User prompt template for review        |
| `EXPLORE_SYSTEM_PROMPT.md`   | —                  | System prompt for the explore agent    |
| `EXPLORE_USER_PROMPT.md`     | `$issue_content`   | User prompt template for exploration   |
| `DIAGNOSE_SYSTEM_PROMPT.md`  | —                  | System prompt for the diagnose agent   |
| `DIAGNOSE_USER_PROMPT.md`    | `$issue_content`   | User prompt template for diagnosis     |

Edit any file to customize the agent's behavior. User prompt templates **must** contain the `$issue_content` variable, which is replaced with the fetched GitHub issue at runtime. askcc validates this on startup and raises an error if a required variable is missing.

Override the config directory by setting the `ASKCC_HOME` environment variable (e.g. for testing).

### Examples

Plan an issue:

```bash
askcc plan --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Review an issue for quality and completeness:

```bash
askcc review --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Validate an issue is ready for development:

```bash
askcc validate --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Develop an issue in a specific project directory:

```bash
askcc --cwd /path/to/project develop --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Install bundled skills:

```bash
askcc install
```

The `install` command auto-detects which agent platforms are available:

- **`~/.claude`** — copies skills to `~/.claude/skills/` (auto-discovered by Claude Code)
- **`~/.openclaw`** — copies skills to `~/.openclaw/workspace/skills/` and registers them in `openclaw.json`

Both targets are installed if both directories exist. Use `--directory` to override auto-detection and install to a specific path.

## Project Structure

```
askcc/
    __init__.py          # Package version
    cli.py               # CLI entry point and subprocess execution
    definitions.py       # Agent types, prompts, and config
    functions.py         # GitHub issue fetching via gh CLI
    settings.py          # Configuration and environment variables
tests/
    test_askcc.py        # Tests for URL parsing
pyproject.toml           # Project metadata and tool config
```

## Development

### Setup

```bash
pre-commit install
uv sync
```

### Adding packages

```bash
uv add {PACKAGE}
```

### Linting and type checking

```bash
uv run poe check
uv run poe typecheck
```

### Running tests

```bash
uv run poe test
```

### Building

```bash
uv build
```
