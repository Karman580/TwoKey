"""TwoKey off-chain services in one FastAPI process.

Routers by role: mock UPI switch, risk-tier service, mock 1930 helpline, bank
operations for both banks, NPCI observer, and demo controls. Every state
change on a held credit goes through the two-key ledger, which verifies the
organisations' signatures; this process cannot move a held credit by itself.
"""

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import risk, simulator
from .ledger import NPCI, Ledger, LedgerRejected, LedgerUnavailable, hash_vpa, parse_keys
from .world import Transfer, World

log = logging.getLogger("twokey")
ROOT = Path(__file__).resolve().parents[2]
VPA_RE = re.compile(r"^[a-z0-9._-]{2,64}@[a-z]{2,32}$")
UPI_P2P_LIMIT_PAISE = 1_00_000_00  # Rs 1 lakh


class Settings:
    def __init__(self, env=os.environ):
        self.ledger_url = env.get("LEDGER_URL", "http://127.0.0.1:8081")
        self.salt = env.get("VPA_HASH_SALT", "")
        self.hold_medium = int(env.get("HOLD_MEDIUM_SECONDS", 30 * 60))
        self.hold_high = int(env.get("HOLD_HIGH_SECONDS", 4 * 60 * 60))
        self.floor = int(env.get("VALUE_FLOOR_PAISE", risk.VALUE_FLOOR_PAISE))
        self.keeper_interval = float(env.get("KEEPER_INTERVAL_SECONDS", 5))
        self.allow_dev_clock = env.get("DEVLEDGER_ALLOW_CLOCK") == "1"
        self.keys = env.get("TWOKEY_SECRET_KEYS", "")
        if len(self.salt) < 16:
            raise RuntimeError("VPA_HASH_SALT must be set (16+ chars); run scripts/gen_keys.py")
        if not (0 < self.hold_medium <= self.hold_high <= 4 * 3600):
            raise RuntimeError("need 0 < HOLD_MEDIUM_SECONDS <= HOLD_HIGH_SECONDS <= 14400")


# ---- request models ----------------------------------------------------------
class PayIn(BaseModel):
    payerVpa: str
    payeeVpa: str
    amountPaise: int = Field(gt=0, le=UPI_P2P_LIMIT_PAISE)
    purpose: Literal["P2P", "MERCHANT", "MANDATE"] = "P2P"

    @field_validator("payerVpa", "payeeVpa")
    @classmethod
    def vpa(cls, v: str) -> str:
        v = v.strip().lower()
        if not VPA_RE.match(v):
            raise ValueError("not a valid VPA, expected name@handle")
        return v


class RefIn(BaseModel):
    txnRef: str = Field(pattern=r"^RRN\d{9,16}$")


class ReportIn(RefIn):
    description: str = Field(default="", max_length=500)


class DisputeIn(RefIn):
    reason: str = Field(min_length=3, max_length=300)


class ResolveIn(RefIn):
    outcome: Literal["RELEASE", "RECALL"]


class AdvanceIn(BaseModel):
    seconds: int = Field(gt=0, le=24 * 3600)


class SimIn(BaseModel):
    n: int = Field(default=200_000, ge=10_000, le=300_000)
    prevalence: float = Field(default=0.002, ge=0.0005, le=0.005)
    cashoutMedianMinutes: float = Field(default=30, ge=5, le=360)
    seed: int = Field(default=42, ge=0, le=2**31)


Bank = Literal["PayerBank", "PayeeBank"]


