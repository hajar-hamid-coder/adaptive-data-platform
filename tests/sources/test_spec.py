from pathlib import Path

import pytest

from adp.errors import AdpError, SourceSpecError
from adp.sources import SourceSpec, load_registry, load_source

VALID = """
name: demo_src
type: file
uri: data/demo.csv
format: csv
frequency: daily
primary_key: [id]
auth_ref: DEMO_TOKEN
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_yaml_loads(tmp_path: Path) -> None:
    spec = load_source(write(tmp_path, "a.yaml", VALID))
    assert isinstance(spec, SourceSpec)
    assert spec.name == "demo_src" and spec.primary_key == ["id"]
    assert spec.auth_ref == "DEMO_TOKEN"


def test_auth_ref_optional(tmp_path: Path) -> None:
    text = "\n".join(line for line in VALID.splitlines() if "auth_ref" not in line)
    assert load_source(write(tmp_path, "a.yaml", text)).auth_ref is None


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (VALID.replace("format: csv", "format: pdf"), "field 'format'"),
        (VALID.replace("primary_key: [id]", "primary_key: []"), "field 'primary_key'"),
        (VALID.replace("uri: data/demo.csv\n", ""), "field 'uri'"),
        (VALID + "extra: 1\n", "field 'extra'"),
        (VALID.replace("DEMO_TOKEN", "sk-abc123secret"), "field 'auth_ref'"),
        ("- just\n- a list\n", "mapping"),
        ("name: [unclosed\n", "cannot read YAML"),
    ],
)
def test_invalid_yaml_raises_clear_error(tmp_path: Path, text: str, fragment: str) -> None:
    path = write(tmp_path, "bad.yaml", text)
    with pytest.raises(SourceSpecError) as exc:
        load_source(path)
    assert "bad.yaml" in str(exc.value) and fragment in str(exc.value)
    assert isinstance(exc.value, AdpError)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SourceSpecError, match="nope.yaml"):
        load_source(tmp_path / "nope.yaml")


def test_registry_loads_and_rejects_duplicates(tmp_path: Path) -> None:
    write(tmp_path, "a.yaml", VALID)
    write(tmp_path, "b.yaml", VALID.replace("demo_src", "other"))
    assert set(load_registry(tmp_path)) == {"demo_src", "other"}
    write(tmp_path, "c.yaml", VALID)
    with pytest.raises(SourceSpecError, match="duplicate source name 'demo_src'"):
        load_registry(tmp_path)


def test_shipped_example_sources_are_valid() -> None:
    reg = load_registry(Path(__file__).parents[2] / "config" / "sources")
    assert {"world_bank_gdp", "hcp_population"} <= set(reg)
    assert reg["world_bank_gdp"].type == "api" and reg["hcp_population"].format == "csv"
