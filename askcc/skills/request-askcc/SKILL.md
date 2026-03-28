---
name: request-askcc
description: Request PREPARE, VALIDATE, ISSUE-REVIEW, PR-REVIEW, PLAN, DEVELOP, EXPLORE, DIAGNOSE, or FIX-CI actions for GitHub issues via the askcc CLI. Use when a user asks to prepare, validate, review an issue, plan, develop, explore, diagnose, fix CI, or review a PR for a GitHub issue.
---

# Request GitHub Issue Action

Use the `askcc` tool to request processing of GitHub issues defined by a URL.

## Instructions

- When a user asks to PREPARE a backlog issue (flesh out acceptance criteria, dependencies, and estimates to get it ready for planning), use `askcc prepare`.
- When a user asks to VALIDATE an issue's readiness for development, use `askcc validate`.
- When a user asks to PLAN an implementation of a prepared GitHub issue (produce step-by-step implementation tasks against the codebase), use `askcc plan`.
- When a user asks to DEVELOP a planned implementation defined in a given GitHub issue, use `askcc develop`.
- When a user asks to REVIEW a github issue, use `askcc issue-review`.
- When a user asks to EXPLORE a github issue (investigate and propose solutions), use `askcc explore`.
- When a user asks to DIAGNOSE a github issue (root cause analysis), use `askcc diagnose`.
- When a user asks to REVIEW a PR (code review of a pull request linked to an issue), use `askcc pr-review`.
- When a user asks to FIX CI failures on a branch or PR (fix failing tests, lint errors, or build errors), use `askcc fix-ci`.

## Examples

- "Prepare https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This fleshes out a backlog issue by suggesting acceptance criteria, identifying dependencies, and proposing an estimate to get it ready for planning.
  askcc prepare --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Validate https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This checks whether a GitHub issue meets readiness criteria for development (acceptance criteria, dependencies, assignee, etc.).
  askcc validate --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Plan https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This analyzes a prepared issue against the codebase and produces a step-by-step implementation plan.
  askcc plan --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Proceed with development of https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This proceeds to implement/develop a github issue that has a clear development/implementation plan.
  askcc develop --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1

  ```

- "Review issue https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This fetches the specified GitHub issue and reviews it for clarity, completeness, and feasibility.
  askcc issue-review --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Explore https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This investigates the github issue, researches the codebase, and proposes best-practice solutions with trade-offs.
  askcc explore --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Diagnose https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This investigates the reported issue, identifies potential root causes, and requests additional information.
  askcc diagnose --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Review the PR for https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This fetches the issue and its linked PR, reviews the code against Definition of Done criteria, and posts a structured review on the PR.
  askcc pr-review --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Fix CI for https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This fetches the linked PR, identifies failing CI checks, and implements fixes to make them pass.
  askcc fix-ci --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Fix CI on the current branch" (no issue URL)

  ```bash
  # This auto-detects the open PR for the current branch, identifies failing CI checks, and implements fixes.
  askcc fix-ci --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY}
  ```

WARNING: If the `{TARGET DEVELOPMENT REPOSITORY}` cannot be determined, ASK user in Slack.

Where:
- {PROJECTS DIRECTORY}: The local root directory where git repositorys/projects are stored.
- {TARGET DEVELOPMENT REPOSITORY}: The local git repository where the work is to be performed.
- {GITHUB ORG}: The target github organization or user.
- {GITHUB REPO}: The target github repository.
