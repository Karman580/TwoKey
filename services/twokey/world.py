"""Simulated bank systems: accounts, balances and payment history.

Everything here is synthetic demo data standing in for two banks' core
banking systems. Cleartext VPAs live only here, never on the shared ledger.
"""

import random
import statistics
from dataclasses import dataclass, field

from . import risk
from .ledger import PAYEE_BANK, PAYER_BANK

DAY = 86_400
BANK_BY_HANDLE = {"payerbank": PAYER_BANK, "payeebank": PAYEE_BANK}


@dataclass
class Account:
    vpa: str
    name: str
    opened_at: float
    kind: str = "individual"  # individual | merchant
    balance_paise: int = 0
    held_paise: int = 0  # incoming money under a TwoKey hold; not spendable

    @property
    def bank(self) -> str:
        return BANK_BY_HANDLE[self.vpa.split("@")[1]]


@dataclass
class Transfer:
    txn_ref: str
    payer: str
    payee: str
    amount_paise: int
    at: float
    purpose: str = "P2P"
    tier: str = "LOW"
    reason_codes: list[str] = field(default_factory=list)
    exemption: str | None = None
    status: str = "SUCCESS"  # SUCCESS (instant) | HELD | RELEASED | RECALLED | DISPUTED
    settled: bool = True
    hold_seconds: int = 0
    report_ref: str | None = None


class World:
    def __init__(self, now: float, floor_paise: int = risk.VALUE_FLOOR_PAISE):
        self.floor = floor_paise
        self.accounts: dict[str, Account] = {}
        self.transfers: list[Transfer] = []
        self.by_ref: dict[str, Transfer] = {}
        self._seq = 0
        self._seed(now)

    # ---- demo data -------------------------------------------------------
    def _seed(self, now: float) -> None:
        rnd = random.Random(1930)
        add = self.add_account
        add("meena@payerbank", "Meena (64, Patiala)", now - 9 * 365 * DAY, balance=2_00_000_00)
        add("arjun@payerbank", "Arjun (student)", now - 3 * 365 * DAY, balance=60_000_00)
        add("ravi.nephew@payeebank", "Ravi, Meena's nephew", now - 2 * 365 * DAY)
        add("landlord.sharma@payeebank", "Mr Sharma, landlord", now - 4 * 365 * DAY)
        add("safe.verify@payeebank", '"Safe verification account"', now - 3 * DAY)
        add("job.offer.desk@payeebank", '"Part-time job desk"', now - 20 * DAY)
        add("layer.one@payeebank", "Onward account", now - 5 * DAY)
        add("kirana.store@payeebank", "Gupta Kirana Store", now - 6 * 365 * DAY, kind="merchant")

        # Meena has paid her nephew Rs 15,000 a month for two years.
        for m in range(24, 0, -1):
            self._history("meena@payerbank", "ravi.nephew@payeebank", 15_000_00, now - m * 30 * DAY)
        for d in (40, 25, 9, 2):
            self._history("arjun@payerbank", "kirana.store@payeebank", rnd.randint(200, 900) * 100,
                          now - d * DAY, purpose="MERCHANT")

        # The mule: 13 different people paid it today, and it forwarded the money on.
        total = 0
        for i in range(1, 14):
            vpa = f"p{i:02d}@payerbank"
            add(vpa, f"Background payer {i}", now - rnd.randint(1, 8) * 365 * DAY, balance=1_00_000_00)
            amount = rnd.randint(8, 45) * 1_000_00
            total += amount
            self._history(vpa, "safe.verify@payeebank", amount, now - rnd.randint(1, 20) * 3600)
        self._history("safe.verify@payeebank", "layer.one@payeebank", int(total * 0.9), now - 1800)

    def add_account(self, vpa, name, opened_at, kind="individual", balance=0):
        self.accounts[vpa] = Account(vpa, name, opened_at, kind, balance)

    def _history(self, payer, payee, amount, at, purpose="P2P"):
        self.transfers.append(Transfer(self.next_ref(), payer, payee, amount, at, purpose))

    def next_ref(self) -> str:
        self._seq += 1
        return f"RRN{random.randrange(10**8, 10**9)}{self._seq:04d}"

    # ---- features ----------------------------------------------------------
    def features(self, payer: str, payee: str, amount: int, purpose: str, now: float) -> risk.Features:
        acct = self.accounts[payee]
        day_ago, hour_ago = now - DAY, now - 3600
        into_payee = [t for t in self.transfers if t.payee == payee and t.status != "RECALLED"]
        pair = [t for t in self.transfers if t.payer == payer and t.payee == payee]
        mine = [t.amount_paise for t in self.transfers if t.payer == payer][-30:]
        recent_pair_small = [t for t in pair if t.at >= day_ago and t.amount_paise < self.floor]
        split_count, split_total = 0, 0
        if amount < self.floor:
            split_count = len(recent_pair_small) + 1
            split_total = sum(t.amount_paise for t in recent_pair_small) + amount

        exemption = None
        if acct.kind == "merchant" or purpose == "MERCHANT":
            exemption = "MERCHANT"
        elif purpose == "MANDATE":
            exemption = "MANDATE"
        elif pair and min(t.at for t in pair) <= now - risk.LONG_STANDING_DAYS * DAY:
            exemption = "LONG_STANDING_BENEFICIARY"

        return risk.Features(
            amount_paise=amount,
            payee_account_age_days=(now - acct.opened_at) / DAY,
            first_payee=not pair,
            distinct_payers_24h=len({t.payer for t in into_payee if t.at >= day_ago} | {payer}),
            payee_inflow_1h_paise=sum(t.amount_paise for t in into_payee if t.at >= hour_ago) + amount,
            payee_inflow_24h_paise=sum(t.amount_paise for t in into_payee if t.at >= day_ago),
            payee_outflow_24h_paise=sum(t.amount_paise for t in self.transfers
                                        if t.payer == payee and t.at >= day_ago),
            payer_median_paise=statistics.median(mine) if len(mine) >= 5 else None,
            split_count_24h=split_count,
            split_total_24h_paise=split_total,
            exemption=exemption,
        )

    # ---- core banking --------------------------------------------------------
    def record(self, t: Transfer) -> None:
        self.transfers.append(t)
        self.by_ref[t.txn_ref] = t

    def settle(self, txn_ref: str, state: str) -> bool:
        """Apply a final ledger state to balances exactly once."""
        t = self.by_ref.get(txn_ref)
        if t is None:
            return False
        if state == "DISPUTED":
            t.status = state
            return False
        if t.settled or state not in ("RELEASED", "RECALLED"):
            return False
        payee, payer = self.accounts[t.payee], self.accounts[t.payer]
        payee.held_paise -= t.amount_paise
        if state == "RELEASED":
            payee.balance_paise += t.amount_paise
        else:
            payer.balance_paise += t.amount_paise
        t.status, t.settled = state, True
        return True
