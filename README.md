# askcc

Drives [Claude Code](https://claude.ai) through the GitHub issue → PR lifecycle. Each subcommand fetches an issue, applies a phase-specific agent prompt, and runs `claude` non-interactively against your repo.

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

## Skills

### `handle-github-issue` (standalone install)

The `handle-github-issue` skill drives an issue through prepare / plan / develop / issue-review / pr-review / explore / diagnose / fix-ci entirely from Claude Code, with no runtime dependency on the askcc Python package. Install it directly into Claude Code:

```bash
mkdir -p ~/.claude/skills/handle-github-issue
curl -fsSL https://raw.githubusercontent.com/monkut/askcc-cli/main/askcc/skills/handle-github-issue/SKILL.md \
  -o ~/.claude/skills/handle-github-issue/SKILL.md
```

Pin to a release tag instead of `main` for reproducibility:

```bash
TAG=$(gh release view --repo monkut/askcc-cli --json tagName -q .tagName)
curl -fsSL "https://raw.githubusercontent.com/monkut/askcc-cli/${TAG}/askcc/skills/handle-github-issue/SKILL.md" \
  -o ~/.claude/skills/handle-github-issue/SKILL.md
```

Requires only [Claude Code](https://claude.ai) and an authenticated [GitHub CLI](https://cli.github.com/) (`gh`). Once installed, invoke it from any Claude Code session — e.g. "plan https://github.com/owner/repo/issues/1".

To install **all** bundled skills (including `request-askcc`) via the askcc CLI, see [`askcc install`](#examples).

## Usage

```
askcc [GLOBAL OPTIONS] COMMAND [COMMAND OPTIONS]
```

Run `askcc --help` or `askcc COMMAND --help` for the full flag list.

### Commands

| Command    | Description                                                              |
|------------|--------------------------------------------------------------------------|
| `prepare`  | Analyze a backlog issue for development readiness (acceptance criteria, dependencies, estimates) |
| `plan`     | Fetch the issue and run Claude in planning mode (architecture/design)    |
| `validate` | Check issue readiness for development (acceptance criteria, assignee; dependencies reported as advisory) |
| `develop`  | Fetch the issue and run Claude in development mode (implementation)      |
| `issue-review` | Review issue quality (clarity, completeness, feasibility)            |
| `pr-review` | Review a PR's code against its linked issue's Definition of Done        |
| `explore`  | Fetch the issue and run Claude in explore mode (investigate and propose solutions) |
| `diagnose` | Fetch the issue and run Claude in diagnose mode (root cause analysis)    |
| `fix-ci`   | Identify failing CI checks on the current PR or branch and implement fixes (`--github-issue-url` optional) |
| `install`  | Install bundled skills to `~/.claude/skills` and/or `~/.openclaw/workspace/skills` |

### Recommended Workflow

askcc commands are designed to run sequentially, where each phase produces artifacts (issue sections, comments, branches, PRs) that feed the next:

```
prepare → plan → develop → pr-review
```

| Phase | Command | Inputs | Outputs |
|-------|---------|--------|---------|
| **Define** | `prepare` | Raw backlog issue | Acceptance criteria, dependencies, estimate |
| **Plan** | `plan` | Prepared issue with acceptance criteria | Implementation plan, assignee; finalizes issue description |
| **Build** | `develop` | Planned issue (validated for readiness) | Feature branch, PR linked to issue |
| **Verify** | `pr-review` | Issue + linked PR | Definition of Done review, approve or request changes |

Supporting commands can be used at any point:

| Command | When to use |
|---------|-------------|
| `issue-review` | Before `prepare` — review issue quality and suggest improvements |
| `validate` | Before `develop` — check readiness gates without running the agent |
| `explore` | Before `plan` — investigate approaches and trade-offs |
| `diagnose` | Any time — root cause analysis for bug reports |
| `fix-ci` | After `develop` — fix failing CI checks on the PR branch |

**Workflow is the caller's job.** askcc runs one action and writes that action's deliverable. It reads no workflow labels, adds or removes no labels, and moves no project cards. The caller (a human, or a PM agent) decides which action to run, applies stop labels such as `needs:decision`, and moves the issue through its board. When an agent needs a decision, it posts a `## Decision Needed` block in its comment for the caller to act on.

**Readiness gate:** `develop` runs readiness validation (acceptance criteria, assignee) before starting; a missing `## Dependencies` section is reported as `WARN` and does not block. Skip it with `--skip-validation`.

**Upgrading from 0.2.x:** askcc no longer applies `action:*` label swaps, project board moves, or `needs:decision`, and no longer requires an `action:` label (`-i/--ignore-labels` is accepted and ignored). Prompt templates already copied to `~/.askcc/templates/` keep the old label instructions: askcc logs a warning when it loads one, and deleting the files there regenerates them. The `DECISION_ISSUE_LABEL` and `ENABLE_ISSUE_LABEL_PREFIX_VALIDATION` environment variables are removed and have no effect. `askcc develop` exits with code `2` when post-develop verification fails.

### Options

Global options (before the command):

| Option | Description |
|---|---|
| `--cwd DIR` | Working directory for the `claude` subprocess (default: cwd) |
| `-l`, `--language` | Output language for agent comments (`english`, `japanese`) |
| `-r`, `--runner` | Runner to execute the task (default: `claude`) |
| `--effort` | Claude thinking effort (`low`, `medium`, `high`, `xhigh`, `max`) |
| `--model` | Claude model (`opus`, `sonnet`, `haiku`, `inherit`) |
| `--max-thinking-tokens N` | Thinking token budget |
| `--disable-thinking` | Force-disable extended thinking |
| `--[no-]disable-adaptive-thinking` | Toggle adaptive reasoning (Opus 4.6, Sonnet 4.6) |
| `--version` | Show version |

Command-specific options:

| Command | Option | Description |
|---|---|---|
| (most) | `-g`, `--github-issue-url URL` | **(required)** GitHub issue URL |
| `develop` | `--skip-validation` | Skip readiness validation before running |
| `fix-ci` | `-g`, `--github-issue-url URL` | Optional — auto-detected from current branch's PR if omitted |
| `install` | `--directory DIR` | Override auto-detection of `~/.claude` and `~/.openclaw` targets |

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, etc.) | `INFO` |
| `ASKCC_HOME` | Root directory for askcc configuration, templates, and logs | `~/.askcc` |
| `ASKCC_LANGUAGE` | Default output language for agent comments (`english`, `japanese`) | `english` |
| `ASKCC_CLAUDE_EFFORT_LEVEL` | Default thinking effort (`low`, `medium`, `high`, `xhigh`, `max`) | `xhigh` |
| `ASKCC_CLAUDE_MODEL` | Claude model (`opus`, `sonnet`, `haiku`, `inherit`) | (frontmatter) |
| `ASKCC_CLAUDE_MAX_THINKING_TOKENS` | Thinking token budget | `21000` |
| `ASKCC_CLAUDE_DISABLE_THINKING` | Force-disable extended thinking (`1`/`true`) | `false` |
| `ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING` | Disable adaptive reasoning (Opus 4.6, Sonnet 4.6) | `true` |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token forwarded to the `claude` subprocess | (none) |
| `CLAUDE_OAUTH_TOKEN_FILE` | Path to a file holding the OAuth token (used when `CLAUDE_CODE_OAUTH_TOKEN` is unset) | (none) |

### Authentication

askcc resolves the Claude OAuth token before spawning `claude`, using the first non-empty source:

1. `CLAUDE_CODE_OAUTH_TOKEN` env var (no log when used).
2. `CLAUDE_OAUTH_TOKEN_FILE` env var — file path.
3. `~/.tokens/.claude-oauth-token` — conventional headless token file.
4. `${XDG_CONFIG_HOME:-~/.config}/claude/oauth-token` — XDG fallback.
5. `~/.claude/.credentials.json` — parses `claudeAiOauth.accessToken`. Logged at `WARNING` because Claude Code refreshes the in-RAM token without always writing back, so this file can be stale.

The chosen source is logged at `INFO` so failures are diagnosable from the run log. Whitespace is stripped. Unreadable files (`PermissionError`) or malformed `.credentials.json` log a `WARNING` and the chain continues. If every source is empty, askcc exits non-zero listing each location checked, **without** invoking `claude`.

### User Configuration File

askcc reads optional defaults from `~/.askcc/config.toml` (resolved relative to `ASKCC_HOME`):

```toml
[defaults]
language = "japanese"
```

The output language for agent comments resolves in this order (highest wins):

1. CLI flag (`--language`)
2. Environment variable (`ASKCC_LANGUAGE`)
3. User config file (`~/.askcc/config.toml` `[defaults] language`)
4. Built-in default (`english`)

A missing config file is silently ignored. A malformed file or invalid value logs a warning and falls back to the next layer — the CLI never crashes on bad config.

### Customizing Prompts

On first run, askcc seeds `~/.askcc/templates/` with two files per agent: `{ACTION}_SYSTEM_PROMPT.md` (system prompt) and `{ACTION}_USER_PROMPT.md` (user prompt). Edit either file to customize behavior.

Action prefixes: `PREPARE`, `PLAN`, `DEVELOP`, `REVIEW` (issue-review), `REVIEWPR` (pr-review), `EXPLORE`, `DIAGNOSE`, `FIXCI`.

User prompt templates **must** include the `$issue_content_file` variable; the `REVIEWPR_USER_PROMPT.md` template additionally requires `$pr_content_file`. Both expand at runtime to tempfile paths under `/tmp/askcc_{COMMAND}_{OWNER}-{REPO}_{ISSUE#}.md`, populated with the fetched issue/PR content and cleaned up after the run. Missing required variables raise an error at startup.

Override the templates directory via `ASKCC_HOME`.

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

For `effort`, `max_thinking_tokens`, and `model`, askcc resolves the effective value in this order (highest wins):

1. Explicit CLI flag (`--effort`, `--max-thinking-tokens`, `--model`)
2. Environment variable (`ASKCC_CLAUDE_EFFORT_LEVEL`, `ASKCC_CLAUDE_MAX_THINKING_TOKENS`, `ASKCC_CLAUDE_MODEL`)
3. Template frontmatter (per-action default in `~/.askcc/templates/`)
4. Built-in default (`xhigh`, `21000`; `model` has no built-in default — when unset everywhere, no `--model` flag is emitted and `claude` picks its own)

### Post-Develop Verification

After the `develop` command completes successfully, askcc can run verification commands (tests, linting, type checks) and log the result. When any check fails, askcc exits with code `2`. This is **optional** — if no verification config is found, verification is skipped.

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

Each entry requires a `name` (used in log output) and `cmd` (shell command to run). If any command fails, the failure details are logged. Each command has a 5-minute timeout.

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

Install bundled skills (for a Claude-Code-only install of just the `handle-github-issue` skill without `askcc`, see [Skills](#skills)):

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
    cli.py               # Argparse entry point and command dispatch
    definitions.py       # Agent actions, default prompts, frontmatter parsing
    functions.py         # GitHub issue/PR fetching, readiness checks, verification
    runners.py           # Runner registry; spawns the `claude` subprocess
    settings.py          # Configuration, env vars, language/effort resolution
    skills/              # Bundled skills installed by `askcc install`
tests/
    test_askcc.py        # Test suite
pyproject.toml           # Project metadata, ruff/poe/pyright config
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
