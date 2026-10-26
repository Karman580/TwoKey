"""Client for the two-key ledger (the local devledger, standing in for Drunix).

Each organisation signs proposals with its own Ed25519 key. In the demo all
three keys live in this one process because one operator plays every role;
in a real deployment each bank holds only its own key, in its own HSM.
"""

import base64
import hashlib
import hmac
import json
import uuid

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PAYER_BANK, PAYEE_BANK, NPCI = "PayerBank", "PayeeBank", "NPCIObserver"
ORGS = (PAYER_BANK, PAYEE_BANK, NPCI)


class LedgerUnavailable(Exception):
    pass


class LedgerRejected(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


def parse_keys(spec: str) -> dict[str, Ed25519PrivateKey]:
    """Parse "Org=base64seed,Org2=base64seed" into private keys."""
    keys = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        org, _, seed = part.partition("=")
        keys[org] = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed))
    missing = set(ORGS) - set(keys)
    if missing:
        raise ValueError(f"TWOKEY_SECRET_KEYS is missing {sorted(missing)}; run scripts/gen_keys.py")
    return keys


def hash_vpa(salt: str, vpa: str) -> str:
    """Salted hash used on the shared channel instead of the VPA itself."""
    return hmac.new(salt.encode(), vpa.lower().encode(), hashlib.sha256).hexdigest()


def signing_bytes(p: dict) -> bytes:
    return f"twokey-v1\n{p['txId']}\n{p['fn']}\n{p['creator']}\n{p['args']}".encode()


class Ledger:
    def __init__(self, url: str, keys: dict[str, Ed25519PrivateKey], timeout: float = 5.0):
        self.keys = keys
        self.http = httpx.Client(base_url=url, timeout=timeout)

    def proposal(self, fn: str, creator: str, args: dict, signers: list[str],
                 forge_as: dict[str, str] | None = None) -> dict:
        """Build a signed proposal. forge_as maps a claimed org to the org whose
        key actually signs; it exists only so the attack demos can show that
        forged endorsements fail verification."""
        p = {"txId": uuid.uuid4().hex, "fn": fn, "creator": creator,
             "args": json.dumps(args, separators=(",", ":"), sort_keys=True), "endorsements": []}
        msg = signing_bytes(p)
        for org in signers:
            signer = (forge_as or {}).get(org, org)
            sig = self.keys[signer].sign(msg)
            p["endorsements"].append({"org": org, "sig": base64.b64encode(sig).decode()})
        return p

    def send(self, proposal: dict) -> dict:
        body = self._call("POST", "/tx", json=proposal)
        return body["credit"]

    def submit(self, fn: str, creator: str, args: dict, signers: list[str]) -> tuple[dict, dict]:
        p = self.proposal(fn, creator, args, signers)
        return self.send(p), p

    def credits(self) -> list[dict]:
        return self._call("GET", "/credits")

    def credit(self, txn_ref: str) -> dict:
        return self._call("GET", f"/credits/{txn_ref}")

    def chain(self) -> dict:
        return self._call("GET", "/chain")

    def now(self) -> int:
        return self._call("GET", "/time")["now"]

    def advance(self, seconds: int) -> int:
        return self._call("POST", "/dev/advance", json={"seconds": seconds})["now"]

    def healthy(self) -> bool:
        try:
            return self._call("GET", "/health")["ok"]
        except (LedgerUnavailable, LedgerRejected):
            return False

    def _call(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise LedgerUnavailable(f"ledger unreachable: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"code": "HTTP_ERROR", "error": r.text[:200]}
            raise LedgerRejected(r.status_code, body.get("code", "ERROR"), body.get("error", ""))
        return r.json()
