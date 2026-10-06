# Evidence index

The end-to-end flow on the fake bank: an LLM discovers a task once, the run becomes an approved artifact, and the artifact is replayed with no LLM, including runs that hit a not-found result, a bad input, injected faults and a step that needs a person.

Every run folder holds `steps.jsonl` (one redacted JSON line per event) and `result.json` (how the run ended). The `demo-replay-*` folders also hold `console.txt` (the exact command and what the terminal showed). Logins appear only as `{{secret:BANK_USER}}` / `{{secret:BANK_PASSWORD}}`.

## 1. Discovery (LLM: gemini-3.5-flash-lite)

| Run | Goal | Result |
|---|---|---|
| [20261006-032607-discovery-...](20261006-032607-discovery-log-in-and-find-the-savings-balance-for-/) + [recording](recordings/20261006-032607-log-in-and-find-the-savings-balance-for-.json) | "Log in and find the savings balance for the member", `member_id=1001` | `done` in 16.4 s, 7 steps, read `$12,450.75`, 2 targets repaired |
| [20261006-033610-discovery-...](20261006-033610-discovery-log-in-to-the-bank-search-for-the-member/) + [recording](recordings/20261006-033610-log-in-to-the-bank-search-for-the-member.json) | open a sub-account for member 1001 | `done` in 25.7 s, created `SA-1001-02`; the amount field and Confirm needed a human "yes" |

## 2. The saved artifacts

| Artifact | Built from |
|---|---|
| [artifacts/member-balance.v1.yaml](artifacts/member-balance.v1.yaml) | the first discovery above; 6 steps, input `member_id`, output `savings_balance` (decimal), approved |
| [artifacts/create-subaccount.v1.yaml](artifacts/create-subaccount.v1.yaml) | the second discovery above; 11 steps, output `sub_account_number`, approved |

(Copies of the files in `/artifacts/`, kept here so the evidence is self-contained.)

## 3. Replay (no LLM) of `member-balance.v1`, against a bank on port 5059

| Run | Command | Result | What it shows |
|---|---|---|---|
| [demo-replay-1-success](demo-replay-1-success/) | `--input member_id=1003` | `SUCCESS` in 0.4 s, `savings_balance = 250000.00`, exit 0 | a different member from discovery; output cast to a decimal |
| [demo-replay-2-not-found](demo-replay-2-not-found/) | `--input member_id=9999` | `BUSINESS_OUTCOME NOT_FOUND`, exit 2 | the bank's "No member found" page is recognised as a normal answer, not a crash |
| [demo-replay-3-bad-input](demo-replay-3-bad-input/) | `--input memberid=1003` (misspelt) | refused before opening the browser, exit 1 | inputs are checked against the artifact first: "Unknown input(s): memberid. This artifact takes: member_id." |
| [demo-replay-4-faults](demo-replay-4-faults/) | start at `/login?popup=1&slow=1` | `SUCCESS` in 6.7 s, exit 0 | an injected maintenance popup is dismissed by a known interruption, and 1 s page delays are absorbed |
| [demo-replay-5-handoff](demo-replay-5-handoff/) | `handoff-test` (the same steps plus a click on a button that does not exist), `--headed --takeover --timeout 3` | paused at step 6, resumed by `bag resume`, then `SUCCESS` in 5.4 s | `LocatorNotFound` → intervention file + blurred screenshot → `AUTOMATION → PAUSED_FOR_HUMAN → HUMAN → AUTOMATION` ([interventions/](demo-replay-5-handoff/interventions/)) |

The safety guard's decisions for these five runs are in [demo-replay-safety-decisions.jsonl](demo-replay-safety-decisions.jsonl).

## 4. Other real runs

| Run | Result |
|---|---|
| [20261006-033903-replay-create-subaccount-v1](20261006-033903-replay-create-subaccount-v1/) | replay of the sub-account artifact: `SUCCESS` in 7.7 s, created `SA-1001-03`; a person approved the amount and Confirm |
| [033732](20261006-033732-replay-member-balance-v1/), [033738](20261006-033738-replay-member-balance-v1/), [034114](20261006-034114-replay-member-balance-v1/), [034225](20261006-034225-replay-member-balance-v1/) | more `SUCCESS` replays of `member-balance.v1` (0.9 to 1.4 s) |
| [20261006-010244-discovery-...](20261006-010244-discovery-log-in-and-find-the-savings-balance-for-/) | an earlier discovery for member 1002: `done` in 107 s, read `$830.10` |
| [20261006-032143-replay-member-balance-new-v1](20261006-032143-replay-member-balance-new-v1/) | `FAILURE` at step 4, with screenshot: an artifact built while the popup was on kept the AI's "click OK" as a step, which duplicated the popup handling (see Cuts in REPORT.md) |
| [20261006-033302-discovery-...](20261006-033302-discovery-log-in-to-the-bank-search-for-the-member/) | discovery ended by the provider: `503 UNAVAILABLE` |
| [recordings/20261004-*](recordings/) | three early attempts ended by provider errors (503, 429) |
