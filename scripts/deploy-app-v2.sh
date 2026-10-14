#!/usr/bin/env bash

# VS-Archive app-v2 deployment helper
#
# Purpose:
#   Deploy the current remote-aligned main branch to the dev app-v2 ECS stack.
#   The CDK stack named dev is the live production environment.
#
# Safety checks performed:
#   - verifies this script is RUN, not sourced
#   - verifies working tree is clean
#   - verifies current branch is main
#   - fetches origin and verifies HEAD == origin/main
#   - verifies AWS account/profile/region
#   - verifies the ECR repository exists
#   - verifies exactly one app-v2 ECS cluster exists
#   - verifies Node, npm, CDK, Poetry, Docker, AWS CLI are available
#   - builds Docker with app/backend as the build context
#   - pushes a unique main-<sha>-<timestamp> image to ECR
#   - verifies the image exists in ECR
#   - runs CDK diff with ALL required image-tag contexts:
#       image_tag
#       web_image_tag
#       worker_image_tag
#   - aborts if the CDK diff unexpectedly contains the default :dev image
#   - creates CloudFormation change sets without executing them
#   - allows a normal deployment only when the app change set is exactly
#     the web and worker task-definition image updates and the ECS service
#     task-definition references those updates cause
#   - requires the network and data stacks to be change-set no-ops
#   - proceeds without a prompt when those checks pass
#   - with --confirm, requires explicit DEPLOY confirmation
#   - executes the reviewed app change set
#   - waits for ECS services to stabilize after deployment
#   - verifies web and worker task definitions use the expected image
#   - verifies desired/running/pending/rollout state
#
# Normal usage:
#
#   cd ~/vs-archive/vs-archive
#   bash scripts/deploy-app-v2.sh
#
# Interactive confirmation:
#
#   bash scripts/deploy-app-v2.sh --confirm
#
# Backward-compatible non-interactive confirmation:
#
#   bash scripts/deploy-app-v2.sh --yes
#
# Reuse an image that already exists in ECR:
#
#   bash scripts/deploy-app-v2.sh \
#     --skip-build \
#     --tag main-<sha>-<timestamp>
#
# Optional environment overrides:
#
#   AWS_PROFILE=default \
#   AWS_REGION=eu-central-1 \
#   AWS_ACCOUNT_ID=185990503355 \
#   ECR_REPO=vs-archive-web \
#   CDK_STACK=vs-archive-dev-app-v2 \
#   bash scripts/deploy-app-v2.sh
#
# Runtime logs are written under /tmp and are NOT committed to the repository.
#
# IMPORTANT:
#   Run this script with `bash scripts/deploy-app-v2.sh`.
#   Do NOT run `source scripts/deploy-app-v2.sh`.

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "ERROR: Do not source this script."
  echo "Run it with:"
  echo "  bash scripts/deploy-app-v2.sh"
  return 2
fi

set -Eeuo pipefail

SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd
)"
REPO_ROOT="$(
  cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1
  pwd
)"

# shellcheck source=deploy_app_v2_lib.sh
source "$SCRIPT_DIR/deploy_app_v2_lib.sh"
DEPLOY_CHANGE_GUARD_PY="$SCRIPT_DIR/deploy_change_guard.py"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-eu-central-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-185990503355}"
ECR_REPO="${ECR_REPO:-vs-archive-web}"
CDK_STACK="${CDK_STACK:-vs-archive-dev-app-v2}"

usage() {
  deploy_app_usage
}

parse_deploy_args "$@"

TIMESTAMP="$(date -u +%Y%m%d%H%M%S)"
LOG="/tmp/vs_archive_deploy_${TIMESTAMP}.log"
TAG_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_image_tag.sh"
DIFF_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_cdk_diff.txt"
SYNTH_DIR="/tmp/vs_archive_deploy_${TIMESTAMP}_cdk.out"
APP_CHANGE_SET_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_app_change_set.json"
CURRENT_TEMPLATE_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_current_template.json"
PROPOSED_TEMPLATE_FILE="/tmp/vs_archive_deploy_${TIMESTAMP}_proposed_template.json"
CHANGE_SET_NAME="vsarchiveguard${TIMESTAMP}"
CHANGE_SET_STACKS=()
KEEP_PREPARED_CHANGE_SETS=0

