"""Quality score (P4-03): dimensions, weights, config, P4-01 integration, persistence."""

import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from sqlalchemy import Engine, create_engine, func, select
from structlog.testing import capture_logs

from adp.connectors.files import read_file
from adp.errors import ConfigError, QualityError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.quality.rules import Rule, evaluate, load_references, load_rules
from adp.quality.score import (
    BatchScore,
    QualityScoreStore,
    ScoreConfig,
    Weights,
    load_score_config,
    quality_score,
    score_batch,
)
from adp.schema.registry import metadata
from adp.silver.clean import clean_table, load_cleaning

ROOT = Path(__file__).resolve().parents[2]
SEM = ROOT / "config" / "semantic"
QDIR = ROOT / "config" / "quality"
DICT = load_dictionary(SEM / "canonical.yaml")
RULE = TypeAdapter(Rule)
SENTINEL = "ZZ_SENTINEL_VALUE"


def mk(rid: str, **kw: object) -> Rule:
    return RULE.validate_python({"id": rid, **kw})


def score(cols, rows, rules, cfg=None, **kw) -> BatchScore:
    rep = evaluate(rules, cols, rows, **kw)
    return score_batch(cols, rows, rep, rules, config=cfg or ScoreConfig(), as_of=kw.get("as_of"))


def col(b: BatchScore, name: str):
    return next(c for c in b.columns if c.column == name)


# --- dimensions ---------------------------------------------------------------------------------


def test_perfect_set_is_100() -> None:
    rules = [
        mk("nn", type="not_null", column="a"),
        mk("u", type="unique", columns=["a"]),
        mk("rg", type="range", column="b", min=0, max=10),
    ]
    b = score(["a", "b"], [[1, 5], [2, 6], [3, 7]], rules)
    assert b.score == 100.0 and b.status == "scored"
    assert (b.completeness, b.validity, b.uniqueness) == (100.0, 100.0, 100.0)
    assert b.freshness is None and b.rows_failed == 0  # no freshness rule: n/a, still 100
    assert col(b, "a").score == 100.0 and col(b, "b").score == 100.0
    assert col(b, "b").uniqueness is None  # not a key column


def test_ten_percent_nulls_on_required_column_gives_90() -> None:
    rows = [[None if i == 0 else i] for i in range(10)]
    b = score(["a"], rows, [mk("nn", type="not_null", column="a")])
    c = col(b, "a")
    assert c.completeness == 90.0 and c.required and b.completeness == 90.0
    assert c.score == 90.0 and b.score == 90.0 and c.rows_failed == 1 and b.rows_failed == 1


def test_optional_column_does_not_penalise_but_is_reported() -> None:
    rows = [[1, None], [2, None], [3, 1], [4, 1]]
    b = score(["a", "b"], rows, [mk("nn", type="not_null", column="a")])
    assert col(b, "b").completeness == 50.0 and not col(b, "b").required
    assert col(b, "b").score is None  # no applicable dimension -> n/a, excluded
    assert b.score == 100.0 and b.completeness == 100.0


def test_required_columns_from_config_override_rules() -> None:
    cfg = ScoreConfig(required_columns=["b"])
    rows = [[None, 1], [1, None]]
    b = score(["a", "b"], rows, [mk("nn", type="not_null", column="a")], cfg)
    assert col(b, "a").required is False and col(b, "b").required is True
    assert b.completeness == 50.0


