"""Property-based tests of `diff_schemas` (P2-07, hypothesis).

Every property below is a property of the REAL implementation (see diff.py module doc), not a
wish. Profiles respect the invariants of `profile_column` (null_count <= row_count,
nullable <=> null_count > 0, distinct_count <= non-null count, type-specific stats...), so a
counter-example is a real diff bug, never an artificial input.

Determinism: `derandomize=True` + `database=None` -> same examples on every run (CI-safe).
The name pool mixes realistic near-duplicates (`prix_ht`, `tel`, `telephone`, `dateNaissance`...)
and random text, so that two independently drawn schemas share names AND trigger rename
candidates; purely random names would almost never overlap.

Known (documented) asymmetry: the rename SCORE of a pair (x, y) is symmetric, but the greedy
one-to-one matching breaks ties on (old name, new name), which is not symmetric. When two
non-rejected candidates have exactly the same score, diff(a, b) and diff(b, a) may select
different pairs. `test_rename_selection_symmetric` therefore asserts selection symmetry only
when the non-rejected scores are all distinct.
"""

from collections.abc import Sequence
from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st

from adp.schema.diff import ChangeKind, DiffConfig, SchemaDiff, diff_schemas
from adp.schema.profile import ColumnProfile, InferredType

PROP = settings(max_examples=500, deadline=None, derandomize=True, database=None)

NAME_POOL = [
    "id", "client_id", "clientId", "prix_ht", "prix_ttc", "tel", "telephone", "code_postal", "cp",
    "date_naissance", "dateNaissance", "dt_naissance", "population", "population_totale",
    "qte", "quantite", "Nom", "nom", "nombre", "Ville",
]  # fmt: skip
ALPHA = st.text(alphabet="abcdeAB_é1 ", min_size=1, max_size=8)
POOL = st.sampled_from(NAME_POOL)
NAMES = st.integers(0, 3).flatmap(lambda i: ALPHA if i == 0 else POOL)  # 3/4 pool: more overlap
TYPES: list[InferredType] = ["integer", "float", "boolean", "date", "datetime", "string", "empty"]
DATE_HINTS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]


@st.composite
def profiles(draw: st.DrawFn) -> ColumnProfile:
    """A ColumnProfile consistent with what `profile_column` can produce."""
    name = draw(NAMES)
    typ: InferredType = draw(st.sampled_from(TYPES))
    rows = draw(st.integers(0, 50)) if typ == "empty" else draw(st.integers(1, 50))
    nulls = rows if typ == "empty" else draw(st.integers(0, rows - 1))
    non_null = rows - nulls
    distinct = 0 if non_null == 0 else draw(st.integers(1, non_null))
    data: dict[str, Any] = {
        "name": name, "inferred_type": typ, "type_confidence": 1.0, "nullable": nulls > 0,
        "null_count": nulls, "row_count": rows, "distinct_count": distinct,
    }  # fmt: skip
    if typ in ("integer", "float"):
        lo = draw(st.integers(-1000, 1000))
        hi = lo + draw(st.integers(0, 1000))
        data.update(
            min=float(lo), max=float(hi), mean=draw(st.floats(lo, hi, allow_nan=False)),
            format_hint=draw(st.sampled_from([None, "percent", "thousands_space"])),
        )  # fmt: skip
    elif typ == "string":
        lo_len = draw(st.integers(0, 20))
        data.update(min_length=lo_len, max_length=lo_len + draw(st.integers(0, 20)))
    elif typ == "boolean":
        data["format_hint"] = draw(st.sampled_from([None, "0/1"]))
    elif typ == "date":
        data["format_hint"] = draw(st.sampled_from(DATE_HINTS))
    elif typ == "datetime":
        data["format_hint"] = "iso8601"
    return ColumnProfile.model_validate(data)


schemas = st.lists(profiles(), max_size=12, unique_by=lambda c: c.name)
configs = st.builds(
    lambda t, w: DiffConfig(review_threshold=min(t), auto_threshold=max(t), name_weight=w[0],
                            type_weight=w[1], dist_weight=w[2] or 0.2),
    st.lists(st.floats(0.01, 1.0), min_size=2, max_size=2),
    st.tuples(st.floats(0, 1), st.floats(0, 1), st.floats(0, 1)),
)  # fmt: skip


