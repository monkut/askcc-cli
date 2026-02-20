---
name: request-askcc
description: Request REVIEW, PLAN or DEVELOP actions for GitHub issues via the askcc CLI. Use when a user asks to review, plan an implementation of a GitHub issue, or to proceed with development of a planned GitHub issue.
---

# Request GitHub Issue Action

Use the `askcc` tool to request processing (plan or develop) of GitHub issues defined by a URL.

## Instructions

- When a user asks to PLAN an implementation of a given GitHub issue, use `askcc plan`.
- When a user asks to DEVELOP a planned implementation defined in a given GitHub issue, use `askcc develop`.
- When a user ask to REVIEW a github issue, use `askcc review`.

## Examples

- "Plan https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This fetches the github issue and plans how to implement it for future development.
  askcc plan --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1
  ```

- "Proceed with development of https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This proceeds to implement/develop a github issue that has a clear development/implementation plan.
  askcc develop --cwd {PROJECTS DIRECTORY}/{TARGET DEVELOPMENT REPOSITORY} --github-issue-url https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1

  ```

- "Review https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1"

  ```bash
  # This fetches the specified GitHub issue and runs Claude in review mode to assess issue quality and completeness. 
  askcc review --github-issue-url  https://github.com/{GITHUB ORG}/{GITHUB REPO}/issues/1                                           
  ```

WARNING: If the `{TARGET DEVELOPMENT REPOSITORY}` cannot be determined, ASK user in Slack.

Where:
- {PROJECTS DIRECTORY}: The local root directory where git repositorys/projects are stored.
- {TARGET DEVELOPMENT REPOSITORY}: The local git repository where the work is to be performed.
- {GITHUB ORG}: The target github organization or user.
- {GITHUB REPO}: The target github repository.
