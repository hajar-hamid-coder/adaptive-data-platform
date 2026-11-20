"""Webhook alerting with anti-noise and RCA (P6-02).

Flow (`notify_run`): audit rows -> `evaluate_alerts` (pure thresholds) -> quiet hours ->
`AlertStore.claim` (dedup) -> grouping (one message per run) -> RCA text -> `WebhookSender`.

Design decisions
- Dedup key = sha256(source, type, normalised motif)[:32]; the motif is built from codes only
  (step, exception type, schema version, reason code), never from data values.
- Dedup state = SQL table `alert_state` (Alembic 0006) in the metadata DB, the same DB as
  `run_audit` (P6-01): Dagster and the API are separate processes and the DB is the shared
  truth. Concurrency-safe without locks: one INSERT (unique key) or one conditional UPDATE
  (`last_sent <= now - window`) per decision; the database arbitrates, so exactly one
  concurrent caller gets "send". Same run evaluated twice (`last_run_id`) = no-op ("duplicate",
  not counted). `occurrences` gives "vu N fois". Delivery is at-most-once: the slot is claimed
  before sending, a failed send is logged and not retried later (no noise storm on outages).
- A run that succeeds again closes the open alerts of its source and sends one `recovered`
  message (`resolve`); a closed alert that comes back is sent immediately.
- Window = cooldown: one setting `dedup_window_minutes`. Quiet hours (UTC) drop alerts below
  `min_severity` without claiming them.
- Webhook URL: ONLY from the environment (`ADP_ALERT_WEBHOOK_URL`, a secret: never in a repo
  file, never read from `.env` by this module), held as SecretStr, never logged nor put in an
  error message. Accepted only if https, default port, no credentials, host in the allowlist
  (`hooks.slack.com`, `discord.com`, `discordapp.com` by default: no SSRF / exfiltration to an
  arbitrary host) and the expected path; localhost only with an explicit test opt-in.
  Redirects are not followed. Short timeout, bounded tenacity retries on transport errors,
  429 and 5xx only. A delivery problem is a structured warning (exception TYPE only, httpx
  messages contain the URL) and NEVER fails a run; `notify_run` never raises.
- Sanitisation of every text field (RCA output included): control characters removed, URLs
  replaced, `@` neutralised with a zero-width space (no @everyone/@here/<@id>), `<`/`>`
  replaced (no Slack control sequences), emphasis characters dropped, length bounded. Slack
  uses `plain_text` blocks; Discord sets `allowed_mentions: {"parse": []}`.
"""

import hashlib
import os
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from adp.errors import AlertError, ConfigError
from adp.logging import get_logger
from adp.observability.audit import AuditStore, run_audit
from adp.schema.registry import SchemaRegistry, metadata

log = get_logger(__name__)
DEFAULT_CONFIG = "config/alerts.yaml"
WEBHOOK_ENV = "ADP_ALERT_WEBHOOK_URL"
DEFAULT_HOSTS = ("hooks.slack.com", "discord.com", "discordapp.com")
_KIND_BY_HOST = {"hooks.slack.com": "slack", "discord.com": "discord", "discordapp.com": "discord"}
_PATH = {"slack": "/services/", "discord": "/api/webhooks/"}
_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})
_RANK = {"info": 0, "warning": 1, "critical": 2}
_COLOR = {"info": 0x3B82F6, "warning": 0xF59E0B, "critical": 0xDC2626}
MAX_TEXT = 1800

Severity = Literal["info", "warning", "critical"]
AlertType = Literal[
    "run_failed", "schema_blocked", "quarantine_rate", "quality_score_below", "sla_freshness",
    "recovered", "replay_exhausted",
]  # fmt: skip
Decision = Literal["send", "suppressed", "duplicate"]
Kind = Literal["slack", "discord"]


