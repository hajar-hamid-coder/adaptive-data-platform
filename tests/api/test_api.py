"""API (P5-03): auth, pagination, products/schemas/quality/runs endpoints, OpenAPI."""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, TypeAdapter
from sqlalchemy import create_engine
from structlog.testing import capture_logs

from adp.api.deps import Deps
from adp.api.main import create_app
from adp.bronze.store import LocalStore
from adp.config import Settings
from adp.connectors.files import RawTable
from adp.errors import QualityError
from adp.gold.contract import load_products
from adp.quality.quarantine import QuarantineStore
from adp.quality.rules import Rule, evaluate
from adp.quality.score import QualityScoreStore, ScoreConfig, score_batch
from adp.schema.profile import profile_table
from adp.schema.registry import SchemaChange, SchemaRegistry, metadata

KEY = "test-key-123"
H = {"X-API-Key": KEY}
RULE = TypeAdapter(Rule)
SENTINEL = "ZZ_RAW_SENTINEL"


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, adp_api_keys=SecretStr(f"{KEY},other"), **kw)  # type: ignore[call-arg]


@pytest.fixture
def deps(tmp_path: Path) -> Deps:
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    metadata.create_all(engine)
    gold = LocalStore(tmp_path / "gold")
    # a built product "population_by_region" (two builds: the newer contract version wins)
    con = duckdb.connect()
    for ver, n, built in (("1.0.0", 1, "2024-01-01"), ("1.0.0", 120, "2024-02-01")):
        f = tmp_path / f"{n}.parquet"
        con.execute(
            "COPY (SELECT 'r' || i AS region, 2000 AS year, i AS population_total "
            f"FROM range({n}) t(i)) TO '{f}' (FORMAT PARQUET)"
        )
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        base = f"gold/population_by_region/v{ver}/{sha[:12]}"
        gold.put_if_absent(f"{base}/data.parquet", f.read_bytes())
        m = {"product": "population_by_region", "contract_version": ver, "rows": n,
             "sha256": sha, "built_at": built}  # fmt: skip
        gold.put_if_absent(f"{base}/manifest.json", json.dumps(m).encode())
    con.close()
    # schema registry: v1 -> v2 with one recorded change
    reg = SchemaRegistry(engine)
    v1 = reg.register("pop", profile_table(RawTable(columns=["a"], rows=[["1"]])))
    v2 = reg.register("pop", profile_table(RawTable(columns=["a", "b"], rows=[["1", "2"]])))
    reg.record_changes("pop", v1.version, v2.version, [SchemaChange(change_type="column_added",
                       column_name="b")])  # fmt: skip
    # quality scores for 3 runs
    cols, rows = ["x"], [["1"]]
    scores = QualityScoreStore(engine)
    for i in range(3):
        sc = score_batch(cols, rows, evaluate([], cols, rows), [], config=ScoreConfig())
        scores.save("pop", f"run{i}", sc, 2)
    # quarantine: 2 bad rows, one carries a sentinel that must never leak
    rules = [RULE.validate_python({"id": "pos", "type": "range", "column": "n", "min": 0})]
    qrows = [[-1, SENTINEL], *[[5, "ok"]] * 38, [-2, SENTINEL]]  # 2/40 = 5 %: row level
    rep = evaluate(rules, ["n", "s"], qrows)
    q = QuarantineStore(LocalStore(tmp_path / "q"))
    q.apply("pop", "b1", ["n", "s"], qrows, rep, run_id="run0")
    reports = tmp_path / "reports"
    (reports / "pop").mkdir(
        parents=True
    )  # same layout as promote_batch: <dir>/<source>/<run_id>.json
    (reports / "pop" / "run0.json").write_text(
        json.dumps({"run_id": "run0", "summary": {"score": 99}})
    )
    return Deps({p.name: p for p in load_products()}, gold, reg, scores, q, reports)


@pytest.fixture
def client(deps: Deps) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(), deps)) as c:
        yield c


PROTECTED = ["/products", "/products/population_by_region", "/runs", "/schemas/pop",
             "/quality/scores", "/quality/quarantine/pop", "/quality/reports/run0"]  # fmt: skip


