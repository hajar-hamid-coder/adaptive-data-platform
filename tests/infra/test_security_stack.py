"""Sécurité as code (P6-07), sans Docker : Caddyfile, compose, CI de sécurité, aucun secret."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
CADDYFILE = (ROOT / "infra" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
CI: dict[str, Any] = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_caddy_image_pinned_and_ports_loopback() -> None:
    svc = COMPOSE["services"]["caddy"]
    assert re.fullmatch(r"caddy:\d+\.\d+\.\d+(-alpine)?", svc["image"]), svc["image"]
    assert svc["ports"] and all(p.startswith("127.0.0.1:") for p in svc["ports"])
    assert "./infra/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" in svc["volumes"]
    assert svc["healthcheck"]


def test_caddyfile_tls_headers_and_proxy() -> None:
    assert "tls internal" in CADDYFILE
    for h in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options",
              "Content-Security-Policy", "Referrer-Policy"):  # fmt: skip
        assert h in CADDYFILE, h
    assert "reverse_proxy host.docker.internal:8000" in CADDYFILE
    assert "redir https://" in CADDYFILE  # HTTP -> HTTPS
    assert "admin off" in CADDYFILE


def test_caddy_logs_never_carry_credentials() -> None:
    for h in ("X-Api-Key", "Authorization", "Cookie"):
        assert f"request>headers>{h} delete" in CADDYFILE, h


def test_no_certificate_or_secret_in_caddy_dir() -> None:
    for f in (ROOT / "infra" / "caddy").rglob("*"):
        assert f.suffix not in {".pem", ".key", ".crt"}, f
        if f.is_file():
            assert "PRIVATE KEY" not in f.read_text(encoding="utf-8"), f


def test_ci_security_job() -> None:
    assert CI["permissions"] == {"contents": "read"}
    steps = CI["jobs"]["security"]["steps"]
    text = yaml.safe_dump(steps)
    assert "pip-audit" in text and "npm audit --audit-level=high" in text
    leaks = next(s for s in steps if s.get("uses", "").startswith("gitleaks/gitleaks-action@"))
    assert re.fullmatch(r"[0-9a-f]{40}", leaks["uses"].split("@")[1])  # pinned SHA
    assert {"ci", "frontend", "e2e"} <= set(CI["jobs"])  # untouched siblings
    assert (ROOT / ".gitleaks.toml").exists()
