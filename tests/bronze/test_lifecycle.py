import uuid
from datetime import date
from pathlib import Path

import pytest

from adp.bronze.lifecycle import (
    RetentionPolicy,
    apply_archive,
    load_retention,
    plan_archive,
)
from adp.bronze.store import LocalStore, S3Store
from adp.bronze.writer import write_bronze
from adp.cli import main
from adp.connectors.files import RawTable
from adp.errors import ConfigError, IngestionError

TODAY = date(2026, 4, 1)  # default cutoff (90 d) = 2026-01-01
POLICY = RetentionPolicy.model_validate(
    {"default": {"retention_days": 90}, "sources": {"gdp": {"retention_days": 365}}}
)


def _batch(store, source, day, n=1):
    t = RawTable(["id"], [[str(n)]])
    return write_bronze(t, source=source, run_id="r", store=store, ingestion_date=day)


def _snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "s")
    _batch(s, "crm", date(2025, 12, 31), 1)  # old
    _batch(s, "crm", date(2026, 1, 1), 2)  # exactly at cutoff: kept
    _batch(s, "crm", date(2026, 3, 30), 3)  # recent
    _batch(s, "gdp", date(2025, 12, 1), 4)  # old for default, kept with 365 d
    return s


def test_old_archived_recent_intact(store):
    old = _batch(store, "crm", date(2025, 12, 31), 1)
    moves = plan_archive(store, POLICY, TODAY)
    res = apply_archive(store, moves)
    assert res.moved == 2
    arch = f"_archive/crm/ingestion_date=2025-12-31/{old.batch_hash}"
    assert store.exists(arch + ".parquet") and store.exists(arch + ".manifest.json")
    assert not store.exists(old.parquet_path)
    live = [k for k in store.list() if not k.startswith("_archive/")]
    assert len(live) == 6  # 3 batches x 2 files still in place (crm x2, gdp x1)


def test_source_override(store):
    assert {m.source for m in plan_archive(store, POLICY, TODAY)} == {"crm"}
    short = RetentionPolicy.model_validate({"sources": {"gdp": {"retention_days": 30}}})
    assert "gdp" in {m.source for m in plan_archive(store, short, TODAY)}


def test_parquet_and_manifest_together_manifest_last(store):
    moves = plan_archive(store, POLICY, TODAY)
    assert [m.src.rsplit(".", 1)[-1] for m in moves] == ["parquet", "json"]
    assert moves[0].batch == moves[1].batch


def test_dry_run_changes_nothing_and_matches_real(store, tmp_path, capsys):
    before = _snapshot(store.root)
    cfg = tmp_path / "r.yaml"
    cfg.write_text("default: {retention_days: 90}\nsources: {gdp: {retention_days: 365}}\n")
    args = ["bronze", "archive", "--config", str(cfg), "--store-root", str(store.root)]
    args += ["--today", "2026-04-01"]
    assert main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert _snapshot(store.root) == before
    plan = plan_archive(store, POLICY, TODAY)
    assert "1 lot(s), 2 fichier(s)" in out
    assert all(m.dst in out for m in plan)
    assert main(args) == 0
    after = set(_snapshot(store.root))
    assert after - set(before) == {m.dst for m in plan}
    assert set(before) - after == {m.src for m in plan}


def test_replay_idempotent(store):
    moves = plan_archive(store, POLICY, TODAY)
    apply_archive(store, moves)
    snap = _snapshot(store.root)
    assert plan_archive(store, POLICY, TODAY) == []  # archive prefix never re-archived
    assert apply_archive(store, moves).moved == 0
    assert _snapshot(store.root) == snap


def test_resume_after_crash_before_manifest(store):
    moves = plan_archive(store, POLICY, TODAY)
    store.move(moves[0].src, moves[0].dst)  # crash: parquet moved, manifest still at source
    rest = plan_archive(store, POLICY, TODAY)
    assert [m.src for m in rest] == [moves[1].src]
    assert apply_archive(store, rest).moved == 1


