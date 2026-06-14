# Contributing to LLM Testbench

## Branch model

`main` is the protected default branch:

- direct push is **disabled** (force-pushes and branch deletion too)
- every change goes through a Pull Request
- 1 approving review required (per project policy) — open + merge after review
- admin can temporarily lift the rule via GitHub UI for emergency hotfixes

This protects against accidental `git push` to the wrong branch and gives a clean
history of "what landed when".

## Workflow

```bash
# always start from fresh main
git checkout main
git pull --ff-only

# branch per change
git checkout -b feature/short-description     # or fix/, chore/, docs/
# ... edit, commit ...
git push -u origin feature/short-description

# open PR (via gh CLI)
gh pr create --fill
# merge after CI/checks pass
gh pr merge --squash --delete-branch
```

Branch prefixes (loose convention, not enforced):

| Prefix | When |
|---|---|
| `feature/` | new functionality |
| `fix/` | bug fix |
| `chore/` | tooling, deps, CI, refactor without behavior change |
| `docs/` | docs only |

## Commit messages

Commits should use one standard template.

Template:

```text
<scope>: <imperative summary>
```

Rules:

- English only
- lowercase scope
- short imperative summary
- no trailing period
- keep it specific to one logical change

Preferred scopes are based on the touched area:

| Scope | When |
|---|---|
| `server` | python/server.py, benchmark_server.py changes |
| `runner` | python/job_runner.py, sql_benchmark.py changes |
| `adapter` | python/adapter.py changes |
| `models` | python/models.py changes |
| `ui` | static/app.js, index.html, style.css changes |
| `tests` | tests/ changes |
| `workflow` | GitHub Actions / CI |
| `docs` | README.md, CHANGELOG.md, ROADMAP.md changes |
| `fix(<area>)` | focused bug fix when that reads better |

Examples:

```text
runner: increase max_retries from 3 to 5
server: add question_timeout_ms to sql benchmark config
fix(adapter): pass max_retries to run_question_tool_calling
tests: add retry behavior test for sql benchmark
docs: update CHANGELOG for v1.2.0
```

## Pull Request titles

Pull Request titles should follow the same template as commit messages.

Template:

```text
<scope>: <imperative summary>
```

Examples:

```text
runner: increase max_retries from 3 to 5
server: add question_timeout_ms to sql benchmark config
fix(adapter): pass max_retries to run_question_tool_calling
```

PR body is free-form, but should usually include:

- summary
- why
- validation

Co-author trailers welcome when AI agents contributed:

```
Co-Authored-By: opencode <noreply@opencode.ai>
```

## License

This project uses the license specified in the root LICENSE file (if present).
Check the project root for license information.

## Local development

```bash
# install dependencies
pip install -e .[dev]

# run tests
pytest tests/

# run server
python -m python.server
# or
./run.sh
```

## Project structure

| Directory | What |
|---|---|
| `python/` | Main Python package (server, runner, adapter, models) |
| `static/` | Frontend assets (app.js, style.css) |
| `tests/` | Pytest test suite |
| `sql_benchmark_data/` | SQL benchmark questions & table data |
| `benchmarks/` | Stored benchmark results |
| `.github/workflows/` | CI/CD pipelines |