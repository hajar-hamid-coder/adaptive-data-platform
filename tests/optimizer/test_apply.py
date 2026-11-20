"""Human-approved application (P6-05): additive, idempotent, reversible, equivalent, fresh."""

import hashlib
import json
import threading
from pathlib import Path

import pytest

from adp.bronze.store import LocalStore
from adp.cli import main
from adp.errors import OptimizerError
from adp.gold.views import build_product, latest_gold
from adp.optimizer import apply as ap
from adp.optimizer.apply import active, journal, revert, same_rows, serve_indicator, serve_product

from .conftest import PRODUCTS, promote

MV, KS = "mv-regional_share-0123abcd", "ks-population_by_region-0123abcd"


def _pages(store: LocalStore, kind: str) -> list[tuple[object, ...]]:
    out = []
    for off in (0, 50, 1499, 2990, 5000):
        if kind == "mv":
            a, b = (serve_indicator(store, "regional_share", 50, off, use_opt=o) for o in (0, 1))
        else:
            m, k = latest_gold(store, "population_by_region")  # type: ignore[misc]
            a, b = (serve_product(store, "population_by_region", m, k, 50, off, use_opt=o)
                    for o in (0, 1))  # fmt: skip
        assert a[:3] == b[:3]  # same columns, rows, total
        out.append((a[3], b[3]))
    return out


def _sources(store: LocalStore) -> dict[str, bytes]:
    return {k: store.get(k) for k in store.list() if not k.startswith("gold_opt/")}


def test_apply_is_additive_idempotent_reversible(store: LocalStore) -> None:
    before = _sources(store)
    res = ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "dashboard")
    assert res.status == "applied" and res.base.startswith("gold_opt/indicators/regional_share/")
    assert _sources(store) == before  # Gold/Silver untouched
    assert MV in active(store) and active(store)[MV].by == "alice"
    assert _pages(store, "mv") == [(False, True)] * 5  # identical rows, optimised path used
    again = ap.apply(store, MV, "materialize_indicator", "regional_share", "bob", "again")
    assert again.status == "noop" and len(journal(store)) == 1
    assert revert(store, MV, "alice", "rollback") is True
    assert revert(store, MV, "alice", "rollback") is False  # no-op
    assert _pages(store, "mv") == [(False, False)] * 5
    assert (
        ap.apply(store, MV, "materialize_indicator", "regional_share", "a", "r").status == "applied"
    )
    assert [e.action for e in journal(store)] == ["apply", "revert", "apply"]


def test_stale_artefact_is_ignored_then_refreshed(store: LocalStore) -> None:
    ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "dashboard")
    promote(store, "20240201", regions=60, years=51, seed=8)  # new batch: new inputs
    cols, rows, total, used = serve_indicator(store, "regional_share", 5000, 0)
    assert used is False and total == 3060  # source served, never stale rows
    res = ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "new batch")
    assert res.status == "refreshed" and len(journal(store)) == 1
    assert serve_indicator(store, "regional_share", 5000, 0)[1:] == (rows, total, True)


def test_keyset_pagination_equivalent_and_stale_on_new_gold(store: LocalStore) -> None:
    res = ap.apply(store, KS, "keyset_pagination", "population_by_region", "alice", "deep pages")
    assert res.base.startswith("gold_opt/products/population_by_region/")
    assert _pages(store, "ks") == [(False, True)] * 5
    gold = latest_gold(store, "population_by_region")
    assert gold is not None
    m = json.loads(store.get(f"{res.base}/manifest.json"))
    assert m["rows"] == 3000 and m["source_fp"] == gold[0]["sha256"]
    promote(store, "20240201", regions=60, years=51, seed=8)
    build_product(PRODUCTS["population_by_region"], store, store)  # new Gold version
    new = latest_gold(store, "population_by_region")
    assert new is not None and new[0]["sha256"] != gold[0]["sha256"]
    cols, rows, total, used = serve_product(store, "population_by_region", *new, 50, 3000)
    assert used is False and total == 3060 and len(rows) == 50


def test_kill_switch(store: LocalStore, monkeypatch: pytest.MonkeyPatch) -> None:
    ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "dashboard")
    monkeypatch.setenv("ADP_OPTIMIZER_DISABLED", "1")
    assert serve_indicator(store, "regional_share", 10, 0)[3] is False


@pytest.mark.parametrize(("by", "reason"), [("", "r"), ("a", ""), ("  ", "r"), ("a", "x\ny")])
def test_by_and_reason_are_mandatory(store: LocalStore, by: str, reason: str) -> None:
    with pytest.raises(OptimizerError):
        ap.apply(store, MV, "materialize_indicator", "regional_share", by, reason)
    with pytest.raises(OptimizerError):
        revert(store, MV, by, reason)
    assert journal(store) == []


def test_invalid_id_and_cli_requires_by_reason(store: LocalStore) -> None:
    with pytest.raises(OptimizerError):
        ap.apply(store, "ks-regional_share-0123abcd", "materialize_indicator", "regional_share",
                 "a", "r")  # fmt: skip
    with pytest.raises(SystemExit):
        main(["optimize", "apply", MV, "--reason", "r", "--gold-dir", str(store.root)])
    with pytest.raises(SystemExit):
        main(["optimize", "revert", MV, "--by", "a", "--gold-dir", str(store.root)])


def test_non_equivalent_results_refuse_activation(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ap, "same_rows", lambda a, b: False)
    with pytest.raises(OptimizerError, match="non équivalents"):
        ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "x")
    assert active(store) == {}


def test_same_rows() -> None:
    assert same_rows([(1, "a", 0.1 + 0.2)], [(1, "a", 0.3)])
    assert same_rows([(float("nan"),)], [(float("nan"),)])
    assert not same_rows([(1,)], [(2,)]) and not same_rows([(1,)], []) and not same_rows(
        [(0.3,)], [(0.31,)])  # fmt: skip


def test_concurrent_applies_converge(store: LocalStore) -> None:
    errors: list[Exception] = []

    def go() -> None:
        try:
            ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "race")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and list(active(store)) == [MV]
    [mk] = [k for k in store.list("gold_opt/indicators/") if k.endswith("manifest.json")]
    m = json.loads(store.get(mk))
    assert (
        m["sha256"]
        == hashlib.sha256(store.get(mk.replace("manifest.json", "data.parquet"))).hexdigest()
    )


def test_tmp_files_not_left(store: LocalStore, tmp_path: Path) -> None:
    ap.apply(store, MV, "materialize_indicator", "regional_share", "alice", "x")
    assert not [p for p in (tmp_path / "store").rglob(".*.tmp")]
