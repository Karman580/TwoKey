# API reference

Base URL `http://127.0.0.1:8000`. JSON in and out. Interactive docs at `/docs` (OpenAPI generated from [`app.py`](../services/twokey/app.py)). Amounts are in **paise** (₹1 = 100 paise).

Errors have the shape `{"detail": {"code": "...", "error": "..."}}`. Request validation errors (422) use FastAPI's list format.

| Code | Status | Meaning |
| --- | --- | --- |
| `ENDORSEMENT_POLICY_FAILURE` | 403 | A required organisation did not sign |
| `INVALID_SIGNATURE` | 401 | A signature does not verify, or the creator did not sign |
| `REPLAY` | 409 | Transaction id already committed |
| `REJECTED` | 422 | State-machine rule (wrong state, wrong role, too late, too early) |
| `NOT_FOUND` / `UNKNOWN_VPA` | 404 | Unknown credit or account |
| `LEDGER_UNAVAILABLE` | 503 | Ledger not reachable; nothing was changed |

## Payments (mock UPI switch, risk tiers)

### `POST /api/upi/pay`

```json
{"payerVpa": "meena@payerbank", "payeeVpa": "safe.verify@payeebank", "amountPaise": 4000000, "purpose": "P2P"}
```

`purpose` ∈ `P2P | MERCHANT | MANDATE`. `amountPaise` must be in `1..10000000` (₹1 lakh P2P limit). The VPA must match `name@handle`.

```json
{"txnRef": "RRN3886825520046", "status": "HELD", "tier": "HIGH", "holdSeconds": 14400,
 "reasonCodes": ["NEW_ACCT", "FIRST_PAYEE", "HIGH_FAN_IN", "PASS_THROUGH"],
 "explanations": ["Payee account is less than 30 days old", "..."],
 "exemption": null, "holdUntil": 1793030810, "credit": { "...shared ledger record..." }}
```

LOW payments return `status: "SUCCESS"`, `credit: null`, and an `exemption` such as `LONG_STANDING_BENEFICIARY`, `MERCHANT`, `MANDATE` or `BELOW_VALUE_FLOOR` when one applied.

### `POST /api/risk/score`

Same body; returns the decision **without** paying, plus the computed `features`. The dashboard uses it for the live preview.

### `GET /api/upi/status/{txnRef}`

`{"txnRef", "status", "tier", "reasonCodes"}`; status ∈ `SUCCESS | HELD | RELEASED | RECALLED | DISPUTED`.

### Reason codes

| Code | Fires when |
| --- | --- |
| `NEW_ACCT` | Payee account < 30 days old |
| `FIRST_PAYEE` | No earlier payment from this payer to this payee |
| `HIGH_FAN_IN` | ≥ 10 distinct payers into the payee in 24 h (including this one) |
| `HIGH_VELOCITY` | ≥ ₹2 lakh into the payee in the last hour |
| `PASS_THROUGH` | Payee outflow ≥ 80% of ≥ ₹50,000 inflow in 24 h |
| `AMOUNT_ANOMALY` | ≥ ₹10,000 and > 5× the payer's median transfer |
| `SPLIT_PATTERN` | Below the floor, 3rd+ such transfer to this payee in 24 h, total ≥ floor |

Tier: no codes → LOW; 1–2 → MEDIUM; 3+ or `NEW_ACCT`+`HIGH_FAN_IN` → HIGH. Exempt (LOW): merchant, mandate, beneficiary first paid ≥ 90 days ago, and amounts below the floor unless a velocity code (`HIGH_FAN_IN`, `SPLIT_PATTERN`, `HIGH_VELOCITY`) fires.

## Recall and release

All take `{"txnRef": "RRN..."}`. Each returns the updated ledger credit or a ledger error.

| Endpoint | Acts as | Ledger function |
| --- | --- | --- |
| `POST /api/helpline/report` (+ optional `description`) | Mock 1930 → payer bank | `RequestRecall`. Returns `{complaint, outcome}`, outcome ∈ `RECALL_REQUESTED`, `NOT_HELD`, `TOO_LATE_OR_INVALID` |
| `POST /api/bank/PayeeBank/confirm-recall` | Payee bank | `ConfirmRecall` |
| `POST /api/bank/{PayerBank\|PayeeBank}/approve-release` | That bank | `ApproveRelease` |
| `POST /api/bank/{PayerBank\|PayeeBank}/dispute` (+ `reason`) | That bank | `RaiseDispute` |
| `POST /api/npci/resolve` (+ `outcome`: `RELEASE\|RECALL`) | NPCI observer | `ResolveDispute` |

## Ledger views

- `GET /api/credits` → `{"now": <ledger unix time>, "credits": [ConditionalCredit + bankView]}`. `bankView` holds the cleartext VPAs from the simulated bank systems and is **not** on the ledger.
- `GET /api/chain` → `{"blocks": [...], "valid": true, "height": n}`.

`ConditionalCredit` fields: `txnRef, amountPaise, payerBank, payeeBank, hashedPayer, hashedPayee, tier, reasonCodes, createdAt, holdUntil, state, releaseApprovals, dispute, events[]`. Each event: `seq, txId, fn, creator, endorsers, at, from, to, note`.

## Attack demos

`POST /api/attack/{name}` with `{"txnRef"}`. Each sends a deliberately invalid transaction to the ledger and reports `{"attack", "blocked", "code", "error", "state"}`.

| name | What it sends | Expected |
| --- | --- | --- |
| `unilateral-release` | `Expire`, signed by the payee bank only | `ENDORSEMENT_POLICY_FAILURE` |
| `forged-endorsement` | `ApproveRelease` with the payer bank's slot signed by the payee bank's key | `INVALID_SIGNATURE` |
| `payer-confirms-own-recall` | `ConfirmRecall` submitted by the payer bank | `REJECTED` |
| `replay-recall` | The exact `RequestRecall` proposal already committed | `REPLAY` (409 `NO_RECALL_YET` if no recall was made yet) |

## Simulator

- `GET /api/simulation` returns the committed 1M-transfer result (`simulator/results.json`).
- `POST /api/simulation` with `{"n": 10000..300000, "prevalence": 0.0005..0.005, "cashoutMedianMinutes": 5..360, "seed"}` re-runs the headline scenario (no sweep).

## Demo controls and health

- `GET /api/health` → `{ok, ledger, holdSeconds, valueFloorPaise, devClock}`
- `GET /api/accounts` → demo accounts with balances and held funds
- `GET /api/transfers` → last 50 payments made in this session
- `POST /api/dev/advance {"seconds": 1..86400}` moves the ledger clock forward and runs the keeper (403 unless `DEVLEDGER_ALLOW_CLOCK=1`)
- `POST /api/demo/reset` resets the simulated bank state (the ledger log is kept)

## Dev ledger (internal, port 8081)

`POST /tx` (signed proposal), `GET /credits`, `GET /credits/{ref}`, `GET /chain`, `GET /time`, `GET /health`, `POST /dev/advance` (only with `DEVLEDGER_ALLOW_CLOCK=1`). Proposal format and signing bytes: [`devledger/main.go`](../chaincode/cmd/devledger/main.go).

## Drunix chaincode functions

[`chaincode/main.go`](../chaincode/main.go): `CreateHold(reqJSON)` (optional transient `private`), `RequestRecall(txnRef, reportRef)`, `ConfirmRecall(txnRef)`, `ApproveRelease(txnRef)`, `Expire(txnRef)`, `RaiseDispute(txnRef, reason)`, `ResolveDispute(txnRef, outcome)`, `GetCredit(txnRef)`. Each state change emits a `CreditChanged` event.
