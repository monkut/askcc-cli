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
askcc [--cwd DIR] {prepare,plan,validate,develop,issue-review,pr-review,explore,diagnose,fix-ci} --github-issue-url URL
askcc fix-ci [--cwd DIR] [--github-issue-url URL]
askcc install [--directory DIR]
```

### Commands

| Command    | Description                                                              |
|------------|--------------------------------------------------------------------------|
| `prepare`  | Analyze a backlog issue for development readiness (acceptance criteria, dependencies, estimates) |
| `plan`     | Fetch the issue and run Claude in planning mode (architecture/design)    |
| `validate` | Check issue readiness for development (acceptance criteria, dependencies, assignee, blocking labels) |
| `develop`  | Fetch the issue and run Claude in development mode (implementation)      |
| `issue-review` | Review issue quality (clarity, completeness, feasibility)            |
| `pr-review` | Review a PR's code against its linked issue's Definition of Done        |
| `explore`  | Fetch the issue and run Claude in explore mode (investigate and propose solutions) |
| `diagnose` | Fetch the issue and run Claude in diagnose mode (root cause analysis)    |
| `fix-ci`   | Identify failing CI checks on the current PR or branch and implement fixes (`--github-issue-url` optional) |
| `install`  | Install bundled skills to `~/.claude/skills` and/or `~/.openclaw/workspace/skills` |

### Recommended Workflow

askcc commands are designed to run sequentially, where each phase produces artifacts (issue sections, labels, project status) that gate and feed the next:

```
prepare → plan → develop → pr-review
```

| Phase | Command | Inputs | Outputs |
|-------|---------|--------|---------|
| **Define** | `prepare` | Raw backlog issue | Acceptance criteria, dependencies, estimate; adds `action:develop` label |
| **Plan** | `plan` | Prepared issue with acceptance criteria | Implementation plan, assignee; finalizes issue description |
| **Build** | `develop` | Planned issue (validated for readiness) | Feature branch, PR linked to issue; swaps label to `action:review` |
| **Verify** | `pr-review` | Issue + linked PR | Definition of Done review, approve or request changes |

Supporting commands can be used at any point:

| Command | When to use |
|---------|-------------|
| `issue-review` | Before `prepare` — review issue quality and suggest improvements |
| `validate` | Before `develop` — check readiness gates without running the agent |
| `explore` | Before `plan` — investigate approaches and trade-offs |
| `diagnose` | Any time — root cause analysis for bug reports |
| `fix-ci` | After `develop` — fix failing CI checks on the PR branch |

**Gating mechanisms:**
- `prepare` adds the `action:develop` label; subsequent commands require an `action:` label prefix
- `develop` runs readiness validation (acceptance criteria, dependencies, assignee, no blocking labels) before starting
- The `needs:decision` label blocks `develop` until resolved
- `develop` swaps `action:develop` → `action:review` and moves the project board status on success

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
| `PREPARE_SYSTEM_PROMPT.md`   | —                  | System prompt for the prepare agent    |
| `PREPARE_USER_PROMPT.md`     | `$issue_content_file`   | User prompt template for preparation   |
| `PLAN_SYSTEM_PROMPT.md`      | —                  | System prompt for the planning agent   |
| `PLAN_USER_PROMPT.md`        | `$issue_content_file`   | User prompt template for planning      |
| `DEVELOP_SYSTEM_PROMPT.md`   | —                  | System prompt for the dev agent        |
| `DEVELOP_USER_PROMPT.md`     | `$issue_content_file`   | User prompt template for development   |
| `REVIEW_SYSTEM_PROMPT.md`    | —                  | System prompt for the issue-review agent |
| `REVIEW_USER_PROMPT.md`      | `$issue_content_file`   | User prompt template for issue-review    |
| `REVIEWPR_SYSTEM_PROMPT.md`  | —                  | System prompt for the pr-review agent  |
| `REVIEWPR_USER_PROMPT.md`    | `$issue_content_file`, `$pr_content_file` | User prompt template for PR review |
| `EXPLORE_SYSTEM_PROMPT.md`   | —                  | System prompt for the explore agent    |
| `EXPLORE_USER_PROMPT.md`     | `$issue_content_file`   | User prompt template for exploration   |
| `DIAGNOSE_SYSTEM_PROMPT.md`  | —                  | System prompt for the diagnose agent   |
| `DIAGNOSE_USER_PROMPT.md`    | `$issue_content_file`   | User prompt template for diagnosis     |
| `FIXCI_SYSTEM_PROMPT.md`     | —                  | System prompt for the fix-ci agent     |
| `FIXCI_USER_PROMPT.md`       | `$issue_content_file`   | User prompt template for CI fixing     |

Edit any file to customize the agent's behavior. User prompt templates **must** contain the `$issue_content_file` variable, which is replaced with the path to a tempfile containing the fetched GitHub issue content at runtime. The tempfile is written to `/tmp` with the naming convention `askcc_{COMMAND}_{OWNER}-{REPO}_{ISSUE#}.md` and is automatically cleaned up after execution. askcc validates required variables on startup and raises an error if one is missing.

Override the config directory by setting the `ASKCC_HOME` environment variable (e.g. for testing).

#### Subagent Frontmatter

Each `*_SYSTEM_PROMPT.md` template may begin with a [Claude Code subagent](https://code.claude.com/docs/en/subagents.md)–style YAML frontmatter block that declares the agent's tool surface, model, and reasoning effort:

```markdown
---
name: develop
description: Develops a planned/defined issue
tools: Read, Write, Edit, Bash, Grep, Glob
model: opus
effort: max
max_thinking_tokens: 32000
max_turns: 200
---
You are an expert software developer ...
```

Recognized fields (all optional):

| Field | Type | Allowed values |
|---|---|---|
| `name` | string | informational |
| `description` | string | informational |
| `tools` | comma-separated list | translated to `--allowedTools` (e.g. `Read, Bash(gh:*)`) |
| `disallowed_tools` | comma-separated list | translated to `--disallowedTools` |
| `model` | string | `opus`, `sonnet`, `haiku`, `inherit` |
| `effort` | string | `low`, `medium`, `high`, `xhigh`, `max` |
| `max_thinking_tokens` | integer | thinking token budget |
| `max_turns` | integer | translated to `--max-turns` |

Templates without frontmatter continue to work unchanged. Invalid values (e.g. `model: opuz`, `effort: turbo`) raise a clear error at load time rather than mid-run.

##### Per-Action Defaults

| Action | tools | model | effort |
|---|---|---|---|
| `prepare` | `Read, Grep, Glob, Bash(gh:*)` | `sonnet` | `medium` |
| `plan` | `Read, Grep, Glob, Bash(gh:*)` | `opus` | `high` |
| `develop` | `Read, Write, Edit, Bash, Grep, Glob` | `opus` | `max` |
| `issue-review` | `Read, Grep, Glob, Bash(gh:*)` | `sonnet` | `medium` |
| `pr-review` | `Read, Grep, Glob, Bash(gh:*,git:*)` | `opus` | `high` |
| `explore` | `Read, Grep, Glob, Bash(gh:*)` | `sonnet` | `high` |
| `diagnose` | `Read, Grep, Glob, Bash(gh:*,git:*)` | `sonnet` | `high` |
| `fix-ci` | `Read, Write, Edit, Bash, Grep, Glob` | `sonnet` | `high` |

Note: askcc runs `claude` with `--dangerously-skip-permissions` so it can execute unattended; the per-action `tools` allowlist is the safety boundary that narrows what each agent can call.

##### Override Precedence

For `effort` and `max_thinking_tokens`, askcc resolves the effective value in this order (highest wins):

1. Explicit CLI flag (`--effort`, `--max-thinking-tokens`)
2. Environment variable (`ASKCC_CLAUDE_EFFORT_LEVEL`, `ASKCC_CLAUDE_MAX_THINKING_TOKENS`)
3. Template frontmatter (per-action default in `~/.askcc/templates/`)
4. Built-in default (`xhigh`, `21000`)

### Post-Develop Verification

After the `develop` command completes successfully, askcc can run verification commands (tests, linting, type checks) before transitioning the issue to `action:review`. This is **optional** — if no verification config is found, the transition proceeds without checks.

Configure verification commands in either of these files (checked in order):

#### `pyproject.toml`

```toml
[[tool.askcc.verify]]
name = "tests"
cmd = "uv run poe test"

[[tool.askcc.verify]]
name = "lint"
cmd = "uv run poe check"

[[tool.askcc.verify]]
name = "typecheck"
cmd = "uv run poe typecheck"
```

#### `.askcc.toml`

For non-Python projects or when `pyproject.toml` is not used:

```toml
[[verify]]
name = "tests"
cmd = "npm test"

[[verify]]
name = "lint"
cmd = "npm run lint"
```

Each entry requires a `name` (used in log output) and `cmd` (shell command to run). If any command fails, the issue label stays at `action:develop` and the failure details are logged. Each command has a 5-minute timeout.

### Examples

Prepare a backlog issue for development:

```bash
askcc prepare --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Plan an issue:

```bash
askcc plan --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Review an issue for quality and completeness:

```bash
askcc issue-review --github-issue-url https://github.com/monkut/askcc-cli/issues/1
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
