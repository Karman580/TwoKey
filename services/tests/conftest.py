import base64
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
ORGS = ("PayerBank", "PayeeBank", "NPCIObserver")


@pytest.fixture(scope="session")
def devledger(tmp_path_factory):
    """Build and start the real Go devledger on a free port."""
    if not shutil.which("go"):
        pytest.skip("Go toolchain not installed")
    tmp = tmp_path_factory.mktemp("ledger")
    binary = tmp / "devledger"
    subprocess.run(["go", "build", "-o", str(binary), "./cmd/devledger"], cwd=ROOT / "chaincode", check=True)
    seeds = {o: os.urandom(32) for o in ORGS}
    pub = {o: Ed25519PrivateKey.from_private_bytes(s).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
           for o, s in seeds.items()}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "DEVLEDGER_ADDR": f"127.0.0.1:{port}", "DEVLEDGER_ALLOW_CLOCK": "1",
           "TWOKEY_PUBKEYS": ",".join(f"{o}={base64.b64encode(k).decode()}" for o, k in pub.items())}
    env.pop("DEVLEDGER_DATA", None)
    proc = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield {"url": url, "secret": ",".join(f"{o}={base64.b64encode(s).decode()}" for o, s in seeds.items())}
    proc.terminate()
    proc.wait()
