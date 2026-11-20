"""Advisor rules (P6-05): trigger / no trigger / insufficient evidence, stable ids."""

from pathlib import Path

import pytest

from adp.errors import ConfigError
from adp.optimizer.advisor import AdvisorConfig, load_advisor_config, rec_id, recommend
from adp.optimizer.usage import ShapeStats

CFG = AdvisorConfig()  # defaults = config/optimizer.yaml


def stats(op: str = "indicator", target: str = "regional_share", obs: int = 100,
          p95: float = 400.0, share: float = 0.6, deep: dict[str, float] | None = None
          ) -> ShapeStats:  # fmt: skip
    route = "/indicators/{name}" if op == "indicator" else "/products/{name}/data"
    return ShapeStats(
        key=f"{route} {op}:{target}||", route=route, op=op, target=target, filters=(), sort=(),  # type: ignore[arg-type]
        agg=(), observations=obs, count=obs, p50=p95 / 2, p95=p95, p99=p95, total_ms=obs * p95,
        time_share=share, deep_share=deep or {"0": 1.0}, limit=50, max_total=1_000_000, ncols=4,
    )  # fmt: skip


def test_config_file_matches_defaults_and_invalid_is_refused(tmp_path: Path) -> None:
    assert load_advisor_config() == CFG
    bad = tmp_path / "o.yaml"
    bad.write_text("advisor: {min_expected_gain: 2}\n")
    with pytest.raises(ConfigError):
        load_advisor_config(bad)


def test_materialize_indicator_triggers_with_figures() -> None:
    [r] = recommend([stats()], CFG)
    assert r.type == "materialize_indicator" and r.target == "regional_share"
    assert r.id == rec_id("materialize_indicator", "regional_share", stats().key)
    assert r.id.startswith("mv-regional_share-") and r.id == recommend([stats()], CFG)[0].id
    assert "100 requêtes" in r.justification and "400.0 ms" in r.justification
    assert "60%" in r.justification and r.expected_gain == 0.8
    assert r.storage_bytes_est == 32_000_000 and r.build_seconds_est == 0.2 and r.risk


@pytest.mark.parametrize("kw", [{"obs": 29}, {"p95": 49.0}, {"share": 0.05}])
def test_no_recommendation_without_enough_evidence(kw: dict[str, float]) -> None:
    assert recommend([stats(**kw)], CFG) == []  # type: ignore[arg-type]


def test_keyset_only_when_deep_offsets_dominate() -> None:
    shallow = stats("page", "population_by_region", deep={"0": 0.7, ">=100k": 0.3})
    assert recommend([shallow], CFG) == []
    deep = stats("page", "population_by_region", deep={"0": 0.2, "<100k": 0.3, ">=100k": 0.5})
    [r] = recommend([deep], CFG)
    assert r.type == "keyset_pagination" and r.id.startswith("ks-population_by_region-")
    assert r.expected_gain == 0.4 and "80% de pages profondes" in r.justification
    # gain below the minimum expected gain -> nothing
    assert recommend([deep], CFG.model_copy(update={"min_expected_gain": 0.5})) == []


def test_already_applied_is_not_repeated_and_order_by_time_share() -> None:
    a, b = stats(share=0.3), stats(target="population_growth", share=0.5)
    recs = recommend([a, b], CFG)
    assert [r.target for r in recs] == ["population_growth", "regional_share"]
    assert [r.target for r in recommend([a, b], CFG, applied=[recs[0].id])] == ["regional_share"]
