"""Smart auto-replay (P6-03): human fix -> one idempotent, bounded replay of the blocked batch.

The scenario uses the REAL chain: Dagster bronze -> schema_check -> silver blocked by
`mapping_incomplete`, a human approval through the real review queue, then `run_replays`
(learn -> schema gate -> silver -> promote on the ORIGINAL Bronze batch).
"""

import random
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import structlog
from dagster import SkipReason, build_sensor_context, materialize
from sqlalchemy import Engine, create_engine, inspect, select

from adp.bronze.store import LocalStore
from adp.cli import main as cli_main
from adp.logging import configure_logging
from adp.mapping.decision import SYSTEM, LogEntry, Proposal, ReviewQueue
from adp.mapping.llm import LLMMapper, LocalProvider, MappingProposal, MappingRequest
from adp.mapping.llm import MappingResponse as Resp
from adp.observability.alerts import Alert
from adp.observability.audit import AuditStore, StepOutcome, run_audit
from adp.orchestration import replay as rp
from adp.orchestration.ingestion import make_bronze_assets
from adp.orchestration.replay import (
    BackoffConfig,
    BlockedBatch,
    ReplayContext,
    ReplayPolicy,
    ReplayStore,
    SourceReplay,
    backoff_delay,
    detect_fixes,
    find_blocked,
    kill_switch,
    make_replay_sensor,
    replay_attempt,
    replay_batch,
    run_replays,
)
from adp.orchestration.schema_gate import SchemaRegistryResource, make_schema_check_assets
from adp.orchestration.silver_chain import make_silver_assets
from adp.schema.registry import metadata
from adp.sources.spec import SourceSpec

ROOT = Path(__file__).parents[2]
DRIFT = ROOT / "tests" / "data" / "drift"
CANON = ROOT / "config" / "semantic" / "canonical.yaml"
POLICY = ROOT / "config" / "evolution_policy.yaml"
SENTINEL = "Zqxsentinelreplay"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class ReviewBandProvider:
    """Fake LLM: proposes `region` at 0.8 (review band) for columns whose candidates include it."""

    name, model, prompt_version = "fake", "fake-1", "map-v1"

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, request: MappingRequest) -> Resp:
        self.calls += 1
        return Resp(proposals=tuple(
            MappingProposal(
                source_col=c.name, target="region" if "region" in c.candidates else None,
                confidence=0.8, rationale="fake", model=self.model,
                prompt_version=self.prompt_version, source="llm",
            )
            for c in request.columns
        ))  # fmt: skip