def test_validity_exact_and_warn_weight() -> None:
    rows = [[v] for v in (1, 2, 3, 4, 5, 6, 7, 8, 99, 100)] + [[None]]
    err = mk("e", type="range", column="a", min=0, max=50)  # 2 / 10 evaluated fail
    b = score(["a"], rows, [err])
    assert col(b, "a").validity == 80.0 and b.rows_failed == 2
    # error rule (80) + warn rule (all pass = 100) at warn_weight 0.25 -> (80 + 25) / 1.25
    warn = mk("w", type="regex", column="a", pattern=r"^\d+$", severity="warn")
    b2 = score(["a"], rows, [err, warn], ScoreConfig(warn_weight=0.25))
    assert col(b2, "a").validity == pytest.approx((80 + 0.25 * 100) / 1.25)
    # a failing warn rule does not count in rows_failed, but lowers validity
    warn_bad = mk("w", type="range", column="a", min=0, max=50, severity="warn")
    b3 = score(["a"], rows, [warn_bad])
    assert col(b3, "a").validity == 80.0 and b3.rows_failed == 0 and col(b3, "a").rows_failed == 0
    b4 = score(["a"], rows, [err, warn_bad], ScoreConfig(warn_weight=0.0))
    assert col(b4, "a").validity == 80.0


def test_null_not_penalised_by_validity() -> None:
    rows = [[1], [None], [None], [2]]
    b = score(["a"], rows, [mk("r", type="range", column="a", min=0, max=5)])
    assert col(b, "a").validity == 100.0


def test_regex_and_reference_validity() -> None:
    rows = [["MAR", "x"], ["MA", "y"], ["MAR", "z"], ["MAR", "x"]]
    rules = [
        mk("re", type="regex", column="a", pattern="^[A-Z]{3}$"),
        mk("rf", type="reference", column="b", values=["x", "y"]),
    ]
    b = score(["a", "b"], rows, rules)
    assert col(b, "a").validity == 75.0 and col(b, "b").validity == 75.0


def test_uniqueness_on_key_columns() -> None:
    rows = [[1, "x", 9], [1, "y", 9], [1, "x", 9], [2, "x", 9]]  # 1 repeated key of 4
    b = score(["a", "b", "c"], rows, [mk("u", type="unique", columns=["a", "b"])])
    assert col(b, "a").uniqueness == 75.0 and col(b, "b").uniqueness == 75.0
    assert col(b, "c").uniqueness is None and b.uniqueness == 75.0
    assert b.rows_failed == 1


def test_freshness_decay() -> None:
    rule = mk("f", type="freshness", column="d", max_age_days=10)

    def fresh(age: int) -> float | None:
        d = date(2026, 6, 30)
        rows = [[date.fromordinal(d.toordinal() - age)]]
        return col(score(["d"], rows, [rule], as_of=d), "d").freshness

    assert fresh(0) == 100.0 and fresh(10) == 100.0
    assert fresh(15) == 50.0 and fresh(20) == 0.0 and fresh(40) == 0.0
    zero = mk("f", type="freshness", column="d", max_age_days=0)
    old = score(["d"], [[date(2026, 6, 29)]], [zero], as_of=date(2026, 6, 30))
    assert col(old, "d").freshness == 0.0
    nodate = score(["d"], [[None]], [rule], as_of=date(2026, 6, 30))
    assert col(nodate, "d").freshness == 0.0  # evaluated, no usable date


def test_freshness_na_without_as_of_or_rule() -> None:
    rule = mk("f", type="freshness", column="d", max_age_days=10)
    b = score(["d"], [[date(2026, 6, 30)]], [rule])  # as_of absent -> skipped
    assert b.freshness is None and b.score is None and b.status == "n/a"
    assert score(["d"], [[date(2026, 6, 30)]], []).freshness is None


# --- weights ------------------------------------------------------------------------------------


def _mixed():
    rows = [[None if i == 0 else i, 100 if i < 2 else 1] for i in range(10)]
    rules = [
        mk("nn", type="not_null", column="a"),
        mk("rg", type="range", column="b", min=0, max=10),
    ]
    return ["a", "b"], rows, rules


