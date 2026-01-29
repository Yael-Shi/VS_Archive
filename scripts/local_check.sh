#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAIL_ON_WARNINGS="${FAIL_ON_WARNINGS:-0}"   # 0 = warnings mode, 1 = strict
RUN_SECRETS="${RUN_SECRETS:-0}"

BACKEND_DIR="$ROOT_DIR/app/backend"
INFRA_DIR="$ROOT_DIR/infra"

log()  { printf "\n==> %s\n" "$*"; }
ok()   { printf "✅ %s\n" "$*"; }
warn() { printf "⚠️  %s\n" "$*"; }
err()  { printf "❌ %s\n" "$*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }
run_in()   { local dir="$1"; shift; (cd "$dir" && "$@"); }

FAILURES=0

run_warn_step() {
  local name="$1"; shift
  log "$name"
  if "$@"; then
    ok "$name"
  else
    warn "$name (see details above)"
    [[ "$FAIL_ON_WARNINGS" == "1" ]] && return 1 || return 0
  fi
}

run_strict_step() {
  local name="$1"; shift
  log "$name"
  if "$@"; then
    ok "$name"
  else
    err "$name"
    return 1
  fi
}

maybe_secrets_scan() {
  if [[ "$RUN_SECRETS" != "1" ]]; then
    warn "Secrets scan skipped (set RUN_SECRETS=1 to enable)"
    return 0
  fi

  if need_cmd gitleaks; then
    run_warn_step "Secrets scan (gitleaks)" gitleaks detect --source "$ROOT_DIR" --no-git
    return 0
  fi

  warn "gitleaks not installed; skipping secrets scan"
  return 0
}

backend_checks() {
  log "Backend checks: $BACKEND_DIR"

  run_strict_step "Backend: poetry install" \
    run_in "$BACKEND_DIR" poetry install --no-interaction --no-root || return 1

  if [[ "$FAIL_ON_WARNINGS" == "1" ]]; then
    run_strict_step "Backend: ruff format --check" \
      run_in "$BACKEND_DIR" poetry run ruff format --check . || return 1
    run_strict_step "Backend: ruff check" \
      run_in "$BACKEND_DIR" poetry run ruff check . || return 1
  else
    run_warn_step "Backend: ruff format --check" \
      run_in "$BACKEND_DIR" poetry run ruff format --check .
    run_warn_step "Backend: ruff check" \
      run_in "$BACKEND_DIR" poetry run ruff check .
  fi

  run_warn_step "Backend: pyright" \
    run_in "$BACKEND_DIR" poetry run pyright

  run_warn_step "Backend: mypy (django-stubs)" \
    run_in "$BACKEND_DIR" poetry run mypy .

  run_warn_step "Backend: pytest" \
    run_in "$BACKEND_DIR" poetry run pytest -q

  run_warn_step "Backend: django check" \
    run_in "$BACKEND_DIR" poetry run python manage.py check
}

infra_checks() {
  log "Infra checks: $INFRA_DIR"

  run_strict_step "Infra: poetry install" \
    run_in "$INFRA_DIR" poetry install --no-interaction --no-root || return 1

  if [[ "$FAIL_ON_WARNINGS" == "1" ]]; then
    run_strict_step "Infra: ruff format --check" \
      run_in "$INFRA_DIR" poetry run ruff format --check . || return 1
    run_strict_step "Infra: ruff check" \
      run_in "$INFRA_DIR" poetry run ruff check . || return 1
  else
    run_warn_step "Infra: ruff format --check" \
      run_in "$INFRA_DIR" poetry run ruff format --check .
    run_warn_step "Infra: ruff check" \
      run_in "$INFRA_DIR" poetry run ruff check .
  fi

  run_warn_step "Infra: pyright" \
    run_in "$INFRA_DIR" poetry run pyright
}

log "Local checks (FAIL_ON_WARNINGS=$FAIL_ON_WARNINGS, RUN_SECRETS=$RUN_SECRETS)"

maybe_secrets_scan

backend_checks || FAILURES=$((FAILURES+1))
infra_checks   || FAILURES=$((FAILURES+1))

log "Summary"
if [[ "$FAILURES" -eq 0 ]]; then
  ok "All checks completed"
  exit 0
fi

if [[ "$FAIL_ON_WARNINGS" == "1" ]]; then
  err "Completed with issues (strict mode)"
  exit 1
fi

warn "Completed with warnings (non-blocking mode)"
exit 0
