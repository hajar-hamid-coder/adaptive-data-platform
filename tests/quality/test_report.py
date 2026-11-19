"""Quality reports (P4-05): JSON + HTML per run, French readers, no raw data values."""

import json
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from adp.errors import QualityError
from adp.quality.anomaly import AnomalyConfig, AnomalyResult, detect_outliers
from adp.quality.drift import DriftResult, detect_drift
from adp.quality.quarantine import Decision, QuarantineConfig, decide
from adp.quality.report import build_report, render_html, write_report
from adp.quality.rules import QualityReport, Rule, RuleResult, evaluate
from adp.quality.score import BatchScore, score_batch

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "docs" / "samples"
RULE = TypeAdapter(Rule)
SENTINEL = "ZZ_SENTINEL_VALUE"
AS_OF = date(2026, 1, 31)
GENERATED = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)

COLUMNS = ["customer_id", "amount", "city", "last_order_date", "basket_items"]
RULES: list[Rule] = [
    RULE.validate_python(
        {"id": "customer_id_present", "type": "not_null", "column": "customer_id"}
    ),
    RULE.validate_python(
        {"id": "customer_id_unique", "type": "unique", "columns": ["customer_id"]}
    ),
    RULE.validate_python(
        {"id": "amount_range", "type": "range", "column": "amount", "min": 0, "max": 1000}
    ),
    RULE.validate_python(
        {
            "id": "city_format",
            "type": "regex",
            "column": "city",
            "pattern": "^[A-Z][a-z]+$",
            "severity": "warn",
        }
    ),
    RULE.validate_python(
        {"id": "order_fresh", "type": "freshness", "column": "last_order_date", "max_age_days": 365}
    ),
]


def _rows(n: int = 40, *, bad_amount: int = 9000, sentinel: bool = False) -> list[list[Any]]:
    """Deterministic synthetic batch: 1 null id, 1 duplicate id, 2 bad amounts, 3 bad cities."""
    rows: list[list[Any]] = []
    for i in range(n):
        cid: Any = 1000 + i
        amount: Any = 20 + (i * 37) % 180
        city = "Lyon" if i % 3 else "lyon"
        basket: Any = 3 + i % 5
        rows.append([cid, amount, city, AS_OF - timedelta(days=i), basket])
    rows[5][0] = None  # not_null
    rows[9][0] = rows[8][0]  # unique (second occurrence)
    rows[12][1] = bad_amount  # range
    rows[20][1] = bad_amount  # range
    rows[30][4] = 60  # robust outlier on basket_items
    if sentinel:
        rows[1][2] = SENTINEL  # fails city_format, value must never leak
    return rows


def _inputs(*, sentinel: bool = False, top_n: int = 5) -> dict[str, Any]:
    rows = _rows(sentinel=sentinel)
    quality: QualityReport = evaluate(RULES, COLUMNS, rows, as_of=AS_OF)
    score: BatchScore = score_batch(COLUMNS, rows, quality, RULES)
    anomalies = {
        "amount": detect_outliers(
            [r[1] for r in rows], method="iqr", config=AnomalyConfig(min_samples=8)
        ),
        "basket_items": detect_outliers(
            [r[4] for r in rows], method="robust_zscore", config=AnomalyConfig(min_samples=8)
        ),
    }
    drift = {
        "amount": detect_drift(
            [20 + (i * 37) % 180 for i in range(60)],
            [60 + (i * 37) % 180 for i in range(60)],
            column="amount",
        )
    }
    decision = decide(quality, QuarantineConfig(), score)
    return {
        "run_id": "run-2026-01-31-001",
        "source": "clients_synthetic",
        "generated_at": GENERATED,
        "quality": quality,
        "score": score,
        "anomalies": anomalies,
        "drift": drift,
        "decision": decision,
        "top_n": top_n,
    }


def _report(**kw: Any) -> dict[str, Any]:
    args = _inputs(**kw)
    return build_report(
        run_id=args.pop("run_id"),
        source=args.pop("source"),
        generated_at=args.pop("generated_at"),
        **args,
    )