class FakeSender:
    """Records alerts instead of posting them (no network)."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True

    def of(self, kind: str) -> list[Alert]:
        return [a for a in self.sent if a.type == kind]


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def _spec(name: str = "popx") -> SourceSpec:
    return SourceSpec(
        name=name, type="file", uri=str(DRIFT / "v1/population.csv"),  # type: ignore[arg-type]
        format="csv", frequency="manual", primary_key=["reg_x", "annee"],
    )  # fmt: skip


def _csv(path: Path, sentinel: bool = False) -> Path:
    lines = (DRIFT / "v1/population.csv").read_text(encoding="utf-8").splitlines()
    body = lines[1:]
    if sentinel:
        body[0] = body[0].replace(body[0].split(",")[0], SENTINEL, 1)
    path.write_text("\n".join([lines[0].replace("region", "reg_x", 1), *body]) + "\n", "utf-8")
    return path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    learned = tmp_path / "learned.yaml"
    monkeypatch.setenv("ADP_LEARNED_PATH", str(learned))
    monkeypatch.setenv("ADP_CANONICAL_PATH", str(CANON))
    monkeypatch.delenv(rp.KILL_ENV, raising=False)
    reg = SchemaRegistryResource(url=f"sqlite:///{tmp_path / 'reg.db'}")
    return {
        "root": tmp_path, "learned": learned, "reg": reg, "engine": reg.engine(),
        "bronze": LocalStore(tmp_path / "bronze"), "silver": LocalStore(tmp_path / "silver"),
        "gold": LocalStore(tmp_path / "gold"), "q": LocalStore(tmp_path / "q"),
    }  # fmt: skip


def _block(env: dict[str, Any], sentinel: bool = False) -> SourceSpec:
    """Real Dagster run: Silver blocked by mapping_incomplete (reg_x waits for review)."""
    spec = _spec()
    reg = {spec.name: spec}
    llm = ReviewBandProvider()
    res = materialize(
        [*make_bronze_assets(reg), *make_schema_check_assets(reg),
         *make_silver_assets(reg, mapper_factory=lambda d: LLMMapper(llm, d))],
        resources={"store": env["bronze"], "silver_store": env["silver"],
                   "schema_registry": env["reg"]},
        run_config={"ops": {"bronze_popx": {"config": {
            "path": str(_csv(env["root"] / "popx.csv", sentinel))}}}},
        raise_on_error=False,
    )  # fmt: skip
    assert not res.success
    return spec


def _ctx(env: dict[str, Any], spec: SourceSpec, factory: Any = None) -> ReplayContext:
    return ReplayContext(
        engine=env["engine"], sources={spec.name: spec}, bronze_store=env["bronze"],
        silver_store=env["silver"], gold_store=env["gold"], quarantine_store=env["q"],
        report_dir=env["root"] / "reports", canonical_path=CANON, learned_path=env["learned"],
        policy_path=POLICY, quality_dir=ROOT / "config/quality",
        semantic_dir=ROOT / "config/semantic",
        mapper_factory=factory or (lambda d: LLMMapper(LocalProvider(d), d)),
    )  # fmt: skip


def _on(**kw: Any) -> ReplayPolicy:
    return ReplayPolicy(enabled=True, **kw)


def _approve(env: dict[str, Any], col: str = "reg_x") -> int:
    q = ReviewQueue(env["engine"])
    item = next(i for i in q.list(status="pending", source_name="popx") if i.source_col == col)
    q.approve(item.id, actor="alice", note="ok")
    return item.id


def _silver_parquets(env: dict[str, Any]) -> list[str]:
    return sorted(k for k in env["silver"].list() if k.endswith(".parquet"))


def _rows(env: dict[str, Any], key: str) -> int:
    con = duckdb.connect()
    try:
        p = str(env["silver"].root / key)
        sql = "SELECT count(*) FROM read_parquet(?, hive_partitioning = false)"
        return int(con.execute(sql, [p]).fetchone()[0])  # type: ignore[index]
    finally:
        con.close()


def _attempts(env: dict[str, Any]) -> list[dict[str, Any]]:
    return ReplayStore(env["engine"]).attempts()


# --- validation test of the backlog card -----------------------------------------------------
def test_approval_replays_blocked_batch_once_without_duplicates(env) -> None:
    spec = _block(env)
    ctx, clock, sender = _ctx(env, spec), Clock(), FakeSender()
    blocked = find_blocked(ctx)
    assert [(b.source, b.cause) for b in blocked] == [("popx", "mapping_incomplete")]
    assert blocked[0].bronze_batch.startswith("popx/ingestion_date=")

    out = run_replays(ctx, load_policy=_on, now=clock, sender=sender)
    assert out == [] and _attempts(env) == []  # no human fix yet: nothing replayed

    _approve(env)
    out = run_replays(ctx, load_policy=_on, now=clock, sender=sender)
    assert [(r.status, r.reason) for r in out] == [("succeeded", "mapping_approved")]
    files = _silver_parquets(env)
    assert len(files) == 1 and _rows(env, files[0]) == 48
    runs = AuditStore(env["engine"]).runs("popx")
    replay_run = next(r for r in runs if r["trigger"] == "replay")
    assert replay_run["status"] in ("success", "quarantined")
    assert [s["step"] for s in AuditStore(env["engine"]).steps(replay_run["run_id"])] == [
        "schema_check", "silver", "promote"]  # fmt: skip

    clock.advance(hours=2)
    assert run_replays(ctx, load_policy=_on, now=clock, sender=sender) == []  # resolved: skip
    assert [a["status"] for a in _attempts(env)] == ["succeeded"]
    assert find_blocked(ctx) == []

    # replaying the same batch again (e.g. manual double run) writes nothing new
    again = replay_batch(spec, blocked[0].bronze_batch, run_id="manual-1", ctx=ctx, learn=True)
    assert again.status == "succeeded" and again.silver_skipped is True
    assert _silver_parquets(env) == files


def test_budget_exhausted_stops_and_alerts_once_then_new_fix_new_budget(env) -> None:
    spec = _block(env)

    def broken(_d: Any) -> LLMMapper:
        raise RuntimeError("mapper down")

    ctx, clock, sender = _ctx(env, spec, broken), Clock(), FakeSender()
    _approve(env)
    statuses = []
    for _ in range(6):
        out = run_replays(ctx, load_policy=_on, now=clock, sender=sender)
        statuses += [r.status for r in out if r.status != "skipped"]
        clock.advance(hours=2)  # beyond the capped backoff
    assert statuses == ["failed", "failed", "exhausted"]
    rows = _attempts(env)
    assert [r["status"] for r in rows] == ["failed", "failed", "exhausted"]
    assert all(r["reason_code"] == "error:RuntimeError" for r in rows)
    assert len(sender.of("replay_exhausted")) == 1
    alert = sender.of("replay_exhausted")[0]
    assert alert.source == "popx" and "mapper down" not in alert.summary

    # a NEW human fix (new fix_id) opens a new budget; the old key stays exhausted
    q = ReviewQueue(env["engine"])
    q.submit([Proposal(source_col="extra_col", target="year", score=0.7, method="llm",
                       origin="llm")], "popx")  # fmt: skip
    _approve(env, "extra_col")
    out = run_replays(ctx, load_policy=_on, now=clock, sender=sender)
    assert [(r.status, r.attempt_no) for r in out] == [("failed", 1)]
    assert len({r["fix_id"] for r in _attempts(env)}) == 2


def test_human_reset_reopens_budget_and_is_journaled(env, capsys) -> None:
    spec = _block(env)

    def broken(_d: Any) -> LLMMapper:
        raise RuntimeError("down")

    ctx, clock = _ctx(env, spec, broken), Clock()
    _approve(env)
    for _ in range(4):
        run_replays(ctx, load_policy=lambda: _on(max_attempts=1), now=clock)
        clock.advance(hours=2)
    assert [r["status"] for r in _attempts(env)] == ["exhausted"]
    batch = find_blocked(ctx)[0].bronze_batch
    db = str(env["engine"].url)
    with pytest.raises(SystemExit):  # --by and --reason are mandatory
        cli_main(["replay", "reset", "popx", batch, "--db", db, "--by", "bob"])
    assert cli_main(["replay", "reset", "popx", batch, "--db", db, "--by", "bob",
                     "--reason", "mapper fixed"]) == 0  # fmt: skip
    reset = [r for r in _attempts(env) if r["status"] == "reset"]
    assert len(reset) == 1 and reset[0]["actor"] == "bob"
    ctx2 = replace(ctx, mapper_factory=lambda d: LLMMapper(LocalProvider(d), d))
    out = run_replays(ctx2, load_policy=lambda: _on(max_attempts=1), now=clock)
    assert [(r.status, r.attempt_no) for r in out] == [("succeeded", 2)]
    assert cli_main(["replay", "status", "--db", db]) == 0
    assert "succeeded" in capsys.readouterr().out


def test_kill_switch_global_source_env_and_between_plan_and_run(env, monkeypatch) -> None:
    spec = _block(env)
    ctx, clock = _ctx(env, spec), Clock()
    _approve(env)
    assert run_replays(ctx, load_policy=lambda: ReplayPolicy(), now=clock) == []  # default off
    off = _on(sources={"popx": SourceReplay(enabled=False)})
    out = run_replays(ctx, load_policy=lambda: off, now=clock)
    assert [(r.status, r.reason) for r in out] == [("skipped", "source_disabled")]
    monkeypatch.setenv(rp.KILL_ENV, "1")
    assert run_replays(ctx, load_policy=_on, now=clock) == []  # env var wins
    monkeypatch.delenv(rp.KILL_ENV)
    assert _attempts(env) == [] and _silver_parquets(env) == []

    calls = {"n": 0}

    def flips() -> ReplayPolicy:  # enabled when planning, cut right before execution
        calls["n"] += 1
        return _on() if calls["n"] <= 2 else ReplayPolicy(enabled=False)

    out = run_replays(ctx, load_policy=flips, now=clock)
    assert [(r.status, r.reason) for r in out] == [("skipped", "global_disabled")]
    assert [a["status"] for a in _attempts(env)] == ["skipped"]
    assert _silver_parquets(env) == []  # nothing executed
    out = run_replays(ctx, load_policy=_on, now=clock)  # a skip does not consume budget
    assert [(r.status, r.attempt_no) for r in out] == [("succeeded", 2)]


def test_kill_switch_pure() -> None:
    p = _on(sources={"a": SourceReplay(enabled=False)})
    assert kill_switch("a", p, env={}) == "source_disabled"
    assert kill_switch("b", p, env={}) is None
    assert kill_switch("b", ReplayPolicy(), env={}) == "global_disabled"
    assert kill_switch("b", p, env={rp.KILL_ENV: "1"}) == "env_disabled"
    assert kill_switch("b", p, env={rp.KILL_ENV: "0"}) is None


def test_policy_file_missing_is_off_invalid_is_refused(tmp_path: Path) -> None:
    from adp.errors import ConfigError

    assert rp.load_replay_policy(tmp_path / "none.yaml").enabled is False
    bad = tmp_path / "bad.yaml"
    bad.write_text("enabled: true\nunknown_key: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        rp.load_replay_policy(bad)
    shipped = rp.load_replay_policy(ROOT / "config/replay.yaml")
    assert shipped.max_attempts == 3  # H-02


# --- pure fix detection ----------------------------------------------------------------------
def _bb(**kw: Any) -> BlockedBatch:
    base: dict[str, Any] = {
        "source": "pop", "bronze_batch": "pop/ingestion_date=2026-03-01/abc.parquet",
        "cause": "mapping_incomplete", "blocked_at": T0, "block_run_id": "r1",
        "schema_version": 1, "schema_accepted_at": None, "config_baseline": None,
    }  # fmt: skip
    return BlockedBatch(**(base | kw))


def _log(i: int, action: str, actor: str, at: datetime, source: str = "pop") -> LogEntry:
    return LogEntry(
        id=i, queue_id=1, source_name=source, source_col="c", target="region", action=action,  # type: ignore[arg-type]
        actor=actor, score=0.8, method="llm", origin="llm", model="m", prompt_version="v",
        reason="review_band", decided_at=at, note=None,
    )  # fmt: skip


def test_detect_fixes_only_human_approval_after_block() -> None:
    later = T0 + timedelta(minutes=5)
    fps = {"canonical": "c1", "evolution_policy": "p1"}
    assert detect_fixes([_bb()], [_log(1, "auto_apply", SYSTEM, later)], fps) == []
    assert detect_fixes([_bb()], [_log(1, "reject", "alice", later)], fps) == []
    assert detect_fixes([_bb()], [_log(1, "approve", "alice", T0 - timedelta(1))], fps) == []
    assert detect_fixes([_bb()], [_log(1, "approve", "alice", later, "other")], fps) == []
    assert detect_fixes([_bb()], [], fps) == []  # time passing is not a fix
    c1 = detect_fixes([_bb()], [_log(1, "approve", "alice", later)], fps)
    c2 = detect_fixes([_bb()], [_log(1, "approve", "alice", later)], fps)
    assert c1 == c2 and len(c1) == 1 and c1[0].reason == "mapping_approved"
    c3 = detect_fixes([_bb()], [_log(1, "approve", "alice", later),
                                _log(2, "approve", "bob", later)], fps)  # fmt: skip
    assert c3[0].fix_id != c1[0].fix_id  # a newer human fix = a new fix id


def test_detect_fixes_config_and_schema_acceptance() -> None:
    fps = {"canonical": "c2", "evolution_policy": "p1"}
    assert detect_fixes([_bb(config_baseline="c2")], [], fps) == []
    cfg = detect_fixes([_bb(config_baseline="c1")], [], fps)
    assert [c.reason for c in cfg] == ["config_changed:canonical"]
    rr = _bb(cause="review_required", config_baseline="p1")
    assert detect_fixes([rr], [], fps) == []
    acc = detect_fixes([replace(rr, schema_accepted_at=T0 + timedelta(1))], [], fps)
    assert [c.reason for c in acc] == ["schema_accepted"]
    pol = detect_fixes([rr], [], fps | {"evolution_policy": "p2"})
    assert [c.reason for c in pol] == ["config_changed:evolution_policy"]


def test_non_recoverable_batches_are_never_replayed(env) -> None:
    spec = _spec()
    a, now = AuditStore(env["engine"]), datetime.now(UTC)
    for run, step, status, why in (
        ("tech", "silver", "failed", []),
        ("hard", "schema_check", "blocked", ["blocking_change"]),
    ):
        h = f"{run}hash"
        env["bronze"].put_if_absent(f"popx/ingestion_date=2026-03-01/{h}.parquet", b"x")
        a.record_step(run, "popx", "bronze", StepOutcome(batch_id=h), started_at=now,
                      finished_at=now)  # fmt: skip
        a.record_step(run, "popx", step, StepOutcome(status=status, reasons=why,
                      error_type="OSError" if status == "failed" else None),
                      started_at=now, finished_at=now)  # fmt: skip
    ctx = _ctx(env, spec)
    assert find_blocked(ctx) == []
    q = ReviewQueue(env["engine"])
    q.submit([Proposal(source_col="c", target="year", score=0.7, method="llm", origin="llm")],
             "popx")  # fmt: skip
    _approve(env, "c")
    assert run_replays(ctx, load_policy=_on, now=Clock()) == []


# --- backoff ---------------------------------------------------------------------------------
def test_backoff_grows_is_capped_and_jitter_bounded_deterministic() -> None:
    cfg = BackoffConfig(cooldown_seconds=60, factor=2, max_seconds=600, jitter=0.0)
    assert [backoff_delay(n, cfg, 0.5) for n in (1, 2, 3, 4, 5, 6)] == [60, 120, 240, 480, 600,
                                                                        600]  # fmt: skip
    j = BackoffConfig(cooldown_seconds=60, factor=2, max_seconds=600, jitter=0.2)
    rng = random.Random(42)
    for n in range(1, 12):
        d = backoff_delay(n, j, rng.random())
        base = min(600, 60 * 2 ** (n - 1))
        assert base * 0.8 <= d <= min(600, base * 1.2)
    a = [backoff_delay(3, j, r) for r in (random.Random(7).random() for _ in range(3))]
    b = [backoff_delay(3, j, r) for r in (random.Random(7).random() for _ in range(3))]
    assert a == b
    assert backoff_delay(3, j, 0.0) == pytest.approx(192)
    assert backoff_delay(3, j, 1.0) == pytest.approx(288)


def test_failure_waits_for_backoff(env) -> None:
    spec = _block(env)

    def broken(_d: Any) -> LLMMapper:
        raise RuntimeError("down")

    ctx, clock = _ctx(env, spec, broken), Clock()
    _approve(env)
    assert [r.status for r in run_replays(ctx, load_policy=_on, now=clock)] == ["failed"]
    out = run_replays(ctx, load_policy=_on, now=clock)
    assert [(r.status, r.reason) for r in out] == [("skipped", "backoff")]
    assert len(_attempts(env)) == 1


# --- store: concurrency, crash, migration ----------------------------------------------------
@pytest.fixture
def mem(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    metadata.create_all(eng)
    return eng


def test_reservation_is_atomic_across_threads(mem: Engine) -> None:
    store, out = ReplayStore(mem), []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        out.append(store.reserve("pop", "pop/b.parquet", "f1", 1, T0, "replay-x-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out.count(True) == 1 and out.count(False) == 7
    with mem.connect() as c:
        assert len(list(c.execute(select(replay_attempt)))) == 1


def test_stale_running_attempt_is_counted_and_does_not_block(env) -> None:
    spec = _block(env)
    ctx, clock = _ctx(env, spec), Clock()
    _approve(env)
    cand = detect_fixes(find_blocked(ctx), ReviewQueue(env["engine"]).journal(),
                        rp.config_fingerprints(ctx))[0]  # fmt: skip
    store = ReplayStore(env["engine"])
    assert store.reserve(cand.source, cand.bronze_batch, cand.fix_id, 1, clock(), "replay-c-1")
    assert store.start(cand.source, cand.bronze_batch, cand.fix_id, 1, clock())  # then "crash"
    out = run_replays(ctx, load_policy=_on, now=clock)
    assert [(r.status, r.reason) for r in out] == [("skipped", "in_flight")]  # not stale yet
    clock.advance(minutes=31)  # > running_timeout (30 min) + cooldown
    clock.advance(hours=2)
    out = run_replays(ctx, load_policy=_on, now=clock)
    assert [(r.status, r.attempt_no) for r in out] == [("succeeded", 2)]
    rows = _attempts(env)
    assert [(r["status"], r["reason_code"]) for r in rows] == [
        ("failed", "stale_timeout"), ("succeeded", "mapping_approved")]  # fmt: skip


def test_reason_codes_are_safe(mem: Engine) -> None:
    from adp.errors import ReplayError

    store = ReplayStore(mem)
    store.reserve("pop", "pop/b.parquet", "f1", 1, T0, "replay-x-1")
    with pytest.raises(ReplayError):
        store.finish("pop", "pop/b.parquet", "f1", 1, "failed", "bad reason with spaces", T0)
    with pytest.raises(ReplayError):
        store.finish("pop", "pop/b.parquet", "f1", 1, "weird", "x", T0)


def test_migration_0007_creates_replay_tables(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    from tests.schema.test_registry_migration import _assert_no_drift, _upgrade

    url = f"sqlite:///{tmp_path / 'mig.db'}"
    _upgrade(url)
    eng = create_engine(url)
    assert {"replay_attempt", "replay_baseline"} <= set(inspect(eng).get_table_names())
    _assert_no_drift(eng)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.downgrade(cfg, "0006")
    assert "replay_attempt" not in inspect(eng).get_table_names()
    eng.dispose()


@pytest.mark.slow
def test_migration_and_reservation_postgres(pg_engine: Engine) -> None:
    from tests.schema.test_registry_migration import _assert_no_drift, _upgrade

    _upgrade(pg_engine.url.render_as_string(hide_password=False))
    _assert_no_drift(pg_engine)
    test_reservation_is_atomic_across_threads(pg_engine)


# --- privacy, dry-run, sensor ----------------------------------------------------------------
def test_no_data_value_in_logs_rows_or_alerts(env, capfd) -> None:
    configure_logging()
    spec = _block(env, sentinel=True)

    def broken(_d: Any) -> LLMMapper:
        raise RuntimeError(SENTINEL)  # even an exception message carrying data stays out

    ctx, clock, sender = _ctx(env, spec, broken), Clock(), FakeSender()
    _approve(env)
    for _ in range(4):
        run_replays(ctx, load_policy=lambda: _on(max_attempts=2), now=clock, sender=sender)
        clock.advance(hours=2)
    out, err = capfd.readouterr()
    with env["engine"].connect() as c:
        audit_rows = [dict(r._mapping) for r in c.execute(select(run_audit))]
    blob = out + err + str(_attempts(env)) + str(sender.sent) + str(audit_rows)
    assert sender.of("replay_exhausted")
    assert SENTINEL.lower() not in blob.lower()
    structlog.reset_defaults()


def test_dry_run_plans_without_writing(env, capsys) -> None:
    spec = _block(env)
    ctx, clock = _ctx(env, spec), Clock()
    _approve(env)
    out = run_replays(ctx, load_policy=_on, now=clock, dry_run=True)
    assert [(r.status, r.reason) for r in out] == [("planned", "mapping_approved")]
    out = run_replays(ctx, load_policy=lambda: _on(dry_run=True), now=clock)
    assert [r.status for r in out] == ["planned"]
    assert _attempts(env) == [] and _silver_parquets(env) == []


def test_global_window_budget_caps_replays(env) -> None:
    spec = _block(env)
    ctx, clock = _ctx(env, spec), Clock()
    _approve(env)
    out = run_replays(ctx, load_policy=lambda: _on(max_per_window=0), now=clock)
    assert [(r.status, r.reason) for r in out] == [("skipped", "window_budget")]
    assert _attempts(env) == []


def test_sensor_never_crashes_the_daemon(env, monkeypatch) -> None:
    spec = _spec()
    sensor = make_replay_sensor({spec.name: spec})
    assert sensor.default_status.value == "STOPPED"

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(rp, "run_replays", boom)
    monkeypatch.setattr(rp.ReplayContext, "from_env", classmethod(lambda cls, s: None))
    res = sensor(build_sensor_context())
    assert isinstance(res, SkipReason) and "RuntimeError" in (res.skip_message or "")