def test_weights_are_respected() -> None:
    cols, rows, rules = _mixed()
    # a: completeness 90 -> column score 90 ; b: validity 80 -> 80 (one dimension each)
    assert score(cols, rows, rules).score == 85.0
    cfg = ScoreConfig(column_weights={"a": 3, "b": 1})
    assert score(cols, rows, rules, cfg).score == pytest.approx((3 * 90 + 80) / 4)
    # dimension weights act inside a column: a has completeness + uniqueness
    r2 = [*rules, mk("u", type="unique", columns=["a"])]
    rows2 = [[1, 1], [1, 1], [2, 1], [3, 1]]  # a complete (100), uniqueness 75
    w = Weights(completeness=3, validity=1, uniqueness=1, freshness=0)
    got = col(score(["a", "b"], rows2, r2, ScoreConfig(weights=w)), "a").score
    assert got == pytest.approx((3 * 100 + 1 * 75) / 4)


def test_zero_weight_and_renormalisation() -> None:
    cols, rows, rules = _mixed()
    only_validity = Weights(completeness=0, validity=1, uniqueness=0, freshness=0)
    b = score(cols, rows, rules, ScoreConfig(weights=only_validity))
    assert col(b, "a").score is None and b.score == 80.0  # a has no weighted dimension
    # n/a dimensions drop out: default weights, only completeness applies -> 90, not 0.35*90
    assert col(score(cols, rows, rules), "a").score == 90.0


def test_column_weight_zero_excludes_column() -> None:
    cols, rows, rules = _mixed()
    assert score(cols, rows, rules, ScoreConfig(column_weights={"a": 0})).score == 80.0
    all_zero = ScoreConfig(column_weights={"a": 0, "b": 0})
    assert score(cols, rows, rules, all_zero).status == "n/a"


def test_config_hash_tracks_weighting() -> None:
    cols, rows, rules = _mixed()
    h = score(cols, rows, rules).config_hash
    assert h == score(cols, rows, rules).config_hash and len(h) == 64
    assert h != score(cols, rows, rules, ScoreConfig(warn_weight=0.5)).config_hash
    assert h != score(cols, rows, rules, ScoreConfig(required_columns=["b"])).config_hash


def test_empty_batch() -> None:
    b = score(["a"], [], [mk("nn", type="not_null", column="a")])
    assert b.status == "n/a" and b.score is None and b.rows_total == 0
    assert b.columns[0].score is None
    assert score([], [], []).status == "n/a"


def test_deterministic() -> None:
    cols, rows, rules = _mixed()
    a = score(cols, rows, rules).model_dump_json()
    assert a == score(cols, rows, rules).model_dump_json()


# --- config -------------------------------------------------------------------------------------


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "score.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_shipped_config_loads() -> None:
    cfg = load_score_config(QDIR / "score.yaml", DICT)
    assert cfg.weights.completeness == 0.35 and cfg.column_weights["region"] == 2


@pytest.mark.parametrize(
    "body, match",
    [
        ("weights: {validity: -1}", "validity"),
        ("weights: {completeness: 0, validity: 0, uniqueness: 0, freshness: 0}", "positive sum"),
        ("bogus: 1", "bogus"),
        ("weights: {nope: 1}", "nope"),
        ("warn_weight: 2", "warn_weight"),
        ("column_weights: {unknown_col: 1}", "glossary"),
        ("required_columns: [unknown_col]", "glossary"),
        ("column_weights: {region: -1}", "region"),
        ("column_weights: {region: .inf}", "region"),
        ("weights: {validity: .nan}", "validity"),
        ("x: !!python/object/apply:os.getcwd []", "YAML"),
        ("- a\n- b", "mapping"),
    ],
)
def test_invalid_config(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match) as ei:
        load_score_config(write(tmp_path, body), DICT)
    assert "score.yaml" in str(ei.value)


