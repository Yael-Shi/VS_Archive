#!/usr/bin/env bash

# Mocked no-op flow for CDK 2.1135.1 prepare-change-set.
# The mock follows the local CLI: exit 0, retain the FAILED change set,
# and leave it available to describe-change-set. No AWS calls are made.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB="$REPO_ROOT/scripts/deploy_app_v2_lib.sh"
GUARD="$REPO_ROOT/scripts/deploy_change_guard.py"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
  rm -f \
    /tmp/vs_archive_deploy_nooptest_network_change_set.json \
    /tmp/vs_archive_deploy_nooptest_data_change_set.json
}
trap cleanup EXIT

MOCK_LOG="$tmpdir/calls.log"
MOCK_STATE="$tmpdir/changesets"
MOCK_TEMPLATE="$tmpdir/template.json"
mkdir -p "$tmpdir/bin" "$MOCK_STATE"

python3 - <<'PY' >"$MOCK_TEMPLATE"
import json
stack = "vs-archive-dev-app-v2"
repo = "vs-archive-web"
tag = "main-bbbbbbbbbbbb-20260202020202"

def task(kind):
    return {
        "Type": "AWS::ECS::TaskDefinition",
        "Metadata": {"aws:cdk:path": f"{stack}/vs-archive-dev-{kind}-td/Resource"},
        "Properties": {
            "ContainerDefinitions": [{
                "Name": kind,
                "Image": {"Fn::Join": ["", [
                    {"Ref": "AWS::AccountId"},
                    f".dkr.ecr.eu-central-1.amazonaws.com/{repo}:{tag}",
                ]]},
            }],
        },
    }

def service(kind, ref):
    return {
        "Type": "AWS::ECS::Service",
        "Metadata": {"aws:cdk:path": f"{stack}/vs-archive-dev-{kind}-svc/Service"},
        "Properties": {"TaskDefinition": {"Ref": ref}},
    }

json.dump({
    "TemplateBody": {
        "Resources": {
            "WebTaskDefinition": task("web"),
            "WorkerTaskDefinition": task("worker"),
            "WebService": service("web", "WebTaskDefinition"),
            "WorkerService": service("worker", "WorkerTaskDefinition"),
        }
    }
}, open("/dev/stdout", "w"))
PY

cat >"$tmpdir/bin/poetry" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'poetry %s\n' "$*" >>"$MOCK_LOG"
if [[ "$*" == *"execute-change-set"* ]]; then
  echo "execute-change-set was invoked" >>"$MOCK_LOG"
  exit 99
fi
if [[ "$*" != *"prepare-change-set"* ]]; then
  echo "unexpected cdk invocation: $*" >>"$MOCK_LOG"
  exit 97
fi
stack=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "deploy" ]]; then
    stack="${args[$((i + 1))]}"
  fi
done
if [[ -z "$stack" ]]; then
  echo "prepare-change-set did not name a stack" >>"$MOCK_LOG"
  exit 98
fi
# CDK 2.1135.1 exits 0 and retains the no-op change set.
mkdir -p "$MOCK_STATE"
: >"$MOCK_STATE/$stack"
printf 'prepare %s exit=0 retained=1\n' "$stack" >>"$MOCK_LOG"
exit 0
EOF

cat >"$tmpdir/bin/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'aws %s\n' "$*" >>"$MOCK_LOG"
operation=""
stack=""
change_set=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    describe-change-set|get-template|delete-change-set)
      operation="${args[$i]}"
      ;;
    --stack-name)
      stack="${args[$((i + 1))]}"
      ;;
    --change-set-name)
      change_set="${args[$((i + 1))]}"
      ;;
  esac
done

