"""Dynamic quarantine (P4-04): row/batch isolation, release, replay, journal, CLI."""

import json
import threading
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from structlog.testing import capture_logs

from adp.bronze.store import LocalStore
from adp.cli import main
from adp.errors import ConfigError, QualityError
from adp.quality.quarantine import (
    QuarantineConfig,
    QuarantineStore,
    decide,
    load_quarantine_config,
)
from adp.quality.rules import Rule, evaluate
from adp.quality.score import score_batch

ROOT = Path(__file__).resolve().parents[2]
RULE = TypeAdapter(Rule)
COLS = ["region", "year", "population_total", "country_code"]
SENTINEL = "ZZ_SENTINEL_VALUE"


def mk(rid: str, **kw: object) -> Rule:
    return RULE.validate_python({"id": rid, **kw})


RULES = [
    mk("region_not_null", type="not_null", column="region"),
    mk("population_non_negative", type="range", column="population_total", min=0),
    mk("country_code_iso3", type="regex", column="country_code", pattern="^[A-Z]{3}$",
       severity="warn"),
]  # fmt: skip


def batch(n: int = 100, bad: int = 0, warn_bad: int = 0) -> list[list[object]]:
    """`bad` rows with a negative population (error), `warn_bad` rows with a bad code (warn)."""
    return [
        [f"r{i}", 2000 + i, -1 if i < bad else 1000 + i, "xx" if i < warn_bad else "MAR"]
        for i in range(n)
    ]


def qs(tmp_path: Path) -> QuarantineStore:
    return QuarantineStore(LocalStore(tmp_path / "q"))


def apply(store: QuarantineStore, rows, rules=RULES, batch_id="b1", **kw):
    rep = evaluate(rules, COLS, rows)
    return store.apply("population", batch_id, COLS, rows, rep, run_id="run1", **kw)


# --- validation test of the backlog ---------------------------------------------------------


def test_5pct_invalid_rows_are_quarantined_95pct_pass(tmp_path: Path) -> None:
    store = qs(tmp_path)
    rows = batch(100, bad=5)
    out = apply(store, rows)
    assert out.decision.level == "row"
    assert out.decision.alert  # 5 % is in the 5-20 % band: row quarantine + alert
    assert len(out.passed) == 95 and out.passed == rows[5:]
    assert out.quarantined == 5
    st_ = store.state("population", "b1")
    assert st_.counts == {"quarantined": 5, "released": 0, "replayed": 0}
    assert [r.row for r in st_.rows] == [0, 1, 2, 3, 4]
    assert all(r.reasons == [("population_non_negative", "error")] for r in st_.rows)
    assert st_.rows[0].values == rows[0]  # rows are kept in the store


def test_low_failure_rate_quarantines_rows_without_alert(tmp_path: Path) -> None:
    out = apply(qs(tmp_path), batch(100, bad=2))
    assert out.decision.level == "row" and not out.decision.alert
    assert len(out.passed) == 98


def test_batch_over_critical_rate_is_isolated_entirely(tmp_path: Path) -> None:
    store = qs(tmp_path)
    out = apply(store, batch(100, bad=20))
    assert out.decision.level == "batch"
    assert out.passed == [] and out.quarantined == 100
    st_ = store.state("population", "b1")
    assert st_.level == "batch" and st_.counts["quarantined"] == 100
    assert "failed_rate" in st_.batch_reasons[0]


def test_batch_under_critical_score_is_isolated(tmp_path: Path) -> None:
    rows = batch(100, bad=1)
    rep = evaluate(RULES, COLS, rows)
    low = score_batch(COLS, rows, rep, RULES).model_copy(update={"score": 30.0})
    out = qs(tmp_path).apply("population", "b1", COLS, rows, rep, score=low, run_id="r")
    assert out.decision.level == "batch" and out.passed == []
    assert any("score" in r for r in out.decision.batch_reasons)


def test_failed_batch_level_error_rule_isolates_batch() -> None:
    rules = [mk("fresh", type="freshness", column="year", max_age_days=1)]
    rep = evaluate(rules, ["year"], [[date(2000, 1, 1)]], as_of=date(2026, 1, 1))
    d = decide(rep, QuarantineConfig())
    assert d.level == "batch" and d.batch_reasons == ["rule:fresh"]


def test_warn_does_not_quarantine(tmp_path: Path) -> None:
    out = apply(qs(tmp_path), batch(100, warn_bad=50))
    assert out.decision.level == "none" and len(out.passed) == 100
    assert out.quarantined == 0
    assert not (tmp_path / "q").exists() or not list((tmp_path / "q").rglob("*.json"))


