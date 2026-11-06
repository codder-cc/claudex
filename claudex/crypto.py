"""AES-256-GCM encryption utilities for claudex profile sharing.

Share token format:
    "cx_" + base64url( uuid_bytes(16) + aes_key(32) )  = "cx_" + 64 chars

Security model (important):
    The token embeds the AES key. ``claudex share push`` currently uploads the
    full ``cx_`` token to the server so that ``share pull <label>`` can fetch the
    key without the user copy-pasting it. This is a convenience trade-off, NOT a
    zero-knowledge design: a server compromise can decrypt every bundle (which
    includes credentials). If you need the server to never see the key, pull by
    the ``cx_`` token instead of by label and do not upload the token.
"""

from __future__ import annotations

import base64
import os
import uuid
from typing import Tuple


def generate_key() -> bytes:
    """Generate a random 32-byte AES-256 key."""
    return os.urandom(32)


def encrypt(key: bytes, data: bytes) -> bytes:
    """Encrypt *data* with AES-256-GCM.

    Returns: nonce(12 bytes) + ciphertext + tag(16 bytes) as a single blob.
    Raises: ImportError if *cryptography* is not installed.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return nonce + ct


def decrypt(key: bytes, blob: bytes) -> bytes:
    """Decrypt a blob produced by :func:`encrypt`.

    Raises:
        ValueError: if the blob is too short or authentication fails.
        ImportError: if *cryptography* is not installed.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    if len(blob) < 28:  # 12 nonce + 16 tag minimum
        raise ValueError("Ciphertext blob is too short")

    nonce = blob[:12]
    ct = blob[12:]
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError("Decryption failed — wrong key or corrupted data")


def encode_share_token(key: bytes) -> str:
    """Build a share token embedding a fresh UUID and the AES key.

    Format: ``cx_<base64url(uuid_bytes + aes_key)>``
    The result is 67 characters: 3 prefix + 64 base64url chars (48 raw bytes).
    The UUID embedded in the token becomes the server-side ``token_id``.
    """
    uid_bytes = uuid.uuid4().bytes         # 16 bytes, fresh each call
    raw = uid_bytes + key                  # 48 bytes total
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "cx_" + b64


def decode_share_token(token: str) -> Tuple[str, bytes]:
    """Parse a share token into (token_id: str, aes_key: bytes).

    Raises:
        ValueError: if the token is malformed.
    """
    if not token.isascii():
        raise ValueError(
            "Share token contains non-ASCII characters (possible Cyrillic/Unicode lookalikes). "
            "Copy-paste the token directly from `claudex share push` output — do not retype it."
        )
    if not token.startswith("cx_"):
        raise ValueError("Not a valid claudex share token (expected 'cx_' prefix)")

    b64 = token[3:]
    # Re-add padding
    padding = 4 - (len(b64) % 4)
    if padding != 4:
        b64 += "=" * padding

    try:
        raw = base64.urlsafe_b64decode(b64)
    except Exception:
        raise ValueError("Share token contains invalid base64")

    if len(raw) != 48:
        raise ValueError(f"Share token has wrong length ({len(raw)} bytes, expected 48)")

    token_id = str(uuid.UUID(bytes=raw[:16]))
    aes_key = raw[16:]
    return token_id, aes_key


# ── Sealed boxes + drop tokens for remote-login hand-off ─────────────────────
#
# Shared format with aimux (`axd_` tokens, X25519 + HKDF-SHA256 + AES-256-GCM):
# a drop created by either tool can be fulfilled by the other.
#
#     blob = ephemeral_pub(32) || nonce(12) || AES-GCM(ct || tag)

_DROP_INFO = b"aimux-drop-v1"
DROP_TOKEN_PREFIX = "axd_"


def generate_keypair() -> Tuple[str, str]:
    """(private_b64url, public_b64url) for an X25519 key pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64e(priv_raw), _b64e(pub_raw)


def seal(public_b64: str, data: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    recipient = X25519PublicKey.from_public_bytes(_b64d(public_b64))
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _derive(eph.exchange(recipient), eph_pub, _b64d(public_b64))
    nonce = os.urandom(12)
    return eph_pub + nonce + AESGCM(key).encrypt(nonce, data, None)


def unseal(private_b64: str, blob: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < 32 + 12 + 16:
        raise ValueError("Sealed blob is too short")
    priv = X25519PrivateKey.from_private_bytes(_b64d(private_b64))
    my_pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
    key = _derive(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, my_pub)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError("Unsealing failed — wrong key or corrupted data")


def _derive(shared: bytes, eph_pub: bytes, recipient_pub: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_DROP_INFO + eph_pub + recipient_pub).derive(shared)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * ((-len(text)) % 4))


def encode_drop_token(endpoint: str, drop_id: str, write_token: str, public_b64: str,
                      backend: str = "claude") -> str:
    """``axd_<base64url(json)>``: endpoint, drop id, single-use write token,
    recipient public key, backend. Carries no decryption capability."""
    import json

    payload = {"v": 1, "e": endpoint.rstrip("/"), "d": drop_id, "w": write_token,
               "k": public_b64, "b": backend}
    return DROP_TOKEN_PREFIX + _b64e(json.dumps(payload, separators=(",", ":")).encode())


def decode_drop_token(token: str) -> dict:
    import json

    token = token.strip()
    if not token.startswith(DROP_TOKEN_PREFIX):
        raise ValueError("Not a drop token (expected 'axd_' prefix)")
    try:
        payload = json.loads(_b64d(token[len(DROP_TOKEN_PREFIX):]))
    except Exception:
        raise ValueError("Drop token is corrupted")
    required = {"v", "e", "d", "w", "k", "b"}
    if not isinstance(payload, dict) or not required.issubset(payload) or payload["v"] != 1:
        raise ValueError("Drop token has an unsupported layout")
    return payload
