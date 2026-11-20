"""RBAC (P6-07): roles, capability matrix, review endpoints, anti-oubli, no leak, constant time."""

import hashlib
import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from structlog.testing import capture_logs

from adp.api import rbac
from adp.api.deps import Deps
from adp.api.main import create_app
from adp.api.rbac import CAPABILITY_ROLES, Capability, Role, parse_keys
from adp.bronze.store import LocalStore
from adp.config import Settings
from adp.gold.contract import load_products
from adp.mapping.decision import Proposal, ReviewQueue, mapping_decision_log
from adp.quality.quarantine import QuarantineStore
from adp.quality.score import QualityScoreStore
from adp.schema.registry import SchemaRegistry, metadata

SENTINEL = "ZZ_RATIONALE_SENTINEL"
KEYS = {"viewer": "k-view", "steward": "k-steward", "admin": "k-admin", "scraper": "k-scrape"}
RAW = (
    "alice:viewer:k-view,bob:steward:k-steward,"
    f"carol:admin:sha256:{hashlib.sha256(b'k-admin').hexdigest()},prom:scraper:k-scrape"
)


def _h(role: str) -> dict[str, str]:
    return {"X-API-Key": KEYS[role]}


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    metadata.create_all(eng)
    return eng


@pytest.fixture
def queue(engine: Engine) -> ReviewQueue:
    return ReviewQueue(engine)


@pytest.fixture
def item_ids(queue: ReviewQueue) -> list[int]:
    props = [
        Proposal(source_col=c, target="region", score=0.7, method="synonym", origin="rules",
                 rationale=SENTINEL)
        for c in ("a_col", "b_col", "c_col")
    ]  # fmt: skip
    return queue.submit(props, "hcp").queued


@pytest.fixture
def app(tmp_path: Path, engine: Engine, queue: ReviewQueue) -> FastAPI:
    deps = Deps(
        {p.name: p for p in load_products()}, LocalStore(tmp_path / "g"), SchemaRegistry(engine),
        QualityScoreStore(engine), QuarantineStore(LocalStore(tmp_path / "q")),
        tmp_path / "reports", review=queue,
    )  # fmt: skip
    return create_app(Settings(_env_file=None, adp_api_keys=SecretStr(RAW)), deps)  # type: ignore[call-arg]


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# --- key parsing ----------------------------------------------------------------------------
def test_parse_formats_and_default_role_is_viewer() -> None:
    h = hashlib.sha256(b"x").hexdigest()
    got = parse_keys(f"raw1,sha256:{h},n1:steward:raw2,n2:admin:sha256:{h}")
    assert [(e.principal.name, e.principal.role) for e in got] == [
        ("key-0", Role.VIEWER), ("key-1", Role.VIEWER), ("n1", Role.STEWARD), ("n2", Role.ADMIN),
    ]  # fmt: skip
    named = parse_keys("n:viewer:raw1")[0]
    assert got[0].digest == hashlib.sha256(b"raw1").digest() == named.digest


def test_malformed_entries_ignored_with_secretless_log() -> None:
    bad = ["bob:root:topsecret1", "bad name:admin:topsecret2", "n:admin:sha256:zz",
           "sha256:nothex", "a:b"]  # fmt: skip
    with capture_logs() as logs:
        got = parse_keys(",".join(bad) + ",ok")
        assert [e.digest for e in got] == [e.digest for e in parse_keys("ok")] != []
    assert len([x for x in logs if x["event"] == "api_key_entry_ignored"]) == len(bad)
    assert "topsecret" not in repr(logs)


# --- authentication and authorization --------------------------------------------------------
def test_401_without_or_with_unknown_key(client: TestClient, item_ids: list[int]) -> None:
    for call in (lambda **k: client.get("/review", **k),
                 lambda **k: client.post(f"/review/{item_ids[0]}/approve", **k)):  # fmt: skip
        assert call().status_code == 401
        r = call(headers={"X-API-Key": "nope"})
        assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_viewer_and_scraper_cannot_decide(
    client: TestClient, queue: ReviewQueue, item_ids: list[int], action: str
) -> None:
    for role in ("viewer", "scraper"):
        r = client.post(f"/review/{item_ids[0]}/{action}", headers=_h(role))
        assert r.status_code == 403 and r.json() == {"detail": "Forbidden"}
    assert queue.get(item_ids[0]).status == "pending"  # type: ignore[union-attr]