def test_replay_after_rule_correction_passes_without_duplicate(tmp_path: Path) -> None:
    store = qs(tmp_path)
    rows = batch(100, bad=5)
    apply(store, rows)
    fixed = [RULES[0], mk("population_non_negative", type="range", column="population_total",
                          min=-10)]  # fmt: skip
    res = store.replay("population", "b1", fixed, by="analyst")
    assert res.passed == [(i, rows[i]) for i in range(5)] and res.still_quarantined == 0
    assert store.state("population", "b1").counts == {
        "quarantined": 0, "released": 0, "replayed": 5}  # fmt: skip
    again = store.replay("population", "b1", fixed, by="analyst")
    assert again.passed == [] and len(store.released_rows("population", "b1")[1]) == 5


def test_replay_after_data_correction_only_passes_fixed_rows(tmp_path: Path) -> None:
    store = qs(tmp_path)
    rows = batch(100, bad=5)
    apply(store, rows)
    corrected = {0: ["r0", 2000, 5, "MAR"], 1: ["r1", 2001, -3, "MAR"]}
    res = store.replay("population", "b1", RULES, by="analyst", corrections=corrected)
    assert res.passed == [(0, ["r0", 2000, 5, "MAR"])] and res.still_quarantined == 4
    cols, released = store.released_rows("population", "b1")
    assert cols == COLS and released == [(0, ["r0", 2000, 5, "MAR"])]
    with pytest.raises(QualityError):  # row 0 is no longer quarantined
        store.replay("population", "b1", RULES, by="a", corrections={0: rows[0]})
    with pytest.raises(QualityError):  # wrong width
        store.replay("population", "b1", RULES, by="a", corrections={2: ["x"]})


def test_isolated_batch_replays_entirely_after_correction(tmp_path: Path) -> None:
    store = qs(tmp_path)
    rows = batch(100, bad=30)
    apply(store, rows)
    assert store.replay("population", "b1", RULES, by="a").passed == []  # still >= 20 %
    fixed = {i: rows[i][:2] + [i] + rows[i][3:] for i in range(30)}
    res = store.replay("population", "b1", RULES, by="a", corrections=fixed)
    assert len(res.passed) == 100 and res.still_quarantined == 0


def test_release_requires_author_and_reason_and_is_idempotent(tmp_path: Path) -> None:
    store = qs(tmp_path)
    apply(store, batch(100, bad=25))
    for by, reason in (("", "ok"), ("a", ""), ("a\nb", "ok")):
        with pytest.raises(QualityError):
            store.release("population", "b1", by=by, reason=reason)
    assert store.release("population", "b1", by="ops", reason="faux positif", rows=[3]) == [3]
    assert store.release("population", "b1", by="ops", reason="lot accepté") == [
        i for i in range(100) if i != 3]  # fmt: skip
    assert store.release("population", "b1", by="ops", reason="encore") == []
    st_ = store.state("population", "b1")
    assert st_.status == "released" and st_.counts["released"] == 100
    with pytest.raises(QualityError):
        store.release("population", "b1", by="ops", reason="x", rows=[999])


def test_quarantine_is_idempotent_and_never_rewritten(tmp_path: Path) -> None:
    store = qs(tmp_path)
    rows = batch(100, bad=5)
    apply(store, rows)
    again = apply(store, rows)  # same batch replayed: no-op
    assert again.quarantined == 5 and len(again.passed) == 95
    with pytest.raises(QualityError):
        apply(store, batch(100, bad=6))  # same batch id, different content


def test_journal_is_append_only(tmp_path: Path) -> None:
    store = qs(tmp_path)
    apply(store, batch(100, bad=5))
    store.release("population", "b1", by="ops", reason="r", rows=[0])
    first = (tmp_path / "q/population/b1/events/000000.json").read_bytes()
    store.release("population", "b1", by="ops", reason="r", rows=[1])
    events = sorted(p.name for p in (tmp_path / "q/population/b1/events").iterdir())
    assert events == ["000000.json", "000001.json"]
    assert (tmp_path / "q/population/b1/events/000000.json").read_bytes() == first
    ev = json.loads(first)
    assert ev["action"] == "release" and ev["by"] == "ops" and ev["rows"] == [0]


def test_concurrent_releases_never_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "q"
    apply(QuarantineStore(LocalStore(root)), batch(100, bad=10))
    got: list[list[int]] = []

    def worker() -> None:
        got.append(QuarantineStore(LocalStore(root)).release("population", "b1", by="o",
                                                             reason="r"))  # fmt: skip

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = sorted(i for g in got for i in g)
    assert flat == list(range(10))


