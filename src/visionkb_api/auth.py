"""Bearer tokens from ``VISIONKB_API_TOKENS_FILE`` (lines ``token<TAB>scopes``)."""

from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass
from pathlib import Path

from .envelope import ApiError

SCOPES = frozenset({"read", "proposals:read", "proposals:write"})


@dataclass(frozen=True)
class Principal:
    token_id: str  # sha256 prefix of the token: identifies the caller without storing the token
    scopes: frozenset[str]


def token_id(token: str) -> str:
    return "t-" + hashlib.sha256(token.encode()).hexdigest()[:16]


def parse_tokens(text: str) -> dict[str, frozenset[str]]:
    """Parse the token file. Scopes are separated by commas or spaces; ``#`` starts a comment."""
    out: dict[str, frozenset[str]] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            raise ValueError(f"tokens file line {n}: expected token<TAB>scopes")
        tok, scopes = line.split("\t", 1)
        tok = tok.strip()
        names = frozenset(s for s in scopes.replace(",", " ").split() if s)
        unknown = names - SCOPES
        if len(tok) < 16:
            raise ValueError(f"tokens file line {n}: token shorter than 16 characters")
        if unknown:
            raise ValueError(f"tokens file line {n}: unknown scopes {sorted(unknown)}")
        out[hashlib.sha256(tok.encode()).hexdigest()] = names
    return out


class TokenStore:
    """Token file, reloaded when its mtime changes. Only token hashes are kept in memory."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._mtime: float | None = None
        self._tokens: dict[str, frozenset[str]] = {}
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            raise ApiError(503, "auth_unavailable", "token file is not readable") from None
        if mtime != self._mtime:
            with self._lock:
                try:
                    self._tokens = parse_tokens(self.path.read_text(encoding="utf-8"))
                except ValueError as exc:
                    raise ApiError(503, "auth_unavailable", f"token file is invalid: {exc}") from None
                self._mtime = mtime

    def authenticate(self, authorization: str | None, scope: str) -> Principal:
        if self.path is None:
            raise ApiError(503, "auth_unavailable", "no token file configured (VISIONKB_API_TOKENS_FILE)")
        self._load()
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ApiError(401, "unauthorized", "missing bearer token")
        token = authorization[7:].strip()
        digest = hashlib.sha256(token.encode()).hexdigest()
        scopes = None
        for known, s in self._tokens.items():
            if hmac.compare_digest(known, digest):
                scopes = s
        if scopes is None:
            raise ApiError(401, "unauthorized", "invalid bearer token")
        if scope not in scopes:
            raise ApiError(403, "forbidden", f"token lacks scope {scope!r}")
        return Principal(token_id=token_id(token), scopes=scopes)
