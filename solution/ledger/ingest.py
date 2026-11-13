"""Ingest one drop directory: lead CSVs first, then event JSONL files.

Idempotency: every file is ingested inside ONE transaction that also records
(file name, content hash) in ingested_files. Either the whole file lands or
nothing does, and a file already recorded is skipped. So re-running a drop,
or crashing half-way and re-running, never double-counts.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .normalize import normalize_phone, parse_timestamp
from .pii import Pii
from .schema import WorkflowSchema, read_csv_rows, validate_row


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    schemas = {}
    for path in sorted(schema_dir.glob("*.json")):
        schema = WorkflowSchema.load(path)
        schemas[schema.workflow_id] = schema
    return schemas


def load_dispositions(conn: sqlite3.Connection, csv_path: Path) -> None:
    def as_bool(value: str) -> int:
        return 1 if value.strip().lower() == "true" else 0

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [(r["workflow_id"].strip(), r["code"].strip(), as_bool(r["is_terminal"]), as_bool(r["is_success"]))
                for r in csv.DictReader(fh)]
    conn.execute("BEGIN")
    conn.execute("DELETE FROM dispositions")
    conn.executemany("INSERT INTO dispositions VALUES (?, ?, ?, ?)", rows)
    conn.execute("COMMIT")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _already_ingested(conn: sqlite3.Connection, file_key: str, sha: str) -> bool:
    row = conn.execute("SELECT 1 FROM ingested_files WHERE file_key = ? AND sha256 = ?", (file_key, sha)).fetchone()
    return row is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- leads


def ingest_lead_file(conn: sqlite3.Connection, pii: Pii, schema: WorkflowSchema, path: Path, file_key: str) -> bool:
    sha = _sha256(path)
    if _already_ingested(conn, file_key, sha):
        return False

    headers, rows = read_csv_rows(path)
    mapping = schema.map_headers(headers)
    batch_id = path.stem
    pii_fields = {f.name for f in schema.fields if f.pii}
    defaults = {f.name: f.default for f in schema.fields if f.default is not None}
    phone_name = schema.phone_field.name
    ref_name = schema.reference_field.name
    rejected = merged = warnings = 0

    conn.execute("BEGIN")
    try:
        for row_number, row in enumerate(rows, start=1):
            result = validate_row(schema, mapping, row)
            if result.reason:
                rejected += 1
                conn.execute("INSERT OR REPLACE INTO rejections VALUES (?, ?, ?, ?)",
                             (schema.workflow_id, batch_id, row_number, result.reason))
                continue
            warnings += result.warnings

            phone = result.values[phone_name]
            phone_hmac = pii.phone_index(phone)
            # Encrypt PII values before they go anywhere near the database.
            incoming = {k: (pii.encrypt(v) if k in pii_fields and v is not None else v)
                        for k, v in result.values.items()}

            existing = conn.execute("SELECT lead_id, fields_json FROM leads WHERE workflow_id = ? AND phone_hmac = ?",
                                    (schema.workflow_id, phone_hmac)).fetchone()
            if existing:
                merged += 1
                fields = json.loads(existing["fields_json"])
                for k, v in incoming.items():
                    if v is not None:  # last non-null write wins
                        fields[k] = v
            else:
                fields = incoming
            # Defaults go only to fields that are still null after merging.
            for k, v in defaults.items():
                if fields.get(k) is None:
                    fields[k] = v
            external_ref = fields.get(ref_name) or ""

            if existing:
                conn.execute("UPDATE leads SET fields_json = ?, external_ref = ? WHERE lead_id = ?",
                             (json.dumps(fields, sort_keys=True), external_ref, existing["lead_id"]))
            else:
                conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hmac, phone_last4, external_ref, batch_id, fields_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (schema.workflow_id, phone_hmac, phone[-4:], external_ref, batch_id,
                     json.dumps(fields, sort_keys=True)))

        conn.execute(
            "INSERT INTO ingested_files (file_key, sha256, kind, ingested_at, rows_total, rows_rejected,"
            " duplicates_merged, field_warnings) VALUES (?, ?, 'leads', ?, ?, ?, ?, ?)",
            (file_key, sha, _now(), len(rows), rejected, merged, warnings))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


# ---------------------------------------------------------------- events


def _event_row(event: dict, pii: Pii, file_key: str, line_number: int) -> tuple:
    occurred = parse_timestamp(event.get("occurred_at"))
    phone = normalize_phone(event["phone"]) if isinstance(event.get("phone"), str) else None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = event.get("event_type")
    code = payload.get("disposition_code") if isinstance(event_type, str) and event_type.startswith("disposition.") else None
    return (
        str(event["event_id"]),
        event_type,
        event.get("workflow_id"),
        str(event["call_id"]) if event.get("call_id") is not None else None,
        occurred.isoformat() if occurred else None,
        int(occurred.timestamp() * 1_000_000) if occurred else None,
        pii.phone_index(phone) if phone else None,
        code,
        json.dumps(payload, sort_keys=True),
        file_key,
        line_number,
    )


def ingest_event_file(conn: sqlite3.Connection, pii: Pii, path: Path, file_key: str) -> bool:
    sha = _sha256(path)
    if _already_ingested(conn, file_key, sha):
        return False

    total = malformed = duplicates = 0
    conn.execute("BEGIN")
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line_number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                total += 1
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(event, dict) or event.get("event_id") in (None, ""):
                    malformed += 1
                    continue
                cursor = conn.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                      _event_row(event, pii, file_key, line_number))
                if cursor.rowcount == 0:  # event_id already in the ledger
                    duplicates += 1

        conn.execute(
            "INSERT INTO ingested_files (file_key, sha256, kind, ingested_at, rows_total, malformed_events,"
            " duplicate_events) VALUES (?, ?, 'events', ?, ?, ?, ?)",
            (file_key, sha, _now(), total, malformed, duplicates))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


# ---------------------------------------------------------------- drop


def ingest_drop(conn: sqlite3.Connection, pii: Pii, schemas: dict[str, WorkflowSchema], drop: Path) -> None:
    if not drop.is_dir():
        raise SystemExit(f"error: {drop} is not a directory")

    leads_dir = drop / "leads"
    for workflow_dir in sorted(p for p in leads_dir.glob("*") if p.is_dir()):
        schema = schemas.get(workflow_dir.name)
        if schema is None:
            print(f"warning: no schema for workflow {workflow_dir.name!r}, skipping {workflow_dir}", file=sys.stderr)
            continue
        for path in sorted(workflow_dir.glob("*.csv")):
            key = f"leads/{workflow_dir.name}/{path.name}"
            done = ingest_lead_file(conn, pii, schema, path, key)
            print(f"{'ingested' if done else 'skipped (already ingested)'}: {key}", file=sys.stderr)

    for path in sorted((drop / "events").glob("*.jsonl")):
        key = f"events/{path.name}"
        done = ingest_event_file(conn, pii, path, key)
        print(f"{'ingested' if done else 'skipped (already ingested)'}: {key}", file=sys.stderr)
