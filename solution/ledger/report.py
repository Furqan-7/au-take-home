"""Build the five report files from the ledger.

All linking and aggregation happens here, in SQL, from the full ledger every
time. That is what makes the reports independent of ingest order: an event
that was an orphan after drop_1 links as soon as its lead exists.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# Step 1: link events to leads. Temp tables live only for this connection.
LINK_SQL = """
DROP TABLE IF EXISTS temp.linked;
DROP TABLE IF EXISTS temp.calls;
DROP TABLE IF EXISTS temp.call_disposition;

-- Every event that reaches a lead. Normal events link by (workflow, phone hash).
-- Overrides have no phone, so they link through call_id to the lead that the
-- call's other (phone-linked) events belong to.
CREATE TEMP TABLE linked AS
SELECT e.event_id, e.event_type, e.workflow_id, e.call_id, e.occurred_at,
       e.occurred_epoch_us, e.disposition_code, l.lead_id
FROM events e
JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hmac = e.phone_hmac
WHERE e.event_type IS NOT 'disposition.override';

INSERT INTO linked
SELECT o.event_id, o.event_type, o.workflow_id, o.call_id, o.occurred_at,
       o.occurred_epoch_us, o.disposition_code, c.lead_id
FROM events o
JOIN (SELECT workflow_id, call_id, MIN(lead_id) AS lead_id
      FROM linked WHERE call_id IS NOT NULL
      GROUP BY workflow_id, call_id) c
  ON c.workflow_id = o.workflow_id AND c.call_id = o.call_id
WHERE o.event_type = 'disposition.override';

CREATE INDEX temp.linked_call ON linked (workflow_id, call_id);

-- A call exists only if its call.initiated event is present and linked.
CREATE TEMP TABLE calls AS
SELECT i.workflow_id, i.call_id, i.lead_id,
       MIN(i.occurred_epoch_us) AS initiated_epoch_us,
       MIN(i.occurred_at)       AS initiated_at,   -- same offset (IST), so text order = time order
       substr(MIN(i.occurred_at), 1, 10) AS call_date,
       EXISTS (SELECT 1 FROM linked x WHERE x.workflow_id = i.workflow_id AND x.call_id = i.call_id
               AND x.event_type = 'call.connected') AS connected,
       EXISTS (SELECT 1 FROM linked x WHERE x.workflow_id = i.workflow_id AND x.call_id = i.call_id
               AND x.event_type = 'call.telephony_failed') AS telephony_failed
FROM linked i
WHERE i.event_type = 'call.initiated' AND i.occurred_at IS NOT NULL AND i.call_id IS NOT NULL
GROUP BY i.workflow_id, i.call_id, i.lead_id;

-- Effective disposition per call: latest override, else latest set.
-- Codes not in the catalogue are ignored. Ties on time break by event_id.
CREATE TEMP TABLE call_disposition AS
SELECT workflow_id, call_id, code, is_terminal, is_success FROM (
    SELECT x.workflow_id, x.call_id, x.disposition_code AS code, d.is_terminal, d.is_success,
           ROW_NUMBER() OVER (
               PARTITION BY x.workflow_id, x.call_id
               ORDER BY (x.event_type = 'disposition.override') DESC,
                        x.occurred_epoch_us DESC, x.event_id DESC) AS rn
    FROM linked x
    JOIN dispositions d ON d.workflow_id = x.workflow_id AND d.code = x.disposition_code
    WHERE x.event_type IN ('disposition.set', 'disposition.override')
)
WHERE rn = 1;
"""

LEAD_SQL = """
WITH per_call AS (
    SELECT c.*, cd.code, COALESCE(cd.is_terminal, 0) AS is_terminal, COALESCE(cd.is_success, 0) AS is_success
    FROM calls c
    LEFT JOIN call_disposition cd ON cd.workflow_id = c.workflow_id AND cd.call_id = c.call_id
),
agg AS (
    SELECT lead_id,
           COUNT(*)              AS total_calls,
           SUM(connected)        AS connected_calls,
           SUM(telephony_failed) AS failed_calls,
           MAX(is_terminal)      AS any_terminal,
           MAX(is_success)       AS ever_success,
           MAX(initiated_at)     AS last_call_at
    FROM per_call GROUP BY lead_id
),
latest_disposition AS (
    SELECT lead_id, code FROM (
        SELECT lead_id, code,
               ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY initiated_epoch_us DESC, call_id DESC) AS rn
        FROM per_call WHERE code IS NOT NULL)
    WHERE rn = 1
)
SELECT l.workflow_id, l.external_ref, l.batch_id, l.phone_last4,
       CASE
           WHEN a.total_calls IS NULL           THEN 'yet_to_reach_out'
           WHEN a.any_terminal = 1              THEN 'completed'
           WHEN a.failed_calls = a.total_calls  THEN 'in_progress_telephony_failed'
           ELSE 'in_progress_dialed'
       END AS bucket,
       COALESCE(a.total_calls, 0)     AS total_calls,
       COALESCE(a.connected_calls, 0) AS connected_calls,
       COALESCE(a.last_call_at, '')   AS last_call_at,
       COALESCE(ld.code, '')          AS current_disposition,
       COALESCE(a.ever_success, 0)    AS ever_success
