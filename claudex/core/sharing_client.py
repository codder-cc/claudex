"""HTTP client for the claudex profile-sharing API.

Credentials for a sharing endpoint are stored in the system credential backend
under the service name ``claudex-sharing``, keyed by a short hash of the
endpoint URL so multiple endpoints can coexist.

Stored keys (all strings):
  ``<hash>:url``          — canonical endpoint URL (trailing slash stripped)
  ``<hash>:jwt``          — current access token
  ``<hash>:refresh``      — refresh token
  ``<hash>:expires_at``   — ISO-8601 string of access token expiry
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin

from claudex.platform import get_credential_backend

# Credential backend service name (separate from profile credentials)
_SHARING_SERVICE = "claudex-sharing"


def _endpoint_hash(endpoint: str) -> str:
    """Short hash of the endpoint URL used as the credential key prefix."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


def _http(method: str, url: str, data: Optional[dict] = None,
          headers: Optional[dict] = None) -> dict:
    """Minimal synchronous HTTP helper using stdlib urllib (no extra deps).

    Returns the parsed JSON response body.
    Raises :class:`SharingAPIError` on non-2xx status or network errors.
    """
    body = json.dumps(data).encode() if data is not None else None
    req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    req = Request(url, data=body, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            body_text = exc.read().decode(errors="replace")
            err_data = json.loads(body_text)
            msg = err_data.get("error", body_text)
        except Exception:
            msg = str(exc)
        raise SharingAPIError(exc.code, msg) from exc
    except URLError as exc:
        raise SharingAPIError(0, f"Network error: {exc.reason}") from exc


class SharingAPIError(Exception):
    """Raised when the sharing API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code
        self.message = message


class SharingCredentials:
    """Read/write sharing credentials for a given endpoint from the system keyring."""

    def __init__(self, endpoint: str):
        self._endpoint = endpoint.rstrip("/")
        self._hash = _endpoint_hash(self._endpoint)
        self._backend = get_credential_backend()

    def _k(self, key: str) -> str:
        return f"{self._hash}:{key}"

    def save(self, jwt: str, refresh: str, expires_at: str) -> None:
        self._backend.store(_SHARING_SERVICE, self._k("url"), self._endpoint)
        self._backend.store(_SHARING_SERVICE, self._k("jwt"), jwt)
        self._backend.store(_SHARING_SERVICE, self._k("refresh"), refresh)
        self._backend.store(_SHARING_SERVICE, self._k("expires_at"), expires_at)

    def load_jwt(self) -> Optional[str]:
        return self._backend.retrieve(_SHARING_SERVICE, self._k("jwt"))

    def load_refresh(self) -> Optional[str]:
        return self._backend.retrieve(_SHARING_SERVICE, self._k("refresh"))

    def clear(self) -> None:
        for key in ("url", "jwt", "refresh", "expires_at"):
            try:
                self._backend.delete(_SHARING_SERVICE, self._k(key))
            except Exception:
                pass


class SharingClient:
    """Authenticated client for the claudex sharing API.

    Args:
        endpoint: Base URL of the sharing server (e.g. ``https://codder.cc``).
        jwt_token: Valid site access token (Bearer).
    """

    def __init__(self, endpoint: str, jwt_token: str):
        self._endpoint = endpoint.rstrip("/")
        self._auth_header = {"Authorization": f"Bearer {jwt_token}"}

    def _url(self, path: str) -> str:
        return self._endpoint + path

    def create_share(
        self,
        label: str,
        encrypted_data_b64: str,
        expires_in_days: Optional[int] = None,
    ) -> str:
        """Upload an encrypted profile bundle.

        Returns the server-side ``token_id`` UUID string.
        """
        payload: Dict[str, Any] = {
            "label": label,
            "encrypted_data": encrypted_data_b64,
        }
        if expires_in_days and expires_in_days > 0:
            payload["expires_in_days"] = expires_in_days

        result = _http(
            "POST",
            self._url("/api/v1/claudex/shares"),
            data=payload,
            headers=self._auth_header,
        )
        return result["token_id"]

    def get_share(self, token_id: str) -> str:
        """Download an encrypted profile bundle by ``token_id``.

        Returns the base64-encoded ciphertext string.
        """
        result = _http(
            "GET",
            self._url(f"/api/v1/claudex/shares/{token_id}"),
            headers=self._auth_header,
        )
        return result["encrypted_data"]

    def list_shares(self) -> List[dict]:
        """Return a list of share metadata dicts for the authenticated user."""
        result = _http(
            "GET",
            self._url("/api/v1/claudex/shares"),
            headers=self._auth_header,
        )
        return result.get("shares", [])

    def revoke_share(self, token_id: str) -> None:
        """Revoke a share. Raises :class:`SharingAPIError` if not found."""
        _http(
            "DELETE",
            self._url(f"/api/v1/claudex/shares/{token_id}"),
            headers=self._auth_header,
        )


def login(endpoint: str, username: str, password: str) -> SharingClient:
    """Authenticate to the sharing endpoint and persist credentials.

    Returns a :class:`SharingClient` ready to use.
    """
    endpoint = endpoint.rstrip("/")
    result = _http(
        "POST",
        endpoint + "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    jwt = result.get("access_token") or result.get("token", "")
    refresh = result.get("refresh_token", "")
    expires_at = result.get("expires_at", "")

    if not jwt:
        raise SharingAPIError(0, "Server did not return an access token")

    creds = SharingCredentials(endpoint)
    creds.save(jwt=jwt, refresh=refresh, expires_at=expires_at)
    return SharingClient(endpoint, jwt)


def load_client(endpoint: str) -> SharingClient:
    """Load a previously authenticated client for *endpoint*.

    Raises:
        RuntimeError: if no credentials are stored for this endpoint.
    """
    creds = SharingCredentials(endpoint)
    jwt = creds.load_jwt()
    if not jwt:
        raise RuntimeError(
            f"No credentials found for {endpoint}. "
            "Run: claudex share auth"
        )
    return SharingClient(endpoint, jwt)