@pytest.mark.parametrize("path", PROTECTED)
def test_401_without_or_with_bad_key_200_with_key(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    bad = client.get(path, headers={"X-API-Key": "nope"})
    assert bad.status_code == 401 and bad.json() == {"detail": "Unauthorized"}
    assert client.get(path, headers=H).status_code == 200


def test_health_is_public_and_no_configured_key_means_401(deps: Deps) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with TestClient(create_app(s, deps)) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/runs", headers=H).status_code == 401
        assert c.get("/runs", headers={"X-API-Key": ""}).status_code == 401


def test_sha256_key_entries(deps: Deps) -> None:
    s = Settings(
        _env_file=None, adp_api_keys=SecretStr("sha256:" + hashlib.sha256(b"k1").hexdigest())
    )  # type: ignore[call-arg]  # noqa: E501
    with TestClient(create_app(s, deps)) as c:
        assert c.get("/runs", headers={"X-API-Key": "k1"}).status_code == 200
        assert c.get("/runs", headers={"X-API-Key": "k2"}).status_code == 401


def test_pagination(client: TestClient) -> None:
    r = client.get("/runs?limit=2&offset=0", headers=H).json()
    assert r["total"] == 3 and len(r["items"]) == 2 and r["limit"] == 2 and r["offset"] == 0
    r2 = client.get("/runs?limit=2&offset=2", headers=H).json()
    assert len(r2["items"]) == 1
    assert {i["run_id"] for i in r["items"] + r2["items"]} == {"run0", "run1", "run2"}
    assert client.get("/runs", headers=H).json()["limit"] == 50
    assert client.get("/runs?offset=9", headers=H).json()["items"] == []
    for q in ("limit=0", "limit=201", "offset=-1", "limit=abc"):
        assert client.get(f"/runs?{q}", headers=H).status_code == 422


def test_products(client: TestClient) -> None:
    items = client.get("/products", headers=H).json()["items"]
    assert {p["name"] for p in items} >= {"population_by_region", "gdp_indicators"}
    d = client.get("/products/population_by_region", headers=H).json()
    assert d["contract"]["sla"]["freshness_hours"] == 168
    assert d["latest_gold"]["rows"] == 120 and d["effective_criticality"]
    assert client.get("/products/gdp_indicators", headers=H).json()["latest_gold"] is None
    assert client.get("/products/unknown", headers=H).status_code == 404
    assert client.get("/products/Bad..Name", headers=H).status_code == 422


def test_product_data_paginated(client: TestClient) -> None:
    r = client.get("/products/population_by_region/data?limit=50&offset=100", headers=H).json()
    assert r["total"] == 120 and len(r["items"]) == 20
    assert set(r["items"][0]) == {"region", "year", "population_total"}
    assert client.get("/products/gdp_indicators/data", headers=H).status_code == 404


def test_schemas_and_diff(client: TestClient) -> None:
    r = client.get("/schemas/pop", headers=H).json()
    assert [v["version"] for v in r["items"]] == [1, 2] and r["total"] == 2
    d = client.get("/schemas/pop/diff?from=1&to=2", headers=H).json()
    assert d["items"] == [{"change_type": "column_added", "column_name": "b", "detail": {}}]
    assert client.get("/schemas/pop/diff?from=1&to=9", headers=H).status_code == 404
    assert client.get("/schemas/pop/diff?from=1", headers=H).status_code == 422
    assert client.get("/schemas/nope", headers=H).status_code == 404
    assert client.get("/schemas/..%2Fetc", headers=H).status_code in (404, 422)


def test_quality_scores_filters_and_reports(client: TestClient) -> None:
    assert client.get("/quality/scores?run_id=run1", headers=H).json()["total"] == 1
    assert client.get("/quality/scores?source=pop", headers=H).json()["total"] == 3
    assert client.get("/quality/scores?source=zzz", headers=H).json()["total"] == 0
    assert client.get("/quality/scores?source=A/B", headers=H).status_code == 422
    assert client.get("/quality/reports/run0", headers=H).json()["run_id"] == "run0"
    assert client.get("/quality/reports/missing", headers=H).status_code == 404
    assert client.get("/quality/reports/..%2Fmeta.db", headers=H).status_code in (404, 422)
    assert client.get("/quality/reports/.hidden", headers=H).status_code == 422


def test_quarantine_exposes_indices_and_reasons_only(client: TestClient) -> None:
    resp = client.get("/quality/quarantine/pop", headers=H)
    assert SENTINEL not in resp.text
    items = resp.json()["items"]
    assert [(i["batch_id"], i["row"]) for i in items] == [("b1", 0), ("b1", 39)]
    assert items[0]["reasons"] == [{"rule_id": "pos", "severity": "error"}]
    assert client.get("/quality/quarantine/empty", headers=H).json()["total"] == 0


def test_domain_error_is_generic_500(deps: Deps) -> None:
    class Boom:
        def batch_scores(self, *a: object) -> None:
            raise QualityError("secret internal detail /var/x")

    bad = Deps(deps.products, deps.gold, deps.registry, Boom(), deps.quarantine, deps.reports_dir)  # type: ignore[arg-type]  # noqa: E501
    with TestClient(create_app(_settings(), bad)) as c:
        r = c.get("/runs", headers=H)
        assert r.status_code == 500 and "secret" not in r.text


def test_cors_disabled_by_default_and_configurable(deps: Deps) -> None:
    origin = {"Origin": "http://localhost:5173"}
    with TestClient(create_app(_settings(), deps)) as c:
        assert "access-control-allow-origin" not in c.get("/health", headers=origin).headers
    with TestClient(create_app(_settings(adp_api_cors_origins="http://localhost:5173"), deps)) as c:
        h = c.get("/health", headers=origin).headers
        assert h["access-control-allow-origin"] == "http://localhost:5173"


def test_request_log_has_request_id_and_no_key(client: TestClient) -> None:
    with capture_logs() as logs:
        r = client.get("/runs?limit=1", headers=H)
    entry = next(e for e in logs if e["event"] == "api_request")
    assert entry["status"] == 200 and entry["path"] == "/runs"
    assert KEY not in json.dumps(logs, default=str)
    assert len(r.headers["x-request-id"]) == 32


def test_openapi_valid(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.") and spec["info"]["title"] == "ADP API"
    assert {"/health", "/products", "/products/{name}/data", "/schemas/{source}/diff",
            "/quality/scores", "/quality/reports/{run_id}", "/quality/quarantine/{source}",
            "/runs"} <= set(spec["paths"])  # fmt: skip
    schemes = spec["components"]["securitySchemes"]
    assert any(s["type"] == "apiKey" and s["name"] == "X-API-Key" and s["in"] == "header"
               for s in schemes.values())  # fmt: skip
    assert "security" in spec["paths"]["/runs"]["get"]
    assert "security" not in spec["paths"]["/health"]["get"]
    assert {t["name"] for t in spec["tags"]} >= {"products", "quality", "runs", "schemas"}
    params = {p["name"] for p in spec["paths"]["/runs"]["get"]["parameters"]}
    assert params == {"limit", "offset"}
