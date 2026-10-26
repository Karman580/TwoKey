"""Generate fresh dev keys and a hashing salt into .env (never commit .env).

Each organisation gets an Ed25519 key pair. The devledger only receives the
public keys; the services receive the private keys because, in the demo, one
process operates all three organisations.
"""

import base64
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ORGS = ("PayerBank", "PayeeBank", "NPCIObserver")
env = Path(__file__).resolve().parents[1] / ".env"
example = env.with_name(".env.example")

if env.exists() and "--force" not in sys.argv:
    sys.exit(f"{env} exists; pass --force to replace its keys")

secret, public = [], []
for org in ORGS:
    seed = secrets.token_bytes(32)
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    secret.append(f"{org}={base64.b64encode(seed).decode()}")
    public.append(f"{org}={base64.b64encode(pub).decode()}")

generated = {
    "TWOKEY_SECRET_KEYS": ",".join(secret),
    "TWOKEY_PUBKEYS": ",".join(public),
    "VPA_HASH_SALT": secrets.token_hex(16),
}
lines = []
for line in example.read_text().splitlines():
    key = line.split("=", 1)[0]
    lines.append(f"{key}={generated[key]}" if key in generated else line)
env.write_text("\n".join(lines) + "\n")
env.chmod(0o600)
print(f"wrote {env}")