# --- model and policy ----------------------------------------------------------------------
class Alert(BaseModel):
    """One alert (codes, counts and column names only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: AlertType
    source: str
    severity: Severity
    motif: str
    title: str
    summary: str
    run_id: str | None = None
    links: tuple[str, ...] = ()
    rca: str | None = None

    @property
    def dedup_key(self) -> str:
        motif = " ".join(self.motif.casefold().split())
        blob = "\x1f".join((self.source, self.type, motif))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class Threshold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    severity: Severity = "warning"


class CriticalThreshold(Threshold):
    severity: Severity = "critical"


class QuarantineThreshold(Threshold):
    max_ratio: float = Field(default=0.05, ge=0, le=1)


class QualityThreshold(Threshold):
    min_score: float = Field(default=80.0, ge=0, le=100)


class FreshnessThreshold(Threshold):
    max_age_hours: float = Field(default=48.0, gt=0)


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_failed: CriticalThreshold = CriticalThreshold()
    schema_blocked: Threshold = Threshold()
    quarantine_rate: QuarantineThreshold = QuarantineThreshold()
    quality_score_below: QualityThreshold = QualityThreshold()
    sla_freshness: FreshnessThreshold = FreshnessThreshold()


class QuietHours(BaseModel):
    """UTC window; alerts below `min_severity` are dropped while it is active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: time
    end: time
    min_severity: Severity = "critical"

    def active(self, now: datetime) -> bool:
        t = now.astimezone(UTC).time().replace(tzinfo=None)
        return (
            self.start <= t < self.end
            if self.start <= self.end
            else not (self.end <= t < self.start)
        )


class RCASettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    use_llm: bool = False  # opt-in; needs ANTHROPIC_API_KEY, else deterministic rules
    max_context_chars: int = Field(default=4000, ge=500, le=20000)


