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

Or run directly with `uvx`:

```bash
uvx --from . --python 3.14 askcc --help
```

## Usage

```
askcc [--cwd DIR] {plan,develop} --github-issue-url URL
```

### Commands

| Command   | Description                                                        |
|-----------|--------------------------------------------------------------------|
| `plan`    | Fetch the issue and run Claude in planning mode (architecture/design) |
| `develop` | Fetch the issue and run Claude in development mode (implementation)   |

### Options

| Option               | Description                                              |
|----------------------|----------------------------------------------------------|
| `--github-issue-url` | **(required)** GitHub issue URL to process               |
| `--cwd`              | Working directory for the Claude subprocess (default: cwd) |
| `--version`          | Show version                                             |

### Examples

Plan an issue:

```bash
askcc plan --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

Develop an issue in a specific project directory:

```bash
askcc --cwd /path/to/project develop --github-issue-url https://github.com/monkut/askcc-cli/issues/1
```

## Project Structure

```
askcc/
    __init__.py          # Package version
    cli.py               # CLI entry point and subprocess execution
    definitions.py       # Agent types, prompts, and config
    functions.py         # GitHub issue fetching via gh CLI
    settings.py          # Logging configuration
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
