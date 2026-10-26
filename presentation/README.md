# Presentation

| File | What it is |
| --- | --- |
| [`TwoKey_Submission.pdf`](TwoKey_Submission.pdf) | The submission deck, 14 slides (exported from Canva as PDF) |
| [`TwoKey_Project_Description.pdf`](TwoKey_Project_Description.pdf) | The 4-page written proposal the implementation follows |

**Format note:** the deck was authored in Canva and exists only as this PDF export. No `.pptx` source was available, so none is included rather than a regenerated copy that would not match the original.

## Slide outline

1. Title: *Targeted Holds for UPI Fraud on Drunix*
2. The velocity of APP fraud defeats post-transaction recovery
3. The regulatory dilemma: status quo vs blanket lag vs TwoKey
4. Solution architecture: risk check, shared control, pre-loss recall
5. Lifecycle of a `ConditionalCredit`
6. Risk-tier engine and reason codes
7. System architecture: Python services and Drunix state
8. On-chain data model: shared channel vs private data
9. Why blockchain: shared control
10. Resilience and adversarial defence grid
11. Policy simulator: quantifying the trade-off
12. Global prior art (UK, Singapore) vs TwoKey
13. Regulatory fit and adoption path
14. Six-week build plan and transparency callout

## Consistency with the implementation

| Slide | Claim | In this repository |
| --- | --- | --- |
| 3, 11 | TwoKey achieves "≥ 80% of blanket lag's recovery, with ≤ 10% of its delayed genuine transfers" | These were **hypotheses**. Measured on synthetic data: 91.0% ✅ and 18.0% ❌ for the default policy; HIGH-only holds meet both. See [SOLUTION.md](../docs/SOLUTION.md#9-measurable-validation-criteria) |
| 5, 9, 10 | State machine, two-bank control, attack defences | ✅ Implemented and tested |
| 6 | Tiers and reason codes; LightGBM + SHAP as stretch | ✅ Rules implemented · LightGBM not built |
| 7 | Fabric Gateway (TypeScript) and Drunix peers | ⚠️ Chaincode written and compiles; the local demo uses a signature-checking dev ledger instead of a Drunix network |
| 7 | Mock switch "enforces OC-215 timers" | ⚠️ Not implemented |
| 8 | Private data collection for VPAs and KYC refs | ⚠️ In the chaincode and `collections_config.json`; not exercised without a network |
| 8 | "Fully compliant with DPDP Act 2023" | ⚠️ A design goal; not legally assessed |
| 10 | Smurfing raises the tier "to HIGH" | ⚠️ `SPLIT_PATTERN` alone raises it to MEDIUM; HIGH when combined with other codes |
| 14 | "All Drunix network and chaincode components are built live and run locally" | ⚠️ Network not yet run; see [roadmap](../docs/ROADMAP.md) |
