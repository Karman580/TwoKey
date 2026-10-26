# Policy simulator output

[`results.json`](results.json) is the output of

```bash
make simulate   # python -m twokey.simulator --n 1000000 --out simulator/results.json --png docs/images/frontier.png
```

Code: [`services/twokey/simulator.py`](../services/twokey/simulator.py). It is seeded (42) and reproducible, and takes about 1 s.

**Model.** Each synthetic transfer is genuine or fraud (prevalence p). Each of seven risk flags is set with a per-class probability, and the tier is computed with the **same rule** as the live service (`risk.tier_from_flags`). Fraud value counts as recoverable if the victim reports before `hold + mule cash-out time`; under the status quo the hold is 0. Reporting delay and cash-out time are lognormal.

**Every number in the model is an assumption** (flag rates, amount distributions, reporting delay, cash-out time), stated at the top of `simulator.py` and copied into `results.json → params.assumptions`. None is calibrated on bank or NPCI data. The `weak` rows of the sweep move fraud flag rates halfway toward genuine ones to show how much the result depends on signal quality.

Results are illustrative only and are not evidence of production performance.
