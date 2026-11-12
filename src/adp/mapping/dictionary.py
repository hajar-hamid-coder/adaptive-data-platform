"""Canonical fields and semantic dictionary (FR/EN synonyms, accent/case-insensitive lookup)."""

import re
import unicodedata
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from adp.errors import ConfigError

CanonicalType = Literal["integer", "float", "boolean", "date", "datetime", "string"]
Domain = Literal["geography", "time", "indicator", "identifier"]

_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(term: str) -> str:
    """Stable form: no accents, lowercase, camelCase and separators folded to '_' tokens."""
    text = unicodedata.normalize("NFKD", term)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _CAMEL_2.sub(r"\1_\2", _CAMEL_1.sub(r"\1_\2", text))
    return _NON_ALNUM.sub("_", text.lower()).strip("_")


class CanonicalField(BaseModel):
    """One target field of the canonical model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: CanonicalType
    description: str = Field(min_length=1)
    synonyms: tuple[str, ...] = ()
    unit: str | None = None
    domain: Domain | None = None

    @model_validator(mode="after")
    def _synonyms_not_empty(self) -> Self:
        for s in self.synonyms:
            if not normalize(s):
                raise ValueError(f"field '{self.name}': empty synonym {s!r}")
        return self

    def terms(self) -> frozenset[str]:
        """Normalized name and synonyms (duplicates inside one field are folded)."""
        return frozenset(normalize(t) for t in (self.name, *self.synonyms))


class SemanticDictionary(BaseModel):
    """Canonical fields with a normalized term index built once at construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: tuple[CanonicalField, ...] = Field(min_length=1)
    _index: dict[str, CanonicalField] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _build_index(self) -> Self:
        index: dict[str, CanonicalField] = {}
        names: set[str] = set()
        for f in self.fields:
            if f.name in names:
                raise ValueError(f"duplicate field name '{f.name}'")
            names.add(f.name)
            for term in sorted(f.terms()):
                other = index.get(term)
                if other is not None:
                    raise ValueError(
                        f"synonym '{term}' is assigned to both '{other.name}' and '{f.name}'"
                    )
                index[term] = f
        self._index = index
        return self

    def lookup(self, term: str) -> CanonicalField | None:
        """Exact match after normalization (case and accent insensitive)."""
        return self._index.get(normalize(term))

    def candidates(self, term: str) -> list[CanonicalField]:
        """Fields sharing at least one token with `term` (input for P3-02 scoring)."""
        tokens = set(normalize(term).split("_")) - {""}
        return [f for f in self.fields if any(tokens & set(t.split("_")) for t in f.terms())]


def load_dictionary(path: Path) -> SemanticDictionary:
    """Load and validate the semantic dictionary YAML; errors name the file and field."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at top level")
    try:
        return SemanticDictionary.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path}: invalid semantic dictionary: {details}") from exc