def of(d: SchemaDiff, kind: ChangeKind) -> list[Any]:
    return [c for c in d.changes if c.kind is kind]


def non_reorder(d: SchemaDiff) -> list[Any]:
    return [c for c in d.changes if c.kind is not ChangeKind.REORDERED]


def names(cols: Sequence[ColumnProfile]) -> set[str]:
    return {c.name for c in cols}


# a. identity ------------------------------------------------------------------------------------


@PROP
@given(a=schemas)
@example(a=[])
def test_diff_of_identical_schemas_is_empty(a: list[ColumnProfile]) -> None:
    """diff(a, a) = nothing: no change, no REORDERED, no rename candidate."""
    d = diff_schemas(a, a)
    assert d.changes == () and d.candidates == ()


# b. order stability -----------------------------------------------------------------------------


@PROP
@given(a=schemas, b=schemas, data=st.data())
def test_stable_under_column_order(
    a: list[ColumnProfile], b: list[ColumnProfile], data: Any
) -> None:
    """Permuting columns of old and/or new changes neither candidates (scores, selection) nor any
    change except REORDERED, which is about order by definition. Deterministic: same call, same
    result."""
    pa, pb = data.draw(st.permutations(a)), data.draw(st.permutations(b))
    base = diff_schemas(a, b)
    assert diff_schemas(a, b) == base
    shuffled = diff_schemas(pa, pb)
    assert shuffled.candidates == base.candidates
    assert non_reorder(shuffled) == non_reorder(base)


@PROP
@given(a=schemas, data=st.data())
def test_reordered_iff_order_differs(a: list[ColumnProfile], data: Any) -> None:
    """A pure permutation yields exactly one REORDERED (if the order changed) and nothing else."""
    p = data.draw(st.permutations(a))
    d = diff_schemas(a, p)
    assert non_reorder(d) == []
    assert len(of(d, ChangeKind.REORDERED)) == (1 if p != a else 0)


# c. add / remove symmetry -----------------------------------------------------------------------


@PROP
@given(a=schemas, b=schemas)
def test_added_removed_symmetry_without_renames(
    a: list[ColumnProfile], b: list[ColumnProfile]
) -> None:
    """With rename thresholds at 1.0 (a rename still needs a perfect 1.0 score, e.g. `a_b` vs
    `ab` with identical stats, so it is not strictly impossible) and no rename selected in either
    direction: ADDED(a,b) = REMOVED(b,a) and REMOVED(a,b) = ADDED(b,a), as sets of names."""
    cfg = DiffConfig(review_threshold=1.0, auto_threshold=1.0)
    ab, ba = diff_schemas(a, b, config=cfg), diff_schemas(b, a, config=cfg)
    if ab.renames or ba.renames:
        return
    assert {c.column for c in of(ab, ChangeKind.ADDED)} == {
        c.column for c in of(ba, ChangeKind.REMOVED)
    }
    assert {c.column for c in of(ab, ChangeKind.REMOVED)} == {
        c.column for c in of(ba, ChangeKind.ADDED)
    }


@PROP
@given(a=schemas, b=schemas)
def test_added_removed_symmetry_after_matching(
    a: list[ColumnProfile], b: list[ColumnProfile]
) -> None:
    """Whatever the renames: ADDED(a,b) + targets of RENAMED(a,b) = names only in b
    = REMOVED(b,a) + sources of RENAMED(b,a)."""
    ab, ba = diff_schemas(a, b), diff_schemas(b, a)
    only_new = names(b) - names(a)
    assert {c.column for c in of(ab, ChangeKind.ADDED)} | {
        r.new_column for r in ab.renames
    } == only_new
    assert {c.column for c in of(ba, ChangeKind.REMOVED)} | {
        r.old_column for r in ba.renames
    } == only_new


@PROP
@given(a=schemas, b=schemas)
def test_candidate_scores_are_symmetric(a: list[ColumnProfile], b: list[ColumnProfile]) -> None:
    """Score of (x -> y) in diff(a,b) = that of (y -> x) in diff(b,a), components included."""
    fwd = {(c.old_column, c.new_column): c for c in diff_schemas(a, b).candidates}
    bwd = {(c.new_column, c.old_column): c for c in diff_schemas(b, a).candidates}
    assert fwd.keys() == bwd.keys()
    for k, c in fwd.items():
        o = bwd[k]
        assert (c.score, c.name_sim, c.type_sim, c.dist_sim, c.decision) == (
            o.score, o.name_sim, o.type_sim, o.dist_sim, o.decision,
        )  # fmt: skip


