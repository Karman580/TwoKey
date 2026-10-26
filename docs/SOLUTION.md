# TwoKey — The Proposed Solution

## 1. The problem, simply

When you pay someone on UPI, the money arrives in seconds and cannot be pulled back. That is great for everyday payments — and it is exactly what scammers exploit.

In **authorised push-payment (APP) fraud**, nobody hacks the victim. A fake police officer, a "digital arrest" call, a fake job or investment offer persuades the victim to send the money themselves. The victim's own PIN approves it, so every security check passes. The money lands in a **mule account**, usually opened recently, and is moved on through several accounts within minutes.

By the time the victim calls the 1930 helpline, most of the money is gone.

## 2. Why existing approaches fall short

| Approach | What it does | Gap |
| --- | --- | --- |
| CFCFRMS / 1930 freezes | Freezes what is left downstream after a complaint | Acts after the money has moved; often freezes innocent account holders, who then need a separate grievance process |
| Bank and RBIH risk scoring (e.g. MuleHunter.AI, the DPIP prototype) | Scores whether an account or payee looks risky | Produces a score, not an action on settlement |
| A blanket delay on all credits (RBI's "lagged credit" option, simplest form) | Delays every transfer above a threshold | Slows millions of genuine payments and weakens UPI's instant promise |
| UK payment delays (up to four business days on reasonable suspicion) | Lets the paying provider delay an outbound payment | Payer-side and discretionary; the receiving bank has no shared control |
| Singapore cooling-off (12 hours after a security-token activation) | Blocks high-risk activities after an account event | Triggered by account events, not by the risk of a specific transfer |

Scale of the problem: the National Cybercrime Reporting Portal recorded about **₹22,931 crore** of cyber-financial fraud in 2025. CFCFRMS had saved about **₹11,158 crore** across 32.8 lakh complaints by mid-2026, against roughly ₹55,050 crore reported lost in 2021–2025 — around one rupee in five, on periods that do not match exactly. RBI's discussion paper of 9 April 2026 states that most digital-payment fraud is APP fraud and asks for views on lagged credit.

## 3. The TwoKey solution, step by step

1. **Check the risk at payment time.** Every eligible P2P payment is scored on payee account age, first-time payee, number of distinct payers in 24 hours, inflow velocity, pass-through ratio, amount anomaly and split patterns.
2. **Settle the safe ones instantly.** LOW-risk payments — expected to be the large majority — go through exactly as today. Merchant payments, mandates, recurring debits, small amounts and long-standing beneficiaries are always exempt.
3. **Hold only the risky ones.** MEDIUM and HIGH payments become a `ConditionalCredit` on Drunix, held for 30 minutes or 4 hours (starting values, with a hard cap), with reason codes such as `NEW_ACCT` and `HIGH_FAN_IN` shown to the payer.
4. **Let the victim pull it back.** If the victim reports to 1930 during the hold, the payer's bank requests a recall and the payee's bank confirms it. The credit becomes RECALLED and the money returns.
5. **Two keys for every move.** Every release or recall needs both banks' endorsement. Neither can act alone; disputes need NPCI co-signature.
6. **Release on time.** If no report arrives, the hold expires and the payee receives the money.

## 4. A realistic user journey

> **Meena, 64, Patiala.** She receives a call from someone claiming to be from a police cyber cell. He says her Aadhaar is linked to a money-laundering case and she must move ₹40,000 to a "safe verification account" immediately.
>
> Meena sends the money on UPI. The receiving account was opened three days ago and has received money from 14 different people today. TwoKey tiers the payment **HIGH**: it is held for 4 hours, and Meena's app shows that the payment is on hold, why, and when it will release.
>
> That evening her son hears what happened and calls 1930. The complaint reaches Meena's bank, which requests a recall. The receiving bank confirms. The payment moves to **RECALLED** and ₹40,000 returns to Meena's account. The fraudster never had access to it.
>
> The same afternoon, Meena also paid ₹40,000 to her nephew, a regular contact with a two-year-old account. That payment was tiered **LOW** and arrived instantly.

## 5. What makes the approach different

- **It acts before the loss.** Recovery is a state change on a held credit, not a chase through layered mule accounts.
- **It is precise.** Only risky transfers are delayed, so UPI stays instant for almost everyone.
- **Control is shared and enforced.** The two banks with opposing incentives must both sign; a shared, tamper-evident record replaces one party's database.
- **It produces evidence for policy.** The simulator measures the trade-off RBI is consulting on: how much fraud a policy recovers versus how many genuine payments it delays.
- **It adopts incrementally.** It can start as an on-us pilot inside one bank, move to an NPCI or RBI sandbox, and then become a scheme rule.

## 6. How it addresses the pain point

| Pain point | TwoKey response |
| --- | --- |
| Money leaves the mule account within minutes | High-risk credits are held long enough for a report to arrive |
| Recovery depends on freezing layered accounts | Recall happens at the first hop, before layering |
| Innocent account holders get frozen downstream | Fewer funds reach downstream accounts, so fewer freezes are needed (to be measured in simulation) |
| A blanket delay would hurt every user | Holds apply only to MEDIUM and HIGH tiers |
| No shared, trusted record between two banks | Both banks endorse every change on Drunix; NPCI-observer can audit |

## 7. Implementation status

| Component | Status |
| --- | --- |
| `ConditionalCredit` state machine, endorsement rule, 4 h hold cap, idempotency, audit trail | ✅ Built and property-tested ([`chaincode/credit`](../chaincode/credit/credit.go)) |
| Drunix/Fabric chaincode with state-based endorsement and private data | ✅ Written and compiles · ⚠️ not yet deployed on a Drunix network |
| Signature-checking dev ledger (stand-in for Drunix in the local demo) | ✅ Built |
| Rules-first risk tiers with reason codes and exemptions | ✅ Built |
| Mock UPI switch, core banking, 1930 helpline, bank ops, NPCI ruling, keeper | ✅ Built (simulated rails) |
| Dashboard with live credits, attack demos, audit trail, simulator | ✅ Built |
| Policy simulator and frontier chart | ✅ Built; results below |
| Fabric Gateway bridge, Drunix 3-org network, OC-215 timers | 🔜 Planned |
| Model tiers (LightGBM + SHAP), segment fairness report | 🔜 Planned |

All NPCI, bank and helpline components are simulated. No real money or customer data is used.

### The user journey, as implemented

Meena's story above runs in the dashboard and in `make demo` with the same accounts: the nephew payment is LOW (long-standing beneficiary), and the "safe verification account" payment is HIGH with `NEW_ACCT`, `FIRST_PAYEE`, `HIGH_FAN_IN` and `PASS_THROUGH`. One difference from the story: the demo mule account has received money from 13 earlier payers, so Meena is the 14th.

### Differences from the deck, and why

| Deck / proposal | Implementation | Reason |
| --- | --- | --- |
| Smurfing raises the tier to HIGH | `SPLIT_PATTERN` alone gives MEDIUM; HIGH needs 3+ codes or `NEW_ACCT` + `HIGH_FAN_IN` | Follows the deck's own tier rule; a lone split pattern is weaker evidence |
| "Release early" by the payee bank, and "every release needs both banks" | Early release needs an explicit approval from **each** bank | Satisfies both statements; neither bank releases alone |
| Fabric Gateway (TypeScript) to Drunix peers | Python ledger client to a local signature-checking ledger | No Docker available in the build environment; same state-machine code |
| Results "≥ 80% recovery, ≤ 10% delays" | These were hypotheses; measured results below (friction target missed by the default policy) | Report the result honestly |

## 8. Limitations

- Live operation needs an RBI direction on lagged credit, NPCI scheme rules and payment-system authorisation.
- How fast mules cash out is not public. If they cash out faster than any acceptable hold, the benefit shrinks; this is a simulator parameter and a question for bank fraud teams.
- "New account" signals can unfairly affect newly banked and rural users; false-hold rates must be reported by segment and holds capped.
- Evaluation is on synthetic data; production performance is not claimed.

## 9. Measurable validation criteria

| Criterion | Target (hypothesis) |
| --- | --- |
| Recoverable fraud value vs a blanket delay | ≥ 80% of what a blanket delay recovers |
| Genuine transfers delayed vs a blanket delay | ≤ 10% of the blanket delay's count |
| Unilateral release or recall by one bank | Always rejected by the endorsement policy |
| Recall–expiry race and replayed requests | Deterministic in 100% of property tests |
| End-to-end demo | Transfer → hold → 1930 report → recall → refund, live |

Method: three policies (status quo, blanket delay, TwoKey) on one million synthetic P2P transfers, sweeping fraud prevalence (0.05–0.5%), mule cash-out time (median 5 minutes to 6 hours) and victim reporting delay. A transfer's value counts as recoverable if the victim reports before its hold ends.

```
Recovery ratio = (fraud value still held at report time) / (total fraud value)
```

### Results (synthetic, `make simulate`, seed 42)

1,000,000 transfers, 0.2% fraud, mule cash-out median 30 min; blanket lag = 4 h hold on every transfer ≥ ₹10,000.

| Criterion | Target | Result |
| --- | --- | --- |
| Recovery vs blanket | ≥ 80% | **91.0%** ✅ (65.8% vs 72.3%; status quo 18.2%) |
| Genuine transfers delayed vs blanket | ≤ 10% | **18.0%** ❌ (12,953 vs 72,090) |
| Same, HIGH-tier holds only | | 88.6% recovery ✅, 0.3% delays ✅ |
| Unilateral release or recall | Always rejected | ✅ Go tests, Python tests, live attack buttons |
| Recall–expiry race, replays | 100% deterministic | ✅ 5,000 random sequences, run twice, identical |
| End-to-end demo | Live | ✅ `make demo`, dashboard |

**Negative and conditional results:** the default 30 min / 4 h policy misses the friction target, because MEDIUM holds on first-time payees catch more genuine payments than allowed. A HIGH-only policy meets both targets under the assumed distributions. When the mule signal is weakened, TwoKey recovers only 29–90% of the blanket lag's recovery across the sweep. The approach therefore depends on how separable real mule accounts are, which only bank data can show. The simulator and the jointly endorsed held-credit chaincode remain useful contributions to RBI's consultation either way.

## Sources

- [RBI publication on digital-payment fraud controls (discussion paper context)](https://www.rbi.org.in/Scripts/PublicationsView.aspx?id=23810)
- [IMPRI — CFCFRMS overview and recovery figures](https://www.impriindia.com/insights/policy-update/citizen-financial-cyber-fraud-reporting-and-management-system-cfcfrms-strengthening-indias-response-to-digital-financial-fraud/)
- [Cyber fraud statistics India 2026 (NCRP figures)](https://cyberdefence.org.in/blog/cyber-fraud-statistics-india-2026/)
- [NPCI Drunix repository](https://github.com/npci/drunix)
- [Business Standard — NPCI circulars after UPI outages](https://www.business-standard.com/finance/news/npci-springs-to-action-with-two-new-circulars-to-contain-upi-outages-125042901208_1.html)