FROM leads l
LEFT JOIN agg a ON a.lead_id = l.lead_id
LEFT JOIN latest_disposition ld ON ld.lead_id = l.lead_id
"""

DAILY_SQL = """
SELECT c.workflow_id, c.call_date,
       COUNT(*)         AS total_calls,
       SUM(c.connected) AS connected_calls,
       COUNT(DISTINCT CASE WHEN cd.is_success = 1 THEN c.lead_id END) AS success_leads
FROM calls c
LEFT JOIN call_disposition cd ON cd.workflow_id = c.workflow_id AND cd.call_id = c.call_id
GROUP BY c.workflow_id, c.call_date
ORDER BY c.workflow_id, c.call_date
"""

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def _bool(value: int) -> str:
    return "true" if value else "false"


def _rate(numerator: int, denominator: int) -> str:
    rate = Decimal(numerator) / Decimal(denominator)
    return str(rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def build_reports(conn: sqlite3.Connection, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    conn.executescript(LINK_SQL)

    # lead_report.csv. Sorting in Python with plain string order is explicit
    # and matches SQLite's BINARY collation anyway.
    leads = sorted(conn.execute(LEAD_SQL).fetchall(), key=lambda r: (r["workflow_id"], r["external_ref"]))
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"],
               [[r["workflow_id"], r["external_ref"], r["batch_id"], "+91******" + r["phone_last4"], r["bucket"],
                 r["total_calls"], r["connected_calls"], r["last_call_at"], r["current_disposition"],
                 _bool(r["ever_success"])] for r in leads])

    # batch_summary.csv, derived from the same per-lead buckets.
    batches: dict[tuple[str, str], dict[str, int]] = {}
    for r in leads:
        counts = batches.setdefault((r["workflow_id"], r["batch_id"]), dict.fromkeys(BUCKETS, 0))
        counts[r["bucket"]] += 1
    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, batch, sum(c.values()), *(c[b] for b in BUCKETS)] for (wf, batch), c in sorted(batches.items())])

    # daily_overview.csv
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
               [[r["workflow_id"], r["call_date"], r["total_calls"], r["connected_calls"],
                 _rate(r["connected_calls"], r["total_calls"]), r["success_leads"]]
                for r in conn.execute(DAILY_SQL)])

    # rejections.csv: no PII, just where and why.
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               [list(r) for r in conn.execute(
                   "SELECT workflow_id, batch_id, row_number, reason FROM rejections"
                   " ORDER BY workflow_id, batch_id, row_number")])

    # data_quality.json
    (out / "data_quality.json").write_text(json.dumps(data_quality(conn), indent=2) + "\n", encoding="utf-8")


def data_quality(conn: sqlite3.Connection) -> dict:
    """Assumes LINK_SQL has already run on this connection."""
    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()[0] or 0

    leads_total = "SELECT SUM({col}) FROM ingested_files WHERE kind = 'leads'"
    events_total = "SELECT SUM({col}) FROM ingested_files WHERE kind = 'events'"
    by_reason = {r[0]: r[1] for r in conn.execute(
        "SELECT reason, COUNT(*) FROM rejections GROUP BY reason ORDER BY reason")}
    return {
        "lead_rows_total": scalar(leads_total.format(col="rows_total")),
        "lead_rows_rejected": scalar(leads_total.format(col="rows_rejected")),
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": scalar(leads_total.format(col="duplicates_merged")),
        "field_warnings": scalar(leads_total.format(col="field_warnings")),
        "events_total": scalar(events_total.format(col="rows_total")),
        "malformed_events": scalar(events_total.format(col="malformed_events")),
        "duplicate_events_dropped": scalar(events_total.format(col="duplicate_events")),
        # Final state: everything in the ledger that still reaches no lead.
        "orphan_events": scalar("SELECT COUNT(*) FROM events") - scalar("SELECT COUNT(*) FROM linked"),
        "unknown_disposition_events": scalar(
            "SELECT COUNT(*) FROM linked x WHERE x.event_type IN ('disposition.set', 'disposition.override')"
            " AND NOT EXISTS (SELECT 1 FROM dispositions d"
            "                 WHERE d.workflow_id = x.workflow_id AND d.code = x.disposition_code)"),
    }
