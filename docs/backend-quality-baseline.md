# Backend quality baseline

Short reference for the post-cleanup backend quality bar in `app/backend`. Preserve this baseline when touching backend code.

## Current intended baseline

From `app/backend`, these checks should pass cleanly (zero errors / zero unapplied migrations):

| Check | Command |
|-------|---------|
| Ruff format | `poetry run ruff format --check .` |
| Ruff lint | `poetry run ruff check .` |
| mypy | `poetry run mypy .` |
| Pyright | `poetry run pyright` |
| Django system check | `poetry run python manage.py check` |
| Migration check | `poetry run python manage.py makemigrations --check --dry-run` |

**Typing split:** mypy + django-stubs (`mypy.ini`) is the source of truth for Django/ORM typing. Pyright (`pyproject.toml`, `standard` mode) provides editor/CI signal but is not ORM-aware—do not “fix” ORM-heavy code only to satisfy Pyright at mypy’s expense.

**Ruff:** migrations are excluded from Ruff; do not reformat migration files for style-only churn.

## CI / local-check policy

- Checks are intentionally useful for **visibility** and regression awareness.
- CI runs `scripts/local_check.sh` with `FAIL_ON_WARNINGS=0` (non-blocking warnings mode).
- Do **not** make warnings, type findings, or lint findings **blocking** in CI unless that is an explicit, agreed decision later.
- Locally, the same script is the convenient full pass (backend + infra):

  ```bash
  # from repo root
  bash scripts/local_check.sh
  ```

  Strict mode (optional): `FAIL_ON_WARNINGS=1 bash scripts/local_check.sh`

## Run backend checks locally

Prerequisite (once per env / after lock changes):

```bash
cd app/backend
poetry install --no-interaction --no-root
```

Then, still from `app/backend`:

```bash
poetry run ruff format --check .
poetry run ruff check .
poetry run mypy .
poetry run pyright
poetry run python manage.py check
poetry run python manage.py makemigrations --check --dry-run
```

## Maintainer notes

- **Do not upgrade Pyright** just because it prints a newer-version warning. Bump the pinned/ranged version in `pyproject.toml` only as a deliberate change.
- **Do not use** `PYRIGHT_PYTHON_FORCE_VERSION=latest` (or similar) to silence version warnings—that bypasses the repo’s pinned tooling.
- When adding **type stubs** or other **dev dependencies**, update `pyproject.toml` and refresh `poetry.lock` deliberately (`poetry lock` / `poetry add --group dev …`), not ad hoc unpinned installs.

## Future work

When editing `app/backend` code:

- Keep the baseline **clean**—fix new Ruff, mypy, Pyright, Django, or migration-check issues you introduce.
- Do not opportunistically weaken configs, ignore files, or exclude modules to hide regressions.
- If a change must temporarily fail a check, call that out in the PR and plan a follow-up; do not silently lower the bar.