def test_missing_file_and_empty_defaults(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_score_config(tmp_path / "absent.yaml")
    assert load_score_config(write(tmp_path, "")) == ScoreConfig()


# --- integration with P4-01 (population.yaml on v1) ----------------------------------------------


def cleaned_v1() -> tuple[list[str], list[list]]:
    table = read_file(ROOT / "tests" / "data" / "drift" / "v1" / "population.csv")
    mapping = {c: m[0].target for c, m in map_columns(table.columns, DICT).items() if m}
    res = clean_table(
        table, mapping, dictionary=DICT,
        transforms=load_field_transforms(SEM / "transforms.yaml", dictionary=DICT),
        text_rules=load_cleaning(SEM / "cleaning.yaml", DICT),
        source="s", run_id="r", now=lambda: datetime(2026, 11, 8, tzinfo=UTC),
    )  # fmt: skip
    return [*res.columns, "country_code"], [[*r, "MAR"] for r in res.rows]


@pytest.fixture(scope="module")
def pop():
    rules = load_rules(QDIR / "population.yaml", DICT)
    return (
        rules,
        load_references(rules, QDIR / "reference"),
        load_score_config(QDIR / "score.yaml", DICT),
    )


def test_population_v1_clean_is_100(pop) -> None:
    rules, refs, cfg = pop
    cols, rows = cleaned_v1()
    b = score_batch(cols, rows, evaluate(rules, cols, rows, references=refs), rules, config=cfg)
    assert b.score == 100.0 and b.rows_total == 48 and b.rows_failed == 0
    assert all(c.score == 100.0 for c in b.columns)
    assert {c.column for c in b.columns if c.required} == {"region", "year"}


def test_population_known_defects_exact_column_scores(pop) -> None:
    rules, refs, cfg = pop
    cols, rows = cleaned_v1()
    rows = [list(r) for r in rows]
    ix = {c: i for i, c in enumerate(cols)}
    rows[2][ix["population_total"]] = -5
    rows[3][ix["unemployment_rate"]] = 150.0
    rows[4][ix["country_code"]] = "maroc"
    rows[5][ix["region"]] = "Atlantide"
    rows[6][ix["region"]] = None
    rows.append(list(rows[0]))  # duplicate of row 0 -> 49 rows
    b = score_batch(cols, rows, evaluate(rules, cols, rows, references=refs), rules, config=cfg)
    pct = lambda ok, n: 100.0 * ok / n  # noqa: E731
    # region (required): completeness 48/49 ; reference 47/48 ; key (region, year): 47/48
    # (the null key is ignored by the unique rule: 48 evaluated, 1 duplicate)
    comp, ref, uni = pct(48, 49), pct(47, 48), pct(47, 48)
    assert col(b, "region").completeness == pytest.approx(comp)
    assert col(b, "region").validity == pytest.approx(ref)
    assert col(b, "region").uniqueness == pytest.approx(uni)
    assert col(b, "region").score == pytest.approx(
        (comp + ref + uni * 0.2 / 0.35) / (2 + 0.2 / 0.35)
    )
    # year (required, complete): range ok, key 47/48
    assert col(b, "year").completeness == 100.0 and col(b, "year").validity == 100.0
    assert col(b, "year").uniqueness == pytest.approx(uni)
    assert col(b, "year").score == pytest.approx((100 + 100 + uni * 0.2 / 0.35) / (2 + 0.2 / 0.35))
    assert col(b, "population_total").validity == pytest.approx(pct(48, 49))
    assert col(b, "unemployment_rate").validity == pytest.approx(pct(48, 49))
    assert col(b, "country_code").validity == pytest.approx(pct(48, 49))
    assert col(b, "population_total").score == col(b, "population_total").validity
    assert b.rows_total == 49 and b.rows_failed == 6
    assert col(b, "region").rows_failed == 1  # lower bound: max(1 null, 1 ref, 1 dup)
    exp = sum(cfg.column_weights.get(c.column, 1.0) * c.score for c in b.columns) / sum(
        cfg.column_weights.get(c.column, 1.0) for c in b.columns
    )
    assert b.score == pytest.approx(exp) and b.score < 100


def test_no_data_value_in_score_or_logs(pop) -> None:
    rules, refs, cfg = pop
    cols, rows = cleaned_v1()
    rows[0][cols.index("region")] = SENTINEL
    rows[1][cols.index("country_code")] = SENTINEL
    with capture_logs() as logs:
        rep = evaluate(rules, cols, rows, references=refs)
        b = score_batch(cols, rows, rep, rules, config=cfg)
    assert SENTINEL not in b.model_dump_json() and SENTINEL not in json.dumps(logs)


# --- persistence --------------------------------------------------------------------------------

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> QualityScoreStore:
    eng = create_engine(f"sqlite:///{tmp_path / 'q.db'}")
    metadata.create_all(eng)
    return QualityScoreStore(eng)


def sample(nulls: int = 1) -> BatchScore:
    cols, rows, rules = _mixed()
    rows = [[None if i < nulls else i, r[1]] for i, r in enumerate(rows)]
    return score(cols, rows, rules)


def test_save_latest_history(store: QualityScoreStore) -> None:
    b1, b2 = sample(1), sample(2)
    assert store.latest("src") is None
    assert store.save("src", "run1", b1, schema_version=3, now=T0) == 3  # batch + 2 columns
    store.save("src", "run2", b2, now=datetime(2026, 11, 20, tzinfo=UTC))
    last = store.latest("src")
    assert last and last.run_id == "run2" and last.score == b2.score and last.column_name is None
    hist = store.history("src")
    assert [h.run_id for h in hist] == ["run2", "run1"] and hist[1].schema_version == 3
    assert hist[1].created_at == T0 and hist[1].config_hash == b1.config_hash
    assert [h.run_id for h in store.history("src", limit=1)] == ["run2"]
    ch = store.column_history("src", "a")
    assert [c.score for c in ch] == [col(b2, "a").score, col(b1, "a").score]
    assert all(c.scope == "column" and c.column_name == "a" for c in ch)
    assert store.latest("other") is None


def test_replay_is_noop_and_conflict_is_error(store: QualityScoreStore) -> None:
    b = sample(1)
    assert store.save("src", "r", b, now=T0) == 3
    assert store.save("src", "r", b, now=datetime(2027, 1, 1, tzinfo=UTC)) == 0
    assert len(store.history("src")) == 1 and store.latest("src").created_at == T0  # type: ignore[union-attr]
    with pytest.raises(QualityError, match="différents"):
        store.save("src", "r", sample(3), now=T0)
    with pytest.raises(QualityError, match="différents"):
        store.save("src", "r", b, schema_version=9, now=T0)
    assert store.latest("src").score == b.score  # type: ignore[union-attr]


def test_validation_and_append_only_api(store: QualityScoreStore) -> None:
    b = sample()
    for bad_src in ("Bad Name", "", "../x"):
        with pytest.raises(QualityError):
            store.save(bad_src, "r", b)
    for bad_run in ("", "a b", "x" * 65, "r;drop"):
        with pytest.raises(QualityError):
            store.save("src", bad_run, b)
    for bad_limit in (0, 1001, True):
        with pytest.raises(QualityError):
            store.history("src", limit=bad_limit)  # type: ignore[arg-type]
    public = {n for n in dir(store) if not n.startswith("_")}
    assert public == {"save", "latest", "history", "column_history"}


def test_no_sentinel_in_persisted_rows_and_logs(store: QualityScoreStore) -> None:
    cols, rows, rules = _mixed()
    rows[0][0] = SENTINEL
    with capture_logs() as logs:
        b = score(cols, rows, rules)
        store.save("src", "r", b, now=T0)
    with store._engine.connect() as c:  # noqa: SLF001
        dump = json.dumps([list(map(str, r)) for r in c.execute(select(quality_score))])
    assert SENTINEL not in dump and SENTINEL not in json.dumps(logs)
    assert any(e["event"] == "quality_score_saved" for e in logs)


def test_empty_batch_persists_as_na(store: QualityScoreStore) -> None:
    b = score(["a"], [], [])
    store.save("src", "r", b, now=T0)
    last = store.latest("src")
    assert last and last.score is None and last.rows_total == 0


def _race(engine: Engine, n: int = 4) -> list[object]:
    st_ = QualityScoreStore(engine)
    b = sample()
    out: list[object] = []
    barrier = threading.Barrier(n)

    def go() -> None:
        barrier.wait()
        try:
            out.append(st_.save("src", "race", b, now=T0))
        except Exception as e:  # noqa: BLE001
            out.append(e)

    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def _count(engine: Engine) -> int:
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(quality_score)).scalar_one()