class AlertPolicy(BaseModel):
    """config/alerts.yaml (no secret in it: the webhook URL comes from the environment)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dedup_window_minutes: int = Field(default=60, ge=1, le=10080)
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS
    quiet_hours: QuietHours | None = None
    thresholds: Thresholds = Thresholds()
    rca: RCASettings = RCASettings()


def load_alert_policy(path: Path) -> AlertPolicy:
    """Policy from YAML; a missing file means defaults. Raises ConfigError if invalid."""
    if not path.exists():
        return AlertPolicy()
    try:
        return AlertPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (ValidationError, yaml.YAMLError) as exc:
        n = exc.error_count() if isinstance(exc, ValidationError) else 1
        raise ConfigError(
            f"{path.name} : configuration d'alertes invalide ({n} erreur(s))"
        ) from None


# --- evaluation (pure, no network) ---------------------------------------------------------
def _alert(t: AlertType, th: Threshold, source: str, run_id: str | None, motif: str, title: str,
           summary: str, links: Sequence[str] = ()) -> Alert:  # fmt: skip
    return Alert(type=t, source=source, severity=th.severity, motif=motif, title=title,
                 summary=summary, run_id=run_id, links=tuple(links))  # fmt: skip


def evaluate_alerts(
    audit: AuditStore, run_id: str, source: str, policy: AlertPolicy
) -> list[Alert]:
    """Alerts raised by one run, from `run_audit`/`step_audit` (deterministic)."""
    run = audit.run(run_id, source)
    if run is None:
        return []
    th, out = policy.thresholds, []
    audit_link = f"/audit/runs/{run_id}/steps"
    for s in audit.steps(run_id, source):
        step = s["step"]
        if s["status"] == "failed" and th.run_failed.enabled:
            err = s["error_type"] or "unknown"
            out.append(_alert("run_failed", th.run_failed, source, run_id, f"{step}:{err}",
                              f"Run en échec : {source}",
                              f"Étape {step} en échec ({err}).", [audit_link]))  # fmt: skip
        if step == "schema_check" and s["status"] == "blocked" and th.schema_blocked.enabled:
            v, why = s["schema_version"], (s["reasons"] or ["blocked"])[0]
            out.append(_alert(
                "schema_blocked", th.schema_blocked, source, run_id, f"v{v}:{why}",
                f"Schéma bloqué : {source} v{v}",
                f"Schéma v{v} bloqué ({why}, impact {s['impact'] or '?'}) ; Silver non alimenté.",
                [f"_schema/{source}/v{v}.report.md", audit_link],
            ))  # fmt: skip
        if step == "promote":
            rq = s["rows_quarantined"] or 0
            total = s["rows_out"] or run["rows_in"] or 0
            ratio = rq / total if total else (1.0 if rq else 0.0)
            qr = th.quarantine_rate
            if qr.enabled and (ratio > qr.max_ratio or s["status"] == "quarantined"):
                out.append(_alert(
                    "quarantine_rate", qr, source, run_id, "promote:quarantine",
                    f"Quarantaine : {source}",
                    f"{rq} ligne(s) en quarantaine sur {total} ({ratio:.0%}, seuil "
                    f"{qr.max_ratio:.0%}).", [audit_link],
                ))  # fmt: skip
            score, qs = s["quality_score"], th.quality_score_below
            if qs.enabled and score is not None and score < qs.min_score:
                out.append(_alert(
                    "quality_score_below", qs, source, run_id, "promote:score",
                    f"Score qualité bas : {source}",
                    f"Score {score:.1f} < {qs.min_score:.0f}.", [audit_link],
                ))  # fmt: skip
    return out


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def freshness_alerts(audit: AuditStore, policy: AlertPolicy, now: datetime) -> list[Alert]:
    """SLA freshness: sources whose last successful run is older than `max_age_hours`."""
    th = policy.thresholds.sla_freshness
    if not th.enabled:
        return []
    r = run_audit
    stmt = (
        select(r.c.source_name, func.max(r.c.finished_at))
        .where(r.c.status == "success")
        .group_by(r.c.source_name)
    )
    try:
        with audit.engine.connect() as c:
            rows = list(c.execute(stmt))
    except SQLAlchemyError as exc:
        raise AlertError(f"lecture de l'audit impossible ({type(exc).__name__})") from None
    out = []
    for src, last in sorted(rows):
        if last is None:
            continue
        age = (now - _utc(last)).total_seconds() / 3600
        if age > th.max_age_hours:
            out.append(_alert("sla_freshness", th, src, None, "stale", f"Données périmées : {src}",
                              f"Dernier run réussi il y a {age:.0f} h (SLA {th.max_age_hours:.0f} h)."))  # fmt: skip  # noqa: E501
    return out


# --- dedup state ---------------------------------------------------------------------------
alert_state = Table(
    "alert_state",
    metadata,
    Column("dedup_key", String(32), primary_key=True),
    Column("source_name", String(255), nullable=False),
    Column("alert_type", String(32), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("last_sent", DateTime(timezone=True), nullable=False),
    Column("occurrences", Integer, nullable=False),
    Column("suppressed", Integer, nullable=False),
    Column("last_run_id", String(64), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Index("ix_alert_state_source", "source_name"),
)


class AlertStore:
    """Dedup state of alerts (see module docstring). Raises AlertError on DB failure."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def claim(
        self, alert: Alert, run_id: str, now: datetime, window: timedelta
    ) -> tuple[Decision, int]:
        """Decide send / suppressed / duplicate atomically; returns (decision, occurrences)."""
        t, key = alert_state, alert.dedup_key
        try:
            try:
                with self._engine.begin() as c:
                    c.execute(insert(t).values(
                        dedup_key=key, source_name=alert.source, alert_type=alert.type,
                        severity=alert.severity, first_seen=now, last_seen=now, last_sent=now,
                        occurrences=1, suppressed=0, last_run_id=run_id,
                    ))  # fmt: skip
                return "send", 1
            except IntegrityError:
                pass  # already known: decide with conditional updates
            mine = (t.c.dedup_key == key, t.c.last_run_id != run_id)
            with self._engine.begin() as c:
                n = c.execute(
                    update(t)
                    .where(*mine, or_(t.c.last_sent <= now - window, t.c.resolved_at.is_not(None)))
                    .values(last_sent=now, last_seen=now, occurrences=t.c.occurrences + 1,
                            suppressed=0, last_run_id=run_id, resolved_at=None,
                            severity=alert.severity)
                    .returning(t.c.occurrences)
                ).scalar()  # fmt: skip
                if n is not None:
                    return "send", int(n)
                n = c.execute(
                    update(t).where(*mine)
                    .values(last_seen=now, occurrences=t.c.occurrences + 1,
                            suppressed=t.c.suppressed + 1, last_run_id=run_id)
                    .returning(t.c.occurrences)
                ).scalar()  # fmt: skip
                if n is not None:
                    return "suppressed", int(n)
                cur = c.execute(select(t.c.occurrences).where(t.c.dedup_key == key)).scalar_one()
                return "duplicate", int(cur)
        except SQLAlchemyError as exc:
            raise AlertError(f"état des alertes indisponible ({type(exc).__name__})") from None

    def resolve(self, source: str, now: datetime) -> int:
        """Close the open alerts of a source; returns how many were open."""
        t = alert_state
        try:
            with self._engine.begin() as c:
                res = c.execute(
                    update(t).where(t.c.source_name == source, t.c.resolved_at.is_(None))
                    .values(resolved_at=now)
                )  # fmt: skip
        except SQLAlchemyError as exc:
            raise AlertError(f"état des alertes indisponible ({type(exc).__name__})") from None
        return int(res.rowcount or 0)


