# Evidence index

Each row is one run, made by `scripts/make_evidence.sh` (Windows: `scripts/make_evidence.ps1`). Every run folder holds `steps.jsonl` (one redacted JSON line per step), `result.json` (how the run ended), `console.txt` (what the terminal showed) and, only after a failure or a human handoff, `screenshots/` (and, for a handoff, `interventions/` with the intervention file and what the person did).

| Run | What it proves | Result |
|---|---|---|
| 01-discovery | An LLM learned the member-balance task once from a plain-English goal; the log holds only placeholders, never the real login. | not run yet |
| 02-replay | The saved artifact replays for a different member with no LLM call and returns typed outputs. | not run yet |
| 03-replay-faults | The same artifact survives an injected maintenance popup and slow pages on its own, with no human. | not run yet |
| 04-handoff | A step that cannot be done pauses the automation, a person gets the visible browser and an intervention file, `bag resume` hands control back, and the run finishes. | not run yet |

Secrets never appear in these files: logins show as `{{secret:NAME}}`, account numbers keep only their last four digits, and screenshots are blurred where a password, a typed secret or an account number is on screen.
