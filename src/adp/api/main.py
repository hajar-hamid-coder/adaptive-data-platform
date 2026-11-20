"""FastAPI application factory (P5-03): read-only API over data products, schemas, quality, runs.

Decisions:
- `create_app(settings, deps)`: no I/O at import; `deps` injectable (tests), default built from
  the environment (stores, registry, products).
- Auth (P6-07, see `adp.api.rbac`): header `X-API-Key`, keys from `ADP_API_KEYS` hashed in memory
  (SHA-256) with a role (viewer/steward/admin/scraper), constant-time comparison, no configured
  key = every protected route 401, insufficient role 403. `/health` stays public.
- Writes: only `POST /review/{id}/approve|reject` (steward+), reusing the CLI review functions;
  H-33 is lifted for that single endpoint pair, everything else stays read-only.
- Errors: 401/404/422 generic; domain errors and unexpected ones -> generic 500, details only
  in logs. Bodies never contain secrets or raw quarantined values.
- Runs: `/runs` keeps listing the quality-score runs (shape consumed by the front); the persistent
  journal (P6-01, closes H-34) is exposed apart under `/audit/runs` and `/audit/runs/{id}/steps`.
- `/metrics` (Prometheus): same API keys, via `X-API-Key` or `Authorization: Bearer`. Labels use
  the route template, never the raw path.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from adp.api.deps import Deps
from adp.api.rbac import Capability, parse_keys, require_role
from adp.api.routers import (
    audit,
    catalog,
    health,
    indicators,
    metrics,
    products,
    quality,
    review,
    runs,
    schemas,
)
from adp.catalog.runtime import default_catalog
from adp.config import Settings, get_settings
from adp.errors import AdpError
from adp.gold.contract import load_products
from adp.logging import get_logger
from adp.mapping.decision import ReviewQueue
from adp.observability.audit import AuditStore
from adp.observability.metrics import Metrics
from adp.optimizer.usage import UsageEvent, UsageLog, load_usage_config
from adp.orchestration.ingestion import build_store
from adp.orchestration.promote_asset import build_gold_store, build_quarantine_store
from adp.orchestration.silver_chain import build_silver_store
from adp.quality.quarantine import QuarantineStore
from adp.quality.score import QualityScoreStore
from adp.schema.registry import SchemaRegistry, make_engine

log = get_logger(__name__)

TAGS = [
    {"name": "health", "description": "Santé du service (public) / liveness"},
    {"name": "products", "description": "Data products Gold / Gold data products"},
    {"name": "indicators", "description": "Indicateurs Gold / Gold indicators"},
    {"name": "schemas", "description": "Registre de schémas / schema registry"},
    {"name": "quality", "description": "Qualité et quarantaine / quality and quarantine"},
    {"name": "runs", "description": "Exécutions / runs"},
    {"name": "audit", "description": "Journal d'audit des runs / run audit journal"},
    {"name": "observability", "description": "Métriques Prometheus / Prometheus metrics"},
    {"name": "review", "description": "Revue humaine des mappings / mapping review (RBAC)"},
    {"name": "catalog", "description": "Catalogue et lignage / catalog and lineage"},
]


def default_deps(settings: Settings) -> Deps:
    engine = make_engine()
    return Deps(
        products={p.name: p for p in load_products()},
        gold=build_gold_store(),
        registry=SchemaRegistry(engine),
        scores=QualityScoreStore(engine),
        quarantine=QuarantineStore(build_quarantine_store()),
        reports_dir=Path(settings.adp_reports_dir),
        catalog=default_catalog,
        audit=AuditStore(engine),
        usage=UsageLog(load_usage_config()),
        compaction_stores=(build_store(), build_silver_store()),
        review=ReviewQueue(engine),
    )


def _journal_usage(app: FastAPI, request: Request, route: str | None, status: int,
                   ms: float) -> None:  # fmt: skip
    """Usage journal (P6-05) of analytical requests: shape only; never breaks a request."""
    usage = getattr(request.state, "usage", None)
    sink: UsageLog | None = app.state.deps.usage
    if usage is None or sink is None or route is None:
        return
    try:
        shape, rows, total, ncols = usage
        sink.record(UsageEvent(ts=datetime.now(UTC), route=route, shape=shape, status=status,
                               ms=round(ms, 3), rows=rows, total=total, ncols=ncols,
                               sample_rate=1.0))  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - journaling must never fail the request
        log.warning("usage_journal_failed", error=type(exc).__name__)


def create_app(settings: Settings | None = None, deps: Deps | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="ADP API",
        version="0.1.0",
        description="API en lecture seule de la Plateforme de Données Intelligente et Évolutive "
        "/ Read-only API of the Adaptive Data Platform.",
        openapi_tags=TAGS,
    )
    app.state.deps = deps if deps is not None else default_deps(settings)
    app.state.key_entries = parse_keys(settings.adp_api_keys.get_secret_value())
    audit_store = app.state.deps.audit
    app.state.metrics = Metrics(
        audit_store.engine if audit_store else None, app.state.deps.compaction_stores
    )

    origins = [o.strip() for o in settings.adp_api_cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key"],
        )  # fmt: skip

    @app.middleware("http")
    async def request_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            route = request.scope.get("route")
            app.state.metrics.api_request(getattr(route, "path", None), request.method, status)
            ms = (time.perf_counter() - start) * 1000
            _journal_usage(app, request, getattr(route, "path", None), status, ms)
            # path only: no query string, no headers (API key)
            log.info("api_request", method=request.method, path=request.url.path, status=status,
                     ms=round(ms, 1))  # fmt: skip
            structlog.contextvars.unbind_contextvars("request_id")

    @app.exception_handler(AdpError)
    async def domain_error(request: Request, exc: AdpError) -> JSONResponse:
        log.error("api_domain_error", error=type(exc).__name__, path=request.url.path)
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    app.include_router(health.router)
    app.include_router(
        metrics.router,
        dependencies=[Depends(require_role(Capability.METRICS_READ, bearer=True))],
    )
    data = [Depends(require_role(Capability.DATA_READ))]
    for mod in (products, indicators, schemas, quality, runs, catalog):
        app.include_router(mod.router, dependencies=data)
    app.include_router(audit.router, dependencies=[Depends(require_role(Capability.AUDIT_READ))])
    app.include_router(review.router)  # per-route capabilities: read vs decide
    return app
