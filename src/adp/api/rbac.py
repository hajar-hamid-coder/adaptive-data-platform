"""RBAC (P6-07): roles, capability table, key parsing and the `require_role` dependency.

Decisions:
- Least privilege: a capability is granted to an explicit set of roles (table below), never by a
  string comparison in a route. `scraper` only reads `/metrics`; a plain entry is a `viewer`.
- `ADP_API_KEYS` entries (comma-separated), no secret in the repo:
    `<raw>` | `sha256:<hex>`                 legacy -> viewer, principal `key-<n>`
    `name:role:sha256:<hex>` | `name:role:<raw>`   explicit principal and role
  Any other shape (unknown role, bad name, bad digest) is IGNORED with a log that carries the
  entry position only, never its content (fail closed). Raw keys containing `:` must therefore
  use the `sha256:` form.
- Constant work per presented key (no early exit); the matched principal (name, role) is put on
  `request.state.principal`, never the key. 401 = no/unknown key, 403 = role too low; both bodies
  are generic and the log carries the principal name (if known) and capability, never the key.
"""

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from adp.logging import get_logger

log = get_logger(__name__)


class Role(StrEnum):
    VIEWER = "viewer"
    STEWARD = "steward"
    ADMIN = "admin"
    SCRAPER = "scraper"  # Prometheus: `/metrics` only


class Capability(StrEnum):
    DATA_READ = "data:read"  # products, indicators, schemas, quality, runs, catalog
    REVIEW_READ = "review:read"
    REVIEW_DECIDE = "review:decide"  # approve / reject a mapping
    AUDIT_READ = "audit:read"  # operations journal
    METRICS_READ = "metrics:read"


_V, _S, _A, _M = Role.VIEWER, Role.STEWARD, Role.ADMIN, Role.SCRAPER
CAPABILITY_ROLES: dict[Capability, frozenset[Role]] = {
    Capability.DATA_READ: frozenset({_V, _S, _A}),
    Capability.REVIEW_READ: frozenset({_V, _S, _A}),
    Capability.REVIEW_DECIDE: frozenset({_S, _A}),
    Capability.AUDIT_READ: frozenset({_S, _A}),
    Capability.METRICS_READ: frozenset({_V, _S, _A, _M}),
}


@dataclass(frozen=True)
class Principal:
    """Authenticated caller: a name and a role, never the key."""

    name: str
    role: Role


@dataclass(frozen=True)
class KeyEntry:
    digest: bytes
    principal: Principal


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ROLES = {r.value for r in Role}


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def _secret_digest(secret: str) -> bytes | None:
    if secret.startswith("sha256:"):
        hexa = secret.removeprefix("sha256:")
        return bytes.fromhex(hexa) if _HEX_RE.match(hexa) else None
    return _digest(secret) if secret else None


def parse_keys(raw: str) -> list[KeyEntry]:
    """Parse `ADP_API_KEYS`; malformed entries are skipped with a content-free log."""
    out: list[KeyEntry] = []
    for pos, item in enumerate(x.strip() for x in raw.split(",")):
        if not item:
            continue
        name, role, secret = f"key-{pos}", Role.VIEWER, item
        if ":" in item and not item.startswith("sha256:"):
            parts = item.split(":", 2)
            if len(parts) != 3 or parts[1] not in _ROLES or not _NAME_RE.match(parts[0]):
                log.warning("api_key_entry_ignored", position=pos)
                continue
            name, role, secret = parts[0], Role(parts[1]), parts[2]
        digest = _secret_digest(secret)
        if digest is None:
            log.warning("api_key_entry_ignored", position=pos)
            continue
        out.append(KeyEntry(digest, Principal(name, role)))
    return out


_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Clé API / API key")


def authenticate(request: Request, key: str | None) -> Principal | None:
    """Principal of the presented key, or None; constant work over all configured keys."""
    found: Principal | None = None
    if key:
        presented = _digest(key)
        for entry in request.app.state.key_entries:  # no early exit
            if hmac.compare_digest(presented, entry.digest):
                found = entry.principal
    return found


def require_role(
    capability: Capability, *, bearer: bool = False
) -> Callable[[Request, str | None], Principal]:
    """FastAPI dependency: 401 unauthenticated, 403 role without `capability`.

    `bearer=True` also accepts `Authorization: Bearer <key>` (Prometheus scrape blocks).
    """
    allowed = CAPABILITY_ROLES[capability]

    def dependency(request: Request, key: Annotated[str | None, Depends(_header)]) -> Principal:
        if bearer and not key:
            auth = request.headers.get("authorization", "")
            if auth[:7].lower() == "bearer ":
                key = auth[7:].strip()
        principal = authenticate(request, key)
        if principal is None:
            log.warning("api_auth_denied", status=401, capability=capability.value,
                        path=request.url.path)  # fmt: skip
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "ApiKey"})
        if principal.role not in allowed:
            log.warning("api_auth_denied", status=403, capability=capability.value,
                        principal=principal.name, path=request.url.path)  # fmt: skip
            raise HTTPException(403, "Forbidden")
        request.state.principal = principal
        return principal

    dependency.capability = capability  # type: ignore[attr-defined]  # read by the route audit test
    return dependency