# ---- application ----------------------------------------------------------------
class TwoKey:
    """Holds the simulated world and talks to the ledger. ponytail: one lock
    serialises all mutations; per-account locking if this ever needs throughput."""

    def __init__(self, settings: Settings, ledger: Ledger):
        self.s, self.ledger = settings, ledger
        self.lock = threading.RLock()
        self.world = World(time.time(), settings.floor)
        self.last_proposal: dict[tuple[str, str], dict] = {}

    def signers(self, credit: dict) -> list[str]:
        orgs = [credit["payerBank"], credit["payeeBank"]]
        return orgs + [NPCI] if credit["state"] == "DISPUTED" else orgs

    def act(self, fn: str, creator: str, txn_ref: str, arg: str = "") -> dict:
        """Submit fn as creator, with endorsements from the credit's required orgs."""
        with self.lock:
            current = self.ledger.credit(txn_ref)
            signers = self.signers(current)
            if creator not in signers:
                signers.append(creator)
            credit, proposal = self.ledger.submit(fn, creator, {"txnRef": txn_ref, "arg": arg}, signers)
            self.last_proposal[(txn_ref, fn)] = proposal
            self.world.settle(txn_ref, credit["state"])
            return credit

    def pay(self, p: PayIn) -> dict:
        w = self.world
        with self.lock:
            payer, payee = w.accounts.get(p.payerVpa), w.accounts.get(p.payeeVpa)
            if payer is None or payee is None:
                raise HTTPException(404, {"code": "UNKNOWN_VPA", "error": "payer or payee VPA not found in the demo banks"})
            if payer.bank == payee.bank:
                raise HTTPException(422, {"code": "ON_US", "error": "on-us transfers are outside this demo"})
            if payer.balance_paise < p.amountPaise:
                raise HTTPException(422, {"code": "INSUFFICIENT_FUNDS", "error": "payer balance too low"})
            now = time.time()
            f = w.features(p.payerVpa, p.payeeVpa, p.amountPaise, p.purpose, now)
            d = risk.decide(f, self.s.hold_medium, self.s.hold_high, self.s.floor)
            t = Transfer(w.next_ref(), p.payerVpa, p.payeeVpa, p.amountPaise, now, p.purpose,
                         d.tier, d.reason_codes, d.exemption, hold_seconds=d.hold_seconds)
            credit = None
            if d.tier == "LOW":
                payer.balance_paise -= p.amountPaise
                payee.balance_paise += p.amountPaise
            else:
                req = {"txnRef": t.txn_ref, "amountPaise": p.amountPaise,
                       "payerBank": payer.bank, "payeeBank": payee.bank,
                       "hashedPayer": hash_vpa(self.s.salt, p.payerVpa),
                       "hashedPayee": hash_vpa(self.s.salt, p.payeeVpa),
                       "tier": d.tier, "reasonCodes": d.reason_codes, "holdSeconds": d.hold_seconds}
                # Fails closed: if the ledger is down the payment is declined
                # before any money moves, and the payer can retry.
                credit, _ = self.ledger.submit("CreateHold", payer.bank, req, [payer.bank, payee.bank])
                payer.balance_paise -= p.amountPaise
                payee.held_paise += p.amountPaise
                t.status, t.settled = "HELD", False
            w.record(t)
            log.info("pay %s tier=%s codes=%s", t.txn_ref, d.tier, d.reason_codes)
            return {"txnRef": t.txn_ref, "status": t.status, "tier": d.tier,
                    "holdSeconds": d.hold_seconds, "reasonCodes": d.reason_codes,
                    "explanations": d.explanations, "exemption": d.exemption,
                    "holdUntil": credit["holdUntil"] if credit else None, "credit": credit}

    def keeper_once(self) -> list[str]:
        """Expire holds past HoldUntil and reconcile final states into core banking."""
        done = []
        with self.lock:
            now = self.ledger.now()
            for c in self.ledger.credits():
                ref = c["txnRef"]
                if c["state"] == "PENDING" and now >= c["holdUntil"]:
                    try:
                        self.act("Expire", c["payeeBank"], ref)
                        done.append(ref)
                    except LedgerRejected as e:
                        log.warning("keeper could not expire %s: %s", ref, e)
                elif self.world.settle(ref, c["state"]):
                    done.append(ref)
        return done


