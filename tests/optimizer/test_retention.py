"""P6-06 retention: deletions only after grace, bounded, journaled, never of referenced data."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from adp.bronze.lifecycle import RetentionPolicy, plan_archive
from adp.bronze.store import LocalStore
from adp.errors import IngestionError, OptimizerError
from adp.optimizer import compaction as cp
from adp.optimizer.compaction import CompactionConfig, compact, live_keys, plan_compaction
from adp.optimizer.retention import (
    JOURNAL,
    apply_deletions,
    plan_gold_opt,
    plan_quarantine,
    plan_superseded,
    run_retention,
)
from tests.optimizer.test_compaction import NOW, PART, TODAY, make_files, rows_of

NO_REF: frozenset[str] = frozenset()


def policy(**kw: object) -> RetentionPolicy:
    sources = kw.pop("sources", {})
    return RetentionPolicy.model_validate({"default": kw, "sources": sources})


@pytest.fixture
def compacted(tmp_path: Path) -> tuple[LocalStore, list[str]]:
    st = LocalStore(tmp_path / "s")
    keys = make_files(st, tmp_path, 20)
    c = CompactionConfig(small_bytes=2**20, target_bytes=2**30)
    compact(plan_compaction(st, "", c, today=TODAY), st, dry_run=False, config=c, now=NOW,
            by="t", reason="r")  # fmt: skip
    return st, keys


def test_not_deleted_before_grace_deleted_after(
    compacted: tuple[LocalStore, list[str]], tmp_path: Path
) -> None:
    st, keys = compacted
    p = policy(compaction_grace_days=7)
    assert plan_superseded(st, p, NOW + timedelta(days=6), NO_REF) == []
    late = NOW + timedelta(days=8)
    plan = plan_superseded(st, p, late, NO_REF)
    assert {d.key for d in plan} == set(keys)
    before = rows_of(st, live_keys(st), tmp_path)
    res = apply_deletions(st, plan, lambda: plan_superseded(st, p, late, NO_REF), p, apply=True,
                          by="me", reason="cleanup", now=late)  # fmt: skip
    assert (res.status, res.deleted) == ("applied", 20)
    assert not any(st.exists(k) for k in keys)
    assert all(st.exists(k.replace(".parquet", ".manifest.json")) for k in keys)  # tombstones
    assert rows_of(st, live_keys(st), tmp_path) == before  # readers unaffected
    entries = [json.loads(st.get(k)) for k in st.list(JOURNAL)]
    assert sorted(e["key"] for e in entries) == sorted(keys)
    assert all(e["by"] == "me" and e["rule"] and "values" not in e for e in entries)


def test_dry_run_default_deletes_nothing(compacted: tuple[LocalStore, list[str]]) -> None:
    st, _ = compacted
    p = policy(compaction_grace_days=0)
    snap = st.list()
    plan = plan_superseded(st, p, NOW, NO_REF)
    res = apply_deletions(st, plan, lambda: plan, p)
    assert res.status == "dry_run" and res.planned == 20 and st.list() == snap
    with pytest.raises(OptimizerError):
        apply_deletions(st, plan, lambda: plan, p, apply=True)  # no by/reason


def test_cap_refuses_whole_pass_unless_confirmed(compacted: tuple[LocalStore, list[str]]) -> None:
    st, keys = compacted
    p = policy(compaction_grace_days=0, max_deletions=5)
    plan = plan_superseded(st, p, NOW, NO_REF)
    res = apply_deletions(st, plan, lambda: plan, p, apply=True, by="a", reason="b")
    assert res.status == "refused_cap" and all(st.exists(k) for k in keys)
    res = apply_deletions(st, plan, lambda: plan, p, apply=True, by="a", reason="b",
                          confirm_over_cap=True)  # fmt: skip
    assert res.deleted == 20


def test_fraction_guard(compacted: tuple[LocalStore, list[str]]) -> None:
    st, keys = compacted
    p = policy(compaction_grace_days=0, max_fraction=0.5)
    plan = plan_superseded(st, p, NOW, NO_REF)
    res = apply_deletions(st, plan, lambda: plan, p, apply=True, by="a", reason="b",
                          scope_total=20)  # fmt: skip
    assert res.status == "refused_fraction" and all(st.exists(k) for k in keys)


def test_kill_switch_config_and_hold(
    compacted: tuple[LocalStore, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    st, keys = compacted
    p = policy(compaction_grace_days=0)
    plan = plan_superseded(st, p, NOW, NO_REF)
    monkeypatch.setenv("ADP_RETENTION_DISABLED", "1")
    assert apply_deletions(st, plan, lambda: plan, p, apply=True, by="a",
                           reason="b").status == "disabled"  # fmt: skip
    monkeypatch.delenv("ADP_RETENTION_DISABLED")
    off = policy(compaction_grace_days=0, enabled=False)
    assert apply_deletions(st, plan, lambda: plan, off, apply=True, by="a",
                           reason="b").status == "disabled"  # fmt: skip
    assert all(st.exists(k) for k in keys)
    held = policy(compaction_grace_days=0, sources={"src": {"retention_days": 30, "hold": True}})
    assert plan_superseded(st, held, NOW, NO_REF) == []
    assert plan_archive(st, held, TODAY + timedelta(days=9999)) == []  # archive honours hold


def test_referenced_and_tampered_sources_are_kept(
    compacted: tuple[LocalStore, list[str]], tmp_path: Path
) -> None:
    st, keys = compacted
    p = policy(compaction_grace_days=0)
    plan = plan_superseded(st, p, NOW, frozenset({keys[0]}))
    assert keys[0] not in {d.key for d in plan} and len(plan) == 19
    rec_key = st.list(cp.RECORDS)[0]
    rec = json.loads(st.get(rec_key))
    (tmp_path / "s" / rec["target"]).write_bytes(b"corrupt")  # target no longer intact
    assert plan_superseded(st, p, NOW, NO_REF) == []


def test_replan_drops_objects_no_longer_expired(compacted: tuple[LocalStore, list[str]]) -> None:
    st, keys = compacted
    p = policy(compaction_grace_days=0)
    plan = plan_superseded(st, p, NOW, NO_REF)
    res = apply_deletions(st, plan, lambda: plan[:2], p, apply=True, by="a", reason="b")
    assert (res.deleted, res.skipped) == (2, 18)
    assert sum(st.exists(k) for k in keys) == 18


def test_run_retention_end_to_end_with_protection(
    compacted: tuple[LocalStore, list[str]], tmp_path: Path
) -> None:
    st, keys = compacted
    gold = LocalStore(tmp_path / "g")
    ref = json.dumps({"silver_path": keys[3]}).encode()
    gold.put_if_absent("gold_ready/src/b/manifest.json", ref)
    p = policy(compaction_grace_days=0)
    out = run_retention({"silver": st}, [st, gold], p, now=NOW, apply=True, by="a", reason="b")
    assert out["silver"].deleted == 19 and st.exists(keys[3])
    again = run_retention({"silver": st}, [st, gold], p, now=NOW, apply=True, by="a", reason="b")
    assert again["silver"].planned == 0  # the protected source is never planned


def test_gold_opt_expiry_keeps_journal_and_served(tmp_path: Path) -> None:
    st = LocalStore(tmp_path / "g")
    old, new = NOW - timedelta(days=40), NOW - timedelta(days=2)
    for name, at in (("old", old), ("new", new)):
        base = f"gold_opt/products/p/ks-aaaaaaaa/{name}"
        st.put_if_absent(f"{base}/data.parquet", b"d")
        st.put_if_absent(f"{base}/manifest.json", json.dumps({"built_at": at.isoformat()}).encode())
    ev = {"action": "apply", "rec_id": "ks-aaaaaaaa", "type": "keyset_pagination", "target": "p",
          "by": "a", "reason": "b", "at": old.isoformat()}  # fmt: skip
    st.put_if_absent("gold_opt/_journal/e.json", json.dumps(ev).encode())
    p = policy(gold_opt_days=30)
    plan = plan_gold_opt(st, p, NOW)
    assert [d.key for d in plan] == [
        "gold_opt/products/p/ks-aaaaaaaa/old/manifest.json",
        "gold_opt/products/p/ks-aaaaaaaa/old/data.parquet",
    ]  # manifest first; journal and recent artefacts untouched
    out = run_retention({"gold": st}, [], p, now=NOW, apply=True, by="a", reason="b")
    assert out["gold"].deleted == 2 and st.exists("gold_opt/_journal/e.json")
    assert st.exists("gold_opt/products/p/ks-aaaaaaaa/new/data.parquet")


def _quarantine(st: LocalStore, source: str, batch: str, at: datetime, released: bool) -> None:
    rec = {"source": source, "batch_id": batch, "level": "row", "alert": False,
           "batch_reasons": [], "columns": ["a"], "created_at": at.isoformat(),
           "rows": [{"row": 0, "values": [1], "reasons": [["r", "error"]]}]}  # fmt: skip
    st.put_if_absent(f"{source}/{batch}/record.json", json.dumps(rec).encode())
    if released:
        ev = {"action": "release", "rows": [0], "at": at.isoformat(), "seq": 0}
        st.put_if_absent(f"{source}/{batch}/events/000000.json", json.dumps(ev).encode())


def test_quarantine_only_resolved_expired_not_held(tmp_path: Path) -> None:
    st = LocalStore(tmp_path / "q")
    old = NOW - timedelta(days=400)
    _quarantine(st, "src", "done", old, True)
    _quarantine(st, "src", "open", old, False)  # still quarantined: kept
    _quarantine(st, "src", "fresh", NOW - timedelta(days=1), True)
    _quarantine(st, "held", "done", old, True)
    p = policy(quarantine_days=180, sources={"held": {"retention_days": 30, "hold": True}})
    plan = plan_quarantine(st, p, NOW)
    assert [d.key for d in plan] == ["src/done/record.json", "src/done/events/000000.json"]
    out = run_retention({"quarantine": st}, [], p, now=NOW, apply=True, by="a", reason="b",
                        confirm_over_cap=True)  # fmt: skip
    assert out["quarantine"].deleted == 2 and st.exists("src/open/record.json")
    assert st.exists("held/done/record.json") and st.exists("src/fresh/record.json")


def test_store_delete_is_single_object_and_contained(tmp_path: Path) -> None:
    st = LocalStore(tmp_path / "s")
    st.put_if_absent("a/b/c.txt", b"x")
    st.put_if_absent("a/b/d.txt", b"y")
    st.delete("a/b/c.txt")
    st.delete("a/b/c.txt")  # missing: no-op
    assert st.list() == ["a/b/d.txt"]
    for bad in ("../outside", "", "a/.."):
        with pytest.raises(IngestionError):
            st.delete(bad)
    assert st.list() == ["a/b/d.txt"]  # no prefix/dir deletion
    assert PART  # imported fixture constant used by sibling tests
