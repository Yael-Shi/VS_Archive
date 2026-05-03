# VS-Archive Global Cursor Rules

VS-Archive is a Django backend project for a historical family document archive.

These are global project guardrails.
For specialized guidance, see the dedicated rule files:

- `architecture.mdc` → system architecture and boundaries
- `python-backend.mdc` → Python/Django backend coding conventions
- `testing.mdc` → testing expectations and test-writing rules
- `documentation.mdc` → documentation / decision-log maintenance rules

## Core principles

- Inspect relevant files before editing.
- Do not assume services, fields, functions, environment variables, integrations, or behavior exist unless verified in code or documentation.
- Do not invent model fields, APIs, settings, workflows, or external services.
- Preserve existing behavior unless explicitly asked to change it.
- If unsure whether behavior is intentional, surface the ambiguity before editing.

## Scope discipline

- Do not make broad rewrites.
- Prefer small, reviewable diffs.
- Do not broaden scope without explicit approval.
- Do not perform opportunistic cleanup unrelated to the requested task.

## Restricted changes

- Do not change public API behavior, URLs, or response shapes unless explicitly asked.
- Do not add UI/admin changes unless explicitly asked.
- Do not touch secrets, credentials, deployment, Docker, AWS, infra, or environment configuration unless explicitly asked.

## External integrations

- Do not implement fake or placeholder external integrations.
- If an integration does not exist, say so clearly.
- If adding a real integration is requested, first identify:
  - required credentials / env vars
  - API/client/library requirements
  - data flow / integration points
  - error handling expectations
  - test requirements

## Workflow expectations

Before editing:

- Summarize current behavior.
- List files you plan to change for non-trivial work.

After editing:

- Explain changed files.
- Explain behavior changes.
- Explain risks / deferred work.
- Report exact test commands run (or explicitly state tests were not run).
- Mention documentation updates or why docs were not updated when relevant.