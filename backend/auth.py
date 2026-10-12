"""
Access control for the FRD (v4.0).

AUTH_MODE (env, read on every request so it can be flipped with a restart-free env change):
  off     — no checks (default; deploying v4 alone can never lock anyone out)
  writes  — POST/PUT/DELETE on /api/* need credentials; reads stay public
  full    — every /api/* route except /api/health and /api/auth/* needs credentials,
            and so do /docs, /redoc and /openapi.json. The UI shell (/, /static/*)
            still loads so it can show the passcode screen, but it holds no data.

Credentials (either one):
  X-API-Key: <API_KEY>                 — Hunter / scripts. Works for reads and writes.
  Authorization: Bearer <token>        — browser; token from POST /api/auth/login with
                                         the shared TEAM_PASSCODE. HMAC-SHA256 signed
                                         with AUTH_SECRET, valid AUTH_TOKEN_DAYS (30).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque

log = logging.getLogger("frd.auth")

PUBLIC_DEFAULT_API_KEY = "rootrise-dev-key-2024"   # shipped in env.example; never accepted
OPEN_PATHS = ("/api/health", "/api/auth/")
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def mode() -> str:
    m = os.environ.get("AUTH_MODE", "off").strip().lower()
    return m if m in ("off", "writes", "full") else "off"


def _api_key():
    key = os.environ.get("API_KEY", "")
    return key if key and key != PUBLIC_DEFAULT_API_KEY else None


def _passcode():
    return os.environ.get("TEAM_PASSCODE") or None


def _secret() -> bytes:
    secret = os.environ.get("AUTH_SECRET")
    if not secret:
        # Derived fallback: rotating the passcode or key invalidates all tokens.
        secret = hashlib.sha256(f"frd|{_passcode()}|{_api_key()}".encode()).hexdigest()
    return secret.encode()


def _same(a: str, b: str) -> bool:
    """Constant-time compare that never raises (compare_digest rejects non-ASCII str)."""
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape"))


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(now: float = None) -> dict:
    now = now or time.time()
    days = int(os.environ.get("AUTH_TOKEN_DAYS", "30"))
    payload = {"sub": "team", "iat": int(now), "exp": int(now + days * 86400)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return {"token": f"{body}.{sig}", "expires_at": payload["exp"]}


def verify_token(token: str) -> bool:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not _same(sig, expected):
            return False
        return json.loads(_unb64(body)).get("exp", 0) > time.time()
    except Exception:
        return False


def is_authenticated(headers) -> bool:
    key = _api_key()
    sent_key = headers.get("x-api-key")
    if key and sent_key and _same(sent_key, key):
        return True
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and _passcode():
        return verify_token(auth[7:].strip())
    return False


def requires_auth(method: str, path: str) -> bool:
    m = mode()
    if m == "off" or method == "OPTIONS":
        return False
    if path.startswith(OPEN_PATHS):
        return False
    if path.startswith("/api/"):
        return m == "full" or method not in SAFE_METHODS
    if m == "full" and path.startswith(DOC_PATHS):
        return True
    return False


# ── login throttle: 10 failed attempts per IP per 10 minutes ──
_failures = defaultdict(deque)
MAX_FAILURES, WINDOW = 10, 600


def login_allowed(ip: str) -> bool:
    q = _failures[ip]
    while q and q[0] < time.time() - WINDOW:
        q.popleft()
    return len(q) < MAX_FAILURES


def record_failure(ip: str):
    _failures[ip].append(time.time())


def check_passcode(passcode: str) -> bool:
    expected = _passcode()
    return bool(expected and passcode and _same(passcode, expected))


def startup_warnings():
    m = mode()
    if m == "off":
        log.warning("AUTH_MODE=off — the API accepts unauthenticated writes")
        return
    if not _api_key():
        log.error("AUTH_MODE=%s but API_KEY is unset (or the public default) — the Hunter will be rejected", m)
    if not _passcode():
        log.error("AUTH_MODE=%s but TEAM_PASSCODE is unset — nobody can sign in to the UI", m)
    if not os.environ.get("AUTH_SECRET"):
        log.warning("AUTH_SECRET unset — using a secret derived from TEAM_PASSCODE/API_KEY")
