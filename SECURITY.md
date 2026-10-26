# Security policy

TwoKey is a hackathon prototype. It runs on simulated payment rails and must not be used with real money, real customer data or real bank keys.

## Reporting a vulnerability

Please report security issues privately through GitHub's private vulnerability reporting (Security tab → "Report a vulnerability") if it is enabled, or by contacting the repository owner (@Karman580) directly. Do not open a public issue. Include the steps to reproduce and the commit hash.

## Scope

In scope: the `ConditionalCredit` state machine and chaincode, the dev ledger's signature and endorsement checks, and the services' input validation.

Known limitations that are documented and accepted for the local demo, such as the unauthenticated API and all organisation keys held in one process, are listed in [`docs/SECURITY.md`](docs/SECURITY.md#residual-risks).