exec > >(tee -a "$LOG") 2>&1

fail() {
  echo
  echo "ERROR: $*"
  echo "Log: $LOG"
  exit 1
}

section() {
  echo
  echo "================================================================"
  echo "=== $*"
  echo "================================================================"
}

trap discard_prepared_change_sets EXIT

section "PREFLIGHT"

[[ -d "$REPO_ROOT/.git" ]] || fail "Repository not found at: $REPO_ROOT"

cd "$REPO_ROOT"

for command_name in git aws docker node npm cdk poetry python3; do
  command -v "$command_name" >/dev/null || fail "$command_name not found"
done

echo "repo=$REPO_ROOT"
echo "AWS_PROFILE=$AWS_PROFILE"
echo "AWS_REGION=$AWS_REGION"
echo "AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID"
echo "ECR_REPO=$ECR_REPO"
echo "CDK_STACK=$CDK_STACK"
echo "confirmation=$(deployment_confirmation_mode)"
echo "node=$(node --version)"
echo "npm=$(npm --version)"
echo "cdk=$(cdk --version)"

if ! RELATED_STACK_OUTPUT="$(related_stack_names "$CDK_STACK")"; then
  fail "Could not identify the network and data stacks for $CDK_STACK."
fi
mapfile -t RELATED_STACKS <<< "$RELATED_STACK_OUTPUT"
NETWORK_STACK="${RELATED_STACKS[0]}"
DATA_STACK="${RELATED_STACKS[1]}"
echo "NETWORK_STACK=$NETWORK_STACK"
echo "DATA_STACK=$DATA_STACK"

section "GIT STATE"

git fetch origin

BRANCH="$(git branch --show-current)"
HEAD_SHA="$(git rev-parse HEAD)"
ORIGIN_MAIN_SHA="$(git rev-parse origin/main)"

echo "branch=$BRANCH"
echo "HEAD=$HEAD_SHA"
echo "origin/main=$ORIGIN_MAIN_SHA"

[[ "$BRANCH" == "main" ]] || \
  fail "Current branch is '$BRANCH', expected 'main'."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean:"
  git status --short
  fail "Commit/stash/remove local changes before deployment."
fi

[[ "$HEAD_SHA" == "$ORIGIN_MAIN_SHA" ]] || \
  fail "Local main is not aligned with origin/main."

section "AWS IDENTITY"

IDENTITY_ACCOUNT="$(
  aws sts get-caller-identity \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query Account \
    --output text
)"

echo "resolved_account=$IDENTITY_ACCOUNT"

[[ "$IDENTITY_ACCOUNT" == "$AWS_ACCOUNT_ID" ]] || \
  fail "AWS account mismatch: got $IDENTITY_ACCOUNT, expected $AWS_ACCOUNT_ID."

aws ecr describe-repositories \
  --repository-names "$ECR_REPO" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  >/dev/null \
  || fail "ECR repository '$ECR_REPO' not found."

mapfile -t APP_V2_CLUSTERS < <(
  aws ecs list-clusters \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query 'clusterArns[?contains(@, `app-v2`)]' \
    --output text \
    | tr '\t' '\n' \
    | sed '/^$/d'
)

if [[ "${#APP_V2_CLUSTERS[@]}" -ne 1 ]]; then
  printf 'Found app-v2 clusters:\n%s\n' "${APP_V2_CLUSTERS[*]:-<none>}"
  fail "Expected exactly one app-v2 ECS cluster."
fi

CLUSTER_ARN="${APP_V2_CLUSTERS[0]}"
echo "cluster=$CLUSTER_ARN"

section "IMAGE TAG"

SHORT_SHA="$(git rev-parse --short=12 HEAD)"

if [[ -n "$EXPLICIT_TAG" ]]; then
  IMAGE_TAG="$EXPLICIT_TAG"
else
  IMAGE_TAG="main-${SHORT_SHA}-$(date -u +%Y%m%d%H%M%S)"
fi

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_URI="${ECR_REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"

echo "IMAGE_TAG=$IMAGE_TAG"
echo "IMAGE_URI=$IMAGE_URI"

