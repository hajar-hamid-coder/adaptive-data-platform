"""API wiring: injectable dependencies, API-key auth and uniform pagination."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from pydantic import BaseModel

from adp.bronze.store import ObjectStore
from adp.catalog.model import Catalog
from adp.gold.contract import DataProduct
from adp.mapping.decision import ReviewQueue
from adp.observability.audit import AuditStore
from adp.optimizer.usage import QueryShape, UsageLog
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
    catalog: Callable[[], Catalog] | None = None  # None: contracts only (partial lineage)
    audit: AuditStore | None = None  # None: no audit journal (empty /audit, bare /metrics)
    usage: UsageLog | None = None  # None: analytical requests are not journaled (P6-05)
    compaction_stores: tuple[ObjectStore, ...] = ()  # Bronze/Silver stores (P6-06 metrics)
    review: ReviewQueue | None = None  # None: /review answers 503 (P6-07)


def note_usage(request: Request, shape: QueryShape, rows: int, total: int, ncols: int) -> None:
    """Hand the query SHAPE (no value) to the request middleware, which journals it."""
    request.state.usage = (shape, rows, total, ncols)


def get_deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


DepsT = Annotated[Deps, Depends(get_deps)]


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
