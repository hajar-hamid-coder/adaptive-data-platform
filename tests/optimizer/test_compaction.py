"""P6-06 compaction: 200 small Parquet -> N targets, same rows (hash), no duplicate for readers,
and the destructive-adjacent guard rails (dry-run, idempotence, crash, concurrency, scope)."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from adp.bronze.store import LocalStore
from adp.errors import OptimizerError
from adp.optimizer import compaction as cp
from adp.optimizer.compaction import (
    CompactionConfig,
    compact,
    latest_reports,
    live_keys,
    plan_compaction,
    referenced_keys,
    run_layers,
)

TODAY = date(2024, 6, 1)
NOW = datetime(2024, 6, 1, tzinfo=UTC)
PART = "src/schema_v1/ingestion_date=2024-01-01"
ROWS_PER_FILE = 5


def parquet(path: Path, i: int, extra: str | None = None) -> bytes:
    con = duckdb.connect()
    col = f", 'x' AS {extra}" if extra else ""
    con.execute(
        f"COPY (SELECT {i * ROWS_PER_FILE} + r AS id, 'v' || lpad((({i}*7+r)%1000)::VARCHAR, 4, "
        f"'0') AS v{col} FROM range({ROWS_PER_FILE}) t(r)) TO '{path}' (FORMAT PARQUET)"
    )
    con.close()
    return path.read_bytes()


def make_files(store: LocalStore, tmp: Path, n: int, part: str = PART, start: int = 0) -> list[str]:
    keys = []
    for i in range(start, start + n):
        k = f"{part}/{i:04d}.parquet"
        store.put_if_absent(k, parquet(tmp / "f.parquet", i))
        store.put_if_absent(k.removesuffix(".parquet") + ".manifest.json", b"{}")
        keys.append(k)
    return keys


def cfg(store: LocalStore, per: int = 8) -> CompactionConfig:
    sizes = [len(store.get(k)) for k in store.list() if k.endswith(".parquet")]
    return CompactionConfig(small_bytes=2**20, target_bytes=per * max(sizes), min_age_days=7)


def rows_of(store: LocalStore, keys: list[str], tmp: Path) -> list[tuple]:
    files = []
    for i, k in enumerate(k for k in keys if k.endswith(".parquet")):
        f = tmp / f"r{i}.parquet"
        f.write_bytes(store.get(k))
        files.append(str(f))
    con = duckdb.connect()
    out = con.execute("SELECT * FROM read_parquet(?) ORDER BY id", [files]).fetchall()
    con.close()
    return out


@pytest.fixture
def cs(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "s")


def test_200_small_files_to_n_targets_same_rows_no_dup(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 200)
    before = rows_of(cs, cs.list(), tmp_path)
    c = cfg(cs, per=8)
    plan = plan_compaction(cs, "", c, today=TODAY)
    n = math.ceil(200 / 8)
    assert len(plan) == n and sum(len(g.sources) for g in plan) == 200
    rep = compact(plan, cs, dry_run=False, config=c, layer="silver", now=NOW, by="t", reason="r")
    assert {r.status for r in rep.results} == {"compacted"}
    assert sum(r.rows_verified for r in rep.results) == 200 * ROWS_PER_FILE
    live = [k for k in live_keys(cs) if k.endswith(".parquet")]
    assert len(live) == n  # readers see only the targets
    after = rows_of(cs, live, tmp_path)
    assert after == before and len({r[0] for r in after}) == len(after)  # identical, no dup
    st = rep.stats()["src"]
    assert (st["files_before"], st["files_after"], st["rows_after"]) == (200, n, 1000)
    assert st["bytes_after"] < st["bytes_before"] and st["median_after"] > st["median_before"]
    assert "src" in rep.markdown() and json.dumps(rep.to_dict())
    assert latest_reports(cs)[0]["files_after"] == n


def test_dry_run_writes_nothing_and_is_default(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 20)
    c = cfg(cs)
    snap = cs.list()
    rep = compact(plan_compaction(cs, "", c, today=TODAY), cs, config=c)
    assert cs.list() == snap and rep.dry_run
    assert {r.status for r in rep.results} == {"planned"}


def test_apply_needs_by_and_reason(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 4)
    plan = plan_compaction(cs, "", cfg(cs), today=TODAY)
    with pytest.raises(OptimizerError):
        compact(plan, cs, dry_run=False)


def test_idempotent_second_pass_is_noop(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 30)
    c = cfg(cs)
    plan = plan_compaction(cs, "", c, today=TODAY)
    compact(plan, cs, dry_run=False, config=c, now=NOW, by="t", reason="r")
    snap = cs.list()
    assert plan_compaction(cs, "", c, today=TODAY) == []  # nothing left to merge
    again = compact(plan, cs, dry_run=False, config=c, now=NOW, by="t", reason="r")
    assert {r.status for r in again.results} == {"noop"} and cs.list() == snap


def test_incompatible_schemas_refused(cs: LocalStore, tmp_path: Path) -> None:
    keys = make_files(cs, tmp_path, 3)
    bad = f"{PART}/0003.parquet"
    cs.put_if_absent(bad, parquet(tmp_path / "b.parquet", 3, extra="other"))
    cs.put_if_absent(bad.replace(".parquet", ".manifest.json"), b"{}")
    c = cfg(cs, per=10)
    snap = cs.list()
    rep = compact(
        plan_compaction(cs, "", c, today=TODAY),
        cs,
        dry_run=False,
        config=c,
        now=NOW,
        by="t",
        reason="r",
    )
    assert [r.status for r in rep.results] == ["refused_schema"]
    assert [k for k in cs.list() if "compacted" in k or k.startswith("_compaction/records")] == []
    assert set(snap) <= set(cs.list()) and keys


def test_crash_before_commit_is_consistent_then_completes(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 10)
    before = rows_of(cs, cs.list(), tmp_path)

    class Crashy(LocalStore):
        def put_if_absent(self, key: str, data: bytes) -> bool:
            if key.startswith(cp.RECORDS):
                raise OSError("crash")
            return super().put_if_absent(key, data)

    c = cfg(cs, per=10)
    crashy = Crashy(cs.root)
    plan = plan_compaction(crashy, "", c, today=TODAY)
    rep = compact(plan, crashy, dry_run=False, config=c, now=NOW, by="t", reason="r")
    assert rep.results[0].status == "verify_failed"  # group failed, nothing committed
    assert any("compacted-" in k for k in cs.list())  # orphan target exists...
    live = live_keys(cs)
    assert not any("compacted-" in k for k in live)  # ...but invisible
    assert rows_of(cs, live, tmp_path) == before  # sources are still the truth, no duplicate
    assert cs.list(cp.LOCKS) == []  # lock released
    rep2 = compact(plan, cs, dry_run=False, config=c, now=NOW, by="t", reason="r")
    assert rep2.results[0].status == "compacted"
    live2 = live_keys(cs)
    assert rows_of(cs, live2, tmp_path) == before
    assert sum(k.endswith(".parquet") for k in live2) == 1


def test_target_with_other_content_fails_verification(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 4)
    c = cfg(cs, per=10)
    g = plan_compaction(cs, "", c, today=TODAY)[0]
    cs.put_if_absent(g.target, parquet(tmp_path / "x.parquet", 999))  # squatter
    rep = compact([g], cs, dry_run=False, config=c, now=NOW, by="t", reason="r")
    assert rep.results[0].status == "verify_failed"
    assert cs.list(cp.RECORDS) == [] and "compacted" not in "".join(live_keys(cs))


def test_concurrent_compactions_commit_once(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 40)
    before = rows_of(cs, cs.list(), tmp_path)
    c = cfg(cs)
    plan = plan_compaction(cs, "", c, today=TODAY)

    def run(_: int) -> list[str]:
        r = compact(plan, cs, dry_run=False, config=c, now=NOW, by="t", reason="r")
        return [x.status for x in r.results]

    with ThreadPoolExecutor(8) as ex:
        outs = list(ex.map(run, range(8)))
    flat = [s for o in outs for s in o]
    assert flat.count("compacted") == len(plan)  # each group merged by exactly one thread
    assert set(flat) <= {"compacted", "noop", "locked"}
    assert len(cs.list(cp.RECORDS)) == len(plan) and cs.list(cp.LOCKS) == []
    assert rows_of(cs, live_keys(cs), tmp_path) == before


def test_stale_lock_is_broken_fresh_lock_blocks(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 4)
    c = cfg(cs, per=10)
    g = plan_compaction(cs, "", c, today=TODAY)[0]
    cs.put_if_absent(f"{cp.LOCKS}{g.gid}.json", json.dumps({"at": NOW.isoformat()}).encode())
    r = compact([g], cs, dry_run=False, config=c, now=NOW, by="t", reason="r")
    assert r.results[0].status == "locked"
    later = datetime(2024, 6, 2, tzinfo=UTC)
    r = compact([g], cs, dry_run=False, config=c, now=later, by="t", reason="r")
    assert r.results[0].status == "compacted"


def test_eligibility_guards(cs: LocalStore, tmp_path: Path) -> None:
    c = CompactionConfig(small_bytes=2**20, target_bytes=2**30)
    ok = make_files(cs, tmp_path, 3)
    # no manifest (batch in flight)
    cs.put_if_absent(f"{PART}/9999.parquet", parquet(tmp_path / "n.parquet", 99))
    # too recent partition, undated partition, gold root, internal root
    make_files(cs, tmp_path, 3, part="src/schema_v1/ingestion_date=2024-05-30", start=10)
    make_files(cs, tmp_path, 3, part="src/undated", start=20)
    make_files(cs, tmp_path, 3, part="gold/p/v1/abc", start=30)
    make_files(cs, tmp_path, 3, part="_archive/src/ingestion_date=2024-01-01", start=40)
    # referenced by a Silver manifest (lineage)
    ref = make_files(cs, tmp_path, 3, part="ref/ingestion_date=2024-01-01", start=50)
    silver = LocalStore(tmp_path / "silver")
    silver.put_if_absent("a/x.manifest.json", json.dumps({"bronze_batch": ref[0]}).encode())
    assert referenced_keys(silver) == {ref[0]}
    plan = plan_compaction(cs, "", c, today=TODAY, protected=referenced_keys(silver))
    planned = {k for g in plan for k in g.sources}
    assert planned == set(ok) | set(ref[1:])
    assert not planned & {f"{PART}/9999.parquet", ref[0]}
    assert all(
        "ingestion_date=2024-01-01" in k and not k.startswith(("gold", "_archive")) for k in planned
    )


def test_prefix_validation(cs: LocalStore) -> None:
    for bad in ("../x", "/abs", "gold/p", "gold_ready/s", "gold_opt/", "_compaction/", "a/../b"):
        with pytest.raises(OptimizerError):
            plan_compaction(cs, bad, CompactionConfig(), today=TODAY)


def test_large_files_and_outputs_not_replanned(cs: LocalStore, tmp_path: Path) -> None:
    make_files(cs, tmp_path, 6)
    c = CompactionConfig(small_bytes=1, target_bytes=2**30)  # nothing is "small"
    assert plan_compaction(cs, "", c, today=TODAY) == []


def test_kill_switch_and_config_disable(
    cs: LocalStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_files(cs, tmp_path, 4)
    c = cfg(cs, per=10)
    plan = plan_compaction(cs, "", c, today=TODAY)
    snap = cs.list()
    monkeypatch.setenv("ADP_COMPACTION_DISABLED", "1")
    r = compact(plan, cs, dry_run=False, config=c, by="t", reason="r")
    assert r.results[0].status == "disabled" and cs.list() == snap


def test_run_layers_protects_and_reports(cs: LocalStore, tmp_path: Path) -> None:
    keys = make_files(cs, tmp_path, 6)
    gold = LocalStore(tmp_path / "gold")
    gold.put_if_absent(
        "gold_ready/src/b/manifest.json", json.dumps({"silver_path": keys[0]}).encode()
    )
    c = cfg(cs, per=10)
    [rep] = run_layers({"silver": cs}, [cs, gold], c, apply=True, today=TODAY, by="t", reason="r")
    sources = {k for r in rep.results for k in [r.gid]}
    assert sources and cs.exists(keys[0])
    rec = json.loads(cs.get(cs.list(cp.RECORDS)[0]))
    assert keys[0] not in rec["sources"] and len(rec["sources"]) == 5
