# Roadmap

What exists today is in the [README implementation status](../README.md#implementation-status). This page covers what comes next, in priority order, with the dependency and the check that shows each step is done.

## Done in this submission

- `ConditionalCredit` state machine with endorsement rule, hold cap, audit trail; unit and property tests
- Drunix/Fabric chaincode (compiles; state-based endorsement, private data, events)
- Signature-checking dev ledger with hash-chained, replayable log
- Rules-first risk tiers with seven reason codes and exemptions
- Mock switch, core banking, 1930, bank ops, NPCI ruling, keeper and settlement reconciliation
- Dashboard with live attack demos; scripted demo; policy simulator with sensitivity sweep
- CI: Go vet/test, gofmt, ruff, pytest end to end against the real ledger

## Next

| # | Work | Depends on | Done when |
| --- | --- | --- | --- |
| 1 | **Drunix 3-org network** (PayerBank, PayeeBank, NPCIObserver, Raft) with `network/` scripts; deploy `chaincode/` with `collections_config.json` | Docker; Drunix test-network docs | `CreateHold → RequestRecall → ConfirmRecall` commits on peers; a single-org endorsement is rejected **by the network** |
| 2 | **Gateway client** replacing [`ledger.py`](../services/twokey/ledger.py): Fabric Gateway SDK, each org with its own identity, events to the services | 1 | The Python test suite passes against Drunix with only the client swapped |
| 3 | **Separate org processes**: one service per bank holding only its own key, with mTLS between them | 2 | No process can produce another org's endorsement |
| 4 | **Policy tuning from the simulator result**: the default policy missed the friction target (18% vs ≤ 10%). Evaluate HIGH-only holds, or MEDIUM holds only above a higher amount | none | A documented policy that meets both targets across the assumed sweep, with the weak-signal case reported |
| 5 | **Segment fairness report**: false-hold rate by account age band, region and first-time-digital flag in the simulator | none | Report generated in CI; disparities called out |
| 6 | **Mock switch timers**: NPCI OC-215 response timers and status-check limits | none | Status checks rate-limited per the circular; tests |
| 7 | **Model tiers**: LightGBM on simulator features with SHAP reason codes, rules kept as fallback | 5 | Model beats rules on the frontier at equal friction; SHAP codes mapped to the reason-code vocabulary |
| 8 | **Read model**: PostgreSQL fed by `CreditChanged` events for regulator queries | 2 | Dashboard and reconciliation read from it |
| 9 | Latency and throughput on a laptop network | 1 | Numbers published, labelled as non-production |

## Adoption path (beyond the hackathon)

1. **On-us pilot** inside one bank, where payer and payee are the same bank: no inter-bank change, the same state machine, and real fraud labels to calibrate the tiers.
2. **NPCI or RBI regulatory sandbox:** two banks on a Drunix partition with live but capped volumes.
3. **UPI scheme rule** alongside CFCFRMS. This needs an RBI direction on lagged credit, NPCI scheme rules and authorisation under the Payment and Settlement Systems Act 2007.

## Open questions for bank fraud teams and NPCI

- How fast do mules cash out in practice? The benefit shrinks if it is faster than any acceptable hold.
- Should a recall finalise automatically if the payee bank does not answer within a time limit? The prototype waits for both banks.
- Would NPCI rather hold credits centrally in the switch? See the trade-off in [ARCHITECTURE.md](ARCHITECTURE.md#10-trade-offs-and-scalability).
