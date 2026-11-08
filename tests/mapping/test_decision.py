"""Decision policy (H-02) and human review queue: pure rules, persistence, journal, CLI."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, inspect, select

from adp.cli import main
from adp.errors import MappingError
from adp.mapping.decision import (
    DecisionConfig,
    Proposal,
    ReviewQueue,
    combine,
    decide,
    decide_batch,
    mapping_decision_log,
)
from adp.mapping.dictionary import load_dictionary
from adp.mapping.llm import (
    AUTO_THRESHOLD,
    LOCAL_MODEL,
    LOCAL_VERSION,
    REVIEW_THRESHOLD,
    LocalProvider,
    MappingProposal,
    trace_record,
)
from adp.mapping.rules import MappingCandidate, RulesConfig
from adp.schema.diff import DiffConfig
from adp.schema.registry import metadata

ROOT = Path(__file__).resolve().parents[2]
DICT = load_dictionary(ROOT / "config" / "semantic" / "canonical.yaml")
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 8, 11, 30, tzinfo=UTC)


def cand(col: str, target: str, score: float, method: str = "synonym", **kw: object):
    return MappingCandidate(source_col=col, target=target, score=score, method=method, **kw)  # type: ignore[arg-type]


def llm(col: str, target: str | None, conf: float, source: str = "llm") -> MappingProposal:
    return MappingProposal(
        source_col=col, target=target, confidence=conf, rationale="ok",
        model="claude-haiku-5-5" if source == "llm" else LOCAL_MODEL,
        prompt_version="map-v1" if source == "llm" else LOCAL_VERSION, source=source,  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    metadata.create_all(eng)
    return eng


@pytest.fixture
def queue(engine: Engine) -> ReviewQueue:
    return ReviewQueue(engine, dictionary=DICT)


# --- backlog validation cases --------------------------------------------------------------
def test_score_095_is_auto_applied_and_journaled(queue: ReviewQueue) -> None:
    res = queue.submit([cand("chomage", "unemployment_rate", 0.95)], "hcp", now=T0)
    assert [d.proposal.target for d in res.auto_applied] == ["unemployment_rate"]
    assert res.queued == [] and queue.list() == []
    (entry,) = queue.journal("hcp")
    assert (entry.action, entry.actor, entry.target, entry.decided_at) == (
        "auto_apply", "system", "unemployment_rate", T0,
    )  # fmt: skip


def test_score_070_goes_to_review_queue(queue: ReviewQueue) -> None:
    res = queue.submit([cand("chomage", "unemployment_rate", 0.70)], "hcp", now=T0)
    assert res.auto_applied == [] and len(res.queued) == 1
    (item,) = queue.list(status="pending")
    assert (item.source_col, item.target, item.score, item.status) == (
        "chomage", "unemployment_rate", 0.70, "pending",
    )  # fmt: skip
    assert queue.journal("hcp") == []  # queuing is not a decision


def test_approval_recorded_with_author_and_timestamp(queue: ReviewQueue) -> None:
    (qid,) = queue.submit([cand("chomage", "unemployment_rate", 0.70)], "hcp", now=T0).queued
    item = queue.approve(qid, "hajar", note="vu", now=T1)
    assert (item.status, item.decided_by, item.decided_at, item.final_target) == (
        "approved", "hajar", T1, "unemployment_rate",
    )  # fmt: skip
    (entry,) = queue.journal("hcp")
    assert (entry.action, entry.actor, entry.decided_at, entry.queue_id, entry.note) == (
        "approve", "hajar", T1, qid, "vu",
    )  # fmt: skip


# --- pure policy ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("score", "action"),
    [(1.0, "auto_apply"), (0.90, "auto_apply"), (0.8999, "review"), (0.60, "review"),
     (0.5999, "reject"), (0.0, "reject")],
)  # fmt: skip
def test_threshold_boundaries(score: float, action: str) -> None:
    assert decide(cand("c", "region", score)).action == action


def test_guards_force_review_never_upgrade() -> None:
    assert decide(cand("c", "region", 0.98, tie=True)).reasons == ("tie",)
    assert decide(cand("c", "region", 0.98, tie=True)).action == "review"
    d = decide(cand("c", "year", 1.0, method="exact", type_compatible=False))
    assert (d.action, d.reasons) == ("review", ("type_incompatible",))
    assert decide(cand("c", "year", 1.0, method="exact", type_compatible=None)).action == (
        "auto_apply"
    )
    assert decide(cand("c", "region", 0.93, method="fuzzy")).action == "review"
    assert decide(cand("c", "region", 0.5, tie=True)).action == "reject"  # never upgraded
    assert decide(llm("c", "region", 0.95)).action == "auto_apply"


def test_abstention_is_rejected() -> None:
    d = decide(llm("c", None, 0.99))
    assert (d.action, d.reasons) == ("reject", ("abstention",))


def test_local_fallback_never_auto_applied() -> None:
    assert decide(llm("c", "region", 0.99, source="local")).action == "review"
    real = LocalProvider(DICT).propose_one("Unemployment Rate")
    assert real.confidence == pytest.approx(0.89) and decide(real).action == "review"


def test_batch_target_conflict_sends_both_to_review() -> None:
    out = decide_batch([
        cand("taux_chomage", "unemployment_rate", 0.95),
        cand("unemp", "unemployment_rate", 0.98, method="normalized"),
        cand("annee", "year", 0.95),
        cand("bruit", "unemployment_rate", 0.50),  # rejected: does not count as a conflict
    ])  # fmt: skip
    assert [d.action for d in out] == ["review", "review", "auto_apply", "reject"]
    assert "target_conflict" in out[0].reasons and "target_conflict" in out[1].reasons


def test_batch_duplicate_column_refused() -> None:
    with pytest.raises(MappingError):
        decide_batch([cand("a", "region", 0.95), cand("a", "year", 0.95)])


def test_combine_rules_and_llm() -> None:
    agree = combine(cand("c", "region", 0.92, method="fuzzy"), llm("c", "region", 0.97))
    assert agree is not None and agree.corroborated and agree.score == 0.97
    assert decide(agree).action == "auto_apply"  # fuzzy corroborated by an independent LLM
    local = combine(cand("c", "region", 0.92, method="fuzzy"), llm("c", "region", 0.89, "local"))
    assert local is not None and not local.corroborated and decide(local).action == "review"
    dis = combine(cand("c", "region", 0.98, method="normalized"), llm("c", "year", 0.95))
    assert dis is not None and dis.target == "region" and dis.disputed
    d = decide(dis)
    assert (d.action, d.reasons) == ("review", ("rules_llm_disagree",))
    abst = combine(cand("c", "region", 0.98, method="normalized"), llm("c", None, 0.9))
    assert abst is not None and abst.origin == "rules" and decide(abst).action == "auto_apply"
    only = combine(None, llm("c", "year", 0.91))
    assert only is not None and only.origin == "llm"
    assert combine(None, None) is None


def test_config_validated() -> None:
    for kw in ({"review_threshold": 0.0}, {"review_threshold": 0.95}, {"auto_threshold": 1.1}):
        with pytest.raises(ValidationError):
            DecisionConfig(**kw)  # type: ignore[arg-type]
    custom = DecisionConfig(review_threshold=0.5, auto_threshold=0.8)
    assert decide(cand("c", "region", 0.8), custom).action == "auto_apply"


def test_thresholds_consistent_with_rules_llm_and_diff() -> None:
    cfg = DecisionConfig()
    assert (cfg.review_threshold, cfg.auto_threshold) == (REVIEW_THRESHOLD, AUTO_THRESHOLD)
    assert RulesConfig().min_score == cfg.review_threshold
    diff = DiffConfig()
    assert (diff.review_threshold, diff.auto_threshold) == (0.60, 0.90) == (
        cfg.review_threshold, cfg.auto_threshold,
    )  # fmt: skip
    band = {"auto": "auto_apply", "review": "review", "reject": "reject"}
    for i in range(101):
        p = llm("c", "region", i / 100)
        assert band[trace_record(p)["decision"]] == decide(p).action, i
    assert trace_record(llm("c", None, 0.99))["decision"] == decide(llm("c", None, 0.99)).action


def test_proposal_inputs_validated() -> None:
    with pytest.raises(ValidationError):
        Proposal(source_col="c", target="region", score=1.2, method="exact", origin="rules")


# --- persistence ---------------------------------------------------------------------------
def test_submit_is_idempotent(queue: ReviewQueue) -> None:
    batch = [cand("a", "region", 0.70), cand("b", "year", 0.95), cand("c", "period", 0.3)]
    first = queue.submit(batch, "hcp", now=T0)
    second = queue.submit(batch, "hcp", now=T1)
    assert first.queued == second.queued and len(queue.list()) == 1
    assert [e.action for e in queue.journal("hcp")] == ["auto_apply", "reject"]


def test_new_proposal_supersedes_pending(queue: ReviewQueue) -> None:
    (old,) = queue.submit([cand("a", "region", 0.70)], "hcp", now=T0).queued
    (new,) = queue.submit([cand("a", "country_name", 0.75)], "hcp", now=T1).queued
    assert {i.id: i.status for i in queue.list()} == {old: "superseded", new: "pending"}
    assert [e.action for e in queue.journal("hcp")] == ["supersede"]
    with pytest.raises(MappingError):
        queue.approve(old, "hajar")


def test_list_filters(queue: ReviewQueue) -> None:
    queue.submit([cand("a", "region", 0.70)], "hcp", now=T0)
    queue.submit([cand("a", "region", 0.70)], "wb", now=T0)
    assert len(queue.list(source_name="wb")) == 1 and queue.list(status="approved") == []
    with pytest.raises(MappingError):
        queue.list(status="nope")


def test_journal_is_append_only(queue: ReviewQueue, engine: Engine) -> None:
    public = {n for n in dir(queue) if not n.startswith("_")}
    assert public == {"submit", "list", "get", "approve", "reject", "revert", "journal"}
    queue.submit([cand("b", "year", 0.95)], "hcp", now=T0)
    (qid,) = queue.submit([cand("a", "region", 0.70)], "hcp", now=T0).queued
    with engine.connect() as c:
        before = list(c.execute(select(mapping_decision_log)))
    queue.approve(qid, "hajar", now=T1)
    queue.revert(queue.journal("hcp")[-1].id, "hajar", note="erreur", now=T1)
    with engine.connect() as c:
        after = list(c.execute(select(mapping_decision_log)))
    assert after[: len(before)] == before and len(after) == len(before) + 2


def test_approve_with_corrected_target(queue: ReviewQueue) -> None:
    (qid,) = queue.submit([cand("pays", "region", 0.70)], "hcp", now=T0).queued
    with pytest.raises(MappingError, match="glossaire"):
        queue.approve(qid, "hajar", target="not_a_field")
    item = queue.approve(qid, "hajar", target="country_name", now=T1)
    assert (item.target, item.final_target) == ("region", "country_name")
    assert queue.journal("hcp")[-1].target == "country_name"


def test_corrected_target_needs_dictionary(engine: Engine) -> None:
    q = ReviewQueue(engine)
    (qid,) = q.submit([cand("pays", "region", 0.70)], "hcp").queued
    with pytest.raises(MappingError):
        q.approve(qid, "hajar", target="country_name")


def test_no_double_decision(queue: ReviewQueue) -> None:
    (qid,) = queue.submit([cand("a", "region", 0.70)], "hcp").queued
    queue.reject(qid, "hajar", note="faux")
    with pytest.raises(MappingError, match="déjà"):
        queue.approve(qid, "bob")
    with pytest.raises(MappingError, match="déjà"):
        queue.reject(qid, "bob")
    with pytest.raises(MappingError, match="inconnu"):
        queue.approve(999, "bob")
    assert [e.action for e in queue.journal("hcp")] == ["reject"]
    queue.submit([cand("a", "region", 0.70)], "hcp")  # human already said no: not re-queued
    assert queue.list(status="pending") == []


@pytest.mark.parametrize("actor", ["", "   ", "\n", "x" * 129])
def test_actor_required(queue: ReviewQueue, actor: str) -> None:
    (qid,) = queue.submit([cand("a", "region", 0.70)], "hcp").queued
    with pytest.raises(MappingError, match="auteur"):
        queue.approve(qid, actor)
    with pytest.raises(MappingError, match="auteur"):
        queue.reject(qid, actor)
    assert queue.get(qid).status == "pending"  # type: ignore[union-attr]


def test_revert_approved_and_auto(queue: ReviewQueue) -> None:
    (qid,) = queue.submit([cand("a", "region", 0.70)], "hcp", now=T0).queued
    queue.approve(qid, "hajar", now=T0)
    item = queue.revert(queue.journal("hcp")[-1].id, "lead", note="mauvaise cible", now=T1)
    assert (item.id, item.status, item.decided_by, item.final_target) == (
        qid,
        "pending",
        None,
        None,
    )
    rev = queue.journal("hcp")[-1]
    assert (rev.action, rev.actor, rev.queue_id, rev.decided_at) == ("revert", "lead", qid, T1)
    with pytest.raises(MappingError):
        queue.revert(rev.id, "lead")  # a revert is not revertible
    queue.submit([cand("b", "year", 0.95)], "hcp", now=T0)
    auto = queue.journal("hcp", source_col="b")[-1]
    back = queue.revert(auto.id, "lead", now=T1)
    assert (back.source_col, back.target, back.status) == ("b", "year", "pending")
    assert queue.journal("hcp", source_col="b")[-1].queue_id == back.id


def test_revert_only_latest_decision(queue: ReviewQueue) -> None:
    queue.submit([cand("b", "year", 0.95)], "hcp", now=T0)
    first = queue.journal("hcp")[-1]
    queue.submit([cand("b", "period", 0.96)], "hcp", now=T1)
    with pytest.raises(MappingError, match="dernière"):
        queue.revert(first.id, "lead")


def test_concurrent_approvals_single_winner(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'c.db'}"
    eng = create_engine(url, connect_args={"timeout": 30})
    metadata.create_all(eng)
    (qid,) = ReviewQueue(eng).submit([cand("a", "region", 0.70)], "hcp").queued
    _race(lambda: ReviewQueue(create_engine(url, connect_args={"timeout": 30})), qid)


def _race(make, qid: int) -> None:  # type: ignore[no-untyped-def]
    barrier, ok, ko = threading.Barrier(4), [], []

    def run(who: str) -> None:
        q = make()
        barrier.wait()
        try:
            q.approve(qid, who)
            ok.append(who)
        except MappingError:
            ko.append(who)

    threads = [threading.Thread(target=run, args=(f"u{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 1 and len(ko) == 3
    entries = make().journal("hcp", source_col="a")
    assert [(e.action, e.actor) for e in entries] == [("approve", ok[0])]


# --- CLI -----------------------------------------------------------------------------------
def test_cli_list_approve_reject(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(engine.url)
    q = ReviewQueue(engine)
    a, b = q.submit([cand("a", "region", 0.70), cand("b", "year", 0.65)], "hcp").queued
    assert main(["review", "list", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "hcp.a -> region" in out and "2 élément(s)" in out
    assert main(["review", "approve", str(a), "--by", "hajar", "--db", db,
                 "--target", "country_name", "--note", "ok"]) == 0  # fmt: skip
    assert "approuvé" in capsys.readouterr().out
    assert main(["review", "reject", str(b), "--by", "hajar", "--db", db]) == 0
    assert main(["review", "list", "--status", "approved", "--source", "hcp", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "hcp.a -> country_name" in out and "1 élément(s)" in out
    assert main(["review", "approve", str(a), "--by", "bob", "--db", db]) == 2
    assert "Erreur" in capsys.readouterr().err


def test_cli_by_is_mandatory(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["review", "approve", "1", "--db", str(engine.url)])
    assert exc.value.code == 2
    assert "--by" in capsys.readouterr().err
    assert main(["review", "approve", "1", "--by", " ", "--db", str(engine.url)]) == 2


def test_cli_db_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADP_REGISTRY_URL", f"sqlite:///{tmp_path / 'sub' / 'env.db'}")
    assert main(["review", "list"]) == 0
    assert "0 élément(s)" in capsys.readouterr().out


# --- migration -----------------------------------------------------------------------------
def _upgrade(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def _assert_no_drift(engine: Engine) -> None:
    with engine.connect() as c:
        assert compare_metadata(MigrationContext.configure(c), metadata) == []


def test_revision_0003_matches_model(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    _upgrade(url)
    eng = create_engine(url)
    assert {"review_queue", "mapping_decision_log"} <= set(inspect(eng).get_table_names())
    _assert_no_drift(eng)
    q = ReviewQueue(eng, DICT)
    (qid,) = q.submit([cand("a", "region", 0.70)], "hcp").queued
    assert q.approve(qid, "hajar").status == "approved"
    eng.dispose()


@pytest.mark.slow
def test_review_queue_postgres(pg_engine: Engine) -> None:
    _upgrade(pg_engine.url.render_as_string(hide_password=False))
    _assert_no_drift(pg_engine)
    q = ReviewQueue(pg_engine, DICT)
    res = q.submit([cand("a", "region", 0.70), cand("b", "year", 0.95)], "hcp", now=T0)
    assert q.submit([cand("a", "region", 0.70)], "hcp").queued == res.queued
    (qid,) = res.queued
    _race(lambda: ReviewQueue(pg_engine), qid)
    assert q.get(qid).decided_at is not None  # type: ignore[union-attr]
    assert [e.action for e in q.journal("hcp", source_col="b")] == ["auto_apply"]


def test_human_rejection_vetoes_later_auto_apply(queue: ReviewQueue) -> None:
    (qid,) = queue.submit([cand("a", "region", 0.70)], "hcp", now=T0).queued
    queue.reject(qid, "hajar", now=T0)
    res = queue.submit([cand("a", "region", 0.97)], "hcp", now=T1)  # same key, higher score
    assert res.auto_applied == [] and [d.proposal.target for d in res.vetoed] == ["region"]
    assert [e.action for e in queue.journal("hcp")] == ["reject"]
