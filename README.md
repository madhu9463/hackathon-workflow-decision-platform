# Configurable Workflow Decision Platform

A resilient, configurable workflow decision system: structured requests go in, rules are
evaluated, workflow stages run, state is persisted, and every decision is auditable.
Built with **Python 3 standard library only** + SQLite. No `pip install` needed.

All business logic lives in JSON configs (`configs/`). The engine contains no business rules.
Two use cases ship as configuration only: **loan approval** and **vendor approval**.

## Requirements
* Python 3.8+ (check with `python --version`; on macOS/Linux you may need `python3`)
* VS Code with the *Python* extension (optional but recommended)

## Run it in VS Code
1. **File > Open Folder...** and choose this project folder.
2. Open a terminal: **Terminal > New Terminal** (Ctrl+`).
3. Run the tests (should print `Ran 26 tests ... OK`):
   ```bash
   python -m unittest discover -s tests -v
   ```
4. Try the CLI (state is kept in `wfdp.db`, created automatically):
   ```bash
   python -m wfdp submit loan_approval samples/cust000.json
   python -m wfdp explain <id-from-previous-output>
   python -m wfdp submit loan_approval samples/cust004.json                      # rejected
   python -m wfdp submit loan_approval samples/cust009.json --simulate-failures 3  # PENDING_RETRY
   python -m wfdp retry <id>                                                      # then APPROVED
   python -m wfdp submit vendor_approval samples/vendor_review.json               # MANUAL_REVIEW
   python -m wfdp review <id> --decision APPROVED --reviewer priya --note "docs verified"
   ```
   Other commands: `show`, `history`, `audit`, `workflows`, `register <file.json>`.
   Use `--db other.db` (or env var `WFDP_DB`) to pick a different database file.
5. Start the REST API:
   ```bash
   python -m wfdp serve --port 8080
   ```
   You can also use the **Run and Debug** panel: the repo includes launch configs for the API,
   the tests and a sample submission.

### Calling the REST API
macOS/Linux/Git Bash:
```bash
curl -i -X POST http://127.0.0.1:8080/requests \
  -H "Content-Type: application/json" -H "Idempotency-Key: order-1001" \
  -d '{"workflow":"loan_approval","payload":{"applicant_id":"cust000","age":34,"income":90000,"loan_amount":150000,"employment":"salaried"}}'
```
Windows PowerShell:
```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/requests `
  -Headers @{"Idempotency-Key"="order-1001"} -ContentType "application/json" `
  -Body '{"workflow":"loan_approval","payload":{"applicant_id":"cust000","age":34,"income":90000,"loan_amount":150000,"employment":"salaried"}}'
```

| Method and path | Purpose | Status codes |
|---|---|---|
| `POST /requests` | Submit a request | 201 created, 200 idempotent replay, 422 invalid input, 409 key conflict, 404 unknown workflow |
| `GET /requests/{id}` | Current state | 200, 404 |
| `GET /requests/{id}/history` | Lifecycle transitions | 200, 404 |
| `GET /requests/{id}/audit` | Rule evaluations, dependency calls, decisions | 200, 404 |
| `GET /requests/{id}/explanation` | Human-readable decision trace (text/plain) | 200, 404 |
| `POST /requests/{id}/retry` | Resume a PENDING_RETRY / interrupted request | 200, 404, 409 |
| `POST /requests/{id}/review` | Resolve MANUAL_REVIEW (`decision`, `reviewer`, `note`) | 200, 400, 404, 409 |
| `GET/POST /workflows` | List / register a workflow version | 200, 201, 400 |
| `POST /admin/dependencies/{name}/fail` | Simulation: fail next N calls (`{"count": 3}`) | 200 |

## Project layout
```
wfdp/
  conditions.py    declarative condition language + evaluator (rule trace, fail-closed)
  schema.py        config-driven intake validation
  config.py        workflow validation + versioned, immutable registry
  engine.py        intake, stage loop, retries, manual review, explanation
  store.py         SQLite persistence, atomic commits, optimistic locking
  dependencies.py  dependency registry + simulated credit bureau / sanctions services
  api.py           REST API (http.server)
  __main__.py      CLI
  errors.py        exceptions
configs/           loan_approval.json, vendor_approval.json  (ALL business rules)
samples/           example payloads
tests/             26 automated tests (unittest)
```

## Design notes
* **Idempotency:** the key is the `Idempotency-Key` header, or a hash of workflow + payload.
  A UNIQUE index makes concurrent duplicates race safely: exactly one request is created.
* **Atomicity:** each stage commits request state, history row and audit rows in one SQLite
  transaction, guarded by an optimistic version check.
* **Append-only logs:** SQLite triggers block UPDATE/DELETE on `history`, `audit` and `workflows`.
* **Retries:** bounded retries with exponential backoff -> `PENDING_RETRY` -> `retry` command ->
  escalation to `MANUAL_REVIEW` after the total attempt cap. Dependency calls carry an idempotency key.
* **Versioning:** each request is pinned to the workflow version it started under.
* **Fail closed:** missing or incomparable data can never make a rule pass.
* **Simulated services:** `credit_bureau` and `sanctions` are simulated. The sample applicants
  `cust000/004/009/020/021` have fixed credit scores (679 / 407 / 680 / 580 / 720) so demos are
  reproducible; other applicant ids get a deterministic hash-derived score (300-850).

## Add a new use case (no code)
Create a JSON file in `configs/` (see `loan_approval.json`) with a `schema`, a `start` stage and
`stages` of type `rules`, `dependency`, `branch` or `terminal`, then restart or
`python -m wfdp register configs/my_flow.json`. Invalid configs (dangling stages, cycles, unknown
operators or services, duplicate rule ids) are rejected, and the previous version keeps serving.

## Upload to GitHub
```bash
git init
git add .
git commit -m "Configurable Workflow Decision Platform"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```
(Create the empty repository on github.com first. The `.gitignore` keeps `wfdp.db` out of the repo.)