class _Checker(HTMLParser):
    VOID = {"meta", "link", "br", "img", "input", "hr", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>, open={self.stack[-1:]}")
            return
        self.stack.pop()


def _check_html(html: str) -> _Checker:
    checker = _Checker()
    checker.feed(html)
    checker.close()
    assert not checker.errors, checker.errors
    assert checker.stack == [], f"unclosed tags: {checker.stack}"
    return checker


def test_build_report_is_json_serialisable_with_stable_schema() -> None:
    report = _report()
    assert report["schema_version"] == 1
    assert report["run_id"] == "run-2026-01-31-001"
    assert report["generated_at"] == "2026-01-31T12:00:00+00:00"
    assert set(report) == {
        "schema_version",
        "run_id",
        "source",
        "generated_at",
        "summary",
        "columns",
        "failed_rules",
        "top_anomalies",
        "drift",
        "quarantine",
    }
    json.dumps(report)  # must not raise


def test_summary_carries_global_and_dimension_scores() -> None:
    summary = _report()["summary"]
    assert summary["status"] == "scored"
    assert isinstance(summary["score"], float)
    assert summary["rows_total"] == 40
    assert summary["rows_failed"] >= 1
    assert set(summary["dimensions"]) == {"completeness", "validity", "uniqueness", "freshness"}


def test_failed_rules_list_only_failures_with_counts_and_rows() -> None:
    failed = {r["rule_id"]: r for r in _report()["failed_rules"]}
    assert "amount_range" in failed and failed["amount_range"]["failed_count"] == 2
    assert failed["amount_range"]["severity"] == "error"
    assert failed["amount_range"]["failed_rows"] == [12, 20]
    assert "customer_id_present" in failed and failed["customer_id_present"]["failed_count"] == 1
    assert "city_format" in failed and failed["city_format"]["severity"] == "warn"
    assert "order_fresh" not in failed  # passing rule is not listed


def test_top_anomalies_are_ranked_and_limited() -> None:
    report = _report(top_n=1)
    assert len(report["top_anomalies"]) == 1
    default = _report()
    assert len(default["top_anomalies"]) <= 5
    top = default["top_anomalies"]
    counts = [a["outlier_count"] for a in top]
    assert counts == sorted(counts, reverse=True)
    assert top[0]["column"] == "amount" and top[0]["outlier_count"] >= 2
    assert all(a["outlier_count"] > 0 for a in top)


def test_columns_and_drift_and_quarantine_decision_included() -> None:
    report = _report()
    cols = {c["column"]: c for c in report["columns"]}
    assert set(cols) == set(COLUMNS)
    drift = report["drift"][0]
    assert drift["column"] == "amount" and drift["drifted"] is True
    assert drift["psi_level"] in {"moderate", "strong"}
    q = report["quarantine"]
    assert q["level"] == "row" and q["alert"] is True
    assert q["quarantined_rows"] == len(_inputs()["decision"].quarantine_rows)


def test_report_never_contains_raw_data_values() -> None:
    report = _report(sentinel=True)
    assert SENTINEL not in json.dumps(report)
    assert SENTINEL not in render_html(report)


def test_html_is_well_formed_french_and_scripts_free() -> None:
    html = render_html(_report())
    assert html.startswith("<!doctype html>")
    checker = _check_html(html)
    html_tag = next(attrs for tag, attrs in checker.tags if tag == "html")
    assert html_tag.get("lang") == "fr"
    metas = [attrs for tag, attrs in checker.tags if tag == "meta"]
    assert any("charset" in m for m in metas)
    assert any(m.get("name") == "viewport" for m in metas)
    assert not any(tag == "script" for tag, _ in checker.tags)
    assert "prefers-color-scheme: dark" in html
    assert "Score qualité" in html and "Règles en échec" in html
    assert "Anomalies" in html and "Dérive" in html and "Quarantaine" in html


def test_html_shows_score_failed_rules_and_top_anomalies() -> None:
    report = _report()
    html = render_html(report)
    assert f"{report['summary']['score']:.1f}" in html or str(report["summary"]["score"]) in html
    assert "amount_range" in html
    assert "amount" in html and "basket_items" in html


def test_html_escapes_untrusted_text() -> None:
    payload = "<script>alert(1)</script>"
    report = _report()
    report["source"] = payload
    report["failed_rules"][0]["message"] = payload
    html = render_html(report)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_write_report_names_files_after_run_id(tmp_path: Path) -> None:
    report = _report()
    json_path, html_path = write_report(report, tmp_path)
    assert json_path == tmp_path / "run-2026-01-31-001.json"
    assert html_path == tmp_path / "run-2026-01-31-001.html"
    assert json.loads(json_path.read_text(encoding="utf-8")) == report
    _check_html(html_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("run_id", ["", "../evil", "a/b", "a\\b", ".hidden", "x" * 65, "a:b"])
def test_invalid_run_id_is_refused(tmp_path: Path, run_id: str) -> None:
    args = _inputs()
    with pytest.raises(QualityError):
        build_report(
            run_id=run_id,
            source="s",
            generated_at=GENERATED,
            quality=args["quality"],
            score=args["score"],
        )
    assert list(tmp_path.iterdir()) == []


def test_sample_files_match_generator() -> None:
    """docs/samples is regenerated from synthetic data only; drift here means regenerate it."""
    report = _report()
    sample_json = json.loads((SAMPLES / "quality_report_sample.json").read_text(encoding="utf-8"))
    assert sample_json == report
    assert (SAMPLES / "quality_report_sample.html").read_text(encoding="utf-8") == render_html(
        report
    )


def test_decision_without_failures_has_no_row_level() -> None:
    quality = evaluate(RULES, COLUMNS, [[1, 10, "Lyon", AS_OF, 3]], as_of=AS_OF)
    decision: Decision = decide(quality, QuarantineConfig())
    assert decision.level == "none"
    report = build_report(
        run_id="clean-1",
        source="s",
        generated_at=GENERATED,
        quality=quality,
        score=score_batch(COLUMNS, [[1, 10, "Lyon", AS_OF, 3]], quality, RULES),
        decision=decision,
    )
    assert report["failed_rules"] == []
    assert report["top_anomalies"] == []
    assert report["quarantine"]["level"] == "none"


def test_result_types_used_by_builder_are_exported() -> None:
    assert RuleResult and AnomalyResult and DriftResult
