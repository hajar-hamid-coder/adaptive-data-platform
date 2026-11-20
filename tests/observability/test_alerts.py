"""Alerting & RCA (P6-02): dedup, thresholds, webhook safety, RCA on a real schema drift.

No real network (respx), no real LLM (fake providers / fake Anthropic client).
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
import structlog
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect

from adp.bronze.store import LocalStore
from adp.cli import main as cli_main
from adp.connectors.files import read_file
from adp.errors import ConfigError
from adp.logging import configure_logging
from adp.mapping.llm import BudgetLedger
from adp.observability.alerts import (
    Alert,
    AlertPolicy,
    AlertStore,
    WebhookSender,
    discord_payload,
    evaluate_alerts,
    freshness_alerts,
    load_alert_policy,
    notify_run,
    sanitize,
    slack_payload,
)
from adp.observability.audit import AuditStore, StepOutcome
from adp.observability.rca import (
    ClaudeRCAProvider,
    RCAReport,
    analyze,
    build_context,
    local_rca,
    render_rca,
)
from adp.orchestration.schema_gate import run_schema_gate
from adp.schema.events import EventBus
from adp.schema.policy import load_policy
from adp.schema.profile import profile_table
from adp.schema.registry import SchemaRegistry, metadata

ROOT = Path(__file__).parents[2]
DRIFT = ROOT / "tests" / "data" / "drift"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SLACK = "https://hooks.slack.com/services/T000/B000/fakeSecretToken123"
DISCORD = "https://discord.com/api/webhooks/1/fakeDiscordToken456"
SENTINEL = "SENTINEL-VALUE-7731"


# --- fixtures --------------------------------------------------------------------------------
@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


def _step(a: AuditStore, run: str, step: str, src: str = "pop", **kw: Any) -> None:
    a.record_step(run, src, step, StepOutcome(**kw), started_at=T0,
                  finished_at=T0 + timedelta(seconds=1))  # fmt: skip


def _gate(engine, store: LocalStore, csv: Path, run_id: str) -> Any:
    """The REAL schema gate (profile -> diff -> policy) on a CSV, audited like the asset."""
    res = run_schema_gate(
        profile_table(read_file(csv)), source_name="pop", primary_key=["region", "annee"],
        registry=SchemaRegistry(engine), policy=load_policy(ROOT / "config/evolution_policy.yaml"),
        bus=EventBus(), store=store, run_id=run_id,
    )  # fmt: skip
    a = AuditStore(engine)
    _step(a, run_id, "bronze", rows_in=48, rows_out=48)
    _step(a, run_id, "schema_check", status="success" if res.status == "passed" else "blocked",
          schema_version=res.version, schema_changes=res.n_changes,
          impact=res.overall_impact.value, reasons=[res.cause] if res.cause else [])  # fmt: skip
    return res


@pytest.fixture
def blocked_run(engine, tmp_path: Path) -> str:
    """v1 -> v2 pass, v3 blocked (review_required) by the real gate: run `r3`."""
    store = LocalStore(tmp_path / "s")
    _gate(engine, store, DRIFT / "v1/population.csv", "r1")
    _gate(engine, store, DRIFT / "v2/population.csv", "r2")
    assert _gate(engine, store, DRIFT / "v3/population.csv", "r3").status == "blocked"
    return "r3"


def _spec() -> Any:
    from adp.sources.spec import SourceSpec

    return SourceSpec(
        name="pop", type="file", uri=str(DRIFT / "v1/population.csv"),  # type: ignore[arg-type]
        format="csv", frequency="manual", primary_key=["region", "annee"],
    )  # fmt: skip


def _alert(**kw: Any) -> Alert:
    base = dict(
        type="run_failed",
        source="pop",
        severity="critical",
        motif="silver:X",
        title="Run en échec",
        summary="s",
        run_id="r1",
    )
    return Alert(**(base | kw))  # type: ignore[arg-type]


class RecSender:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


# --- dedup (validation test #1) --------------------------------------------------------------
def test_same_alert_not_repeated_within_window(engine) -> None:
    s, a, w = AlertStore(engine), _alert(), timedelta(minutes=60)
    assert s.claim(a, "r1", T0, w) == ("send", 1)
    assert s.claim(a, "r1", T0, w) == ("duplicate", 1)  # same run evaluated twice: not counted
    assert s.claim(a, "r2", T0 + timedelta(minutes=10), w) == ("suppressed", 2)
    assert s.claim(a, "r3", T0 + timedelta(minutes=59), w) == ("suppressed", 3)
    assert s.claim(a, "r4", T0 + timedelta(minutes=61), w) == ("send", 4)  # "vu 4 fois"
    other = _alert(motif="silver:Y")
    assert other.dedup_key != a.dedup_key
    assert s.claim(other, "r5", T0 + timedelta(minutes=62), w)[0] == "send"


def test_dedup_key_stable_and_value_free() -> None:
    a, b = _alert(summary="x", run_id="r1"), _alert(summary="y", run_id="r2")
    assert a.dedup_key == b.dedup_key and len(a.dedup_key) == 32
    assert _alert(motif="  Silver:X ").dedup_key == a.dedup_key  # normalised motif


def test_resolve_reopens_and_counts(engine) -> None:
    s, a, w = AlertStore(engine), _alert(), timedelta(minutes=60)
    s.claim(a, "r1", T0, w)
    assert s.resolve("pop", T0 + timedelta(minutes=5)) == 1
    assert s.resolve("pop", T0 + timedelta(minutes=6)) == 0  # idempotent
    assert s.claim(a, "r2", T0 + timedelta(minutes=7), w)[0] == "send"  # recurrence after fix


def test_dedup_is_concurrency_safe(engine) -> None:
    s, a, w = AlertStore(engine), _alert(), timedelta(minutes=60)
    out: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        out.append(s.claim(a, f"run{i}", T0, w)[0])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out.count("send") == 1 and out.count("suppressed") == 7


# --- thresholds ------------------------------------------------------------------------------
def test_thresholds_from_audit(engine) -> None:
    a, p = AuditStore(engine), AlertPolicy()
    _step(a, "ok", "bronze", rows_in=100, rows_out=100)
    _step(a, "ok", "promote", rows_out=100, rows_quarantined=1, quality_score=99.0)
    assert evaluate_alerts(a, "ok", "pop", p) == []
    _step(a, "q", "bronze", rows_in=100, rows_out=100)
    _step(a, "q", "promote", status="quarantined", rows_out=100, rows_quarantined=30,
          quality_score=60.0, reasons=["rule:population_non_negative"])  # fmt: skip
    got = {x.type: x for x in evaluate_alerts(a, "q", "pop", p)}
    assert set(got) == {"quarantine_rate", "quality_score_below"}
    assert "30" in got["quarantine_rate"].summary
    _step(a, "f", "bronze")
    _step(a, "f", "silver", status="failed", error_type="TransformError")
    (f,) = evaluate_alerts(a, "f", "pop", p)
    assert f.type == "run_failed" and f.severity == "critical" and "TransformError" in f.summary
    off = AlertPolicy.model_validate({"thresholds": {"run_failed": {"enabled": False}}})
    assert evaluate_alerts(a, "f", "pop", off) == []
    assert evaluate_alerts(a, "unknown", "pop", p) == []


def test_freshness_sla(engine) -> None:
    a = AuditStore(engine)
    _step(a, "old", "bronze")
    p = AlertPolicy.model_validate({"thresholds": {"sla_freshness": {"max_age_hours": 24}}})
    assert freshness_alerts(a, p, T0 + timedelta(hours=2)) == []
    (x,) = freshness_alerts(a, p, T0 + timedelta(hours=30))
    assert x.type == "sla_freshness" and x.source == "pop"


def test_policy_file_loads_and_rejects_unknown_keys(tmp_path: Path) -> None:
    p = load_alert_policy(ROOT / "config/alerts.yaml")
    assert p.dedup_window_minutes > 0 and "hooks.slack.com" in p.allowed_hosts
    bad = tmp_path / "a.yaml"
    bad.write_text("dedup_window_minutes: 5\nwebhook_url: https://evil.example\n")
    with pytest.raises(ConfigError):
        load_alert_policy(bad)
    assert load_alert_policy(tmp_path / "missing.yaml") == AlertPolicy()


# --- payloads and sanitisation ---------------------------------------------------------------
def test_slack_and_discord_payloads() -> None:
    a = _alert(rca="1. Changement de schéma", links=("_schema/pop/v3.report.md", "/audit/runs/r1"))
    sp = slack_payload(a)
    assert sp["text"].startswith("[CRITICAL]") and sp["blocks"][0]["type"] == "header"
    assert all(b["text"]["type"] == "plain_text" for b in sp["blocks"])
    assert "v3.report.md" in json.dumps(sp)
    dp = discord_payload(a)
    assert dp["allowed_mentions"] == {"parse": []}
    assert dp["embeds"][0]["title"].startswith("[CRITICAL]") and "color" in dp["embeds"][0]
    assert len(dp["embeds"][0]["description"]) <= 4096


def test_sanitize_neutralises_mentions_links_markup() -> None:
    evil = "@everyone @here <!channel> <@U123> see https://evil.example/x?a=1 \x07 **bold**"
    out = sanitize(evil, 60)
    assert "@everyone" not in out and "@here" not in out and "https://" not in out
    assert "<!" not in out and "<@" not in out and "\x07" not in out and len(out) <= 60
    dp = json.dumps(discord_payload(_alert(rca=evil)))
    assert "@everyone" not in dp and "evil.example" not in dp and "**bold**" not in dp
    sp = json.dumps(slack_payload(_alert(rca=evil)))
    assert "@here" not in sp and "evil.example" not in sp and "<!channel>" not in sp


# --- webhook security ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/x",  # not https
        "https://evil.example/services/x",  # host not allowlisted
        "https://hooks.slack.com.evil.example/services/x",  # suffix trick
        "https://user:pw@hooks.slack.com/services/x",  # credentials in URL
        "https://hooks.slack.com:8443/services/x",  # non-default port
        "https://hooks.slack.com/other/x",  # wrong path
        "https://169.254.169.254/latest/meta-data",  # metadata endpoint
        "http://localhost:9999/hook",  # localhost without the test opt-in
        "",
    ],
)
def test_webhook_url_refused_without_leaking_it(url: str) -> None:
    with pytest.raises(ConfigError) as e:
        WebhookSender(SecretStr(url))
    assert (url == "" or url not in str(e.value)) and "fakeSecret" not in str(e.value)


def test_webhook_url_kinds_and_repr() -> None:
    assert WebhookSender(SecretStr(SLACK)).kind == "slack"
    d = WebhookSender(SecretStr(DISCORD))
    assert d.kind == "discord" and "fakeDiscord" not in repr(d)
    loc = WebhookSender(SecretStr("http://127.0.0.1:9/h"), allow_localhost=True, kind="discord")
    assert loc.kind == "discord"


@respx.mock
def test_send_slack_ok_and_network_failure_never_raises(capsys) -> None:
    configure_logging()
    route = respx.post(SLACK).mock(return_value=httpx.Response(200, text="ok"))
    s = WebhookSender(SecretStr(SLACK), wait_s=0)
    assert s.send(_alert()) is True
    assert json.loads(route.calls[0].request.content)["text"].startswith("[CRITICAL]")
    route.mock(side_effect=httpx.ConnectError("boom " + SLACK))
    assert s.send(_alert()) is False  # no exception
    assert route.call_count == 1 + 3  # bounded retries
    route.mock(return_value=httpx.Response(503))
    assert s.send(_alert()) is False
    route.mock(return_value=httpx.Response(302, headers={"location": "https://evil.example"}))
    assert s.send(_alert()) is False  # redirects are not followed
    route.mock(return_value=httpx.Response(400))
    n = route.call_count
    assert s.send(_alert()) is False and route.call_count == n + 1  # 4xx not retried
    logs = capsys.readouterr().out
    assert "alert_send_failed" in logs
    assert "fakeSecretToken123" not in logs and "hooks.slack.com/services" not in logs
    structlog.reset_defaults()


# --- RCA (validation test #2) ----------------------------------------------------------------
def test_rca_on_simulated_failure_cites_real_schema_change(engine, blocked_run: str) -> None:
    ctx = build_context(blocked_run, "pop", audit=AuditStore(engine),
                        registry=SchemaRegistry(engine))  # fmt: skip
    assert (ctx.from_version, ctx.to_version) == (2, 3)
    refs = ctx.refs()
    assert "schema:drop_column:taux_activite" in refs and "step:schema_check:blocked" in refs
    rep = analyze(ctx)  # no provider: deterministic fallback (default path)
    assert rep.source == "local"
    top = rep.hypotheses[0]
    assert "schema:drop_column:taux_activite" in top.evidence
    assert "taux_activite" in top.cause and "v2" in top.cause and "v3" in top.cause
    text = render_rca(rep)
    assert "drop_column taux_activite" in text and "format_change population_totale" in text
    assert all(e in refs for h in rep.hypotheses for e in h.evidence)


def test_rca_context_has_no_values(engine, tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "s")
    _gate(engine, store, DRIFT / "v1/population.csv", "a1")
    lines = (DRIFT / "v1/population.csv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    rows = [line.split(",") for line in lines[1:]]
    drop = len(header) - 1  # drop the last column, put the sentinel in a new text column
    out = [",".join([*header[:drop], "commentaire"])]
    out += [",".join([*r[:drop], f"{SENTINEL}{i}"]) for i, r in enumerate(rows)]
    f = tmp_path / "v.csv"
    f.write_text("\n".join(out) + "\n", encoding="utf-8")
    assert _gate(engine, store, f, "a2").status == "blocked"
    ctx = build_context("a2", "pop", audit=AuditStore(engine), registry=SchemaRegistry(engine))
    blob = ctx.model_dump_json() + render_rca(local_rca(ctx))
    assert SENTINEL not in blob and "commentaire" in blob


def _fake(reply: str | Exception, calls: list[tuple[str, str]] | None = None) -> Any:
    class P:
        name, model = "fake", "fake-1"

        def complete(self, system: str, user: str) -> tuple[str, int, int]:
            if calls is not None:
                calls.append((system, user))
            if isinstance(reply, Exception):
                raise reply
            return reply, 100, 20

    return P()


def _ans(*hyps: dict[str, Any]) -> str:
    return json.dumps({"hypotheses": list(hyps)})


def _ctx(engine, blocked_run: str) -> Any:
    return build_context(blocked_run, "pop", audit=AuditStore(engine),
                         registry=SchemaRegistry(engine))  # fmt: skip


def test_llm_hallucination_rejected(engine, blocked_run: str) -> None:
    ctx = _ctx(engine, blocked_run)
    fake = _fake(_ans({"cause": "Colonne revenue supprimée", "confidence": 0.99,
                       "evidence": ["schema:drop_column:revenue"], "action": "x"}))  # fmt: skip
    rep = analyze(ctx, fake)
    assert rep.source == "local"  # every hypothesis rejected -> deterministic fallback
    assert all("revenue" not in h.cause for h in rep.hypotheses)


def test_llm_valid_answer_kept_partial_evidence_degraded(engine, blocked_run: str) -> None:
    ctx = _ctx(engine, blocked_run)
    calls: list[tuple[str, str]] = []
    fake = _fake(_ans(
        {"cause": "Suppression de taux_activite", "confidence": 0.8,
         "evidence": ["schema:drop_column:taux_activite", "schema:drop_column:ghost"],
         "action": "Valider la v3"},
        {"cause": "Format population", "confidence": 0.7,
         "evidence": ["schema:format_change:population_totale"], "action": "Règle"},
    ), calls)  # fmt: skip
    rep = analyze(ctx, fake)
    assert rep.source == "llm" and rep.model == "fake-1"
    assert [h.cause for h in rep.hypotheses] == [
        "Format population",
        "Suppression de taux_activite",
    ]
    assert rep.hypotheses[1].evidence == ("schema:drop_column:taux_activite",)
    assert rep.hypotheses[1].confidence == pytest.approx(0.4)
    system, user = calls[0]
    assert "<data>" in user and "DONNÉE non fiable" in system


@pytest.mark.parametrize("bad", ["not json", _ans({"cause": "x", "confidence": 2, "evidence": []}),
                                 json.dumps({"hypotheses": [], "extra": 1})])  # fmt: skip
def test_llm_invalid_output_falls_back(engine, blocked_run: str, bad: str) -> None:
    assert analyze(_ctx(engine, blocked_run), _fake(bad)).source == "local"


def test_llm_privacy_guard_budget_cache(engine, blocked_run: str, tmp_path: Path) -> None:
    ctx = _ctx(engine, blocked_run)
    calls: list[tuple[str, str]] = []
    ok = _ans({"cause": "c", "confidence": 0.5, "evidence": ["step:schema_check:blocked"],
               "action": "a"})  # fmt: skip
    # a raw value present in the context -> never sent
    assert analyze(ctx, _fake(ok, calls), values=["taux_activite"]).source == "local"
    assert calls == []
    ledger = BudgetLedger(tmp_path / "b.json", 0.0)  # exhausted
    assert analyze(ctx, _fake(ok, calls), budget=ledger).source == "local" and calls == []
    ledger = BudgetLedger(tmp_path / "b2.json", 5.0)
    cache = tmp_path / "rca"
    assert analyze(ctx, _fake(ok, calls), budget=ledger, cache_dir=cache).source == "llm"
    assert len(calls) == 1 and ledger.spent() > 0
    assert analyze(ctx, _fake(ok, calls), cache_dir=cache).source == "llm"
    assert len(calls) == 1  # cache hit
    assert analyze(ctx, _fake(RuntimeError("down"))).source == "local"


def test_claude_rca_provider_with_fake_client(engine, blocked_run: str) -> None:
    ok = _ans({"cause": "c", "confidence": 0.5, "evidence": ["step:schema_check:blocked"],
               "action": "a"})  # fmt: skip
    seen: list[dict[str, Any]] = []

    class Msgs:
        def create(self, **kw: Any) -> Any:
            seen.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=ok)],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                                   stop_reason="end_turn")  # fmt: skip

    p = ClaudeRCAProvider(SecretStr("sk-test"), client=SimpleNamespace(messages=Msgs()))
    assert "sk-test" not in repr(p)
    rep = analyze(_ctx(engine, blocked_run), p)
    assert rep.source == "llm" and seen[0]["model"] == p.model


def test_rca_report_schema_strict() -> None:
    with pytest.raises(ValueError):
        RCAReport.model_validate({"hypotheses": [], "source": "llm", "model": "m", "x": 1})


# --- notify_run integration ------------------------------------------------------------------
def test_notify_run_groups_dedups_and_attaches_rca(engine, blocked_run: str, capsys) -> None:
    configure_logging()
    a, sender = AuditStore(engine), RecSender()
    sent = notify_run(a, blocked_run, "pop", sender=sender, now=T0)
    assert len(sent) == 1 and sender.sent[0].type == "schema_blocked"
    assert "drop_column taux_activite" in (sender.sent[0].rca or "")
    assert notify_run(a, blocked_run, "pop", sender=sender, now=T0) == []  # same run: no-op
    # a replay of the same blocked batch in another run within the window: suppressed
    _step(a, "r4", "bronze")
    _step(a, "r4", "schema_check", status="blocked", schema_version=3, impact="review",
          reasons=["review_required"])  # fmt: skip
    assert notify_run(a, "r4", "pop", sender=sender, now=T0 + timedelta(minutes=5)) == []
    assert len(sender.sent) == 1
    # recovery
    _step(a, "r5", "bronze", rows_in=1, rows_out=1)
    _step(a, "r5", "promote", rows_out=1, rows_quarantined=0, quality_score=100.0)
    (rec,) = notify_run(a, "r5", "pop", sender=sender, now=T0 + timedelta(minutes=6))
    assert rec.type == "recovered" and rec.severity == "info"
    assert "fakeSecret" not in capsys.readouterr().out
    structlog.reset_defaults()


def test_notify_run_disabled_without_webhook_and_never_raises(
    engine, blocked_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADP_ALERT_WEBHOOK_URL", raising=False)
    a = AuditStore(engine)
    assert notify_run(a, blocked_run, "pop") == []
    assert AlertStore(engine).claim(_alert(motif="probe"), "z", T0, timedelta(1))[0] == "send"

    class Boom:
        def send(self, alert: Alert) -> bool:
            raise RuntimeError("x")

    assert notify_run(a, "r1", "pop", sender=Boom(), now=T0) == []  # no exception
    assert notify_run(a, blocked_run, "pop", sender=Boom(), now=T0) == []


def test_notify_quiet_hours(engine, blocked_run: str) -> None:
    p = AlertPolicy.model_validate({"quiet_hours": {"start": "11:00", "end": "13:00",
                                                    "min_severity": "critical"}})  # fmt: skip
    sender = RecSender()
    assert notify_run(AuditStore(engine), blocked_run, "pop", sender=sender, policy=p,
                      now=T0) == []  # fmt: skip
    assert sender.sent == []


@respx.mock
def test_dagster_blocked_run_sends_one_alert_with_real_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """End to end: real Dagster chain, audited_asset hook, webhook from the environment."""
    from dagster import materialize

    from adp.orchestration.ingestion import make_bronze_assets
    from adp.orchestration.schema_gate import SchemaRegistryResource, make_schema_check_assets

    configure_logging()
    route = respx.post(DISCORD).mock(return_value=httpx.Response(204))
    monkeypatch.setenv("ADP_ALERT_WEBHOOK_URL", DISCORD)
    monkeypatch.setenv("ADP_ALERTS_CONFIG", str(ROOT / "config/alerts.yaml"))
    spec = _spec()
    res = SchemaRegistryResource(url=f"sqlite:///{tmp_path}/r.db")
    store = LocalStore(tmp_path / "b")

    def run(v: str) -> Any:
        return materialize(
            [*make_bronze_assets({"pop": spec}), *make_schema_check_assets({"pop": spec})],
            resources={"store": store, "schema_registry": res},
            run_config={"ops": {"bronze_pop": {"config": {"path": str(DRIFT / v)}}}},
            raise_on_error=False,
        )  # fmt: skip

    assert run("v1/population.csv").success and run("v2/population.csv").success
    assert route.call_count == 0  # no alert while healthy
    assert not run("v3/population.csv").success  # run outcome unchanged
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    text = json.dumps(body, ensure_ascii=False)
    assert "taux_activite" in text and "drop_column" in text and "1 080" not in text
    assert not run("v3/population.csv").success  # replay within the window: no new message
    assert route.call_count == 1
    logs = capsys.readouterr().out
    assert "fakeDiscordToken456" not in logs
    structlog.reset_defaults()


def test_dagster_alert_outage_does_not_fail_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dagster import materialize

    import adp.observability.dagster as dag
    from adp.orchestration.ingestion import make_bronze_assets
    from adp.orchestration.schema_gate import SchemaRegistryResource

    monkeypatch.setattr(dag, "notify_run", lambda *a, **k: 1 / 0)
    spec = _spec()
    r = materialize(make_bronze_assets({"pop": spec}),
                    resources={"store": LocalStore(tmp_path / "b"),
                               "schema_registry": SchemaRegistryResource(
                                   url=f"sqlite:///{tmp_path}/r.db")},
                    run_config={"ops": {"bronze_pop": {"config": {
                        "path": str(DRIFT / "v1/population.csv")}}}},
                    raise_on_error=False)  # fmt: skip
    assert r.success


# --- CLI -------------------------------------------------------------------------------------
def test_cli_dry_run_prints_payload_without_sending(capsys, monkeypatch) -> None:
    monkeypatch.delenv("ADP_ALERT_WEBHOOK_URL", raising=False)
    with respx.mock(assert_all_called=False) as m:
        assert cli_main(["alerts", "test", "--dry-run", "--kind", "discord"]) == 0
        assert not m.calls
    out = json.loads(capsys.readouterr().out)
    assert out["allowed_mentions"] == {"parse": []}
    assert cli_main(["alerts", "test"]) == 2  # no webhook configured


# --- migration -------------------------------------------------------------------------------
def test_migration_0006_creates_alert_state(tmp_path: Path) -> None:
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    url = f"sqlite:///{tmp_path / 'mig.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    assert "alert_state" in inspect(eng).get_table_names()
    with eng.connect() as c:
        assert compare_metadata(MigrationContext.configure(c), metadata) == []
    command.downgrade(cfg, "0005")
    assert "alert_state" not in inspect(eng).get_table_names()
    eng.dispose()
