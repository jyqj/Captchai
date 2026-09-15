# GitHub connector pull request smoke test

This documentation-only change was requested by the repository owner to test
whether the connected GitHub tools can create a branch, commit, and pull request.
It is not a product change and does not need to be merged.

## Submission path

- `api_tool.list_resources` discovers the available GitHub tool actions.
- `GitHub.create_branch` creates the isolated test branch from `main`.
- `GitHub.create_file` writes this file and creates a commit through the GitHub Contents API.
- `GitHub.create_pull_request` is used to open the draft pull request.

The test does not use a sandbox shell, `git push`, or the `gh` command-line tool.
No application code, dependencies, deployment settings, or workflows are changed.
The default branch is not modified by this test.

## Validation scope

This is a repository-write and PR-creation smoke test, not an application test.
Application tests, type checks, and the documentation build are not run for this change.
The pull request should remain a draft and must not be automatically merged.