def test_concurrent_saves_sqlite(store: QualityScoreStore) -> None:
    out = _race(store._engine)  # noqa: SLF001
    assert not [o for o in out if isinstance(o, Exception)], out
    assert sorted(out) == [0, 0, 0, 3] and _count(store._engine) == 3  # noqa: SLF001


@pytest.mark.slow
def test_postgres_full_scenario(pg_engine: Engine) -> None:
    from tests.schema.test_registry_migration import _assert_no_drift, _upgrade

    _upgrade(pg_engine.url.render_as_string(hide_password=False))
    _assert_no_drift(pg_engine)
    s = QualityScoreStore(pg_engine)
    b1, b2 = sample(1), sample(2)
    s.save("src", "r1", b1, schema_version=1, now=T0)
    s.save("src", "r2", b2, now=datetime(2026, 11, 20, tzinfo=UTC))
    assert s.save("src", "r1", b1, schema_version=1, now=T0) == 0
    with pytest.raises(QualityError):
        s.save("src", "r1", b2, now=T0)
    assert [h.run_id for h in s.history("src")] == ["r2", "r1"]
    assert s.latest("src").score == b2.score  # type: ignore[union-attr]
    assert s.history("src")[1].created_at == T0
    assert [c.run_id for c in s.column_history("src", "a")] == ["r2", "r1"]
    out = _race(pg_engine)
    assert not [o for o in out if isinstance(o, Exception)], out
    assert _count(pg_engine) == 6 + 3  # r1, r2 (3 rows each) + one race run


