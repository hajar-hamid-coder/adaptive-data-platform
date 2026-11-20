"""End-to-end mechanics on a small dataset (no performance threshold: that is `make bench-optim`):
API usage -> journal -> recommendation -> human apply -> equivalence -> before/after report."""

import re
from pathlib import Path

import pytest

from adp.bronze.store import LocalStore
from adp.cli import main
from adp.optimizer import advisor
from adp.optimizer.report import Entry, gain, render, stats
from adp.optimizer.usage import UsageConfig, UsageLog

from .test_usage import KEY, _client, _deps


def test_stats_gain_and_render_verdicts() -> None:
    s = stats([1.0, 2.0, 3.0, 4.0])
    assert (s.n, s.p50, s.p95, s.mean) == (4, 2.0, 4.0, 2.5) and s.stdev > 0
    assert gain(100, 60) == pytest.approx(0.4) and gain(0, 1) == 0.0
    e = Entry("mv-x-00000000", "materialize_indicator", "x", "w", "m", "2026-01-01", stats([10.0]),
              stats([5.0]), 2_000_000, True, target_gain=0.3)  # fmt: skip
    md = render([e])
    assert "**50.0%**" in md and "**PASS**" in md and "2.00 Mo" in md and "vérifiée" in md
    bad = render([Entry(**{**e.__dict__, "after": stats([8.0])})])
    assert "**FAIL**" in bad
    assert render([e]) == md  # deterministic text for the same figures


def test_journal_to_report(
    tmp_path: Path, store: LocalStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    usage_dir = tmp_path / "usage"
    log = UsageLog(UsageConfig(dir=usage_dir))
    with _client(_deps(tmp_path, store, log)) as c:
        for i in range(40):  # the dashboard's frequent query
            assert c.get(f"/indicators/regional_share?offset={i * 50}",
                         headers={"X-API-Key": KEY}).status_code == 200  # fmt: skip
        for _ in range(5):
            c.get("/products/population_by_region/data", headers={"X-API-Key": KEY})
    # small dataset: the latency gate of the real config is lowered, the evidence gate is kept
    cfg = advisor.AdvisorConfig(min_p95_ms=0)
    monkeypatch.setattr(advisor, "load_advisor_config", lambda: cfg)
    common = ["--gold-dir", str(store.root), "--usage-dir", str(usage_dir)]
    assert main(["optimize", "recommend", *common]) == 0
    out = capsys.readouterr().out
    rid = re.search(r"(mv-regional_share-[0-9a-f]{8})", out).group(1)  # type: ignore[union-attr]
    assert "40 requêtes observées" in out and "keyset" not in out

    assert main(["optimize", "report", rid, *common]) == 2  # not applied yet
    assert main(["optimize", "apply", rid, "--by", "alice", "--reason", "dashboard", *common]) == 0
    assert "appliquée" in capsys.readouterr().out
    assert main(["optimize", "apply", rid, "--by", "alice", "--reason", "again", *common]) == 0
    assert "déjà active" in capsys.readouterr().out
    assert main(["optimize", "recommend", *common]) == 0
    assert f"actif : {rid}" in capsys.readouterr().out  # no redundant recommendation

    doc = tmp_path / "optimisation.md"
    assert main(["optimize", "report", rid, "--reps", "5", "--output", str(doc), *common]) == 0
    md = doc.read_text()
    assert rid in md and "Équivalence des résultats : vérifiée" in md and "Gain p95" in md
    assert "alice" in md

    assert main(["optimize", "revert", rid, "--by", "alice", "--reason", "fin", *common]) == 0
    assert "désactivée" in capsys.readouterr().out
    assert main(["optimize", "apply", "mv-unknown-00000000", "--by", "a", "--reason", "r",
                 *common]) == 2  # fmt: skip


def test_api_serves_optimised_rows_identically(tmp_path: Path, store: LocalStore) -> None:
    from adp.optimizer.apply import apply

    h = {"X-API-Key": KEY}
    with _client(_deps(tmp_path, store, None)) as c:
        before = [c.get(f"/indicators/population_growth?offset={o}", headers=h).json()
                  for o in (0, 1234, 2990)]  # fmt: skip
        pbefore = c.get("/products/population_by_region/data?offset=2500", headers=h).json()
        apply(store, "mv-population_growth-00000000", "materialize_indicator",
              "population_growth", "alice", "test")  # fmt: skip
        apply(store, "ks-population_by_region-00000000", "keyset_pagination",
              "population_by_region", "alice", "test")  # fmt: skip
        after = [c.get(f"/indicators/population_growth?offset={o}", headers=h).json()
                 for o in (0, 1234, 2990)]  # fmt: skip
        assert c.get("/products/population_by_region/data?offset=2500", headers=h).json() == pbefore
    assert after == before and before[0]["items"][0]["growth_rate"] is None