def create_app(settings: Settings | None = None, ledger: Ledger | None = None,
               run_keeper: bool = True) -> FastAPI:
    settings = settings or Settings()
    ledger = ledger or Ledger(settings.ledger_url, parse_keys(settings.keys))
    tk = TwoKey(settings, ledger)
    stop = threading.Event()

    def keeper_loop():
        while not stop.wait(settings.keeper_interval):
            try:
                tk.keeper_once()
            except (LedgerUnavailable, LedgerRejected) as e:
                log.warning("keeper: %s", e)

    @asynccontextmanager
    async def lifespan(_):
        th = threading.Thread(target=keeper_loop, daemon=True)
        if run_keeper:
            th.start()
        yield
        stop.set()

    app = FastAPI(title="TwoKey", version="0.1.0", lifespan=lifespan,
                  description="Targeted, jointly controlled holds for risky UPI P2P payments. "
                              "All banks, the UPI switch and the 1930 helpline are simulated.")
    app.state.tk = tk

    @app.exception_handler(LedgerRejected)
    async def rejected(_: Request, e: LedgerRejected):
        return JSONResponse({"detail": {"code": e.code, "error": e.message}}, status_code=e.status)

    @app.exception_handler(LedgerUnavailable)
    async def unavailable(_: Request, e: LedgerUnavailable):
        return JSONResponse({"detail": {"code": "LEDGER_UNAVAILABLE",
                                        "error": "the ledger is not reachable; nothing was changed"}},
                            status_code=503)

    # ---- system ---------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "ledger": ledger.healthy(),
                "holdSeconds": {"MEDIUM": settings.hold_medium, "HIGH": settings.hold_high},
                "valueFloorPaise": settings.floor, "devClock": settings.allow_dev_clock}

    @app.get("/api/accounts")
    def accounts():
        return [{"vpa": a.vpa, "name": a.name, "bank": a.bank, "kind": a.kind,
                 "ageDays": round((time.time() - a.opened_at) / 86400),
                 "balancePaise": a.balance_paise, "heldPaise": a.held_paise}
                for a in tk.world.accounts.values() if not a.name.startswith("Background")]

    # ---- mock UPI switch + risk service ---------------------------------------
    @app.post("/api/upi/pay")
    def pay(p: PayIn):
        return tk.pay(p)

    @app.get("/api/upi/status/{txn_ref}")
    def status(txn_ref: str):
        t = tk.world.by_ref.get(txn_ref)
        if t is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "error": "unknown txnRef"})
        return {"txnRef": t.txn_ref, "status": t.status, "tier": t.tier, "reasonCodes": t.reason_codes}

    @app.post("/api/risk/score")
    def score(p: PayIn):
        w = tk.world
        if p.payerVpa not in w.accounts or p.payeeVpa not in w.accounts:
            raise HTTPException(404, {"code": "UNKNOWN_VPA", "error": "VPA not found in the demo banks"})
        f = w.features(p.payerVpa, p.payeeVpa, p.amountPaise, p.purpose, time.time())
        d = risk.decide(f, settings.hold_medium, settings.hold_high, settings.floor)
        return {"tier": d.tier, "holdSeconds": d.hold_seconds, "reasonCodes": d.reason_codes,
                "explanations": d.explanations, "exemption": d.exemption, "features": f.__dict__}

    @app.get("/api/transfers")
    def transfers():
        out = [t for t in tk.world.transfers if t.txn_ref in tk.world.by_ref]
        return [t.__dict__ for t in reversed(out[-50:])]

    # ---- mock 1930 helpline -> payer bank ---------------------------------------
    @app.post("/api/helpline/report")
    def report(r: ReportIn):
        t = tk.world.by_ref.get(r.txnRef)
        if t is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "error": "unknown txnRef"})
        complaint = f"1930-{int(time.time()) % 10**7:07d}"
        if t.status == "SUCCESS":
            return {"complaint": complaint, "outcome": "NOT_HELD",
                    "message": "This payment settled instantly, so there is no hold to recall. "
                               "The complaint follows the existing CFCFRMS route."}
        payer_bank = tk.world.accounts[t.payer].bank
        try:
            credit = tk.act("RequestRecall", payer_bank, r.txnRef, complaint)
        except LedgerRejected as e:
            return {"complaint": complaint, "outcome": "TOO_LATE_OR_INVALID", "message": e.message}
        t.report_ref = complaint
        return {"complaint": complaint, "outcome": "RECALL_REQUESTED", "credit": credit}

    # ---- bank operations ------------------------------------------------------------
    @app.post("/api/bank/{bank}/confirm-recall")
    def confirm_recall(bank: Bank, r: RefIn):
        return tk.act("ConfirmRecall", bank, r.txnRef)

    @app.post("/api/bank/{bank}/approve-release")
    def approve_release(bank: Bank, r: RefIn):
        return tk.act("ApproveRelease", bank, r.txnRef)

    @app.post("/api/bank/{bank}/dispute")
    def dispute(bank: Bank, r: DisputeIn):
        return tk.act("RaiseDispute", bank, r.txnRef, r.reason)

    @app.post("/api/npci/resolve")
    def resolve(r: ResolveIn):
        return tk.act("ResolveDispute", NPCI, r.txnRef, r.outcome)

    # ---- ledger views ------------------------------------------------------------------
    @app.get("/api/credits")
    def credits():
        """Shared-channel view from the ledger, joined with each bank's own records."""
        out = []
        for c in ledger.credits():
            t = tk.world.by_ref.get(c["txnRef"])
            c["bankView"] = {"payerVpa": t.payer, "payeeVpa": t.payee,
                             "payeeName": tk.world.accounts[t.payee].name} if t else None
            out.append(c)
        return {"now": ledger.now(), "credits": out}

    @app.get("/api/chain")
    def chain():
        return ledger.chain()

    # ---- adversarial demos ----------------------------------------------------------------
    @app.post("/api/attack/{name}")
    def attack(name: Literal["unilateral-release", "forged-endorsement", "payer-confirms-own-recall",
                             "replay-recall"], r: RefIn):
        """Each attack is sent to the real ledger; the response shows how it was stopped."""
        c = ledger.credit(r.txnRef)
        payee, payer = c["payeeBank"], c["payerBank"]
        args = {"txnRef": r.txnRef, "arg": ""}
        if name == "unilateral-release":
            p = ledger.proposal("Expire", payee, args, [payee])
        elif name == "forged-endorsement":
            p = ledger.proposal("ApproveRelease", payee, args, [payee, payer], forge_as={payer: payee})
        elif name == "payer-confirms-own-recall":
            p = ledger.proposal("ConfirmRecall", payer, args, [payer, payee])
        else:
            p = tk.last_proposal.get((r.txnRef, "RequestRecall"))
            if p is None:
                raise HTTPException(409, {"code": "NO_RECALL_YET", "error": "report the payment to 1930 first, then replay"})
        try:
            ledger.send(p)
        except LedgerRejected as e:
            return {"attack": name, "blocked": True, "status": e.status, "code": e.code,
                    "error": e.message, "state": ledger.credit(r.txnRef)["state"]}
        return {"attack": name, "blocked": False, "state": ledger.credit(r.txnRef)["state"]}

    # ---- policy simulator --------------------------------------------------------------
    results_path = ROOT / "simulator" / "results.json"

    @app.get("/api/simulation")
    def simulation():
        if not results_path.exists():
            raise HTTPException(404, {"code": "NO_RESULTS", "error": "run `make simulate` first"})
        return json.loads(results_path.read_text())

    @app.post("/api/simulation")
    def run_simulation(s: SimIn):
        return simulator.run(n=s.n, prevalence=s.prevalence, cashout_median_min=s.cashoutMedianMinutes,
                             seed=s.seed, sweep=False)

    # ---- demo controls --------------------------------------------------------------------
    @app.post("/api/dev/advance")
    def advance(a: AdvanceIn):
        if not settings.allow_dev_clock:
            raise HTTPException(403, {"code": "DISABLED", "error": "set DEVLEDGER_ALLOW_CLOCK=1 to enable"})
        now = ledger.advance(a.seconds)
        return {"now": now, "settled": tk.keeper_once()}

    @app.post("/api/demo/reset")
    def reset():
        with tk.lock:
            tk.world = World(time.time(), settings.floor)
        return {"ok": True}

    frontend = ROOT / "frontend"
    app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(frontend / "index.html")

    return app


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
