"""Declarative source contract (SourceSpec) and YAML loaders."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.errors import SourceSpecError


class SourceSpec(BaseModel):
    """Description of one source.

    `auth_ref` is the NAME of an env var / secret holding the credential, never the value.
    It is not checked against secret patterns; callers must keep values out of YAML.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["api", "file", "database"]
    uri: str = Field(min_length=1)
    format: Literal["csv", "json", "xlsx", "parquet"]
    frequency: Literal["daily", "weekly", "monthly", "manual"]
    primary_key: list[str] = Field(min_length=1)
    auth_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")


def load_source(path: Path) -> SourceSpec:
    """Load and validate one YAML source file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SourceSpecError(f"{path}: cannot read YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SourceSpecError(f"{path}: expected a YAML mapping at top level")
    try:
        return SourceSpec.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
        )
        raise SourceSpecError(f"{path}: invalid source spec: {details}") from exc


def load_registry(directory: Path) -> dict[str, SourceSpec]:
    """Load every `*.yaml` in `directory`; source names must be unique."""
    registry: dict[str, SourceSpec] = {}
    origin: dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        spec = load_source(path)
        if spec.name in registry:
            raise SourceSpecError(
                f"{path}: duplicate source name '{spec.name}' (already defined in "
                f"{origin[spec.name]})"
            )
        registry[spec.name] = spec
        origin[spec.name] = path
    return registry
