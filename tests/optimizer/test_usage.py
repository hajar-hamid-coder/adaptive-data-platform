"""Usage journal (P6-05): shape only, sampling, rotation, concurrency, pure aggregation."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine

from adp.api.deps import Deps
from adp.api.main import create_app
from adp.bronze.store import LocalStore
from adp.config import Settings
from adp.errors import ConfigError
from adp.gold.contract import load_products
from adp.optimizer.usage import (
    QueryShape,
    UsageConfig,
    UsageEvent,
    UsageLog,
    aggregate,
    depth_bucket,
    load_usage_config,
    percentile,
)
from adp.quality.quarantine import QuarantineStore
from adp.quality.score import QualityScoreStore
from adp.schema.registry import SchemaRegistry, metadata

KEY = "usage-test-key"
SENTINEL = "ZZ_USAGE_SENTINEL"


def ev(target: str = "regional_share", ms: float = 10.0, op: str = "indicator", status: int = 200,
       depth: str = "0", rate: float = 1.0) -> UsageEvent:  # fmt: skip
    route = "/indicators/{name}" if op == "indicator" else "/products/{name}/data"
    return UsageEvent(
        ts=datetime(2026, 1, 1, tzinfo=UTC), route=route, status=status, ms=ms, rows=50,
        total=1000, ncols=3, sample_rate=rate,
        shape=QueryShape(op=op, target=target, depth=depth, limit=50),  # type: ignore[arg-type]
    )  # fmt: skip


def test_depth_bucket_and_percentile() -> None:
    assert [depth_bucket(o) for o in (0, 1, 999, 1000, 9999, 10_000, 99_999, 100_000, 10**9)] == [
        "0", "<1k", "<1k", "<10k", "<10k", "<100k", "<100k", ">=100k", ">=100k"]  # fmt: skip
    assert percentile([], 95) == 0.0
    vals = [float(i) for i in range(1, 101)]
    assert (percentile(vals, 50), percentile(vals, 95), percentile(vals, 99)) == (50, 95, 99)
    assert percentile([3.0], 95) == 3.0


def test_aggregate_frequency_latency_share_and_weights() -> None:
    events = [ev(ms=float(i)) for i in range(1, 21)]  # 20 indicator calls
    events += [ev("population_by_region", 1.0, "page", depth=">=100k", rate=0.5)] * 5  # 10 weighted
    events += [ev(ms=999.0, status=500)]  # errors are ignored
    stats = aggregate(events)
    assert [s.target for s in stats] == ["regional_share", "population_by_region"]
    top, page = stats
    assert top.observations == 20 and top.count == 20 and top.p50 == 10 and top.p95 == 19
    assert top.total_ms == 210 and page.count == 10 and page.total_ms == 10
    assert top.time_share == round(210 / 220, 4) and page.deep_share == {">=100k": 1.0}
    assert top.limit == 50 and top.max_total == 1000 and top.ncols == 3
    assert aggregate([]) == []


def test_shape_refuses_free_text() -> None:
    with pytest.raises(ValidationError):
        QueryShape(op="page", target="a b; drop")
    with pytest.raises(ValidationError):
        QueryShape(op="page", target="ok", filters=("year = 2020",))


def test_sampling_rotation_retention_and_bad_lines(tmp_path: Path) -> None:
    log = UsageLog(UsageConfig(dir=tmp_path, sample_rate=0.5, max_bytes=1000, max_files=2),
                   rand=iter([0.1, 0.9] * 100).__next__)  # fmt: skip
    kept = [log.record(ev()) for _ in range(40)]
    assert kept.count(True) == 20
    assert len(log.rotated()) <= 2  # retention cap
    (tmp_path / "usage.jsonl").open("a").write("not json\n{}\n")
    events = log.events()
    assert events and all(e.sample_rate == 0.5 for e in events) and len(events) < 20


def test_record_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert UsageLog(UsageConfig(dir=blocker / "sub")).record(ev()) is False
    assert UsageLog(UsageConfig(dir=tmp_path, sample_rate=0)).record(ev()) is False


def test_concurrent_writers_never_interleave(tmp_path: Path) -> None:
    log = UsageLog(UsageConfig(dir=tmp_path))
    threads = [threading.Thread(target=lambda: [log.record(ev()) for _ in range(200)])
               for _ in range(8)]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "usage.jsonl").read_text().splitlines()
    assert len(lines) == 1600 and len(log.events()) == 1600


def test_config_loading(tmp_path: Path) -> None:
    assert load_usage_config().sample_rate == 1.0
    assert load_usage_config(tmp_path / "missing.yaml") == UsageConfig()
    bad = tmp_path / "o.yaml"
    bad.write_text("usage: {sample_rate: 3}\n")
    with pytest.raises(ConfigError):
        load_usage_config(bad)
    bad.write_text("usage: [1]\n")
    with pytest.raises(ConfigError):
        load_usage_config(bad)


def _deps(tmp_path: Path, store: LocalStore, usage: object) -> Deps:
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    metadata.create_all(engine)
    return Deps({p.name: p for p in load_products()}, store, SchemaRegistry(engine),
                QualityScoreStore(engine), QuarantineStore(LocalStore(tmp_path / "q")),
                tmp_path / "reports", usage=usage)  # type: ignore[arg-type]  # fmt: skip


def _client(deps: Deps) -> TestClient:
    s = Settings(_env_file=None, adp_api_keys=SecretStr(KEY))  # type: ignore[call-arg]
    return TestClient(create_app(s, deps))


def test_api_journals_shape_but_never_values(tmp_path: Path, store: LocalStore) -> None:
    log = UsageLog(UsageConfig(dir=tmp_path / "usage"))
    with _client(_deps(tmp_path, store, log)) as c:
        h = {"X-API-Key": KEY}
        r = c.get(f"/products/population_by_region/data?limit=7&offset=987654&x={SENTINEL}",
                  headers=h)  # fmt: skip
        assert r.status_code == 200 and r.json()["items"] == []
        assert c.get("/indicators/regional_share?offset=1500", headers=h).status_code == 200
        assert c.get("/indicators/nope", headers=h).status_code == 404
        assert c.get("/products", headers=h).status_code == 200  # not analytical: not journaled
    text = (tmp_path / "usage" / "usage.jsonl").read_text()
    for forbidden in (SENTINEL, "987654", KEY, "x=", "offset"):
        assert forbidden not in text
    events = log.events()
    assert [(e.route, e.shape.op, e.shape.depth, e.shape.limit) for e in events] == [
        ("/products/{name}/data", "page", ">=100k", 7),
        ("/indicators/{name}", "indicator", "<10k", 50)]  # fmt: skip
    assert events[0].total == 3000 and events[1].rows == 50 and events[1].ncols == 4


def test_journal_failure_never_breaks_a_request(tmp_path: Path, store: LocalStore) -> None:
    class Boom:
        def record(self, e: object) -> bool:
            raise RuntimeError("disk on fire")

    with _client(_deps(tmp_path, store, Boom())) as c:
        r = c.get("/indicators/regional_share?limit=3", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and len(r.json()["items"]) == 3
