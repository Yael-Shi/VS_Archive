#!/usr/bin/env bash

# Argument parsing and confirmation helpers for scripts/deploy-app-v2.sh.
# This file is safe to source from tests. The deploy script itself is not.

YES=0
CONFIRM=0
SKIP_BUILD=0
EXPLICIT_TAG=""

deploy_app_usage() {
  cat <<'EOF'
Usage:
  bash scripts/deploy-app-v2.sh [options]

Options:
  --confirm
      Ask for the interactive DEPLOY confirmation before executing.
      Without this option, a deployment proceeds when every preflight
      and infrastructure check passes.

  --yes
      Backward-compatible alias for a non-interactive deployment.
      Cannot be combined with --confirm.

  --skip-build
      Reuse an existing ECR image instead of building and pushing a new one.
      Requires --tag.

  --tag TAG
      Use the specified image tag.
      Required with --skip-build.

  -h, --help
      Show this help.
EOF
}

parse_deploy_args() {
  YES=0
  CONFIRM=0
  SKIP_BUILD=0
  EXPLICIT_TAG=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        YES=1
        shift
        ;;
      --confirm)
        CONFIRM=1
        shift
        ;;
      --skip-build)
        SKIP_BUILD=1
        shift
        ;;
      --tag)
        [[ $# -ge 2 ]] || {
          echo "ERROR: --tag requires a value" >&2
          exit 2
        }
        EXPLICIT_TAG="$2"
        shift 2
        ;;
      -h|--help)
        deploy_app_usage
        exit 0
        ;;
      *)
        echo "ERROR: Unknown option: $1" >&2
        deploy_app_usage >&2
        exit 2
        ;;
    esac
  done

  if [[ "$YES" -eq 1 && "$CONFIRM" -eq 1 ]]; then
    echo "ERROR: --yes and --confirm cannot be used together." >&2
    echo "Use --confirm to type DEPLOY, or omit both to deploy non-interactively." >&2
    exit 2
  fi

  if [[ "$SKIP_BUILD" -eq 1 && -z "$EXPLICIT_TAG" ]]; then
    echo "ERROR: --skip-build requires --tag TAG" >&2
    exit 2
  fi

  if [[ "$SKIP_BUILD" -eq 0 && -n "$EXPLICIT_TAG" ]]; then
    echo "ERROR: --tag may only be used together with --skip-build" >&2
    exit 2
  fi
}

deployment_confirmation_mode() {
  if [[ "$CONFIRM" -eq 1 && "$YES" -eq 1 ]]; then
    echo "conflict"
    return 2
  fi
  if [[ "$CONFIRM" -eq 1 ]]; then
    echo "interactive"
    return 0
  fi
  if [[ "$YES" -eq 1 ]]; then
    echo "yes"
    return 0
  fi
  echo "automatic"
}

confirm_deployment() {
  local confirm=""
  if ! read -r -p "Type DEPLOY to continue: " confirm; then
    return 1
  fi
  [[ "$confirm" == "DEPLOY" ]]
}

cancel_deployment() {
  echo "Deployment cancelled."
  echo "Nothing was deployed by this script."
  if [[ -n "${IMAGE_URI:-}" ]]; then
    echo "Image remains available in ECR:"
    echo "  $IMAGE_URI"
  fi
  exit 1
}

accept_guard_decision() {
  case "$1" in
    decision=deploy|decision=noop)
      return 0
      ;;
    *)
      echo "ERROR: unrecognized change-guard decision." >&2
      return 1
      ;;
  esac
}

run_app_change_guard() {
  if [[ -z "${DEPLOY_CHANGE_GUARD_PY:-}" ]]; then
    echo "ERROR: DEPLOY_CHANGE_GUARD_PY is not set" >&2
    return 2
  fi
  python3 "$DEPLOY_CHANGE_GUARD_PY" validate-app "$@"
}

run_dependency_change_guard() {
  if [[ -z "${DEPLOY_CHANGE_GUARD_PY:-}" ]]; then
    echo "ERROR: DEPLOY_CHANGE_GUARD_PY is not set" >&2
    return 2
  fi
  python3 "$DEPLOY_CHANGE_GUARD_PY" validate-noop "$@"
}

related_stack_names() {
  local stack_name="$1"
  local prefix
  case "$stack_name" in
    *-app-v2)
      prefix="${stack_name%-app-v2}"
      if [[ -z "$prefix" ]]; then
        echo "ERROR: CDK stack name ${stack_name} cannot identify related stacks." >&2
        return 1
      fi
      printf '%s\n' "${prefix}-network-v2" "${prefix}-data-v2"
      ;;
    *)
      echo "ERROR: CDK stack name ${stack_name} must end with -app-v2." >&2
      return 1
      ;;
  esac
}

delete_change_set() {
  local stack="$1"
  local existing
  local remaining=()
  aws cloudformation delete-change-set \
    --stack-name "$stack" \
    --change-set-name "$CHANGE_SET_NAME" \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    >/dev/null 2>&1 || true
  for existing in "${CHANGE_SET_STACKS[@]}"; do
    if [[ "$existing" != "$stack" ]]; then
      remaining+=("$existing")
    fi
  done
  CHANGE_SET_STACKS=("${remaining[@]+"${remaining[@]}"}")
}

discard_prepared_change_sets() {
  local stack
  if [[ "$KEEP_PREPARED_CHANGE_SETS" -eq 1 ]]; then
    return 0
  fi
  for stack in "${CHANGE_SET_STACKS[@]+"${CHANGE_SET_STACKS[@]}"}"; do
    aws cloudformation delete-change-set \
      --stack-name "$stack" \
      --change-set-name "$CHANGE_SET_NAME" \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      >/dev/null 2>&1 || true
  done
}

