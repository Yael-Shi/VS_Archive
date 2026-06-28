# VS-Archive documentation map

One-page index for developers and operators (ongoing stabilization, family-facing QA, and ops). The root [`README.md`](../README.md) still describes the original V1 upload slice; use the links below for current OCR/HTR, processing, and ops behavior.

## Start here

- [`docs/ai-context/vs-archive-context.md`](ai-context/vs-archive-context.md) — domain model, current OCR/HTR state, review lifecycle
- [`.cursor/rules/architecture.mdc`](../.cursor/rules/architecture.mdc) — layer boundaries and code-change contracts

## OCR/HTR

- [`docs/ocr-routing-reference.md`](ocr-routing-reference.md) — routing matrix, model selection, translation behavior (**current reference**)
- [`docs/ai-context/decision-log.md`](ai-context/decision-log.md#current-state--ocrhtr-and-transkribus-read-this-first) — durable decisions; see **“Current state — OCR/HTR and Transkribus”**
- [`docs/ai-context/tasks/engine-selection.md`](ai-context/tasks/engine-selection.md) — historical task background (routing is implemented)

## Processing semantics

- [`docs/ai-context/status-flow.md`](ai-context/status-flow.md) — status layers (`upload_status`, `processing_state_user`, `DocumentTextResult`, `TranskribusRun`)

## Quality & CI

- [`docs/backend-quality-baseline.md`](backend-quality-baseline.md) — backend lint/type/migration baseline
- [`scripts/local_check.sh`](../scripts/local_check.sh) — local/CI check runner (backend + infra; warnings non-blocking in CI)

## Deploy / ops

- [`docs/ai-context/deploy-aws-cdk.md`](ai-context/deploy-aws-cdk.md) — safe CDK deploy runbook and post-deploy checks

## Planned work

- [`docs/gemini-page-retry-plan.md`](gemini-page-retry-plan.md) — bounded Gemini per-page retry (planning only; not implemented)

## Manual QA

- [`docs/V1_CHECKLIST.md`](V1_CHECKLIST.md) — upload/list/backlog smoke checklist (legacy V1 scope)
- [`docs/ai-context/unified-ocr-upload-flow.md`](ai-context/unified-ocr-upload-flow.md) — unified archive create/upload flow (current UI behavior)