@PROP
@given(a=schemas, b=schemas)
def test_rename_selection_symmetric(a: list[ColumnProfile], b: list[ColumnProfile]) -> None:
    """x->y selected in diff(a,b) iff y->x selected in diff(b,a), when the non-rejected scores are
    pairwise distinct (greedy ties are broken by (old, new) name: documented asymmetry)."""
    ab, ba = diff_schemas(a, b), diff_schemas(b, a)
    live = [c.score for c in ab.candidates if c.decision != "rejected"]
    if len(live) != len(set(live)):
        return
    assert {(r.old_column, r.new_column) for r in ab.renames} == {
        (r.new_column, r.old_column) for r in ba.renames
    }


# d. structural coherence ------------------------------------------------------------------------


@PROP
@given(a=schemas, b=schemas, cfg=configs)
def test_structural_coherence(
    a: list[ColumnProfile], b: list[ColumnProfile], cfg: DiffConfig
) -> None:
    """No column lost or counted twice; one-to-one matching; scores in [0,1]; decisions consistent
    with the thresholds; greedy matching is maximal (no free non-rejected pair is left)."""
    d = diff_schemas(a, b, config=cfg)
    removed = [c.column for c in of(d, ChangeKind.REMOVED)]
    added = [c.column for c in of(d, ChangeKind.ADDED)]
    src = [c.old_column for c in of(d, ChangeKind.RENAMED)]
    dst = [c.column for c in of(d, ChangeKind.RENAMED)]
    # old-only columns: removed XOR renamed source, exactly once; same for new-only columns
    assert sorted(removed + src) == sorted(names(a) - names(b))
    assert sorted(added + dst) == sorted(names(b) - names(a))
    assert len(set(src)) == len(src) and len(set(dst)) == len(dst)
    assert sorted(zip(src, dst, strict=True)) == sorted(
        (r.old_column, r.new_column) for r in d.renames
    )
    assert len(d.candidates) == len(names(a) - names(b)) * len(names(b) - names(a))
    for c in d.candidates:
        assert 0.0 <= c.score <= 1.0
        expected = (
            "auto" if c.score >= cfg.auto_threshold
            else "review" if c.score >= cfg.review_threshold
            else "rejected"
        )  # fmt: skip
        assert c.decision == expected
        assert c.selected <= (c.decision != "rejected")
    used_o, used_n = set(src), set(dst)
    for c in d.candidates:
        if c.decision != "rejected" and not c.selected:
            assert c.old_column in used_o or c.new_column in used_n


# e. per-column changes --------------------------------------------------------------------------


@PROP
@given(a=schemas, b=schemas)
def test_attribute_changes_of_same_name_columns(
    a: list[ColumnProfile], b: list[ColumnProfile]
) -> None:
    """For a column present on both sides: TYPE_CHANGED iff the inferred types differ; else
    FORMAT_CHANGED iff the format hints differ; NULLABILITY_CHANGED iff `nullable` differs;
    an unchanged profile yields nothing."""
    d = diff_schemas(a, b)
    old, new = {c.name: c for c in a}, {c.name: c for c in b}
    for n in old.keys() & new.keys():
        x, y = old[n], new[n]
        got = {c.kind for c in d.changes if c.column == n and c.old_column is None}
        got -= {ChangeKind.REORDERED}
        want = set()
        if x.inferred_type != y.inferred_type:
            want.add(ChangeKind.TYPE_CHANGED)
        elif x.format_hint != y.format_hint:
            want.add(ChangeKind.FORMAT_CHANGED)
        if x.nullable != y.nullable:
            want.add(ChangeKind.NULLABILITY_CHANGED)
        assert got == want


# f. serialisation -------------------------------------------------------------------------------


@PROP
@given(a=schemas, b=schemas)
def test_schema_changes_one_row_per_change(a: list[ColumnProfile], b: list[ColumnProfile]) -> None:
    """`to_schema_changes()` yields one row per change, with the same vocabulary."""
    d = diff_schemas(a, b)
    rows = d.to_schema_changes()
    assert [r.change_type for r in rows] == [c.kind.value for c in d.changes]