write_change_set_json() {
  local stack="$1"
  local dest="$2"
  local page_dir
  local next=""
  local page=0
  page_dir="$(mktemp -d)"
  while true; do
    if [[ -n "$next" ]]; then
      aws cloudformation describe-change-set \
        --stack-name "$stack" \
        --change-set-name "$CHANGE_SET_NAME" \
        --next-token "$next" \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        --output json \
        >"$page_dir/${page}.json"
    else
      aws cloudformation describe-change-set \
        --stack-name "$stack" \
        --change-set-name "$CHANGE_SET_NAME" \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        --output json \
        >"$page_dir/${page}.json"
    fi
    next="$(
      python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("NextToken") or "")' \
        "$page_dir/${page}.json"
    )"
    page=$((page + 1))
    if [[ -z "$next" ]]; then
      break
    fi
    if [[ "$page" -ge 20 ]]; then
      fail "Change set for $stack exceeded 20 description pages."
    fi
  done
  python3 "$DEPLOY_CHANGE_GUARD_PY" merge-pages "$page_dir" >"$dest"
  rm -rf "$page_dir"
}

# CDK 2.1135.1 prepare-change-set exits 0 when CloudFormation reports no
# changes, and it leaves that FAILED change set in place. describe-change-set
# is therefore still valid. Executing deployments delete the empty change set;
# this non-executing path must not expect that deletion.
prepare_and_validate_deployment_plan() {
  local stack
  section "COLLECT DEPLOYMENT PLAN"

  for stack in "$NETWORK_STACK" "$DATA_STACK" "$CDK_STACK"; do
    CHANGE_SET_STACKS+=("$stack")
    poetry run cdk deploy \
      "$stack" \
      --app "$SYNTH_DIR" \
      --exclusively \
      --method prepare-change-set \
      --change-set-name "$CHANGE_SET_NAME" \
      --require-approval never \
      --profile "$AWS_PROFILE"
  done

  NETWORK_CHANGE_SET_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_network_change_set.json"
  DATA_CHANGE_SET_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_data_change_set.json"
  write_change_set_json "$NETWORK_STACK" "$NETWORK_CHANGE_SET_FILE"
  write_change_set_json "$DATA_STACK" "$DATA_CHANGE_SET_FILE"
  write_change_set_json "$CDK_STACK" "$APP_CHANGE_SET_FILE"

  aws cloudformation get-template \
    --stack-name "$CDK_STACK" \
    --template-stage Original \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --output json \
    >"$CURRENT_TEMPLATE_FILE"

  if aws cloudformation get-template \
    --stack-name "$CDK_STACK" \
    --change-set-name "$CHANGE_SET_NAME" \
    --template-stage Original \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --output json \
    >"$PROPOSED_TEMPLATE_FILE"; then
    echo "Proposed template saved to: $PROPOSED_TEMPLATE_FILE"
  else
    rm -f "$PROPOSED_TEMPLATE_FILE"
    PROPOSED_TEMPLATE_FILE=""
    echo "No proposed template is available for change set $CHANGE_SET_NAME."
  fi

  echo "App change set saved to: $APP_CHANGE_SET_FILE"
  echo "Network change set saved to: $NETWORK_CHANGE_SET_FILE"
  echo "Data change set saved to: $DATA_CHANGE_SET_FILE"
  echo "Current template saved to: $CURRENT_TEMPLATE_FILE"

  section "VALIDATE DEPLOYMENT PLAN"

  if ! run_dependency_change_guard \
    --change-set "$NETWORK_CHANGE_SET_FILE" \
    --stack-name "$NETWORK_STACK"; then
    fail "Network stack change guard rejected this deployment. Change set: $NETWORK_CHANGE_SET_FILE"
  fi
  delete_change_set "$NETWORK_STACK"

  if ! run_dependency_change_guard \
    --change-set "$DATA_CHANGE_SET_FILE" \
    --stack-name "$DATA_STACK"; then
    fail "Data stack change guard rejected this deployment. Change set: $DATA_CHANGE_SET_FILE"
  fi
  delete_change_set "$DATA_STACK"

  GUARD_ARGS=(
    --change-set "$APP_CHANGE_SET_FILE"
    --current-template "$CURRENT_TEMPLATE_FILE"
    --image-uri "$IMAGE_URI"
    --repository-name "$ECR_REPO"
    --stack-name "$CDK_STACK"
  )
  if [[ -n "$PROPOSED_TEMPLATE_FILE" ]]; then
    GUARD_ARGS+=(--proposed-template "$PROPOSED_TEMPLATE_FILE")
  fi

  DECISION=""
  if ! DECISION="$(run_app_change_guard "${GUARD_ARGS[@]}")"; then
    fail "Infrastructure change guard rejected this deployment. Change set: $APP_CHANGE_SET_FILE"
  fi
  accept_guard_decision "$DECISION" || \
    fail "Infrastructure change guard returned an unrecognized decision."

  echo "Change guard decision: $DECISION"
}

apply_reviewed_change_set() {
  if [[ "$DECISION" == "decision=noop" ]]; then
    echo "CloudFormation reported no infrastructure changes."
    echo "Skipping change-set execution and verifying the current services."
    return 0
  fi

  KEEP_PREPARED_CHANGE_SETS=1
  poetry run cdk deploy \
    "$CDK_STACK" \
    --app "$SYNTH_DIR" \
    --exclusively \
    --method execute-change-set \
    --change-set-name "$CHANGE_SET_NAME" \
    --require-approval never \
    --profile "$AWS_PROFILE"
  KEEP_PREPARED_CHANGE_SETS=0
}