# --- sanitisation and payloads -------------------------------------------------------------
_URL = re.compile(r"(?i)\b(?:https?|ftp|file|data|javascript|mailto):\S*|\bwww\.\S+")
_DROP = str.maketrans({"*": None, "~": None, "|": None, "`": None, "<": "‹", ">": "›"})


def sanitize(text: str, max_len: int = MAX_TEXT) -> str:
    """Neutralise mentions, links, markup and control characters; bound the length."""
    text = "".join(
        c if c == "\n" or not unicodedata.category(c).startswith("C") else " " for c in text
    )
    text = _URL.sub("[lien retiré]", text).translate(_DROP).replace("@", "@​")
    text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _title(a: Alert) -> str:
    return sanitize(f"[{a.severity.upper()}] {a.title}", 150)


def _body(a: Alert) -> str:
    parts = [a.summary]
    if a.run_id:
        parts.append(f"run_id : {a.run_id}")
    if a.links:
        parts.append("Liens : " + ", ".join(a.links))
    if a.rca:
        parts.append("Cause probable (proposition à vérifier) :\n" + a.rca)
    return sanitize("\n".join(parts))


def slack_payload(a: Alert) -> dict[str, Any]:
    """Slack incoming webhook: plain_text blocks only (no mrkdwn, no links, no mentions)."""
    title = _title(a)
    return {
        "text": sanitize(f"{title} : {a.summary}", 300),
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            {"type": "section", "text": {"type": "plain_text", "text": _body(a)}},
        ],
    }


def discord_payload(a: Alert) -> dict[str, Any]:
    """Discord webhook: embed, every mention type disabled."""
    title = _title(a)
    return {
        "content": title,
        "allowed_mentions": {"parse": []},
        "embeds": [{"title": title, "description": _body(a)[:4096], "color": _COLOR[a.severity]}],
    }


# --- delivery ------------------------------------------------------------------------------
class AlertSender(Protocol):
    """Delivers one alert; returns False on failure and never raises."""

    def send(self, alert: Alert) -> bool: ...


class _Transient(Exception):  # noqa: N818 - internal retry signal
    """Retryable delivery failure; args hold a code, never the URL."""


def _check_url(url: str, allowed: Sequence[str], allow_localhost: bool) -> Kind | None:
    """Kind detected from the host (None for an opted-in localhost). Raises ConfigError."""
    refused = ConfigError("URL de webhook refusée (https, hôte autorisé et chemin attendu requis)")
    try:
        p = urlsplit(url.strip())
        host, port = (p.hostname or "").lower(), p.port
    except ValueError:
        raise refused from None
    if not host or p.username or p.password:
        raise refused
    if host in _LOCAL:
        if not allow_localhost or p.scheme not in ("http", "https"):
            raise refused
        return None
    if p.scheme != "https" or port not in (None, 443) or host not in allowed:
        raise refused
    kind = _KIND_BY_HOST.get(host)
    if kind is not None and not p.path.startswith(_PATH[kind]):
        raise refused
    return kind  # type: ignore[return-value]


