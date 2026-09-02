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

## OCR feature flags (env-gated routing)

Some OCR routes are activated by environment flags read in `select_ocr_route` (not by CDK defaults alone):

| Flag | Routing-code default if unset | Current worker CDK | Route when enabled |
|------|-------------------------------|--------------------|-------------------|
| `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN` | `false` (SSM; see below) | see SSM | `he` + `HANDWRITTEN` → Transkribus |
| `ENABLE_ANTIGRAVITY_ARABIC_PRINTED` | `false` | `true` | `ar` + `PRINTED` → Antigravity |
| `ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED` | `false` | `false` | execution only; does not change routing |

**Antigravity Arabic printed rollout**

- **`ENABLE_ANTIGRAVITY_ARABIC_PRINTED=false`** is the routing-code default when the variable is absent. Current `app_stack.py` worker environment sets **`ENABLE_ANTIGRAVITY_ARABIC_PRINTED=true`** (live-preserving). This flag is route activation only; it is not an Interactions request-contract field.
- **`ANTIGRAVITY_AGENT_ID`** defaults to `antigravity-preview-05-2026` when unset.
- The requested Antigravity model pin is the code constant **`ANTIGRAVITY_REQUESTED_MODEL`** (`gemini-3.7-flash`) in `antigravity_defaults.py`. There is no ECS/environment-variable override for that model.
- **Phase 1 — deploy with flag off:** historical safe-rollout step. The earlier statement that `app_stack.py` does not define Antigravity env vars is **no longer current**.
- **Phase 2 — controlled test:** worker routing uses **`ENABLE_ANTIGRAVITY_ARABIC_PRINTED=true`** (and optionally `ANTIGRAVITY_AGENT_ID` if overriding the default). Uses existing `GEMINI_API_KEY`. A follow-up CDK/SSM wiring change (mirroring Transkribus) is optional but recommended before broader enablement.
- **`ar` + `HANDWRITTEN`** is not routed to Antigravity regardless of the flag.
- The adapter also validates `worker_env.enable_antigravity_arabic_printed` as a second safety check.
- **`ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED=false`** is the execution-code default and the current `app_stack.py` **worker** environment value. It is **not** set on the web task. Turning it on later requires a separate worker-only change plus a Cloud Vision secret; this phase does **not** add `GOOGLE_CLOUD_VISION_API_KEY` to CDK, ECS, web, or Secrets Manager.

Local template: `app/backend/.env.template`. Routing reference: `docs/ocr-routing-reference.md`.

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