@pytest.mark.parametrize("role", ["steward", "admin"])
def test_steward_and_admin_decide_with_principal_as_by(
    client: TestClient, queue: ReviewQueue, engine: Engine, item_ids: list[int], role: str
) -> None:
    who = {"steward": "bob", "admin": "carol"}[role]
    r = client.post(f"/review/{item_ids[0]}/approve", headers=_h(role), json={"note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert r.json()["decided_by"] == who
    with engine.connect() as c:
        rows = list(c.execute(select(mapping_decision_log).where(
            mapping_decision_log.c.queue_id == item_ids[0],
            mapping_decision_log.c.action == "approve")))  # fmt: skip
    assert [x.actor for x in rows] == [who]
    r = client.post(f"/review/{item_ids[1]}/reject", headers=_h(role))
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_by_cannot_be_forged_by_body(client: TestClient, item_ids: list[int]) -> None:
    r = client.post(f"/review/{item_ids[0]}/approve", headers=_h("steward"),
                    json={"by": "mallory"})  # fmt: skip
    assert r.status_code == 422
    r = client.post(f"/review/{item_ids[0]}/approve", headers=_h("steward"),
                    json={"note": "x" * 501})  # fmt: skip
    assert r.status_code == 422


def test_replay_is_idempotent_and_opposite_decision_conflicts(
    client: TestClient, engine: Engine, item_ids: list[int]
) -> None:
    url = f"/review/{item_ids[0]}"
    first = client.post(f"{url}/approve", headers=_h("steward"))
    again = client.post(f"{url}/approve", headers=_h("admin"))
    assert again.status_code == 200 and again.json() == first.json()  # decider unchanged
    with engine.connect() as c:
        n = len(list(c.execute(select(mapping_decision_log).where(
            mapping_decision_log.c.queue_id == item_ids[0],
            mapping_decision_log.c.action == "approve"))))  # fmt: skip
    assert n == 1
    assert client.post(f"{url}/reject", headers=_h("steward")).status_code == 409


def test_unknown_item_404_and_no_review_service_503(client: TestClient, app: FastAPI) -> None:
    assert client.post("/review/999/approve", headers=_h("steward")).status_code == 404
    assert client.post("/review/0/approve", headers=_h("steward")).status_code == 422
    app.state.deps = Deps(**{**app.state.deps.__dict__, "review": None})
    assert client.get("/review", headers=_h("viewer")).status_code == 503


def test_list_all_roles_filter_and_no_data_in_responses(
    client: TestClient, item_ids: list[int]
) -> None:
    for role in ("viewer", "steward", "admin"):
        r = client.get("/review?status=pending&source=hcp", headers=_h(role))
        assert r.status_code == 200 and r.json()["total"] == 3
    assert client.get("/review", headers=_h("scraper")).status_code == 403
    assert client.get("/review?status=bogus", headers=_h("viewer")).status_code == 422
    bodies = [
        client.get("/review", headers=_h("viewer")).text,
        client.post(f"/review/{item_ids[2]}/approve", headers=_h("steward")).text,
        client.post(f"/review/{item_ids[2]}/approve", headers=_h("viewer")).text,
        client.post("/review/999/reject", headers=_h("steward")).text,
        client.post(f"/review/{item_ids[2]}/reject", headers=_h("steward")).text,
    ]
    assert all(SENTINEL not in b and "rationale" not in b for b in bodies)


# --- matrix --------------------------------------------------------------------------------
MATRIX = {  # route -> (method, capability)
    "/products": ("GET", Capability.DATA_READ),
    "/indicators": ("GET", Capability.DATA_READ),
    "/schemas/pop": ("GET", Capability.DATA_READ),
    "/quality/scores": ("GET", Capability.DATA_READ),
    "/runs": ("GET", Capability.DATA_READ),
    "/catalog": ("GET", Capability.DATA_READ),
    "/audit/runs": ("GET", Capability.AUDIT_READ),
    "/metrics": ("GET", Capability.METRICS_READ),
    "/review": ("GET", Capability.REVIEW_READ),
}


@pytest.mark.parametrize("path", MATRIX)
def test_role_matrix_on_routes(client: TestClient, path: str) -> None:
    method, cap = MATRIX[path]
    for role in Role:
        code = client.request(method, path, headers=_h(role.value)).status_code
        assert (code == 403) == (role not in CAPABILITY_ROLES[cap]), (path, role, code)
        assert code != 401


def test_hierarchy_is_monotonic() -> None:
    for cap, roles in CAPABILITY_ROLES.items():
        if cap is not Capability.METRICS_READ or Role.VIEWER in roles:
            assert (Role.STEWARD in roles) or Role.VIEWER not in roles
        assert Role.ADMIN in roles
    assert CAPABILITY_ROLES[Capability.REVIEW_DECIDE] == {Role.STEWARD, Role.ADMIN}


def test_every_non_public_route_is_guarded(client: TestClient, app: FastAPI) -> None:
    """Anti-oubli, black box over the OpenAPI paths: a new unguarded route fails here.

    Without key -> 401; with the least-privileged `scraper` key -> 403 (except `/metrics`).
    """
    public = {("get", "/health")}
    seen = set()
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            if (method, path) in public:
                seen.add((method, path))
                continue
            url = path.replace("{item_id}", "1")
            url = "/".join("a" if p.startswith("{") else p for p in url.split("/"))
            r1 = client.request(method, url)
            assert r1.status_code == 401, f"route sans garde RBAC : {method} {path}"
            r2 = client.request(method, url, headers=_h("scraper"))
            assert (r2.status_code == 403) == (path != "/metrics"), f"{method} {path}"
    assert seen == public


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_metrics_accepts_bearer_only_there(client: TestClient) -> None:
    bearer = {"Authorization": f"Bearer {KEYS['scraper']}"}
    assert client.get("/metrics", headers=bearer).status_code == 200
    assert client.get("/runs", headers=bearer).status_code == 401


def test_denials_logged_without_key(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/audit/runs", headers=_h("viewer"))
        client.get("/audit/runs", headers={"X-API-Key": "leaky-key"})
    denied = [x for x in logs if x["event"] == "api_auth_denied"]
    assert [(x["status"], x.get("principal")) for x in denied] == [(403, "alice"), (401, None)]
    assert "leaky-key" not in repr(logs) and "k-view" not in repr(logs)


def test_constant_time_comparison_kept() -> None:
    src = inspect.getsource(rbac.authenticate)
    assert "hmac.compare_digest" in src and "break" not in src and "return" not in src.split(
        "for entry")[1].split("return found")[0]  # fmt: skip
