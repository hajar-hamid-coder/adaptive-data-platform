import importlib.util
from pathlib import Path

import pytest

from adp.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "POSTGRES_PORT", "ADP_S3_ENDPOINT", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()


def test_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.postgres_port == 5433
    assert s.adp_s3_endpoint == "http://127.0.0.1:9000"
    assert s.anthropic_api_key is None


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "6000")
    monkeypatch.setenv("ADP_S3_ENDPOINT", "http://minio:9000")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.postgres_port == 6000
    assert s.adp_s3_endpoint == "http://minio:9000"


def test_secrets_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2-xyz")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "hunter2-xyz" not in repr(s)
    assert "hunter2-xyz" not in str(s)
    assert s.postgres_password.get_secret_value() == "hunter2-xyz"


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()


def _load_checker():  # type: ignore[no-untyped-def]
    path = Path(__file__).parent.parent / "scripts" / "check_secrets.py"
    spec = importlib.util.spec_from_file_location("check_secrets", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_secrets_flags_and_passes(tmp_path: Path) -> None:
    mod = _load_checker()
    bad = tmp_path / "bad.txt"
    bad.write_text("key = " + "AKIA" + "ABCDEFGHIJKLMNOP" + "\n")
    good = tmp_path / "good.txt"
    good.write_text("nothing here\n")
    assert mod.main([str(bad)]) == 1
    assert mod.main([str(good)]) == 0
    pem = tmp_path / "k.pem"
    pem.write_text("-----BEGIN " + "RSA PRIVATE KEY-----\n")
    assert mod.main([str(pem)]) == 1
