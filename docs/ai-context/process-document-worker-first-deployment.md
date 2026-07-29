# PROCESS_DOCUMENT worker-first deployment

Durable `PROCESS_DOCUMENT` callers must not become live before a worker that
understands request-aware payloads. CloudFormation may update independent ECS
services concurrently, so a shared image tag alone cannot guarantee a safe
mixed-version rollout.

## Image-tag contexts

The AppStack resolves image contexts with this precedence:

- web: `web_image_tag` → `image_tag` → `dev`;
- worker: `worker_image_tag` → `image_tag` → `dev`.

Existing commands that provide only `image_tag` remain backward compatible.
Production deployment must always provide explicit dated tags; never rely on
the `dev` fallback.

## Migration bootstrap

If the new image contains unapplied migrations required by the worker, stop the
worker through the stack before creating the migration task:

```bash
poetry run cdk deploy vs-archive-dev-app-v2 \
  --profile default \
  -c image_tag=LIVE_TAG \
  -c web_image_tag=LIVE_TAG \
  -c worker_image_tag=NEW_TAG \
  -c worker_desired_count=0
```

The `worker_desired_count` context accepts only `0` or `1` and defaults to `1`.
With the service at zero, run a one-off ECS task from the new worker task
definition with `python manage.py migrate --noinput`. Require exit code zero
and verify the expected migrations in `django_migrations` before starting the
worker service.

Do not register a task definition manually. The migration task must reuse the
CDK-created worker task definition so its roles, networking, environment, and
secrets remain identical to the service.

## Phase 1: worker first

Keep the web service on the verified live tag and move only the worker:

```bash
poetry run cdk diff vs-archive-dev-app-v2 \
  --profile default \
  -c image_tag=LIVE_TAG \
  -c web_image_tag=LIVE_TAG \
  -c worker_image_tag=NEW_TAG \
  -c worker_desired_count=1

poetry run cdk deploy vs-archive-dev-app-v2 \
  --profile default \
  -c image_tag=LIVE_TAG \
  -c web_image_tag=LIVE_TAG \
  -c worker_image_tag=NEW_TAG \
  -c worker_desired_count=1
```

Before phase 2, require a completed worker rollout, the expected new worker
task definition/image, the old web image, healthy queue/DLQ state, and safe
worker logs.

## Phase 2: web cutover

After worker validation, move the web service to the same new tag:

```bash
poetry run cdk diff vs-archive-dev-app-v2 \
  --profile default \
  -c image_tag=NEW_TAG \
  -c web_image_tag=NEW_TAG \
  -c worker_image_tag=NEW_TAG \
  -c worker_desired_count=1

poetry run cdk deploy vs-archive-dev-app-v2 \
  --profile default \
  -c image_tag=NEW_TAG \
  -c web_image_tag=NEW_TAG \
  -c worker_image_tag=NEW_TAG \
  -c worker_desired_count=1
```

Require `UPDATE_COMPLETE`, completed ECS rollouts, healthy ALB targets, site
health/static checks, applied migrations, and controlled Request lifecycle
checks.

Never register replacement task definitions manually. Stop on unexpected
changes to CPU, memory, commands, environment, secrets, services, health
settings, queues, database, or networking.