case "$operation" in
  describe-change-set)
    if [[ ! -f "$MOCK_STATE/$stack" ]]; then
      echo "describe-change-set failed because $stack was not retained" >&2
      exit 3
    fi
    printf 'describe %s available=1\n' "$stack" >>"$MOCK_LOG"
    cat <<'JSON'
{
  "Status": "FAILED",
  "ExecutionStatus": "UNAVAILABLE",
  "StatusReason": "The submitted information didn't contain changes. Submit different information to create a change set.",
  "Changes": []
}
JSON
    ;;
  get-template)
    if [[ -n "$change_set" && ! -f "$MOCK_STATE/$stack" ]]; then
      echo "get-template failed because $stack change set was not retained" >&2
      exit 4
    fi
    printf 'get-template %s change-set=%s\n' "$stack" "${change_set:-none}" >>"$MOCK_LOG"
    cat "$MOCK_TEMPLATE"
    ;;
  delete-change-set)
    rm -f "$MOCK_STATE/$stack"
    printf 'delete %s\n' "$stack" >>"$MOCK_LOG"
    ;;
  *)
    echo "unexpected aws operation: $*" >&2
    exit 5
    ;;
esac
EOF

chmod +x "$tmpdir/bin/poetry" "$tmpdir/bin/aws"

export PATH="$tmpdir/bin:$PATH"
export MOCK_LOG MOCK_STATE MOCK_TEMPLATE
export DEPLOY_CHANGE_GUARD_PY="$GUARD"
export AWS_PROFILE=default
export AWS_REGION=eu-central-1
export ECR_REPO=vs-archive-web
export CDK_STACK=vs-archive-dev-app-v2
export NETWORK_STACK=vs-archive-dev-network-v2
export DATA_STACK=vs-archive-dev-data-v2
export CHANGE_SET_NAME=vsarchiveguardtest
CHANGE_SET_STACKS=()
KEEP_PREPARED_CHANGE_SETS=0
export TIMESTAMP=nooptest
export SYNTH_DIR="$tmpdir/cdk.out"
export APP_CHANGE_SET_FILE="$tmpdir/app_change_set.json"
export CURRENT_TEMPLATE_FILE="$tmpdir/current_template.json"
export PROPOSED_TEMPLATE_FILE="$tmpdir/proposed_template.json"
export IMAGE_URI="185990503355.dkr.ecr.eu-central-1.amazonaws.com/vs-archive-web:main-bbbbbbbbbbbb-20260202020202"

# shellcheck source=../deploy_app_v2_lib.sh
source "$LIB"

section() {
  echo "=== $*"
}

prepare_and_validate_deployment_plan
[[ "$DECISION" == "decision=noop" ]] || fail "expected decision=noop, got ${DECISION:-<empty>}"
[[ -f "$MOCK_STATE/$CDK_STACK" ]] || fail "app no-op change set was deleted before exit cleanup"
[[ ! -f "$MOCK_STATE/$NETWORK_STACK" ]] || fail "network no-op change set was not cleaned after validation"
[[ ! -f "$MOCK_STATE/$DATA_STACK" ]] || fail "data no-op change set was not cleaned after validation"

apply_reviewed_change_set
if grep -q "execute-change-set" "$MOCK_LOG"; then
  fail "no-op flow executed a change set"
fi
[[ -f "$MOCK_STATE/$CDK_STACK" ]] || fail "app change set disappeared before trap cleanup"

discard_prepared_change_sets
[[ ! -f "$MOCK_STATE/$CDK_STACK" ]] || fail "exit cleanup did not delete the retained app change set"

python3 - "$MOCK_LOG" <<'PY'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
stacks = [
    "vs-archive-dev-network-v2",
    "vs-archive-dev-data-v2",
    "vs-archive-dev-app-v2",
]
for stack in stacks:
    prepared = [i for i, line in enumerate(lines) if line == f"prepare {stack} exit=0 retained=1"]
    described = [i for i, line in enumerate(lines) if line == f"describe {stack} available=1"]
    deleted = [i for i, line in enumerate(lines) if line == f"delete {stack}"]
    if len(prepared) != 1 or len(described) != 1 or len(deleted) != 1:
        raise SystemExit(
            f"{stack} counts prepare={len(prepared)} describe={len(described)} delete={len(deleted)}"
        )
    if not (prepared[0] < described[0] < deleted[0]):
        raise SystemExit(f"{stack} was not retained until after describe-change-set")
if any("execute-change-set" in line for line in lines):
    raise SystemExit("execute-change-set was recorded")
print("noop prepare, describe, validate, and cleanup order passed")
PY
