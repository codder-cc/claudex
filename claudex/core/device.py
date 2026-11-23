"""Per-machine device identity.

Each claudex install has one persistent X25519 keypair and a stable device id.
It underpins everything peer-to-peer:

  * remote fleet jobs — a host publishes this public key; submitters seal prompts
    to it and the host seals results back to the submitter's device key, so the
    relay server only ever sees ciphertext.
  * peer links (LAN / relay) — peers are identified and trusted by this key
    (trust-on-first-use): the first time we see a peer we pin its public key, and
    every later connection must present the same one.

The private key never leaves this machine.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from claudex.constants import CLAUDEX_HOME
from claudex import crypto

DEVICE_FILE = CLAUDEX_HOME / "device.json"


@dataclass
class DeviceIdentity:
    device_id: str          # short, stable id derived from the public key
    name: str               # human label (defaults to the hostname)
    public_key: str         # X25519 public key (base64url)
    private_key: str        # X25519 private key (base64url) — never leaves here

    def public(self) -> dict:
        """The shareable half (never includes the private key)."""
        return {"device_id": self.device_id, "name": self.name,
                "public_key": self.public_key}


def _device_id_for(public_key: str) -> str:
    import hashlib
    return hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:12]


def load_device(path: Optional[Path] = None) -> DeviceIdentity:
    """Load this machine's identity, creating it on first use."""
    p = path or DEVICE_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return DeviceIdentity(
            device_id=data["device_id"], name=data.get("name") or socket.gethostname(),
            public_key=data["public_key"], private_key=data["private_key"],
        )
    except (OSError, ValueError, KeyError):
        return _create_device(p)


def _create_device(path: Path) -> DeviceIdentity:
    private_key, public_key = crypto.generate_keypair()
    ident = DeviceIdentity(
        device_id=_device_id_for(public_key),
        name=socket.gethostname(),
        public_key=public_key,
        private_key=private_key,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "device_id": ident.device_id, "name": ident.name,
        "public_key": ident.public_key, "private_key": ident.private_key,
    }), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return ident


def set_device_name(name: str, path: Optional[Path] = None) -> DeviceIdentity:
    p = path or DEVICE_FILE
    ident = load_device(p)
    ident.name = name.strip() or ident.name
    p.write_text(json.dumps({
        "device_id": ident.device_id, "name": ident.name,
        "public_key": ident.public_key, "private_key": ident.private_key,
    }), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return ident
