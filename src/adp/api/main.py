"""FastAPI application factory (P5-03): read-only API over data products, schemas, quality, runs.

Decisions:
- `create_app(settings, deps)`: no I/O at import; `deps` injectable (tests), default built from
  the environment (stores, registry, products).
- Auth: header `X-API-Key`, keys from `ADP_API_KEYS` hashed in memory (SHA-256), constant-time
  comparison, no configured key = every protected route answers 401. `/health` stays public.
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
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from adp.api.deps import Deps, key_digests, require_key, require_scrape_key
from adp.api.routers import audit, catalog, health, metrics, products, quality, runs, schemas
from adp.catalog.runtime import default_catalog
from adp.config import Settings, get_settings
from adp.errors import AdpError
from adp.gold.contract import load_products
from adp.logging import get_logger
from adp.observability.audit import AuditStore
from adp.observability.metrics import Metrics
from adp.orchestration.promote_asset import build_gold_store, build_quarantine_store
from adp.quality.quarantine import QuarantineStore
from adp.quality.score import QualityScoreStore
from adp.schema.registry import SchemaRegistry, make_engine

log = get_logger(__name__)

TAGS = [
    {"name": "health", "description": "Santé du service (public) / liveness"},
    {"name": "products", "description": "Data products Gold / Gold data products"},
    {"name": "schemas", "description": "Registre de schémas / schema registry"},
    {"name": "quality", "description": "Qualité et quarantaine / quality and quarantine"},
    {"name": "runs", "description": "Exécutions / runs"},
    {"name": "audit", "description": "Journal d'audit des runs / run audit journal"},
    {"name": "observability", "description": "Métriques Prometheus / Prometheus metrics"},
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
    )


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
    app.state.key_digests = key_digests(settings)
    audit_store = app.state.deps.audit
    app.state.metrics = Metrics(audit_store.engine if audit_store else None)

    origins = [o.strip() for o in settings.adp_api_cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["GET"],
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
            # path only: no query string, no headers (API key)
            log.info("api_request", method=request.method, path=request.url.path, status=status,
                     ms=round((time.perf_counter() - start) * 1000, 1))  # fmt: skip
            structlog.contextvars.unbind_contextvars("request_id")

    @app.exception_handler(AdpError)
    async def domain_error(request: Request, exc: AdpError) -> JSONResponse:
        log.error("api_domain_error", error=type(exc).__name__, path=request.url.path)
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    app.include_router(health.router)
    app.include_router(metrics.router, dependencies=[Depends(require_scrape_key)])
    protected = [Depends(require_key)]
    for mod in (products, schemas, quality, runs, audit, catalog):
        app.include_router(mod.router, dependencies=protected)
    return app
