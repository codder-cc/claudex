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
