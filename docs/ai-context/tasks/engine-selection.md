# Task: OCR/HTR Engine Selection

Goal:
Route OCR/HTR processing based on:
- source language
- handwritten vs printed

Constraints:
- First inspect whether these metadata fields already exist.
- Do not change models or create migrations unless explicitly approved.
- If only one OCR/HTR engine exists today, keep behavior compatible and isolate routing logic for future engines.
- Do not implement new external integrations in this task.
- Add selector logic in a dedicated service/function.
- Keep worker/view code simple.