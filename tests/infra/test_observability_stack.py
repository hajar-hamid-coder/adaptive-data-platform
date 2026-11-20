"""Stack d'observabilité as code (P6-04), sans Docker : fichiers valides, cohérence, sécurité.

Ce qui est vérifié : YAML/JSON parsables ; dashboard « ADP Overview » (titre, uid, refresh, plage) ;
chaque métrique `adp_*` des requêtes PromQL figure dans le tableau de docs/observability.md (et
inversement, chaque métrique documentée est utilisée) ; images Prometheus/Grafana épinglées ;
ports publiés liés à 127.0.0.1 ; scrape Bearer depuis un fichier secret monté ; Grafana sans
compte administrateur ni inscription ; aucun secret littéral dans infra/ ni dans le compose.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
INFRA = ROOT / "infra"
COMPOSE = ROOT / "docker-compose.yml"
PROM_CFG = INFRA / "prometheus" / "prometheus.yml"
DATASOURCE = INFRA / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
PROVIDER = INFRA / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
DASHBOARD = INFRA / "grafana" / "dashboards" / "adp-overview.json"
DOC = ROOT / "docs" / "observability.md"

METRIC = re.compile(r"\badp_[a-z_]+")
DOC_ROW = re.compile(r"^\|\s*`(adp_[a-z_]+)`", re.MULTILINE)
_KEYWORD = r"(?i:password|passwd|secret|token|api[_-]?key)"
_VALUE = r"[\"']?(?![\s\"']|REPLACE)[^\s\"'#]+"
SECRET_LITERAL = re.compile(rf"sha256:[0-9a-f]{{64}}|{_KEYWORD}[\"']?[:=][ \t]*{_VALUE}")
SUBSTITUTION = re.compile(r"\$\{[^}]*\}")  # ${VAR:-valeur_de_dev} : défaut local, pas un secret


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return _load(COMPOSE)


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _doc_metrics() -> set[str]:
    return set(DOC_ROW.findall(DOC.read_text(encoding="utf-8")))


def _base(name: str) -> str:
    """Nom exposé -> nom documenté : `_bucket`/`_sum`/`_count` ramenés au nom d'histogramme."""
    return re.sub(r"_(bucket|sum|count)$", "", name)


def _dashboard_exprs(dash: dict[str, Any]) -> list[str]:
    return [t["expr"] for p in dash["panels"] for t in p.get("targets", []) if "expr" in t]


def test_config_files_parse() -> None:
    for path in (COMPOSE, PROM_CFG, DATASOURCE, PROVIDER):
        assert _load(path) is not None, path
    json.loads(DASHBOARD.read_text(encoding="utf-8"))


def test_dashboard_identity(dashboard: dict[str, Any]) -> None:
    assert dashboard["title"] == "ADP Overview"
    assert dashboard["uid"] == "adp-overview"
    assert dashboard["refresh"] == "30s"
    assert dashboard["time"] == {"from": "now-6h", "to": "now"}
    assert dashboard["tags"]
    assert dashboard["schemaVersion"] >= 41
    assert dashboard["templating"]["list"][0]["name"] == "source"


def test_dashboard_panel_ids_unique(dashboard: dict[str, Any]) -> None:
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    assert all(p["datasource"]["uid"] == "prometheus" for p in dashboard["panels"])


def test_dashboard_metrics_are_documented(dashboard: dict[str, Any]) -> None:
    used = {_base(m) for expr in _dashboard_exprs(dashboard) for m in METRIC.findall(expr)}
    documented = _doc_metrics()
    assert documented, "tableau de docs/observability.md introuvable"
    assert used - documented == set(), "métriques du dashboard absentes de docs/observability.md"


def test_documented_metrics_are_all_on_dashboard(dashboard: dict[str, Any]) -> None:
    used = {_base(m) for expr in _dashboard_exprs(dashboard) for m in METRIC.findall(expr)}
    assert _doc_metrics() - used == set()


def test_compose_pins_observability_images(compose: dict[str, Any]) -> None:
    for name in ("prometheus", "grafana"):
        image = compose["services"][name]["image"]
        assert ":" in image and not image.endswith(":latest"), image


def test_compose_publishes_ports_on_loopback_only(compose: dict[str, Any]) -> None:
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"


def test_compose_has_healthchecks_and_volumes(compose: dict[str, Any]) -> None:
    for name in ("prometheus", "grafana"):
        assert "healthcheck" in compose["services"][name], name
    assert {"prometheus-data", "grafana-data"} <= set(compose["volumes"])


def test_compose_secret_mounted_as_directory_read_only(compose: dict[str, Any]) -> None:
    volumes = compose["services"]["prometheus"]["volumes"]
    secret = [v for v in volumes if v.endswith(":/run/secrets:ro")]
    assert len(secret) == 1
    # Un bind mount de fichier absent créerait un répertoire : on monte le dossier.
    assert secret[0].startswith("./infra/secrets:")


def test_prometheus_scrapes_api_with_bearer_secret_file() -> None:
    job = next(j for j in _load(PROM_CFG)["scrape_configs"] if j["job_name"] == "adp-api")
    assert job["metrics_path"] == "/metrics"
    assert job["scrape_interval"] == "15s"
    assert job["authorization"]["type"] == "Bearer"
    assert job["authorization"]["credentials_file"].startswith("/run/secrets/")
    targets = [t for s in job["static_configs"] for t in s["targets"]]
    assert targets == ["host.docker.internal:8000"]


def test_grafana_is_locked_down(compose: dict[str, Any]) -> None:
    env = compose["services"]["grafana"]["environment"]
    assert "GF_SECURITY_ADMIN_PASSWORD" not in env
    assert env["GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION"] == "true"
    assert env["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert env["GF_ANALYTICS_REPORTING_ENABLED"] == "false"
    assert env["GF_ANALYTICS_CHECK_FOR_UPDATES"] == "false"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"


def test_grafana_provisioning_is_consistent(compose: dict[str, Any]) -> None:
    ds = _load(DATASOURCE)["datasources"][0]
    assert ds["uid"] == "prometheus" and ds["type"] == "prometheus"
    assert ds["url"] == "http://prometheus:9090"
    assert not {"basicAuth", "secureJsonData", "password"} & set(ds)
    provider = _load(PROVIDER)["providers"][0]
    assert provider["allowUiUpdates"] is False
    assert provider["options"]["path"] in (
        v.split(":")[1] for v in compose["services"]["grafana"]["volumes"]
    )


def _secret_files() -> list[Path]:
    files = [COMPOSE, PROM_CFG, DATASOURCE, PROVIDER, DASHBOARD]
    files += [p for p in INFRA.rglob("*") if p.is_file() and "secrets" not in p.parts]
    files += [p for p in (INFRA / "secrets").glob("*") if p.suffix in {".md", ".example"}]
    return files


def test_no_secret_literal_in_repo_config() -> None:
    hits = {
        str(p.relative_to(ROOT)): m.group(0)
        for p in _secret_files()
        if (m := SECRET_LITERAL.search(SUBSTITUTION.sub("", p.read_text(encoding="utf-8"))))
    }
    assert hits == {}


@pytest.mark.skipif(shutil.which("git") is None, reason="git requis")
def test_secret_files_are_git_ignored() -> None:
    def ignored(rel: str) -> bool:
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT, check=False)
        return r.returncode == 0

    assert ignored("infra/secrets/adp_scrape_key")
    assert not ignored("infra/secrets/README.md")
    assert not ignored("infra/secrets/adp_scrape_key.example")
