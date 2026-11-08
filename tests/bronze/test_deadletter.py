import json
from datetime import date

import duckdb
import pytest

from adp.bronze.deadletter import RetryPolicy, ingest_with_deadletter
from adp.bronze.store import LocalStore
from adp.errors import IngestionError

D = date(2026, 1, 2)


def _csv(tmp_path):
    lines = ["id,name"] + [f"{i},n{i}" for i in range(1, 101)]
    lines[10] = "10"  # file line 11: too few fields
    lines[50] = "50,a,b,c"  # file line 51: too many
    lines[90] = "90;;;;x,y,z"  # file line 91
    p = tmp_path / "in.csv"
    p.write_text("\n".join(lines) + "\n")
    return p


def _go(p, store, **kw):
    kw.setdefault("retry", RetryPolicy(sleep=lambda s: None))
    return ingest_with_deadletter(p, source="crm", run_id="r1", store=store, ingestion_date=D, **kw)


def _count(root, suffix):
    return sorted(f for f in root.rglob("*") if f.name.endswith(suffix))


def test_three_bad_of_hundred(tmp_path):
    store = LocalStore(tmp_path / "s")
    s = _go(_csv(tmp_path), store)
    assert (s.rows_ok, s.rows_rejected) == (97, 3)
    assert s.by_reason == {"wrong_field_count": 3}
    n = duckdb.sql(f"SELECT count(*) FROM read_parquet('{tmp_path / 's' / s.bronze.parquet_path}')")
    assert n.fetchall()[0][0] == 97
    lines = (tmp_path / "s" / s.rejects_path).read_text().splitlines()
    rej = [json.loads(x) for x in lines]
    assert [r["line_number"] for r in rej] == [11, 51, 91]
    assert [r["raw_line"] for r in rej] == ["10", "50,a,b,c", "90;;;;x,y,z"]
    assert all(r["reason"] == "wrong_field_count" and r["source"] == "crm" for r in rej)
    assert rej[0]["run_id"] == "r1" and rej[0]["rejected_at"]
    mf = json.loads(next(iter(_count(tmp_path / "s", ".rejects.manifest.json"))).read_text())
    assert mf["count"] == 3 and mf["by_reason"] == {"wrong_field_count": 3}


def test_replay_no_duplicates(tmp_path):
    store, p = LocalStore(tmp_path / "s"), _csv(tmp_path)
    _go(p, store)
    before = sorted(f.name for f in (tmp_path / "s").rglob("*") if f.is_file())
    s = _go(p, store)
    assert s.bronze.skipped
    assert sorted(f.name for f in (tmp_path / "s").rglob("*") if f.is_file()) == before
    assert len(_count(tmp_path / "s", ".rejects.jsonl")) == 1


def test_transient_store_error_retried(tmp_path):
    class Flaky(LocalStore):
        fails = 2

        def put_if_absent(self, key, data):
            if self.fails:
                self.fails -= 1
                raise OSError("boom")
            return super().put_if_absent(key, data)

    sleeps: list[float] = []
    s = _go(_csv(tmp_path), Flaky(tmp_path / "s"), retry=RetryPolicy(sleep=sleeps.append))
    assert s.rows_ok == 97 and len(sleeps) == 2


def test_retry_gives_up(tmp_path):
    class Down(LocalStore):
        def put_if_absent(self, key, data):
            raise OSError("down")

    with pytest.raises(OSError):
        _go(_csv(tmp_path), Down(tmp_path / "s"))


def test_corrupt_data_not_retried(tmp_path):
    sleeps: list[float] = []
    s = _go(_csv(tmp_path), LocalStore(tmp_path / "s"), retry=RetryPolicy(sleep=sleeps.append))
    assert s.rows_rejected == 3 and sleeps == []


def test_threshold_raises_but_rejects_written(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("a,b\n1,2\n3\n4,5\n6\n")
    store = LocalStore(tmp_path / "s")
    with pytest.raises(IngestionError, match="2/4"):
        _go(p, store)
    assert len(_count(tmp_path / "s", ".rejects.jsonl")) == 1
    assert _count(tmp_path / "s", ".parquet") == []
    s = _go(p, LocalStore(tmp_path / "s2"), max_reject_rate=0.6)
    assert (s.rows_ok, s.rows_rejected) == (2, 2)


def test_invariant_ok_plus_rejected_equals_read(tmp_path):
    s = _go(_csv(tmp_path), LocalStore(tmp_path / "s"))
    assert s.rows_ok + s.rows_rejected == 100


def test_jsonl_invalid_line(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text('{"a": 1}\n{"a": 2\n{"a": 3}\n[1]\n' + '{"a": 4}\n' * 16)
    s = _go(p, LocalStore(tmp_path / "s"), max_reject_rate=0.5)
    assert (s.rows_ok, s.rows_rejected) == (18, 2)
    assert s.by_reason == {"invalid_json": 1, "not_an_object": 1}
    rej = [json.loads(x) for x in (tmp_path / "s" / s.rejects_path).read_text().splitlines()]
    assert [(r["line_number"], r["raw_line"]) for r in rej] == [(2, '{"a": 2'), (4, "[1]")]


def test_logs_have_no_row_values(tmp_path, capsys):
    _go(_csv(tmp_path), LocalStore(tmp_path / "s"))
    out = capsys.readouterr()
    assert "a,b,c" not in out.out + out.err
