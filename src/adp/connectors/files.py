"""File connector: CSV / Excel / JSON (array or JSON lines) into a raw string table."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
from charset_normalizer import from_bytes

from adp.errors import IngestionError

_EXT = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}
_WESTERN = ("cp1252", "latin_1", "iso8859_15")
_DELIMS = ",;\t|"


@dataclass(frozen=True)
class RawTable:
    """Bronze-level table: header + rows of strings (None = empty)."""

    columns: list[str]
    rows: list[list[str | None]]
    meta: dict[str, Any] = field(default_factory=dict)


def _str(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise IngestionError(f"{path}: cannot read file ({e.strerror or e})") from e


def _decode(path: Path, raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    matches = from_bytes(raw)
    # Short Western files are often mis-guessed as cp1250/cp1251: prefer Western candidates.
    best = next((m for m in matches if m.encoding in _WESTERN), None) or matches.best()
    if best is None:
        raise IngestionError(f"{path}: cannot detect encoding")
    return str(best), best.encoding


def _read_csv(path: Path) -> RawTable:
    text, enc = _decode(path, _read_bytes(path))
    if not text.strip():
        raise IngestionError(f"{path}: empty file")
    try:
        delim = csv.Sniffer().sniff(text[:8192], delimiters=_DELIMS).delimiter
    except csv.Error:
        delim = ","
    records = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delim) if r]
    header, body = records[0], records[1:]
    rows = [([_str(c) for c in r] + [None] * len(header))[: len(header)] for r in body]
    return RawTable(
        [h.strip() for h in header],
        rows,
        {"encoding": enc, "delimiter": delim, "row_count": len(rows)},
    )


def _read_json(path: Path) -> RawTable:
    text, enc = _decode(path, _read_bytes(path))
    try:
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError:
            data = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    except json.JSONDecodeError as e:
        raise IngestionError(f"{path}: invalid JSON ({e})") from e
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(o, dict) for o in data):
        raise IngestionError(f"{path}: JSON must be an array of objects or JSON lines")
    cols = list(dict.fromkeys(k for o in data for k in o))
    rows = [[_str(o.get(c)) for c in cols] for o in data]
    return RawTable(cols, rows, {"encoding": enc, "row_count": len(rows)})


def _open_xlsx(path: Path) -> Any:
    try:
        return openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # openpyxl raises many types on bad files
        raise IngestionError(f"{path}: cannot open workbook ({e})") from e


def list_sheets(path: Path) -> list[str]:
    """Sheet names of an Excel workbook."""
    wb = _open_xlsx(Path(path))
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _read_xlsx(path: Path, sheet: str | None) -> RawTable:
    wb = _open_xlsx(path)
    try:
        if sheet is not None and sheet not in wb.sheetnames:
            raise IngestionError(f"{path}: sheet '{sheet}' not found (have {wb.sheetnames})")
        for name in [sheet] if sheet else wb.sheetnames:
            data = [
                r for r in wb[name].iter_rows(values_only=True) if any(c is not None for c in r)
            ]
            if data:
                break
        else:
            raise IngestionError(f"{path}: no non-empty sheet")
        if not data:
            raise IngestionError(f"{path}: sheet '{sheet}' is empty")
    finally:
        wb.close()
    header = [str(c).strip() if c is not None else f"col_{i}" for i, c in enumerate(data[0])]
    rows = [([_str(c) for c in r] + [None] * len(header))[: len(header)] for r in data[1:]]
    return RawTable(header, rows, {"sheet": name, "row_count": len(rows)})


def read_file(path: Path, fmt: str | None = None, sheet: str | None = None) -> RawTable:
    """Read a CSV/JSON/XLSX file; format inferred from the extension when `fmt` is None."""
    path = Path(path)
    fmt = fmt or _EXT.get(path.suffix.lower())
    if fmt not in ("csv", "json", "xlsx"):
        raise IngestionError(f"{path}: unsupported format {fmt!r}")
    if not path.is_file():
        raise IngestionError(f"{path}: file not found")
    if fmt == "csv":
        return _read_csv(path)
    if fmt == "json":
        return _read_json(path)
    return _read_xlsx(path, sheet)