@pytest.mark.parametrize("bad_id", ["../x", "a/b", "", ".hidden", "a..b", "x" * 200, "a b"])
def test_identifiers_are_validated(tmp_path: Path, bad_id: str) -> None:
    store = qs(tmp_path)
    with pytest.raises(QualityError):
        apply(store, batch(10, bad=1), batch_id=bad_id)
    with pytest.raises(QualityError):
        store.state("../population", "b1")


def test_unknown_batch_raises(tmp_path: Path) -> None:
    with pytest.raises(QualityError):
        qs(tmp_path).state("population", "nope")


def test_logs_carry_counts_and_rule_ids_never_values(tmp_path: Path) -> None:
    store = qs(tmp_path)
    rows = batch(100, bad=5)
    for r in rows[:5]:
        r[0] = SENTINEL
    with capture_logs() as logs:
        apply(store, rows)
        store.replay("population", "b1", RULES, by="a", corrections={0: ["ok", 1, 1, "MAR"]})
        store.release("population", "b1", by="ops", reason="r")
    assert SENTINEL not in json.dumps(logs, default=str)
    q = next(e for e in logs if e["event"] == "quarantine_applied")
    assert q["run_id"] == "run1" and q["by_rule"] == {"population_non_negative": 5}


def test_values_roundtrip_dates_and_types(tmp_path: Path) -> None:
    store = qs(tmp_path)
    cols = ["d", "t", "f", "b"]
    from datetime import datetime

    rows = [[date(2020, 1, 2), datetime(2020, 1, 2, 3, 4), 1.5, True], [None, None, None, None]]
    rules = [mk("d_nn", type="not_null", column="d")]
    rep = evaluate(rules, cols, rows)
    store.apply("population", "b2", cols, rows, rep, run_id="r")
    assert [r.values for r in store.state("population", "b2").rows] == rows  # 50 %: batch
    store.release("population", "b2", by="o", reason="r")
    assert store.released_rows("population", "b2")[1] == list(enumerate(rows))


# --- config ---------------------------------------------------------------------------------


def test_project_config_loads() -> None:
    cfg = load_quarantine_config(ROOT / "config/quality/quarantine.yaml")
    assert cfg.alert_rate == 0.05 and cfg.batch_rate == 0.20


def test_invalid_config_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "q.yaml"
    for body in ("alert_rate: 0.5\nbatch_rate: 0.2\n", "batch_rate: 2\n", "unknown: 1\n", "- x"):
        p.write_text(body)
        with pytest.raises(ConfigError):
            load_quarantine_config(p)


# --- property -------------------------------------------------------------------------------


@settings(max_examples=60, derandomize=True, database=None, deadline=None)
@given(st.lists(st.booleans(), min_size=1, max_size=60))
def test_split_is_a_partition(bads: list[bool]) -> None:
    rows = [["r", 2000, -1 if b else 1, "MAR"] for b in bads]
    rep = evaluate(RULES, COLS, rows)
    d = decide(rep, QuarantineConfig())
    rate = sum(bads) / len(bads)
    assert d.level == ("none" if rate == 0 else "batch" if rate >= 0.2 else "row")
    if d.level == "row":
        assert set(d.quarantine_rows) == {i for i, b in enumerate(bads) if b}


# --- CLI ------------------------------------------------------------------------------------


def test_cli_list_release_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "q"
    store = QuarantineStore(LocalStore(root))
    rows = batch(100, bad=5)
    apply(store, rows)
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n  - {id: population_non_negative, type: range, column: population_total,"
        " min: -5}\n"
    )
    base = ["--store-root", str(root)]
    assert main(["quarantine", "list", "population", *base]) == 0
    assert "b1" in capsys.readouterr().out
    assert main(["quarantine", "release", "population", "b1", "--by", "ops", "--reason",
                 "faux positif", "--rows", "0", *base]) == 0  # fmt: skip
    assert "1 ligne(s)" in capsys.readouterr().out
    assert main(["quarantine", "replay", "population", "b1", "--by", "ops", "--rules",
                 str(rules), *base]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "4 ligne(s)" in out and SENTINEL not in out
    assert store.state("population", "b1").counts == {
        "quarantined": 0, "released": 1, "replayed": 4}  # fmt: skip
    assert main(["quarantine", "release", "population", "../x", "--by", "o", "--reason", "r",
                 *base]) == 2  # fmt: skip


def test_cli_release_requires_by(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["quarantine", "release", "population", "b1", "--reason", "x",
              "--store-root", str(tmp_path)])  # fmt: skip
