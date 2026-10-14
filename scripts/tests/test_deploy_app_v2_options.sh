#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB="$REPO_ROOT/scripts/deploy_app_v2_lib.sh"
GUARD="$REPO_ROOT/scripts/deploy_change_guard.py"
DEPLOY="$REPO_ROOT/scripts/deploy-app-v2.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_eq() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  [[ "$actual" == "$expected" ]] || fail "$label: expected [$expected], got [$actual]"
}

# Default non-interactive mode.
(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  parse_deploy_args
  assert_eq "$(deployment_confirmation_mode)" "automatic" "default mode"
  assert_eq "$YES" "0" "default --yes"
  assert_eq "$CONFIRM" "0" "default --confirm"
)

# Explicit confirmation.
(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  parse_deploy_args --confirm
  assert_eq "$(deployment_confirmation_mode)" "interactive" "--confirm mode"
)

# Backward-compatible --yes.
(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  parse_deploy_args --yes --skip-build --tag main-abc-20260101010101
  assert_eq "$(deployment_confirmation_mode)" "yes" "--yes mode"
  assert_eq "$SKIP_BUILD" "1" "--skip-build"
  assert_eq "$EXPLICIT_TAG" "main-abc-20260101010101" "--tag"
)

# Conflicting options.
conflict_status=0
conflict_output="$(bash "$DEPLOY" --yes --confirm 2>&1)" || conflict_status=$?
assert_eq "$conflict_status" "2" "--yes --confirm exit"
[[ "$conflict_output" == *"cannot be used together"* ]] || \
  fail "conflict message missing: $conflict_output"

reverse_status=0
bash "$DEPLOY" --confirm --yes >/dev/null 2>&1 || reverse_status=$?
assert_eq "$reverse_status" "2" "--confirm --yes exit"

# Help and usage errors stop before any AWS call.
help_status=0
help_output="$(bash "$DEPLOY" --help 2>&1)" || help_status=$?
assert_eq "$help_status" "0" "--help exit"
[[ "$help_output" == *"--confirm"* ]] || fail "help does not mention --confirm"

unknown_status=0
bash "$DEPLOY" --not-a-real-option >/dev/null 2>&1 || unknown_status=$?
assert_eq "$unknown_status" "2" "unknown option exit"

tag_status=0
bash "$DEPLOY" --tag only-tag >/dev/null 2>&1 || tag_status=$?
assert_eq "$tag_status" "2" "--tag without --skip-build"

skip_status=0
bash "$DEPLOY" --skip-build >/dev/null 2>&1 || skip_status=$?
assert_eq "$skip_status" "2" "--skip-build without --tag"

# Interactive confirmation and cancellation.
(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  printf 'DEPLOY\n' | confirm_deployment
)
cancel_status=0
(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  printf 'no\n' | confirm_deployment
) || cancel_status=$?
assert_eq "$cancel_status" "1" "wrong confirmation text"

eof_status=0
(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  confirm_deployment </dev/null
) || eof_status=$?
assert_eq "$eof_status" "1" "EOF confirmation"

cancel_exit=0
cancel_output="$(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  IMAGE_URI="185990503355.dkr.ecr.eu-central-1.amazonaws.com/vs-archive-web:example"
  cancel_deployment
)" || cancel_exit=$?
assert_eq "$cancel_exit" "1" "cancel_deployment exit"
[[ "$cancel_output" == *"Nothing was deployed"* ]] || \
  fail "cancel message missing: $cancel_output"
[[ "$cancel_output" == *"example"* ]] || fail "cancel message omits image"

# Failure propagation from the structured guard.
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
python3 - <<PY
import json
from pathlib import Path
root = Path("$tmpdir")
stack = "vs-archive-dev-app-v2"
repo = "vs-archive-web"
tag = "main-bbbbbbbbbbbb-20260202020202"
def task(role, logical_tag):
    path = f"{stack}/vs-archive-dev-{'web' if role == 'web' else 'worker'}-td/Resource"
    return {
        "Type": "AWS::ECS::TaskDefinition",
        "Metadata": {"aws:cdk:path": path},
        "Properties": {
            "ContainerDefinitions": [{
                "Name": role,
                "Image": {"Fn::Join": ["", [
                    {"Ref": "AWS::AccountId"},
                    f".dkr.ecr.eu-central-1.amazonaws.com/{repo}:{logical_tag}",
                ]]},
            }]
        },
    }
template = {
    "Resources": {
        "WebTaskDefinition": task("web", tag),
        "WorkerTaskDefinition": task("worker", tag),
        "WebService": {
            "Type": "AWS::ECS::Service",
            "Metadata": {"aws:cdk:path": f"{stack}/vs-archive-dev-web-svc/Service"},
            "Properties": {"TaskDefinition": {"Ref": "WebTaskDefinition"}},
        },
        "WorkerService": {
            "Type": "AWS::ECS::Service",
            "Metadata": {"aws:cdk:path": f"{stack}/vs-archive-dev-worker-svc/Service"},
            "Properties": {"TaskDefinition": {"Ref": "WorkerTaskDefinition"}},
        },
    }
}
(root / "current.json").write_text(json.dumps(template))
(root / "bad.json").write_text(json.dumps({
    "Status": "CREATE_COMPLETE",
    "ExecutionStatus": "AVAILABLE",
    "Changes": [{
        "Type": "Resource",
        "ResourceChange": {
            "Action": "Add",
            "LogicalResourceId": "NewRole",
            "ResourceType": "AWS::IAM::Role",
        },
    }],
}))
PY

(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  export DEPLOY_CHANGE_GUARD_PY="$GUARD"
  decision=""
  if decision="$(
    run_app_change_guard \
      --change-set "$tmpdir/bad.json" \
      --current-template "$tmpdir/current.json" \
      --proposed-template "$tmpdir/current.json" \
      --image-uri "185990503355.dkr.ecr.eu-central-1.amazonaws.com/vs-archive-web:main-bbbbbbbbbbbb-20260202020202" \
      --repository-name vs-archive-web \
      --stack-name vs-archive-dev-app-v2
  )"; then
    echo "guard failure was not propagated: $decision" >&2
    exit 1
  fi
  [[ -z "$decision" ]] || {
    echo "failed guard still produced a decision: $decision" >&2
    exit 1
  }
)

(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  accept_guard_decision "decision=deploy"
  accept_guard_decision "decision=noop"
  if accept_guard_decision "decision=maybe"; then
    echo "unrecognized decision was accepted" >&2
    exit 1
  fi
)

(
  # shellcheck source=../deploy_app_v2_lib.sh
  source "$LIB"
  output="$(related_stack_names vs-archive-dev-app-v2)"
  assert_eq "$output" $'vs-archive-dev-network-v2\nvs-archive-dev-data-v2' "related stacks"
  if related_stack_names custom-stack >/dev/null; then
    echo "unrecognized stack name was accepted" >&2
    exit 1
  fi
)

echo "deploy option and guard propagation tests passed"
