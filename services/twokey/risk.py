"""Rules-first, explainable risk tiering.

Every eligible P2P payment gets reason codes from simple features. The tier
follows directly from the codes, so every hold can be explained to the payer.
The same rule is used by the live service (scalars) and the policy simulator
(numpy arrays): it only uses operators that work on both.
"""

from dataclasses import dataclass, field

import numpy as np

LOW, MEDIUM, HIGH = 0, 1, 2
TIER_NAMES = ("LOW", "MEDIUM", "HIGH")

# Thresholds. Starting values from the design, not calibrated on bank data.
NEW_ACCT_DAYS = 30
HIGH_FAN_IN_PAYERS = 10  # distinct payers into the payee in 24h, this one included
HIGH_VELOCITY_PAISE = 2_00_000_00  # payee inflow in the last hour (Rs 2 lakh)
PASS_THROUGH_RATIO = 0.8  # payee outflow / inflow over 24h
PASS_THROUGH_MIN_INFLOW_PAISE = 50_000_00
AMOUNT_ANOMALY_MULTIPLE = 5  # vs the payer's median transfer
AMOUNT_ANOMALY_MIN_PAISE = 10_000_00
SPLIT_MIN_COUNT = 3  # sub-floor transfers payer -> payee within 24h
LONG_STANDING_DAYS = 90  # first payment to this payee at least this long ago
VALUE_FLOOR_PAISE = 10_000_00  # Rs 10,000; assumption, configurable via env

REASON_TEXT = {
    "NEW_ACCT": f"Payee account is less than {NEW_ACCT_DAYS} days old",
    "FIRST_PAYEE": "No earlier payment from this payer to this payee",
    "HIGH_FAN_IN": f"{HIGH_FAN_IN_PAYERS}+ different people paid this payee in the last 24h",
    "HIGH_VELOCITY": "Unusually large inflow into the payee in the last hour",
    "PASS_THROUGH": "Payee moves most incoming money straight out again",
    "AMOUNT_ANOMALY": "Amount is far above this payer's usual transfers",
    "SPLIT_PATTERN": "Several transfers just under the value floor to the same payee",
}
VELOCITY_CODES = ("HIGH_FAN_IN", "SPLIT_PATTERN", "HIGH_VELOCITY")


def tier_from_flags(new_acct, first_payee, fan_in, split, velocity, pass_through, anomaly,
                    exempt_kind, below_floor):
    """Tier from boolean flags. Works on Python bools or numpy bool arrays.

    exempt_kind: merchant, mandate or long-standing beneficiary (always exempt).
    below_floor: small amounts are exempt unless a velocity code fires, so
    smurfing just under the floor still gets scored.
    """
    new_acct, first_payee, fan_in, split, velocity, pass_through, anomaly, exempt_kind, below_floor = (
        np.asarray(x, dtype=bool) for x in
        (new_acct, first_payee, fan_in, split, velocity, pass_through, anomaly, exempt_kind, below_floor))
    n = new_acct * 1 + first_payee + fan_in + split + velocity + pass_through + anomaly
    high = (n >= 3) | (new_acct & fan_in)
    exempt = exempt_kind | (below_floor & ~(fan_in | split | velocity))
    return np.where(exempt, LOW, np.where(high, HIGH, np.where(n >= 1, MEDIUM, LOW)))


@dataclass
class Features:
    amount_paise: int
    payee_account_age_days: float
    first_payee: bool
    distinct_payers_24h: int
    payee_inflow_1h_paise: int
    payee_inflow_24h_paise: int
    payee_outflow_24h_paise: int
    payer_median_paise: float | None
    split_count_24h: int  # sub-floor transfers payer -> payee in 24h, this one included
    split_total_24h_paise: int
    exemption: str | None = None  # MERCHANT | MANDATE | LONG_STANDING_BENEFICIARY


@dataclass
class Decision:
    tier: str
    hold_seconds: int
    reason_codes: list[str]
    exemption: str | None
    explanations: list[str] = field(default_factory=list)


def reason_codes(f: Features, floor_paise: int = VALUE_FLOOR_PAISE) -> list[str]:
    inflow = f.payee_inflow_24h_paise
    checks = {
        "NEW_ACCT": f.payee_account_age_days < NEW_ACCT_DAYS,
        "FIRST_PAYEE": f.first_payee,
        "HIGH_FAN_IN": f.distinct_payers_24h >= HIGH_FAN_IN_PAYERS,
        "HIGH_VELOCITY": f.payee_inflow_1h_paise >= HIGH_VELOCITY_PAISE,
        "PASS_THROUGH": inflow >= PASS_THROUGH_MIN_INFLOW_PAISE
        and f.payee_outflow_24h_paise >= PASS_THROUGH_RATIO * inflow,
        "AMOUNT_ANOMALY": f.payer_median_paise is not None
        and f.amount_paise >= AMOUNT_ANOMALY_MIN_PAISE
        and f.amount_paise > AMOUNT_ANOMALY_MULTIPLE * f.payer_median_paise,
        "SPLIT_PATTERN": f.amount_paise < floor_paise
        and f.split_count_24h >= SPLIT_MIN_COUNT
        and f.split_total_24h_paise >= floor_paise,
    }
    return [code for code, hit in checks.items() if hit]


def decide(f: Features, hold_medium: int, hold_high: int,
           floor_paise: int = VALUE_FLOOR_PAISE) -> Decision:
    codes = reason_codes(f, floor_paise)
    has = {c: c in codes for c in REASON_TEXT}
    tier = int(tier_from_flags(
        has["NEW_ACCT"], has["FIRST_PAYEE"], has["HIGH_FAN_IN"], has["SPLIT_PATTERN"],
        has["HIGH_VELOCITY"], has["PASS_THROUGH"], has["AMOUNT_ANOMALY"],
        f.exemption is not None, f.amount_paise < floor_paise,
    ))
    exemption = f.exemption
    if tier == LOW and exemption is None and codes:
        exemption = "BELOW_VALUE_FLOOR"
    hold = (0, hold_medium, hold_high)[tier]
    return Decision(TIER_NAMES[tier], hold, codes if tier else [], exemption,
                    [REASON_TEXT[c] for c in codes] if tier else [])
