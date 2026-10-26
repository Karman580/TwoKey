import numpy as np
import pytest
from fastapi.testclient import TestClient

from twokey import risk, simulator
from twokey.app import Settings, create_app
from twokey.ledger import Ledger, parse_keys

MEENA, NEPHEW, MULE, LANDLORD = ("meena@payerbank", "ravi.nephew@payeebank", "safe.verify@payeebank",
                                 "landlord.sharma@payeebank")


@pytest.fixture
def client(devledger):
    s = Settings({"LEDGER_URL": devledger["url"], "VPA_HASH_SALT": "test-salt-0123456789",
                  "TWOKEY_SECRET_KEYS": devledger["secret"], "DEVLEDGER_ALLOW_CLOCK": "1"})
    app = create_app(s, run_keeper=False)
    with TestClient(app) as c:
        yield c


def pay(c, payee, amount=40_000_00, payer=MEENA, **kw):
    r = c.post("/api/upi/pay", json={"payerVpa": payer, "payeeVpa": payee, "amountPaise": amount, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def account(c, vpa):
    return next(a for a in c.get("/api/accounts").json() if a["vpa"] == vpa)


def test_meena_story(client):
    nephew = pay(client, NEPHEW)
    assert nephew["tier"] == "LOW" and nephew["status"] == "SUCCESS"
    assert nephew["exemption"] == "LONG_STANDING_BENEFICIARY"

    mule = pay(client, MULE)
    assert mule["tier"] == "HIGH" and mule["status"] == "HELD" and mule["holdSeconds"] == 4 * 3600
    assert {"NEW_ACCT", "HIGH_FAN_IN"} <= set(mule["reasonCodes"])
    assert account(client, MULE)["heldPaise"] == 40_000_00
    assert account(client, MULE)["balancePaise"] == 0  # the fraudster cannot touch it

    shared = mule["credit"]
    assert MEENA not in str(shared) and MULE not in str(shared)  # only salted hashes on the ledger

    before = account(client, MEENA)["balancePaise"]
    r = client.post("/api/helpline/report", json={"txnRef": mule["txnRef"]}).json()
    assert r["outcome"] == "RECALL_REQUESTED"
    c = client.post("/api/bank/PayeeBank/confirm-recall", json={"txnRef": mule["txnRef"]}).json()
    assert c["state"] == "RECALLED" and c["events"][-1]["endorsers"] == ["PayeeBank", "PayerBank"]
    assert account(client, MEENA)["balancePaise"] == before + 40_000_00
    assert account(client, MULE)["heldPaise"] == 0

    assert client.get("/api/chain").json()["valid"]


def test_first_payee_is_medium_and_merchant_exempt(client):
    assert pay(client, LANDLORD)["tier"] == "MEDIUM"
    assert pay(client, "kirana.store@payeebank", 500_00, payer="arjun@payerbank", purpose="MERCHANT")["tier"] == "LOW"


def test_smurfing_is_caught_on_third_split(client):
    tiers = [pay(client, "job.offer.desk@payeebank", 9_500_00, payer="arjun@payerbank")["tier"] for _ in range(4)]
    assert tiers == ["LOW", "LOW", "MEDIUM", "MEDIUM"]


def test_attacks_are_blocked(client):
    ref = pay(client, MULE)["txnRef"]
    expect = {"unilateral-release": "ENDORSEMENT_POLICY_FAILURE", "forged-endorsement": "INVALID_SIGNATURE",
              "payer-confirms-own-recall": "REJECTED"}
    for name, code in expect.items():
        r = client.post(f"/api/attack/{name}", json={"txnRef": ref}).json()
        assert r["blocked"] and r["code"] == code and r["state"] == "PENDING", r
    assert client.post("/api/attack/replay-recall", json={"txnRef": ref}).status_code == 409
    client.post("/api/helpline/report", json={"txnRef": ref})
    r = client.post("/api/attack/replay-recall", json={"txnRef": ref}).json()
    assert r["blocked"] and r["code"] == "REPLAY" and r["state"] == "RECALL_REQUESTED"


def test_wrong_bank_cannot_confirm_recall(client):
    ref = pay(client, MULE)["txnRef"]
    client.post("/api/helpline/report", json={"txnRef": ref})
    r = client.post("/api/bank/PayerBank/confirm-recall", json={"txnRef": ref})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REJECTED"


def test_early_release_needs_both_banks(client):
    ref = pay(client, LANDLORD)["txnRef"]
    assert client.post("/api/bank/PayeeBank/approve-release", json={"txnRef": ref}).json()["state"] == "PENDING"
    assert client.post("/api/bank/PayerBank/approve-release", json={"txnRef": ref}).json()["state"] == "RELEASED"
    assert account(client, LANDLORD)["balancePaise"] == 40_000_00


def test_expiry_releases_and_late_report_falls_back(client):
    ref = pay(client, LANDLORD)["txnRef"]
    r = client.post("/api/dev/advance", json={"seconds": 31 * 60}).json()
    assert ref in r["settled"]
    assert client.get(f"/api/upi/status/{ref}").json()["status"] == "RELEASED"
    late = client.post("/api/helpline/report", json={"txnRef": ref}).json()
    assert late["outcome"] == "TOO_LATE_OR_INVALID"


def test_dispute_needs_npci(client):
    ref = pay(client, MULE)["txnRef"]
    client.post("/api/helpline/report", json={"txnRef": ref})
    d = client.post("/api/bank/PayeeBank/dispute", json={"txnRef": ref, "reason": "customer says genuine"}).json()
    assert d["state"] == "DISPUTED"
    assert client.post("/api/bank/PayerBank/approve-release", json={"txnRef": ref}).status_code == 422
    r = client.post("/api/npci/resolve", json={"txnRef": ref, "outcome": "RECALL"}).json()
    assert r["state"] == "RECALLED" and "NPCIObserver" in r["events"][-1]["endorsers"]


def test_input_validation(client):
    bad = [{"payerVpa": "not a vpa", "payeeVpa": MULE, "amountPaise": 100},
           {"payerVpa": MEENA, "payeeVpa": MULE, "amountPaise": 0},
           {"payerVpa": MEENA, "payeeVpa": MULE, "amountPaise": 2_00_000_00}]
    for body in bad:
        assert client.post("/api/upi/pay", json=body).status_code == 422
    assert client.post("/api/helpline/report", json={"txnRef": "x'; drop"}).status_code == 422


def test_ledger_down_fails_closed(devledger):
    s = Settings({"LEDGER_URL": "http://127.0.0.1:9", "VPA_HASH_SALT": "test-salt-0123456789",
                  "TWOKEY_SECRET_KEYS": devledger["secret"]})
    with TestClient(create_app(s, run_keeper=False)) as c:
        before = account(c, MEENA)["balancePaise"]
        r = c.post("/api/upi/pay", json={"payerVpa": MEENA, "payeeVpa": MULE, "amountPaise": 40_000_00})
        assert r.status_code == 503 and account(c, MEENA)["balancePaise"] == before


def test_settings_refuse_weak_config():
    with pytest.raises(RuntimeError):
        Settings({"VPA_HASH_SALT": "short"})
    with pytest.raises(RuntimeError):
        Settings({"VPA_HASH_SALT": "x" * 20, "HOLD_HIGH_SECONDS": "99999"})
    with pytest.raises(ValueError):
        parse_keys("PayerBank=AAAA")


def test_vectorised_tier_matches_scalar_rule():
    rng = np.random.default_rng(0)
    flags = rng.random((500, 9)) < 0.3
    vec = risk.tier_from_flags(*flags.T)
    for row, t in zip(flags, vec, strict=True):
        assert int(risk.tier_from_flags(*row)) == t


def test_simulator_small_run():
    r = simulator.run(n=50_000, seed=1, sweep=False)["headline"]
    assert r["statusQuo"]["genuineDelayed"] == 0
    assert r["blanket"]["recoveryRatio"] >= r["statusQuo"]["recoveryRatio"]
    assert 0 < r["twokey"]["genuineDelayed"] < r["blanket"]["genuineDelayed"]


def test_ledger_client_unreachable():
    from twokey.ledger import LedgerUnavailable
    led = Ledger("http://127.0.0.1:9", {})
    assert not led.healthy()
    with pytest.raises(LedgerUnavailable):
        led.credits()
