"""
SwasthyaSync — password hashing, tokens and a tiny in-memory rate limiter.

Passwords: stdlib hashlib.scrypt with a random 16-byte salt, stored as "scrypt$<salt_hex>$<hash_hex>".
Legacy values (plaintext or unsalted SHA-256 hex) still verify once and are upgraded in place by the caller.
Tokens: secrets.token_urlsafe(32); the role lives in the DB row, not in the token. Expire after 12 h.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_N, _R, _P = 2 ** 14, 8, 1
TOKEN_TTL_HOURS = 12


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def is_hashed(stored: str | None) -> bool:
    return bool(stored) and stored.startswith("scrypt$")


def verify_password(stored: str | None, password: str) -> tuple[bool, bool]:
    """Return (ok, needs_upgrade)."""
    if not stored or password is None:
        return False, False
    if stored.startswith("scrypt$"):
        try:
            _, salt_hex, hash_hex = stored.split("$", 2)
            dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P, dklen=32)
            return hmac.compare_digest(dk.hex(), hash_hex), False
        except Exception:
            return False, False
    sha = hashlib.sha256(password.encode()).hexdigest()
    if hmac.compare_digest(stored, sha):
        return True, True
    if hmac.compare_digest(stored, password):  # legacy plaintext
        return True, True
    return False, False


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ──────────────────────────────────────────────────────────────────────
# Rate limiting (per client IP, sliding 60 s window, single instance)
# ──────────────────────────────────────────────────────────────────────

_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, limit: int, window_seconds: int = 60):
    """FastAPI dependency: allow `limit` requests per IP per window for this bucket."""
    async def _dep(request: Request):
        key = f"{bucket}:{_client_ip(request)}"
        now = time.time()
        q = _hits[key]
        while q and q[0] < now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests — please wait a minute")
        q.append(now)
    return _dep
