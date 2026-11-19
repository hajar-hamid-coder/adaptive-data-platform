"""API wiring: injectable dependencies, API-key auth and uniform pagination."""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from adp.bronze.store import ObjectStore
from adp.config import Settings
from adp.gold.contract import DataProduct
from adp.quality.quarantine import QuarantineStore
from adp.quality.score import QualityScoreStore
from adp.schema.registry import SchemaRegistry

# Path-parameter patterns: reject anything that could leave a store or report directory.
SOURCE_RE = r"^[a-z][a-z0-9_]{0,254}$"
PRODUCT_RE = r"^[a-z][a-z0-9_]*$"
RUN_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"


@dataclass(frozen=True)
class Deps:
    """Everything the routers read; tests inject in-memory or tmp_path implementations."""

    products: Mapping[str, DataProduct]
    gold: ObjectStore
    registry: SchemaRegistry
    scores: QualityScoreStore
    quarantine: QuarantineStore
    reports_dir: Path


def get_deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


DepsT = Annotated[Deps, Depends(get_deps)]


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def parse_keys(raw: str) -> list[bytes]:
    """Comma-separated keys -> SHA-256 digests (`sha256:<hex>` entries are taken as digests)."""
    out: list[bytes] = []
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        if item.startswith("sha256:"):
            out.append(bytes.fromhex(item.removeprefix("sha256:")))
        else:
            out.append(_digest(item))
    return out


def key_digests(settings: Settings) -> list[bytes]:
    return parse_keys(settings.adp_api_keys.get_secret_value())


_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Clé API / API key")


def require_key(request: Request, key: Annotated[str | None, Depends(_header)]) -> None:
    """401 (generic body) without a key or with an unknown one; constant-time comparison."""
    ok = False
    if key:
        presented = _digest(key)
        for known in request.app.state.key_digests:  # no early exit: constant work per key
            ok |= hmac.compare_digest(presented, known)
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "ApiKey"})


class Page(BaseModel):
    """Uniform envelope of every list endpoint."""

    items: list[Any]
    total: int
    limit: int
    offset: int


class PageParams:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200, description="Taille de page / page size")] = 50,
        offset: Annotated[int, Query(ge=0, description="Décalage / offset")] = 0,
    ) -> None:
        self.limit, self.offset = limit, offset

    def apply(self, rows: Sequence[Any]) -> Page:
        return Page(
            items=list(rows[self.offset : self.offset + self.limit]),
            total=len(rows), limit=self.limit, offset=self.offset,
        )  # fmt: skip


Paging = Annotated[PageParams, Depends()]