printf 'export IMAGE_TAG=%q\nexport IMAGE_URI=%q\n' \
  "$IMAGE_TAG" \
  "$IMAGE_URI" \
  > "$TAG_FILE"

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  section "ECR LOGIN"

  aws ecr get-login-password \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    | docker login \
        --username AWS \
        --password-stdin "$ECR_REGISTRY"

  section "DOCKER BUILD"

  # Dockerfile expects pyproject.toml at the build-context root.
  docker build \
    -f app/backend/Dockerfile \
    -t "$IMAGE_URI" \
    app/backend

  section "DOCKER PUSH"

  docker push "$IMAGE_URI"
else
  section "SKIP BUILD"
  echo "Reusing existing image tag: $IMAGE_TAG"
fi

section "VERIFY ECR IMAGE"

aws ecr describe-images \
  --repository-name "$ECR_REPO" \
  --image-ids imageTag="$IMAGE_TAG" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --query 'imageDetails[0].{tags:imageTags,pushedAt:imagePushedAt,digest:imageDigest}' \
  --output table

section "CDK SYNTH"

cd "$REPO_ROOT/infra"

poetry run cdk synth \
  --quiet \
  --output "$SYNTH_DIR" \
  --profile "$AWS_PROFILE" \
  -c image_tag="$IMAGE_TAG" \
  -c web_image_tag="$IMAGE_TAG" \
  -c worker_image_tag="$IMAGE_TAG"

section "CDK DIFF"

poetry run cdk diff \
  --app "$SYNTH_DIR" \
  --profile "$AWS_PROFILE" \
  "$CDK_STACK" \
  "$NETWORK_STACK" \
  "$DATA_STACK" \
  2>&1 | tee "$DIFF_FILE"

echo
echo "CDK diff saved to: $DIFF_FILE"

if grep -F -q "/${ECR_REPO}:dev" "$DIFF_FILE"; then
  fail "CDK diff contains ':dev'. Image-tag context was not applied correctly."
fi

if ! grep -F -q "/${ECR_REPO}:${IMAGE_TAG}" "$DIFF_FILE"; then
  echo
  echo "Expected tag is not present in the diff."
  echo "Checking whether the environment already runs this exact tag."

  WEB_SERVICE_PRE="$(
    aws ecs list-services \
      --cluster "$CLUSTER_ARN" \
      --region "$AWS_REGION" \
      --profile "$AWS_PROFILE" \
      --query 'serviceArns[?contains(@, `websvc`)] | [0]' \
      --output text
  )"

  [[ "$WEB_SERVICE_PRE" != "None" && -n "$WEB_SERVICE_PRE" ]] || \
    fail "Web service not found."

  WEB_TD_PRE="$(
    aws ecs describe-services \
      --cluster "$CLUSTER_ARN" \
      --services "$WEB_SERVICE_PRE" \
      --region "$AWS_REGION" \
      --profile "$AWS_PROFILE" \
      --query 'services[0].taskDefinition' \
      --output text
  )"

  CURRENT_WEB_IMAGE="$(
    aws ecs describe-task-definition \
      --task-definition "$WEB_TD_PRE" \
      --region "$AWS_REGION" \
      --profile "$AWS_PROFILE" \
      --query 'taskDefinition.containerDefinitions[0].image' \
      --output text
  )"

  [[ "$CURRENT_WEB_IMAGE" == *":${IMAGE_TAG}" ]] || \
    fail "Expected image tag is neither in CDK diff nor current web runtime."
fi

prepare_and_validate_deployment_plan

section "DEPLOY CONFIRMATION"

echo "Ready to deploy:"
echo "  stack:     $CDK_STACK"
echo "  image:     $IMAGE_URI"
echo "  account:   $AWS_ACCOUNT_ID"
echo "  region:    $AWS_REGION"
echo "  profile:   $AWS_PROFILE"
echo "  changeset: $CHANGE_SET_NAME"
echo "  decision:  $DECISION"
echo

case "$(deployment_confirmation_mode)" in
  interactive)
    if ! confirm_deployment; then
      cancel_deployment
    fi
    ;;
  yes)
    echo "Confirmation flag --yes supplied; continuing."
    ;;
  automatic)
    echo "Preflight and infrastructure checks passed; continuing without an interactive prompt."
    echo "Re-run with --confirm to require typing DEPLOY."
    ;;
  *)
    fail "Unrecognized confirmation mode."
    ;;
esac

