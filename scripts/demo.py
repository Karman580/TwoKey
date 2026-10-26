"""Scripted end-to-end demo against a running `make dev` (http://127.0.0.1:8000).

Meena's story from the proposal: two identical Rs 40,000 payments behave
differently, a 1930 report recalls the risky one, and one bank cannot move a
held credit alone. Everything here is simulated.
"""

import os
import sys

import httpx

api = httpx.Client(base_url=os.environ.get("TWOKEY_URL", "http://127.0.0.1:8000"), timeout=10)


def call(method, path, **kw):
    r = api.request(method, path, **kw)
    body = r.json()
    if r.status_code >= 400:
        sys.exit(f"{method} {path} -> {r.status_code} {body}")
    return body


def rupees(p):
    return f"Rs {p / 100:,.0f}"


def balance(vpa):
    a = next(a for a in call("GET", "/api/accounts") if a["vpa"] == vpa)
    return f"{a['vpa']}: available {rupees(a['balancePaise'])}, held {rupees(a['heldPaise'])}"


call("POST", "/api/demo/reset")
print("1. Meena pays her nephew Rs 40,000 (two-year-old account, paid monthly)")
r = call("POST", "/api/upi/pay", json={"payerVpa": "meena@payerbank", "payeeVpa": "ravi.nephew@payeebank",
                                       "amountPaise": 40_000_00})
print(f"   tier {r['tier']} -> {r['status']} ({r['exemption']})")

print("2. Meena pays the 'safe verification account' Rs 40,000 (3 days old, 13 payers today)")
r = call("POST", "/api/upi/pay", json={"payerVpa": "meena@payerbank", "payeeVpa": "safe.verify@payeebank",
                                       "amountPaise": 40_000_00})
ref = r["txnRef"]
print(f"   tier {r['tier']} -> {r['status']} for {r['holdSeconds'] // 3600}h, reasons {r['reasonCodes']}")
print("  ", balance("safe.verify@payeebank"))

print("3. The payee bank tries to move the held credit on its own")
for attack in ("unilateral-release", "forged-endorsement"):
    a = call("POST", f"/api/attack/{attack}", json={"txnRef": ref})
    print(f"   {attack}: blocked={a['blocked']} {a.get('code')} -> credit still {a['state']}")

print("4. Meena's son calls 1930; the payer bank requests a recall")
r = call("POST", "/api/helpline/report", json={"txnRef": ref, "description": "fake police call"})
print(f"   complaint {r['complaint']}: {r['outcome']}")
a = call("POST", "/api/attack/replay-recall", json={"txnRef": ref})
print(f"   replaying that recall: blocked={a['blocked']} {a['code']}")

print("5. The payee bank confirms the recall")
c = call("POST", "/api/bank/PayeeBank/confirm-recall", json={"txnRef": ref})
print(f"   credit {c['state']}; endorsed by {c['events'][-1]['endorsers']}")
print("  ", balance("meena@payerbank"))

chain = call("GET", "/api/chain")
print(f"6. Ledger: {chain['height']} committed transactions, hash chain valid={chain['valid']}")