# --- properties ---------------------------------------------------------------------------------

PROP_RULES = [
    mk("nn", type="not_null", column="a"),
    mk("u", type="unique", columns=["a"]),
    mk("rg", type="range", column="b", min=0, max=10),
    mk("rx", type="regex", column="c", pattern="^[a-z]+$", severity="warn"),
]
CELL = st.one_of(st.none(), st.integers(-5, 20))
ROW = st.tuples(CELL, CELL, st.one_of(st.none(), st.sampled_from(["ab", "X1", "zz"]))).map(list)
PROP = settings(
    max_examples=120, derandomize=True, database=None, deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)  # fmt: skip


@PROP
@given(rows=st.lists(ROW, min_size=1, max_size=30))
def test_prop_scores_in_range(rows) -> None:
    b = score(["a", "b", "c"], rows, PROP_RULES)
    vals = [b.score, b.completeness, b.validity, b.uniqueness]
    for c in b.columns:
        vals += [c.score, c.completeness, c.validity, c.uniqueness]
    assert all(v is None or 0 <= v <= 100 for v in vals)


@PROP
@given(n=st.integers(1, 30))
def test_prop_clean_data_scores_100(n: int) -> None:
    rows = [[i, i % 11, "ab"] for i in range(n)]
    b = score(["a", "b", "c"], rows, PROP_RULES)
    assert b.rows_failed == 0 and b.score == 100.0


@PROP
@given(rows=st.lists(ROW, min_size=1, max_size=20), extra=st.integers(1, 10))
def test_prop_adding_invalid_rows_never_raises_score(rows, extra) -> None:
    base = score(["a", "b", "c"], rows, PROP_RULES).score
    # invalid rows: null key (completeness), out-of-range b, regex failure, duplicate key
    bad = [[None, 99, "X1"] for _ in range(extra)]
    worse = score(["a", "b", "c"], [*rows, *bad], PROP_RULES).score
    assert base is not None and worse is not None and worse <= base + 1e-9
