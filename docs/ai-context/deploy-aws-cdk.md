# VS-Archive AWS CDK Safe Deploy Runbook

This runbook hardens deploys for `vs-archive-dev-app-v2` so CDK deploys do not unintentionally drift worker runtime behavior.

## Scope and source of truth

- Source of truth is:
  - CDK code under `infra/`
  - live AWS state (ECS, SSM, Secrets Manager, IAM)
  - `cdk synth` / `cdk diff` output
  - repository docs/rules
- `worker_task.json` and `web_task.json` are temporary deploy/debug exports only. They are not source of truth.

## Critical deploy rule

Always deploy with an explicit image tag:

```bash
poetry run cdk deploy vs-archive-dev-app-v2 -c image_tag=<CURRENT_OR_INTENDED_ECR_TAG>
```

Never rely on the CDK default `image_tag=dev`.

## Find the current live worker image tag

```bash
# 1) Worker service -> active task definition
aws ecs describe-services \
  --region eu-central-1 \
  --cluster <APP_V2_CLUSTER_ARN> \
  --services <WORKER_SERVICE_ARN> \
  --query "services[0].taskDefinition" \
  --output text

# 2) Task definition -> worker image
aws ecs describe-task-definition \
  --region eu-central-1 \
  --task-definition <WORKER_TASK_DEF_ARN> \
  --query "taskDefinition.containerDefinitions[?name=='vs-archive-dev-worker'].image" \
  --output text
```

Use the tag from that image in `-c image_tag=...`.

## Required Transkribus runtime config

### SSM Parameter Store (non-secret)

- `/vs-archive/dev/transkribus/enable-hebrew-handwritten`
- `/vs-archive/dev/transkribus/dev-upload-mode`
- `/vs-archive/dev/transkribus/use-existing-server-document`
- `/vs-archive/dev/transkribus/collection-id`
- `/vs-archive/dev/transkribus/model-id`

### Secrets Manager (secret)

- `vs-archive/dev/transkribus/username`
- `vs-archive/dev/transkribus/password`
- `vs-archive/dev/transkribus/api-token`

## Preflight checks before deploy

1. Confirm active worker/web task definitions and image tag from ECS.
2. Confirm required SSM parameters exist (and expected boolean values).
3. Confirm required Secrets Manager secrets exist.
4. Confirm ECS execution role can read the required SSM parameters and secrets.
5. Run synth/diff with explicit image tag:

```bash
cd infra
poetry run cdk synth -c image_tag=<CURRENT_OR_INTENDED_ECR_TAG>
poetry run cdk diff vs-archive-dev-app-v2 -c image_tag=<CURRENT_OR_INTENDED_ECR_TAG>
```

## Diff review checklist

- Worker image tag is intended (not accidental `dev` or stale tag).
- Worker Transkribus `valueFrom` references point to the expected SSM/Secrets paths.
- Worker Gemini tuning env vars are preserved (no silent fallback to defaults for intentional runtime tuning).
- Web rollout is collateral and understood.
- `GOOGLE_APPLICATION_CREDENTIALS_JSON` is legacy and should not be restored unless explicitly reintroduced by a future decision.

## Deploy

```bash
cd infra
poetry run cdk deploy vs-archive-dev-app-v2 -c image_tag=<CURRENT_OR_INTENDED_ECR_TAG>
```

## Post-deploy checks

1. ECS services become stable and desired tasks are running.
2. Worker starts without env/secret initialization failures.
3. Hebrew handwritten upload routes to Transkribus and returns text.
4. No Gemini fallback for Hebrew handwritten route.
5. Non-Transkribus (Gemini) route still works, or at minimum there is no startup/config regression.

## Rollback notes

- Roll back to previous ECS task definition revision and previous known-good ECR image tag.
- Do not mutate SSM/Secrets during rollback unless those values were identified as the root cause.