section "CDK DEPLOY"

apply_reviewed_change_set

section "POST-DEPLOY ECS VERIFICATION"

WEB_SERVICE="$(
  aws ecs list-services \
    --cluster "$CLUSTER_ARN" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'serviceArns[?contains(@, `websvc`)] | [0]' \
    --output text
)"

WORKER_SERVICE="$(
  aws ecs list-services \
    --cluster "$CLUSTER_ARN" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'serviceArns[?contains(@, `workersvc`)] | [0]' \
    --output text
)"

[[ "$WEB_SERVICE" != "None" && -n "$WEB_SERVICE" ]] || \
  fail "Web service not found."

[[ "$WORKER_SERVICE" != "None" && -n "$WORKER_SERVICE" ]] || \
  fail "Worker service not found."

echo "Waiting for ECS services to become stable..."

aws ecs wait services-stable \
  --cluster "$CLUSTER_ARN" \
  --services "$WEB_SERVICE" "$WORKER_SERVICE" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE"

aws ecs describe-services \
  --cluster "$CLUSTER_ARN" \
  --services "$WEB_SERVICE" "$WORKER_SERVICE" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --query 'services[].{service:serviceName,desired:desiredCount,running:runningCount,pending:pendingCount,rollout:deployments[0].rolloutState,taskDefinition:taskDefinition}' \
  --output table

WEB_TD="$(
  aws ecs describe-services \
    --cluster "$CLUSTER_ARN" \
    --services "$WEB_SERVICE" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'services[0].taskDefinition' \
    --output text
)"

WORKER_TD="$(
  aws ecs describe-services \
    --cluster "$CLUSTER_ARN" \
    --services "$WORKER_SERVICE" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'services[0].taskDefinition' \
    --output text
)"

WEB_IMAGE="$(
  aws ecs describe-task-definition \
    --task-definition "$WEB_TD" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'taskDefinition.containerDefinitions[0].image' \
    --output text
)"

WORKER_IMAGE="$(
  aws ecs describe-task-definition \
    --task-definition "$WORKER_TD" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'taskDefinition.containerDefinitions[0].image' \
    --output text
)"

echo
echo "web_image=$WEB_IMAGE"
echo "worker_image=$WORKER_IMAGE"

[[ "$WEB_IMAGE" == *":${IMAGE_TAG}" ]] || \
  fail "Web task definition does not use expected tag '$IMAGE_TAG'."

[[ "$WORKER_IMAGE" == *":${IMAGE_TAG}" ]] || \
  fail "Worker task definition does not use expected tag '$IMAGE_TAG'."

read -r WEB_DESIRED WEB_RUNNING WEB_PENDING WEB_ROLLOUT < <(
  aws ecs describe-services \
    --cluster "$CLUSTER_ARN" \
    --services "$WEB_SERVICE" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'services[0].[desiredCount,runningCount,pendingCount,deployments[0].rolloutState]' \
    --output text
)

read -r WORKER_DESIRED WORKER_RUNNING WORKER_PENDING WORKER_ROLLOUT < <(
  aws ecs describe-services \
    --cluster "$CLUSTER_ARN" \
    --services "$WORKER_SERVICE" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'services[0].[desiredCount,runningCount,pendingCount,deployments[0].rolloutState]' \
    --output text
)

[[ "$WEB_RUNNING" == "$WEB_DESIRED" \
   && "$WEB_PENDING" == "0" \
   && "$WEB_ROLLOUT" == "COMPLETED" ]] \
  || fail "Web service is not healthy after deployment."

[[ "$WORKER_RUNNING" == "$WORKER_DESIRED" \
   && "$WORKER_PENDING" == "0" \
   && "$WORKER_ROLLOUT" == "COMPLETED" ]] \
  || fail "Worker service is not healthy after deployment."

section "SUCCESS"

echo "Deployment verified successfully."
echo "HEAD=$HEAD_SHA"
echo "IMAGE_TAG=$IMAGE_TAG"
echo "WEB_IMAGE=$WEB_IMAGE"
echo "WORKER_IMAGE=$WORKER_IMAGE"
echo "Log: $LOG"
echo "CDK diff: $DIFF_FILE"
echo "App change set: $APP_CHANGE_SET_FILE"
echo "Tag vars: $TAG_FILE"