class WebhookSender:
    """Slack or Discord incoming webhook (see module docstring for the safety rules)."""

    def __init__(
        self,
        url: SecretStr,
        *,
        allowed_hosts: Sequence[str] = DEFAULT_HOSTS,
        kind: Kind | None = None,
        timeout_s: float = 5.0,
        attempts: int = 3,
        wait_s: float = 0.5,
        allow_localhost: bool = False,
    ) -> None:
        detected = _check_url(url.get_secret_value(), [h.lower() for h in allowed_hosts],
                              allow_localhost)  # fmt: skip
        if detected and kind and detected != kind:
            raise ConfigError("type de webhook incohérent avec l'hôte")
        if not (detected or kind):
            raise ConfigError("type de webhook inconnu (slack ou discord)")
        if not 1 <= attempts <= 5:
            raise ConfigError("attempts doit être dans [1, 5]")
        self.kind: Kind = detected or kind  # type: ignore[assignment]
        self._url = url
        self._timeout = timeout_s
        self._attempts = attempts
        self._wait = wait_s

    def __repr__(self) -> str:
        return f"WebhookSender(kind={self.kind!r})"

    def payload(self, alert: Alert) -> dict[str, Any]:
        return slack_payload(alert) if self.kind == "slack" else discord_payload(alert)

    def _post(self, payload: dict[str, Any]) -> int:
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                status = client.post(self._url.get_secret_value(), json=payload).status_code
        except httpx.TransportError as exc:
            raise _Transient(type(exc).__name__) from None
        if status == 429 or status >= 500:
            raise _Transient(f"http_{status}")
        return status

    def send(self, alert: Alert) -> bool:
        """POST with bounded retries; False (logged) on failure, never an exception."""
        payload = self.payload(alert)
        status = None
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self._attempts),
                wait=wait_exponential(multiplier=self._wait, max=5),
                retry=retry_if_exception_type(_Transient), reraise=True,
            ):  # fmt: skip
                with attempt:
                    status = self._post(payload)
        except Exception as exc:  # delivery must never fail a run
            why = str(exc) if isinstance(exc, _Transient) else type(exc).__name__
            log.warning("alert_send_failed", kind=self.kind, error=why, source=alert.source,
                        run_id=alert.run_id)  # fmt: skip
            return False
        if status is None or not 200 <= status < 300:
            log.warning("alert_send_failed", kind=self.kind, error=f"http_{status}",
                        source=alert.source, run_id=alert.run_id)  # fmt: skip
            return False
        log.info("alert_sent", kind=self.kind, alert_type=alert.type, source=alert.source,
                 run_id=alert.run_id)  # fmt: skip
        return True


def sender_from_env(policy: AlertPolicy) -> WebhookSender | None:
    """Sender from `ADP_ALERT_WEBHOOK_URL`; None (alerting disabled) if unset or refused."""
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        return None
    try:
        return WebhookSender(SecretStr(url), allowed_hosts=policy.allowed_hosts)
    except ConfigError:
        log.warning("alert_webhook_refused")  # the URL itself is never logged
        return None


# --- orchestration -------------------------------------------------------------------------
def _group(alerts: Sequence[Alert], source: str) -> Alert:
    if len(alerts) == 1:
        return alerts[0]
    top = max(alerts, key=lambda a: _RANK[a.severity])
    links = tuple(dict.fromkeys(link for a in alerts for link in a.links))
    return top.model_copy(update={
        "title": f"{len(alerts)} alertes : {source}",
        "summary": "\n".join(f"- {a.title} : {a.summary}" for a in alerts), "links": links,
    })  # fmt: skip


def dispatch(
    alerts: Sequence[Alert], run_key: str, *, store: AlertStore, sender: AlertSender,
    policy: AlertPolicy, now: datetime, rca: Callable[[], str | None] | None = None,
) -> list[Alert]:  # fmt: skip
    """Quiet hours -> dedup -> one grouped message (+ RCA). Returns the alerts delivered."""
    window = timedelta(minutes=policy.dedup_window_minutes)
    qh = policy.quiet_hours
    todo: list[Alert] = []
    for a in alerts:
        if qh is not None and qh.active(now) and _RANK[a.severity] < _RANK[qh.min_severity]:
            log.info("alert_quiet", alert_type=a.type, source=a.source, run_id=a.run_id)
            continue
        decision, n = store.claim(a, run_key, now, window)
        log.info("alert_" + decision, alert_type=a.type, source=a.source, run_id=a.run_id,
                 occurrences=n)  # fmt: skip
        if decision == "send":
            todo.append(a if n == 1 else a.model_copy(
                update={"summary": f"{a.summary} (vu {n} fois)"}))  # fmt: skip
    if not todo:
        return []
    msg = _group(todo, todo[0].source)
    if rca is not None:
        try:
            msg = msg.model_copy(update={"rca": rca()})
        except Exception as exc:  # RCA is a bonus: the alert still goes out
            log.warning("rca_failed", error=type(exc).__name__, run_id=msg.run_id)
    return list(todo) if sender.send(msg) else []


