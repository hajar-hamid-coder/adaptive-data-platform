"""REST connector: paginated JSON APIs with retry, backoff and rate limiting."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from adp.connectors.files import RawTable
from adp.errors import IngestionError
from adp.logging import get_logger

log = get_logger(__name__)

Extractor = str | Callable[[Any], Any]


@dataclass(frozen=True)
class Pagination:
    """Pagination strategy: ``page`` (page number), ``offset`` (offset+limit) or ``cursor``.

    ``total_pages`` (page mode) reads the page count from a response body, e.g.
    ``lambda b: b[0]["pages"]`` for the World Bank format; ``next_cursor`` is a dotted key
    or callable returning the next cursor (None/absent ends the iteration).
    """

    kind: Literal["page", "offset", "cursor"]
    size: int = 100
    page_param: str = "page"
    size_param: str | None = None  # default: "limit" (offset) / none (page)
    offset_param: str = "offset"
    cursor_param: str = "cursor"
    start_page: int = 1
    total_pages: Callable[[Any], int | None] | None = None
    next_cursor: Extractor = "next_cursor"


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


def _dig(body: Any, path: Extractor | None) -> Any:
    if path is None:
        return body
    if callable(path):
        return path(body)
    for key in path.split("."):
        body = body.get(key) if isinstance(body, dict) else None
    return body


def _safe_url(url: str) -> str:
    u = httpx.URL(url)
    return f"{u.scheme}://{u.host}{u.path}"


def _cell(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else str(v)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def fetch_all(
    url: str,
    *,
    pagination: Pagination,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    records: Extractor | None = None,
    rate_limit_per_s: float | None = None,
    max_retries: int = 3,
    timeout: float = 30.0,
    max_pages: int = 10_000,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RawTable:
    """Fetch every page of ``url`` and return a raw string table (Bronze).

    ``headers`` must already be resolved (never logged). ``records`` is a dotted JSON key or
    a callable extracting the record list from a response body (default: body itself).
    """
    shown = _safe_url(url)
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    last_call: float | None = None

    def wait(state: Any) -> float:
        exc = state.outcome.exception()
        if isinstance(exc, _Retryable) and exc.retry_after is not None:
            return exc.retry_after
        return float(wait_exponential(multiplier=0.5, max=30)(state))

    retrying = Retrying(
        retry=retry_if_exception_type(_Retryable),
        stop=stop_after_attempt(max_retries + 1),
        wait=wait,
        sleep=sleep,
        reraise=True,
    )

    def get(query: dict[str, Any]) -> Any:
        nonlocal last_call
        if rate_limit_per_s:
            now = clock()
            if last_call is not None:
                gap = 1.0 / rate_limit_per_s - (now - last_call)
                if gap > 0:
                    sleep(gap)
            last_call = clock()
        try:
            resp = http.get(url, params=query, headers=headers)
        except httpx.TransportError as e:
            raise _Retryable(f"transport error: {type(e).__name__}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}", _retry_after(resp))
        if resp.status_code >= 400:
            raise IngestionError(f"{shown}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise IngestionError(f"{shown}: response is not valid JSON") from e

    pg = pagination
    rows_in: list[dict[str, Any]] = []
    cursor: str | None = None
    page, offset = pg.start_page, 0
    try:
        for n in range(1, max_pages + 1):
            query = dict(params or {})
            if pg.kind == "page":
                query[pg.page_param] = page
                if pg.size_param:
                    query[pg.size_param] = pg.size
            elif pg.kind == "offset":
                query[pg.offset_param] = offset
                query[pg.size_param or "limit"] = pg.size
            elif cursor is not None:
                query[pg.cursor_param] = cursor
            try:
                body = retrying(get, query)
            except _Retryable as e:
                raise IngestionError(f"{shown}: giving up after retries ({e})") from e
            recs = _dig(body, records)
            if recs is None:
                recs = []
            if not isinstance(recs, list):
                raise IngestionError(f"{shown}: records are not a JSON list")
            rows_in.extend(r if isinstance(r, dict) else {"value": r} for r in recs)
            log.info("rest_page_fetched", url=shown, page_no=n, records=len(recs))
            if not recs:
                break
            if pg.kind == "page":
                total = pg.total_pages(body) if pg.total_pages else None
                if total is not None and page - pg.start_page + 1 >= total:
                    break
                page += 1
            elif pg.kind == "offset":
                if len(recs) < pg.size:
                    break
                offset += pg.size
            else:
                nxt = _dig(body, pg.next_cursor)
                if nxt is None or nxt == "" or str(nxt) == cursor:
                    break
                cursor = str(nxt)
        else:
            raise IngestionError(f"{shown}: exceeded max_pages={max_pages}")
    finally:
        if own:
            http.close()

    columns = list(dict.fromkeys(k for r in rows_in for k in r))
    rows = [[_cell(r.get(c)) for c in columns] for r in rows_in]
    return RawTable(columns, rows, {"url": shown, "pagination": pg.kind, "records": len(rows)})
