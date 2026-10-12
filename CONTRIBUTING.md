<!-- generated-by: repo-docs-sync; file: CONTRIBUTING.md -->

# Contributing to llm-testbench

## Project Context
Repository: `bashrusakh/llm-testbench` — default branch: `main` — observed commit: `bcc21ca151f12a15f783f81eab5f410445db425d`.

Project purpose: Not recorded in the collected context.

Facts not found in the repository context:
- CI pipeline command details: Content of .github/workflows/ci.yml not provided in the extracted context

Facts the repository does not support:
- Docker requirement: Project states "No Docker required" in README

## Branch Model
Use this stable policy:
- `main` is the protected default branch when it is the repository default branch.
- If the repository default branch has another name, treat that default branch as protected.
- Direct push to the default branch is disabled.
- Force-pushes and branch deletion on protected branches are disabled.
- Every change goes through a Pull Request.
- One approving review is required when the repository policy requires review.
- Emergency hotfixes may temporarily change branch protection only via GitHub UI/admin action.
- This protects against accidental pushes to the wrong branch and keeps a clean history.

## Workflow
```bash
git checkout main
git pull --ff-only
git checkout -b feature/short-description
# or fix/, chore/, docs/
# edit, commit, validate
git push -u origin feature/short-description
gh pr create --fill
```
Replace `main` with the repository's actual default branch when needed. Merge happens after review and CI/checks pass.

Branch prefixes:
- `feature/` — new functionality
- `fix/` — bug fix
- `chore/` — tooling, dependencies, CI, or refactor without behavior change
- `docs/` — documentation only

## Getting Started
Prerequisites observed for repository commands:
- `python -m pip install -r python/requirements.txt pytest` (listed by Run full test suite)

## Dev Commands
**Install dependencies** — working directory `.`; evidence: `README.md`

```bash
python -m pip install -r python/requirements.txt pytest
```

**Run test suite** — working directory `.`; evidence: `README.md`

```bash
python -m pytest tests -q
```

**Start backend server** — working directory `.`; evidence: `README.md`

```bash
python -m python.server
```

## Before Submitting
**Run full test suite** — working directory `.`; prerequisites: `python -m pip install -r python/requirements.txt pytest`; evidence: `README.md`

```bash
python -m pytest
```
- Check git status.
- Review the full diff.
- Make sure only intended files are included.
- Run the narrowest relevant tests/checks from repository evidence.
- If tests cannot run, report the exact command and exact error.

## Code Style

- Keep changes focused.
- Prefer clear, simple code over clever code.
- Avoid unrelated refactors.
- Do not silently change behavior outside the task scope.
- Reuse existing project style and architecture.
- Do not introduce new dependencies, frameworks, tooling, generated files, or large artifacts unless the change explicitly requires it.
- Do not commit secrets, tokens, credentials, local-only config, logs, cache files, benchmark outputs, or generated runtime artifacts unless explicitly required.

## Commit Messages
Use this convention:
```text
scope: short imperative summary
```
- English only.
- Lowercase scope.
- Short imperative summary.
- No trailing period.
- Keep each commit specific to one logical change.

## Pull Requests
PR titles should be concise and describe the change. When the repository uses scoped PR titles, use:
```text
scope: short imperative summary
```
The PR body should include, at minimum:
- summary
- why/context
- validation/tests run
- screenshots or manual verification steps for UI changes, when relevant

This PR guidance is independent of the commit-message convention above. Do not import an unrelated title grammar or require a scope when the repository does not use scoped PR titles.
Do not mark work as ready if checks are unknown or failing.
AI-assisted contributions should include a co-author trailer when appropriate:

```text
Co-Authored-By: opencode
```

## Triage labels
Labels are triage metadata, not a correctness determination or merge approval. Apply the correct type label when opening an issue or pull request, subject to available labels.

Apply exactly one type label:
- `bug` — something is broken or behaves incorrectly.
- `enhancement` — new functionality or a feature request.
- `documentation` — documentation-only change or doc issue.
- `question` — a usage or support question.
- `refactor` — internal change with no behavior change.
- `ci` — CI, build, or tooling change.

Automation-managed labels (do not apply by hand): `needs-info` and `duplicate`.
`confirmed` is not automation-managed. It records human or verification-owned confirmation and must be applied only by maintainers or authorized triagers after relevant verification.
`priority-*` and other operational labels are maintainer-owned.

## Issues
Before opening an issue, verify the problem against the current code/state and search existing issues if possible. Describe facts only; do not invent root cause. If root cause is only suspected, label it as a hypothesis. Include the category's relevant reproduction, expected behavior, affected component, environment/version, evidence, and constraints. Keep the issue actionable and specific.

## Documentation Maintenance
Project structure changes over time. AGENTS.md and CONTRIBUTING.md are guidance, not a replacement for checking the actual tree. If project structure, commands, architecture, test flow, contribution rules, release flow, or important entry points change, update the relevant Markdown file in the same PR. Keep docs useful and stable, and keep Markdown updates small. Do not duplicate the full file tree unless the project explicitly requires it. Describe stable conventions and important entry points, not every file.

## Releases
Every release must include meaningful release notes. Before publishing, check previous notes, review changes since the previous tag, and use actual commits, merged PRs, linked issues, and final code changes. Do not invent features, fixes, impact, or breaking changes. Call out user-visible, breaking, migration, compatibility, security, benchmark/default, and data-format changes when relevant. If a release is created automatically by GitHub Actions, its body must still be reviewed and updated before completion. A GitHub Release with an empty, placeholder, too thin, or unreviewed auto-generated-only body is considered unfinished, even if the tag and assets were created successfully. Before marking release work as done, verify the GitHub Release page contains the correct tag, title, body, and assets.

## Project Structure
Components and important paths observed in the repository:
- **Backend server** — aiohttp backend entry point (paths: `python/server.py`; evidence: `AGENTS.md`)
- **Benchmark orchestrator** — HTTP handlers and server‑side orchestration (paths: `python/benchmark_server.py`; evidence: `AGENTS.md`)
- **Job runner** — Executes benchmark jobs and LLM calls (paths: `python/job_runner.py`; evidence: `AGENTS.md`)
- **SQL benchmark runner** — Runs SQL generation benchmark against DuckDB fixtures (paths: `python/sql_benchmark.py`; evidence: `AGENTS.md`)
- **Adapter interfaces** — Defines benchmark adapter API and concrete adapters (Speed, SQL) (paths: `python/adapter.py`; evidence: `AGENTS.md`)
- **Static frontend assets** — Plain HTML/CSS/JavaScript UI (no bundler) (paths: `static`; evidence: `AGENTS.md`)
- **Test suite** — Pytest tests for backend, adapters, fixtures and UI integration (paths: `tests`; evidence: `AGENTS.md`)

## Security and Runtime Notes
Runtime environments observed in the repository:
- **Windows** — Supported via run.bat launcher (evidence: `README.md`)
- **Linux/macOS** — Supported via run.sh launcher (evidence: `README.md`)

Security notes: Not found in repository.

## Questions / Support
Not found in repository.

## License
Not found in repository.