def _rca_text(audit: AuditStore, run_id: str, source: str, policy: AlertPolicy,
              provider: Any) -> str:  # fmt: skip
    from adp.config import get_settings
    from adp.mapping.llm import BudgetLedger
    from adp.observability.rca import ClaudeRCAProvider, analyze, build_context, render_rca

    ctx = build_context(run_id, source, audit=audit, registry=SchemaRegistry(audit.engine),
                        max_chars=policy.rca.max_context_chars)  # fmt: skip
    budget = cache = None
    if provider is None and policy.rca.use_llm:
        s = get_settings()
        if s.anthropic_api_key is not None and s.anthropic_api_key.get_secret_value().strip():
            provider = ClaudeRCAProvider(s.anthropic_api_key, model=s.adp_llm_model)
            budget = BudgetLedger(s.adp_llm_cache_dir / "budget.json",
                                  s.adp_llm_monthly_budget_eur)  # fmt: skip
            cache = s.adp_llm_cache_dir / "rca"
    return render_rca(analyze(ctx, provider, budget=budget, cache_dir=cache))


def notify_run(
    audit: AuditStore, run_id: str, source: str, *, step: str | None = None,
    sender: AlertSender | None = None, policy: AlertPolicy | None = None,
    provider: Any = None, now: datetime | None = None,
) -> list[Alert]:  # fmt: skip
    """Evaluate and deliver the alerts of a finished run. Disabled (returns []) without a
    webhook; NEVER raises. `step` = the step that just ended (a success is only final at
    `promote`)."""
    try:
        policy = policy or load_alert_policy(Path(os.environ.get("ADP_ALERTS_CONFIG",
                                                                 DEFAULT_CONFIG)))  # fmt: skip
        sender = sender or sender_from_env(policy)
        if sender is None:
            return []
        run = audit.run(run_id, source)
        if run is None or run["status"] == "running":
            return []
        if run["status"] == "success" and step not in (None, "promote"):
            return []  # not the end of the chain yet
        now = now or datetime.now(UTC)
        store = AlertStore(audit.engine)
        alerts = evaluate_alerts(audit, run_id, source, policy)
        if not alerts:
            if run["status"] != "success" or not store.resolve(source, now):
                return []
            rec = Alert(type="recovered", source=source, severity="info", motif="recovered",
                        title=f"Rétablissement : {source}",
                        summary="Run réussi : les alertes ouvertes de la source sont closes.",
                        run_id=run_id, links=(f"/audit/runs/{run_id}/steps",))  # fmt: skip
            return [rec] if sender.send(rec) else []
        return dispatch(alerts, run_id, store=store, sender=sender, policy=policy, now=now,
                        rca=lambda: _rca_text(audit, run_id, source, policy, provider))  # fmt: skip
    except Exception as exc:  # alerting must never fail a run
        log.warning("alert_notify_failed", source=source, run_id=run_id,
                    error=type(exc).__name__)  # fmt: skip
        return []


def sample_alert() -> Alert:
    """Synthetic alert for `adp alerts test` (no real data)."""
    return Alert(type="schema_blocked", source="demo", severity="warning", motif="test",
                 title="Alerte de test : demo", summary="Message de test de la plateforme ADP.",
                 run_id="test-run", links=("/audit/runs/test-run/steps",),
                 rca="[règles]\n1. Exemple d'hypothèse (confiance 90%) ; preuves : step:schema_check:blocked")  # fmt: skip  # noqa: E501
