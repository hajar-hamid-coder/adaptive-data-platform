"""Catalog and lineage (P5-04): a real Bronze -> Silver -> Gold chain built with repo functions."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine

from adp.api.deps import Deps
from adp.api.main import create_app
from adp.bronze.store import LocalStore
from adp.bronze.writer import write_bronze
from adp.catalog.build import Evidence, build_catalog, trace_sql
from adp.catalog.model import Catalog
from adp.catalog.openlineage import OLExport, export_openlineage
from adp.config import Settings
from adp.connectors.files import RawTable
from adp.gold.contract import load_products
from adp.gold.views import build_all
from adp.mapping.decision import ReviewQueue
from adp.mapping.dictionary import load_dictionary
from adp.mapping.llm import LLMMapper, LocalProvider
from adp.orchestration.promotion_gate import promote_batch
from adp.orchestration.silver_chain import run_silver_chain
from adp.quality.quarantine import QuarantineStore
from adp.quality.score import QualityScoreStore
from adp.schema.registry import SchemaRegistry, metadata
from adp.sources.spec import SourceSpec

CANON = Path("config/semantic/canonical.yaml")
KEY = "test-key-123"
H = {"X-API-Key": KEY}
PRODUCTS = {p.name: p for p in load_products()}

POP = RawTable(
    columns=["wilaya", "annee", "population"],
    rows=[
        ["Souss-Massa", "2020", "100"], ["Fès-Meknès", "2020", "300"],
        ["Souss-Massa", "2021", "120"], ["Fès-Meknès", "2021", "330"],
    ],
)  # fmt: skip
GDP = RawTable(
    columns=["countryiso3code", "country", "annee", "indicator", "gdp"],
    rows=[["MAR", "Maroc", "2020", "NY.GDP.MKTP.CD", "4000.0"],
          ["MAR", "Maroc", "2021", "NY.GDP.MKTP.CD", "9000.0"]],
)  # fmt: skip


def _spec(name: str, pk: list[str]) -> SourceSpec:
    return SourceSpec(name=name, type="file", uri="x.csv", format="csv",  # type: ignore[arg-type]
                      frequency="manual", primary_key=pk)  # fmt: skip


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> tuple[Catalog, Evidence, Path]:
    tmp = tmp_path_factory.mktemp("chain")
    bronze, silver, gold = (LocalStore(tmp / n) for n in ("bronze", "silver", "gold_ready"))
    out = LocalStore(tmp / "gold")
    engine = create_engine(f"sqlite:///{tmp / 'meta.db'}")
    metadata.create_all(engine)
    d = load_dictionary(CANON)
    queue = ReviewQueue(engine, d)
    for src, table, pk in (("hcp_population", POP, ["wilaya", "annee"]),
                           ("world_bank_gdp", GDP, ["countryiso3code", "annee"])):  # fmt: skip
        bm = write_bronze(table, source=src, run_id="r1", store=bronze)
        res = run_silver_chain(
            table, spec=_spec(src, pk), schema_version=1, dictionary=d,
            mapper=LLMMapper(LocalProvider(d), d), queue=queue, store=silver, run_id="r1",
            bronze_path=bm.parquet_path,
        )  # fmt: skip
        assert res.status == "written" and res.manifest
        pr = promote_batch(
            source=src, silver_path=res.manifest.parquet_path, run_id="r1", dictionary=d,
            silver_store=silver, gold_store=gold, quarantine_store=LocalStore(tmp / "q"),
            report_dir=tmp / "rep",
        )  # fmt: skip
        assert pr.status == "promoted", pr.reasons
    build_all(gold, out, list(PRODUCTS.values()))
    ev = Evidence(gold_ready=gold, silver=silver, journal=queue.journal())
    return build_catalog(PRODUCTS.values(), evidence=ev), ev, tmp


def test_gold_column_lineage_reaches_bronze(chain) -> None:  # type: ignore[no-untyped-def]
    cat, _, _ = chain
    col = cat.column("socioeconomic_summary", "population_total")
    assert col and col.lineage_confidence == "full"
    (path,) = col.lineage
    layers = [(n.layer, n.dataset, n.column) for n in path.nodes]
    assert layers == [
        ("gold", "socioeconomic_summary", "population_total"),
        ("gold", "population_by_region", "population_total"),
        ("silver", "hcp_population", "population_total"),
        ("bronze", "hcp_population", "population"),  # raw column name, via the mapping journal
    ]
    assert path.nodes[2].version == "1" and path.nodes[3].batches
    assert path.nodes[3].batches[0].startswith("hcp_population/ingestion_date=")


def test_derived_column_traced_through_cte(chain) -> None:  # type: ignore[no-untyped-def]
    cat, _, _ = chain
    col = cat.column("socioeconomic_summary", "gdp_per_capita_usd")
    assert col and col.lineage_confidence == "full"
    raws = {(p.nodes[-1].dataset, p.nodes[-1].column) for p in col.lineage}
    assert raws == {("world_bank_gdp", "gdp"), ("hcp_population", "population")}


def test_missing_evidence_is_partial_not_guessed() -> None:
    cat = build_catalog(PRODUCTS.values())
    col = cat.column("population_by_region", "region")
    assert col and col.lineage_confidence == "partial"
    assert {"no_silver_evidence", "no_mapping_decision"} <= set(col.lineage_notes)
    assert col.lineage[0].nodes[-1].column is None


def test_sql_trace_and_fallback(tmp_path: Path) -> None:
    got = trace_sql("SELECT CAST(a AS BIGINT) AS x, t.b + 1 AS y, count(*) AS n FROM src_s t")
    assert got and got["x"].leaves == {("src_s", "a")} and not got["x"].partial
    assert got["y"].leaves == {("src_s", "b")}
    assert trace_sql("SELEC nope") is None and trace_sql("SELECT 1 UNION SELECT 2") is None
    (tmp_path / "population_by_region.sql").write_text("SELECT * FROM src_hcp_population")
    cat = build_catalog([PRODUCTS["population_by_region"]], sql_dir=tmp_path)
    col = cat.column("population_by_region", "year")
    assert col and "name_fallback" in col.lineage_notes and col.lineage_confidence == "partial"
    assert col.lineage[0].nodes[1].column == "year"  # same canonical name, flagged


def test_openlineage_export_deterministic_and_valid(chain) -> None:  # type: ignore[no-untyped-def]
    cat, ev, _ = chain
    doc = export_openlineage(cat)
    OLExport.model_validate(doc)
    assert json.dumps(doc) == json.dumps(
        export_openlineage(build_catalog(reversed(list(PRODUCTS.values())), evidence=ev))
    )
    jobs = {e["job"]["name"]: e for e in doc["events"]}
    assert {"gold.socioeconomic_summary", "silver.hcp_population"} <= set(jobs)
    silver = jobs["silver.hcp_population"]["outputs"][0]["facets"]["columnLineage"]["fields"]
    assert silver["population_total"]["inputFields"] == [
        {"namespace": "adp", "name": "bronze.hcp_population", "field": "population"}
    ]
    gold = jobs["gold.socioeconomic_summary"]["outputs"][0]["facets"]["columnLineage"]["fields"]
    assert gold["gdp_per_capita_usd"]["inputFields"][0]["name"].startswith("gold.")


def test_cli_export(chain, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from adp.cli import main

    dest = tmp_path / "ol.json"
    assert main(["catalog", "export", "--output", str(dest)]) == 0
    OLExport.model_validate(json.loads(dest.read_text()))


# --- API ---------------------------------------------------------------------------------
@pytest.fixture
def client(chain, tmp_path: Path) -> TestClient:  # type: ignore[no-untyped-def]
    cat, _, _ = chain
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    metadata.create_all(engine)
    deps = Deps(
        products=PRODUCTS, gold=LocalStore(tmp_path / "g"), registry=SchemaRegistry(engine),
        scores=QualityScoreStore(engine), quarantine=QuarantineStore(LocalStore(tmp_path / "q")),
        reports_dir=tmp_path / "r", catalog=lambda: cat,
    )  # fmt: skip
    st = Settings(_env_file=None, adp_api_keys=SecretStr(KEY))  # type: ignore[call-arg]
    return TestClient(create_app(st, deps))


def test_api_catalog_products_and_owners(client: TestClient) -> None:
    assert client.get("/catalog").status_code == 401
    r = client.get("/catalog", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    row = next(p for p in body["items"] if p["name"] == "socioeconomic_summary")
    assert row["owner"] == "Équipe Analytics engineering" and row["consumers"]
    r = client.get("/catalog/products/socioeconomic_summary", headers=H)
    assert r.status_code == 200 and r.json()["columns"][0]["lineage"]
    assert client.get("/catalog/products/nope", headers=H).status_code == 404
    assert client.get("/catalog/products/..%2Fx", headers=H).status_code in (404, 422)


def test_api_lineage_endpoint(client: TestClient) -> None:
    r = client.get("/catalog/lineage", params={"product": "socioeconomic_summary",
                                               "column": "region"}, headers=H)  # fmt: skip
    assert r.status_code == 200
    nodes = r.json()["lineage"][0]["nodes"]
    assert nodes[-1]["layer"] == "bronze" and nodes[-1]["column"] == "wilaya"
    bad = {"product": "x; drop", "column": "region"}
    assert client.get("/catalog/lineage", params=bad, headers=H).status_code == 422
    unknown = {"product": "socioeconomic_summary", "column": "zzz"}
    assert client.get("/catalog/lineage", params=unknown, headers=H).status_code == 404


def test_api_without_catalog_factory_uses_contracts(tmp_path: Path, client: TestClient) -> None:
    import dataclasses

    app = client.app
    app.state.deps = dataclasses.replace(app.state.deps, catalog=None)  # type: ignore[attr-defined]
    r = client.get("/catalog", headers=H)
    assert r.status_code == 200 and r.json()["total"] == 3
