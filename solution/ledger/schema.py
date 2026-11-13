"""Workflow lead schemas and row validation (pure, no database)."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_header, normalize_phone, parse_date, parse_decimal, parse_enum


@dataclass
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    default: str | None = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]

    @classmethod
    def load(cls, path: Path) -> "WorkflowSchema":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = Field.__dataclass_fields__
        fields = [Field(**{k: v for k, v in f.items() if k in known}) for f in data["fields"]]
        return cls(data["workflow_id"], fields)

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    def map_headers(self, headers: list[str]) -> dict[str, int]:
        """Return {field name: column index}. Unknown columns are ignored.

        If two columns map to the same field, the first (left-most) one wins.
        """
        lookup: dict[str, str] = {}
        for f in self.fields:
            for name in [f.name, *f.aliases]:
                lookup.setdefault(normalize_header(name), f.name)
        mapping: dict[str, int] = {}
        for index, header in enumerate(headers):
            name = lookup.get(normalize_header(header))
            if name is not None and name not in mapping:
                mapping[name] = index
        return mapping


@dataclass
class RowResult:
    values: dict[str, str | None]   # canonical values; None = missing/invalid
    reason: str | None = None       # rejection reason, None if the row is valid
    warnings: int = 0


def _parse(f: Field, raw: str) -> str | None:
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalize_phone(raw)
    if f.type == "date":
        parsed = parse_date(raw)
        return parsed.isoformat() if parsed else None
    if f.type == "decimal":
        parsed = parse_decimal(raw)
        return str(parsed) if parsed is not None else None
    if f.type == "enum":
        return parse_enum(raw, f.values)
    raise ValueError(f"unknown field type {f.type!r}")


def validate_row(schema: WorkflowSchema, mapping: dict[str, int], row: list[str]) -> RowResult:
    """Walk fields in schema order; the first failure is the rejection reason."""
    values: dict[str, str | None] = {}
    warnings = 0
    for f in schema.fields:
        index = mapping.get(f.name)
        raw = row[index].strip() if index is not None and index < len(row) else ""
        if raw == "":
            if f.required:
                return RowResult(values, f"missing_required:{f.name}")
            values[f.name] = None
            continue
        parsed = _parse(f, raw)
        if parsed is None:
            if f.required:
                # The spec only names invalid_phone; a required field of another
                # type that is present but unparseable gets invalid:<field>.
                reason = "invalid_phone" if f.type == "phone" else f"invalid:{f.name}"
                return RowResult(values, reason)
            warnings += 1
        values[f.name] = parsed
    return RowResult(values, None, warnings)


def read_csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (headers, non-blank data rows). utf-8-sig strips a BOM if present."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        headers = next(reader, [])
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    return headers, rows
