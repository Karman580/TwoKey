# Security and privacy

TwoKey is a hackathon prototype on simulated rails. This page describes the controls that exist, the threats they address and what is still open. Passing tests does not make the system production-secure.

## Threat model

| Actor | Wants to | Control | Where | Tested |
| --- | --- | --- | --- | --- |
| Colluding payee bank | Release a mule's held money early | Every transaction needs signatures from both banks; early release needs a separate approval from each | [`credit.go`](../chaincode/credit/credit.go) `CheckEndorsement`, `ApproveRelease` | ✅ |
| Payee bank | Forge the payer bank's endorsement | Ed25519 verification against each org's registered public key | [`devledger`](../chaincode/cmd/devledger/main.go) `Submit` | ✅ |
| Payer bank | Claw back a genuine payment alone | Recall needs the payee bank's `ConfirmRecall`; a dispute needs an NPCI ruling | `credit.Apply` | ✅ |
| Anyone with network access to the ledger | Replay an old recall or release | Transaction ids are single-use | `Ledger.seen` | ✅ |
| Victim / payer bank | Recall after the hold ended | Strict time check against the ledger clock | `RequestRecall` | ✅ |
| Switch / misconfiguration | Hold longer than allowed | 4 h hard cap enforced in chaincode, not config | `MaxHoldSeconds` | ✅ |
| Fraudster | Smurf under the value floor | `SPLIT_PATTERN`, `HIGH_FAN_IN` and velocity override the small-amount exemption | [`risk.py`](../services/twokey/risk.py) | ✅ |
| Operator with disk access | Rewrite ledger history | SHA-256 hash chain recomputed on read; on restart every logged transaction is re-verified | `verifyChain`, `replay` | ✅ |
| Other ledger members | Learn who paid whom | Only salted HMAC-SHA256 of VPAs on the shared ledger | [`ledger.py`](../services/twokey/ledger.py) `hash_vpa` | ✅ |

## Controls in the code

- **Input validation:** pydantic models validate every request body (VPA regex, amount `1..₹1 lakh`, txnRef pattern `^RRN\d{9,16}$`, bounded strings, enumerated outcomes and bank names). The ledger caps request bodies at 64 KiB and validates hold requests again.
- **Fail closed:** if the ledger is unreachable a risky payment is declined before balances change (503).
- **Error handling:** errors return stable codes and short messages; no stack traces or internal paths.
- **XSS:** the dashboard escapes every server-supplied string before inserting HTML (`esc()` in [`app.js`](../frontend/app.js)) and loads no third-party scripts.
- **Secrets:** generated locally by [`gen_keys.py`](../scripts/gen_keys.py) into `.env` (git-ignored, mode 600). The ledger receives only public keys. CI uses throwaway keys generated inside the test run, and no secrets are configured.
- **Network exposure:** both processes bind to `127.0.0.1` by default.
- **Dev-only clock:** the fast-forward endpoint exists only when `DEVLEDGER_ALLOW_CLOCK=1`.
- **Dependencies:** Python versions are pinned in [`pyproject.toml`](../services/pyproject.toml); Go modules are pinned by `go.sum`.

## Residual risks

These are known and accepted for a local demo. Each must be fixed before any shared deployment.

1. **One process holds all three organisations' private keys,** so one operator can drive the demo. Real deployments: each bank signs with its own key in its own HSM, and NPCI holds its own.
2. **The services API has no authentication or authorisation.** Anyone who can reach port 8000 can act as any bank. Needed: mTLS or OAuth2 per institution, with role checks matching the org.
3. **No rate limiting** on the API or ledger.
4. **No CSRF protection.** This matters only if the API is exposed beyond localhost; it has no cookies or sessions today.
5. **The dev ledger is not Byzantine fault tolerant:** a single process, no ordering service, no peer replication. Its log proves tampering only to someone who still holds the public keys and checks the chain.
6. **VPA hashes are linkable** across credits by design (needed for fan-in analysis) and are guessable by anyone with the salt. Rotate the salt per network.
7. **Fairness:** `NEW_ACCT` can disproportionately hold newly banked and rural users. False-hold rates by segment are not measured yet ([roadmap](ROADMAP.md)).

## Privacy (DPDP Act 2023)

The design follows data minimisation: the shared ledger carries no names, VPAs or KYC data. Compliance with the DPDP Act has **not** been assessed by counsel; the deck's "fully compliant" wording is a design goal, not a verified status.

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) in the repository root.
