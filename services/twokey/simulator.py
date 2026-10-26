"""Policy simulator: status quo vs blanket delay vs TwoKey on synthetic P2P transfers.

A fraudulent transfer counts as recoverable if the victim reports while the
money is still in the first (mule) account:

    recovery ratio = fraud value still held at report time / total fraud value

Under the status quo money stays in the mule account until the mule cashes
out. Under a hold it cannot leave before the hold ends, and cash-out starts
only then. Every distribution below is an ASSUMPTION chosen for illustration.
They are not calibrated on bank or NPCI data, and the results depend on them,
in particular on how well mule accounts are separated from genuine ones.

Run:  python -m twokey.simulator --n 1000000 --out simulator/results.json
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np

from . import risk

# Feature rates: probability that a flag is set on a genuine vs fraud transfer.
GENUINE = dict(long_standing=0.55, new_acct=0.03, first_payee=0.12, fan_in=0.01, split=0.002,
               velocity=0.005, pass_through=0.01, anomaly=0.02)
FRAUD = dict(long_standing=0.0, new_acct=0.60, first_payee=0.90, fan_in=0.50, split=0.15,
             velocity=0.30, pass_through=0.60, anomaly=0.40)
GENUINE_AMOUNT = (1_500, 1.3)  # lognormal (median rupees, sigma)
FRAUD_AMOUNT = (25_000, 1.0)
REPORT_DELAY = (120, 1.2)  # victim reporting delay: median minutes, sigma
CASHOUT_SIGMA = 1.0
BLANKET_HOLD_MIN = 240  # blanket lag compared at the same 4h hold as TwoKey HIGH
HOLD_MIN = {risk.MEDIUM: 30, risk.HIGH: 240}
WEAK_SIGNAL = 0.5  # sensitivity case: fraud flag rates pulled halfway to genuine


def _flags(rng, n, rates):
    return {k: rng.random(n) < p for k, p in rates.items()}


def population(rng, n, prevalence, fraud_rates=FRAUD):
    fraud = rng.random(n) < prevalence
    g, f = _flags(rng, n, GENUINE), _flags(rng, n, fraud_rates)
    flags = {k: np.where(fraud, f[k], g[k]) for k in GENUINE}
    amount = np.where(fraud, rng.lognormal(np.log(FRAUD_AMOUNT[0]), FRAUD_AMOUNT[1], n),
                      rng.lognormal(np.log(GENUINE_AMOUNT[0]), GENUINE_AMOUNT[1], n))
    amount = np.minimum(amount, 1_00_000)  # UPI P2P limit
    return fraud, flags, amount


def twokey_tiers(flags, amount, floor_rupees):
    return risk.tier_from_flags(flags["new_acct"], flags["first_payee"], flags["fan_in"], flags["split"],
                                flags["velocity"], flags["pass_through"], flags["anomaly"],
                                flags["long_standing"], amount < floor_rupees)


def evaluate(fraud, amount, hold_min, report_min, cashout_min):
    """Recovery ratio and genuine transfers delayed for one hold vector (minutes)."""
    recovered = fraud & (report_min < hold_min + cashout_min)
    total = amount[fraud].sum()
    return {
        "recoveryRatio": float(amount[recovered].sum() / total) if total else 0.0,
        "recalledWithinHold": float(amount[fraud & (report_min < hold_min)].sum() / total) if total else 0.0,
        "genuineDelayed": int(((hold_min > 0) & ~fraud).sum()),
        "genuineDelayedPct": float(((hold_min > 0) & ~fraud).sum() / max((~fraud).sum(), 1) * 100),
    }


def scenario(rng, n, prevalence, cashout_median_min, floor_rupees=10_000, fraud_rates=FRAUD):
    fraud, flags, amount = population(rng, n, prevalence, fraud_rates)
    report = rng.lognormal(np.log(REPORT_DELAY[0]), REPORT_DELAY[1], n)
    cashout = rng.lognormal(np.log(cashout_median_min), CASHOUT_SIGMA, n)
    tiers = twokey_tiers(flags, amount, floor_rupees)
    zero = np.zeros(n)
    blanket_hold = np.where(amount >= floor_rupees, BLANKET_HOLD_MIN, 0)
    tk_hold = np.select([tiers == risk.HIGH, tiers == risk.MEDIUM], [HOLD_MIN[risk.HIGH], HOLD_MIN[risk.MEDIUM]], 0)
    out = {
        "statusQuo": evaluate(fraud, amount, zero, report, cashout),
        "blanket": evaluate(fraud, amount, blanket_hold, report, cashout),
        "twokey": evaluate(fraud, amount, tk_hold, report, cashout),
        "fraudCount": int(fraud.sum()),
        "fraudHeldShare": float((tiers[fraud] > risk.LOW).mean()) if fraud.any() else 0.0,
    }
    b, t = out["blanket"], out["twokey"]
    out["twokeyRecoveryVsBlanket"] = t["recoveryRatio"] / b["recoveryRatio"] if b["recoveryRatio"] else 0.0
    out["twokeyDelayVsBlanket"] = t["genuineDelayed"] / b["genuineDelayed"] if b["genuineDelayed"] else 0.0
    out["meetsRecoveryTarget"] = out["twokeyRecoveryVsBlanket"] >= 0.8
    out["meetsFrictionTarget"] = out["twokeyDelayVsBlanket"] <= 0.10

    # Frontier: other settings of each policy on the same population.
    frontier = []
    for fl in (2_000, 5_000, 10_000, 25_000, 50_000):
        e = evaluate(fraud, amount, np.where(amount >= fl, BLANKET_HOLD_MIN, 0), report, cashout)
        frontier.append({"policy": "blanket", "label": f"blanket >= Rs {fl:,}", **e})
    for label, med, high in (("TwoKey HIGH only", 0, 240), ("TwoKey 15m/2h", 15, 120),
                             ("TwoKey 30m/4h", 30, 240), ("TwoKey 4h/4h", 240, 240)):
        hold = np.select([tiers == risk.HIGH, tiers == risk.MEDIUM], [high, med], 0)
        frontier.append({"policy": "twokey", "label": label, **evaluate(fraud, amount, hold, report, cashout)})
    frontier.append({"policy": "statusQuo", "label": "status quo", **out["statusQuo"]})
    out["frontier"] = frontier
    return out


def run(n=1_000_000, prevalence=0.002, cashout_median_min=30, seed=42, sweep=True):
    started = time.time()
    rng = np.random.default_rng(seed)
    result = {
        "params": {"n": n, "prevalence": prevalence, "cashoutMedianMinutes": cashout_median_min, "seed": seed,
                   "reportDelayMedianMinutes": REPORT_DELAY[0], "blanketHoldMinutes": BLANKET_HOLD_MIN,
                   "twokeyHoldMinutes": {"MEDIUM": 30, "HIGH": 240}, "valueFloorRupees": 10_000,
                   "assumptions": {"genuineFlagRates": GENUINE, "fraudFlagRates": FRAUD,
                                   "genuineAmount": GENUINE_AMOUNT, "fraudAmount": FRAUD_AMOUNT}},
        "headline": scenario(rng, n, prevalence, cashout_median_min),
        "disclaimer": "Synthetic data with assumed distributions. Illustrates the trade-off; "
                      "not evidence of production performance.",
    }
    if sweep:
        rows = []
        weak = {k: FRAUD[k] + (GENUINE[k] - FRAUD[k]) * WEAK_SIGNAL for k in FRAUD}
        for signal, rates in (("assumed", FRAUD), ("weak", weak)):
            for prev in (0.0005, 0.002, 0.005):
                for cash in (5, 30, 120, 360):
                    s = scenario(rng, n // 4, prev, cash, fraud_rates=rates)
                    rows.append({"signal": signal, "prevalence": prev, "cashoutMedianMinutes": cash,
                                 "statusQuo": s["statusQuo"]["recoveryRatio"],
                                 "blanket": s["blanket"]["recoveryRatio"],
                                 "twokey": s["twokey"]["recoveryRatio"],
                                 "recoveryVsBlanket": s["twokeyRecoveryVsBlanket"],
                                 "delayVsBlanket": s["twokeyDelayVsBlanket"],
                                 "meetsBoth": bool(s["meetsRecoveryTarget"] and s["meetsFrictionTarget"])})
        result["sweep"] = rows
    result["seconds"] = round(time.time() - started, 2)
    return result


def plot(result, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"statusQuo": "#8a94a6", "blanket": "#c2410c", "twokey": "#0e7490"}
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    pts = result["headline"]["frontier"]
    for pol in ("blanket", "twokey", "statusQuo"):
        ps = sorted((p for p in pts if p["policy"] == pol), key=lambda p: p["genuineDelayedPct"])
        style = "o-" if pol == "blanket" else ("D" if pol == "twokey" else "o")  # TwoKey variants are not a curve
        ax.plot([p["genuineDelayedPct"] for p in ps], [p["recoveryRatio"] * 100 for p in ps], style,
                color=colors[pol], label={"statusQuo": "status quo", "twokey": "TwoKey variants"}.get(pol, "blanket lag"))
        for p in ps:
            ax.annotate(p["label"].replace("blanket ", "").replace("TwoKey ", ""),
                        (p["genuineDelayedPct"], p["recoveryRatio"] * 100), fontsize=7,
                        xytext=(6, -3), textcoords="offset points", color=colors[pol])
    ax.set_xscale("symlog", linthresh=0.1)
    ax.set_xticks([0, 0.1, 0.5, 1, 5, 10, 40], ["0", "0.1", "0.5", "1", "5", "10", "40"])
    ax.set_xlabel("Genuine P2P transfers delayed (%, log scale)")
    ax.set_ylabel("Fraud value recoverable (%)")
    pr = result["params"]
    ax.set_title(f"Friction vs recovery, synthetic ({pr['n']:,} transfers, "
                 f"cash-out median {pr['cashoutMedianMinutes']:g} min)", fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(frameon=False)
    fig.text(0.01, 0.01, "Simulation on assumed distributions; not a production result.", fontsize=7, color="#64748b")
    fig.tight_layout()
    fig.savefig(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=1_000_000)
    ap.add_argument("--prevalence", type=float, default=0.002)
    ap.add_argument("--cashout", type=float, default=30, help="median mule cash-out time, minutes")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--png", type=Path)
    a = ap.parse_args()
    res = run(a.n, a.prevalence, a.cashout, a.seed)
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    if a.png:
        plot(res, a.png)
    h = res["headline"]
    print(f"recovery  status quo {h['statusQuo']['recoveryRatio']:.1%}  blanket {h['blanket']['recoveryRatio']:.1%}"
          f"  twokey {h['twokey']['recoveryRatio']:.1%}")
    print(f"delayed   blanket {h['blanket']['genuineDelayed']:,}  twokey {h['twokey']['genuineDelayed']:,}")
    print(f"twokey/blanket: recovery {h['twokeyRecoveryVsBlanket']:.1%} (target >= 80%), "
          f"delayed {h['twokeyDelayVsBlanket']:.1%} (target <= 10%)")


if __name__ == "__main__":
    main()
