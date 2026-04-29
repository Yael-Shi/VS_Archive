# VS-Archive Cursor Rules

This is a Django backend project for a historical family document archive.

Core principles:
- Inspect relevant files before editing.
- Do not assume services, fields, functions, environment variables, or integrations exist unless you find them in the code or documentation.
- Do not invent model fields, APIs, settings, or external services.
- Do not make broad rewrites.
- Prefer small, reviewable diffs.
- Preserve existing behavior unless explicitly asked otherwise.
- Keep domain logic isolated in services where practical.
- Keep views/workers thin when practical.

Database rules:
- Do not change Django models unless explicitly asked.
- Do not create migrations unless explicitly asked.
- If a model change seems necessary, explain why first.

API/UI rules:
- Do not change public API behavior, URLs, or response shapes unless explicitly asked.
- Do not add UI/admin changes unless explicitly asked.

Infrastructure/security rules:
- Do not touch secrets, credentials, deployment, Docker, AWS, or environment configuration unless explicitly asked.
- Do not assume where files are stored. Inspect the storage implementation/settings.

External integrations:
- Do not implement fake or placeholder external integrations.
- If an integration does not exist, say so clearly.
- If adding a real integration is requested, first identify required credentials, API client, data flow, error handling, and tests.

Workflow:
- Before editing, summarize current behavior and list files you plan to change.
- After editing, explain changed files, behavior changes, risks, and exact test commands.