def test_source_and_destination_exist(store):
    m = plan_archive(store, POLICY, TODAY)[0]
    store.put_if_absent(m.dst, store.get(m.src))  # copy done, delete not
    res = apply_archive(store, [m])
    assert res.moved == 1 and not store.exists(m.src) and store.exists(m.dst)


def test_destination_differs_loses_nothing(store):
    m = plan_archive(store, POLICY, TODAY)[0]
    store.put_if_absent(m.dst, b"other")
    with pytest.raises(IngestionError):
        apply_archive(store, [m])
    assert store.exists(m.src)


def test_rejects_travel_with_batch(tmp_path):
    s = LocalStore(tmp_path)
    base = "crm/ingestion_date=2020-01-01/abc"
    for suffix in (".rejects.jsonl", ".rejects.manifest.json", ".parquet", ".manifest.json"):
        s.put_if_absent(base + suffix, b"x")
    moves = plan_archive(s, POLICY, TODAY)
    assert len(moves) == 4 and moves[-1].src.endswith(".manifest.json")
    apply_archive(s, moves)
    assert s.list("crm/") == []
    assert len(s.list("_archive/crm/")) == 4


def test_store_list_ignores_temp_and_prefix(tmp_path):
    s = LocalStore(tmp_path)
    s.put_if_absent("a/x", b"1")
    s.put_if_absent("b/y", b"1")
    (tmp_path / "a" / ".x.tmp").write_bytes(b"")
    assert s.list("a/") == ["a/x"] and s.list() == ["a/x", "b/y"]


@pytest.mark.parametrize(
    "text, needle",
    [
        ("default: {retention_days: 0}\n", "default.retention_days"),
        ("default: {retention_days: 5, foo: 1}\n", "default.foo"),
        ("sources: {x: {}}\n", "sources.x.retention_days"),
        ("- a\n", "mapping"),
        ("a: [\n", "cannot read YAML"),
    ],
)
def test_invalid_config(tmp_path, text, needle):
    p = tmp_path / "r.yaml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=needle) as e:
        load_retention(p)
    assert str(p) in str(e.value)


def test_shipped_config_loads():
    pol = load_retention(Path("config/retention.yaml"))
    assert pol.retention_for("world_bank_gdp") == 365 and pol.retention_for("x") == 90


def test_cli_bad_config_exit_code(tmp_path, capsys):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("default: {retention_days: -1}\n")
    code = main(["bronze", "archive", "--config", str(cfg), "--store-root", str(tmp_path)])
    assert code != 0 and "Erreur" in capsys.readouterr().err


def test_cli_bad_date(tmp_path, capsys):
    code = main(["bronze", "archive", "--store-root", str(tmp_path), "--today", "nope"])
    assert code != 0


def test_cli_summary_by_source(store, capsys):
    code = main(["bronze", "archive", "--dry-run", "--config", "config/retention.yaml",
                 "--store-root", str(store.root), "--today", "2026-04-01"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "crm : 1 lot(s), 2 fichier(s)" in out and "SIMULATION" in out


@pytest.mark.slow
def test_s3_list_and_move_on_minio():
    store = S3Store("bronze")
    try:
        store.exists("probe")
    except IngestionError as e:
        pytest.skip(f"MinIO unreachable: {e}")
    p = f"_test_{uuid.uuid4().hex[:8]}"
    a, b, c = f"{p}/src/a.bin", f"{p}/dst/a.bin", f"{p}/dst/c.bin"
    try:
        assert store.put_if_absent(a, b"hello")
        assert store.list(p) == [a]
        store.move(a, b)
        assert store.list(p) == [b] and store.get(b) == b"hello"
        # destination existe avec un contenu différent : refus, source conservée
        assert store.put_if_absent(a, b"other")
        with pytest.raises(IngestionError):
            store.move(a, b)
        assert store.exists(a) and store.get(b) == b"hello"
        # destination identique : seule la source est supprimée
        assert store.put_if_absent(c, b"same")
        assert store.put_if_absent(f"{p}/src/c.bin", b"same")
        store.move(f"{p}/src/c.bin", c)
        assert not store.exists(f"{p}/src/c.bin") and store.get(c) == b"same"
    finally:
        for k in store.list(p):
            store.delete(k)
