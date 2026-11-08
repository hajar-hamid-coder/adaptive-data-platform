"""REST connector tests: pagination, retry/backoff, Retry-After, rate limit (respx only)."""

from __future__ import annotations

import httpx
import pytest
import respx

from adp.connectors.rest import Pagination, fetch_all
from adp.errors import IngestionError

URL = "https://api.example.test/items"


def _kw(sleeps: list[float], **extra):
    return {"sleep": sleeps.append, **extra}


@respx.mock
def test_page_pagination_world_bank_format() -> None:
    def body(p: int) -> list:
        return [
            {"page": p, "pages": 3, "per_page": 2, "total": 6},
            [{"id": f"{p}a", "v": p}, {"id": f"{p}b", "v": None, "x": {"k": 1}}],
        ]

    route = respx.get(URL).mock(
        side_effect=lambda req: httpx.Response(200, json=body(int(req.url.params["page"])))
    )
    pg = Pagination("page", size=2, total_pages=lambda b: b[0]["pages"], size_param="per_page")
    t = fetch_all(URL, pagination=pg, records=lambda b: b[1], **_kw([]))
    assert route.call_count == 3
    assert t.columns == ["id", "v", "x"]
    assert len(t.rows) == 6
    assert t.rows[1] == ["1b", None, '{"k": 1}']
    assert t.rows[0] == ["1a", "1", None]


@respx.mock
def test_page_stops_on_empty_page() -> None:
    pages = {1: [{"a": 1}], 2: [{"a": 2}], 3: []}
    respx.get(URL).mock(
        side_effect=lambda req: httpx.Response(
            200, json={"data": pages[int(req.url.params["page"])]}
        )
    )
    t = fetch_all(URL, pagination=Pagination("page"), records="data", **_kw([]))
    assert t.rows == [["1"], ["2"]]


@respx.mock
def test_offset_pagination() -> None:
    data = [{"i": i} for i in range(5)]
    route = respx.get(URL).mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "items": data[
                    int(req.url.params["offset"]) : int(req.url.params["offset"])
                    + int(req.url.params["limit"])
                ]
            },
        )
    )
    t = fetch_all(URL, pagination=Pagination("offset", size=2), records="items", **_kw([]))
    assert route.call_count == 3
    assert [r[0] for r in t.rows] == ["0", "1", "2", "3", "4"]


@respx.mock
def test_cursor_pagination() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        c = req.url.params.get("cursor")
        out = {
            None: {"results": [{"i": 1}], "meta": {"next": "c2"}},
            "c2": {"results": [{"i": 2}], "meta": {"next": "c3"}},
            "c3": {"results": [{"i": 3}], "meta": {"next": None}},
        }
        return httpx.Response(200, json=out[c])

    route = respx.get(URL).mock(side_effect=handler)
    t = fetch_all(
        URL, pagination=Pagination("cursor", next_cursor="meta.next"), records="results", **_kw([])
    )
    assert route.call_count == 3
    assert t.rows == [["1"], ["2"], ["3"]]


@respx.mock
def test_retry_on_503_then_success() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"data": [{"a": 1}]}),
        ]
    )
    sleeps: list[float] = []
    t = fetch_all(
        URL,
        pagination=Pagination("offset", size=10),
        records="data",
        max_retries=3,
        **_kw(sleeps),
    )
    assert route.call_count == 3
    assert t.rows == [["1"]]
    assert len(sleeps) == 2


@respx.mock
def test_retry_on_transport_error() -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"data": []})]
    )
    fetch_all(URL, pagination=Pagination("offset"), records="data", **_kw([]))
    assert route.call_count == 2


@respx.mock
def test_429_respects_retry_after() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"data": [{"a": 1}]}),
        ]
    )
    sleeps: list[float] = []
    fetch_all(URL, pagination=Pagination("offset", size=10), records="data", **_kw(sleeps))
    assert route.call_count == 2
    assert sleeps == [7.0]


@respx.mock
def test_404_no_retry() -> None:
    route = respx.get(URL, params={"token": "s3cret"}).mock(return_value=httpx.Response(404))
    with pytest.raises(IngestionError) as ei:
        fetch_all(
            URL,
            params={"token": "s3cret"},
            pagination=Pagination("offset"),
            records="data",
            **_kw([]),
        )
    assert route.call_count == 1
    assert "404" in str(ei.value)
    assert "s3cret" not in str(ei.value)


@respx.mock
def test_retries_exhausted() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(IngestionError, match="503"):
        fetch_all(URL, pagination=Pagination("offset"), records="data", max_retries=2, **_kw([]))
    assert route.call_count == 3


@respx.mock
def test_rate_limit_spaces_requests() -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"a": 1}]}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    sleeps: list[float] = []
    fetch_all(
        URL,
        pagination=Pagination("page"),
        records="data",
        rate_limit_per_s=2.0,
        clock=lambda: 100.0,
        **_kw(sleeps),
    )
    assert sleeps == [pytest.approx(0.5)]


@respx.mock
def test_headers_sent_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"data": []}))
    fetch_all(
        URL,
        headers={"Authorization": "Bearer topsecret"},
        pagination=Pagination("offset"),
        records="data",
        **_kw([]),
    )
    assert route.calls[0].request.headers["Authorization"] == "Bearer topsecret"
    assert "topsecret" not in capsys.readouterr().out
