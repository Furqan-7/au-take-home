"""End-to-end tests on tiny hand-written drops, one rule per test."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from ledger.__main__ import main

LEAD_HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


def write_drop(root: Path, name: str, leads: dict[str, str] | None = None, events: list | None = None) -> Path:
    """leads: {"batch_001": "<csv body without header>"}; events: dicts or raw strings."""
    drop = root / name
    for batch, body in (leads or {}).items():
        path = drop / "leads" / "motor_renewal" / f"{batch}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEAD_HEADER + "\n" + body, encoding="utf-8")
    if events is not None:
        path = drop / "events" / f"events_{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n",
                        encoding="utf-8")
    return drop


def ev(event_id, event_type, call_id, at, phone="9876543210", **payload):
    e = {"event_id": event_id, "event_type": event_type, "workflow_id": "motor_renewal",
         "call_id": call_id, "occurred_at": at, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return e


class Pipeline:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.db = str(tmp_path / "test.db")

    def ingest(self, *drops: Path):
        main(["--db", self.db, "ingest", *map(str, drops)])

    def report(self, name="out") -> Path:
        out = self.tmp / name
        main(["--db", self.db, "report", "--out", str(out)])
        return out

    def leads(self, out: Path) -> list[dict]:
        with (out / "lead_report.csv").open() as fh:
            return list(csv.DictReader(fh))

    def quality(self, out: Path) -> dict:
        return json.loads((out / "data_quality.json").read_text())


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRET", "test-secret")
    return Pipeline(tmp_path)


ONE_LEAD = {"batch_001": "Asha,9876543210,P1,,,\n"}


# ------------------------------------------------------------------ leads


def test_rejection_reason_is_first_failure_in_schema_order_and_blank_rows_not_counted(pipe):
    drop = write_drop(pipe.tmp, "d1", {"batch_001": (
        ",,,,,\n"                      # blank row: skipped, not counted
        ",12345,,,,\n"                 # row 1: name missing comes before bad phone
        "Ravi,12345,P2,,,\n"           # row 2: invalid phone
        "Meena,,P3,,,\n"               # row 3: phone missing
        "Kiran,9876500000,P4,,,\n"     # row 4: ok
    )})
    pipe.ingest(drop)
    out = pipe.report()
    rows = list(csv.reader((out / "rejections.csv").open()))
    assert rows[1:] == [
        ["motor_renewal", "batch_001", "1", "missing_required:customer_name"],
        ["motor_renewal", "batch_001", "2", "invalid_phone"],
        ["motor_renewal", "batch_001", "3", "missing_required:phone"],
    ]
    q = pipe.quality(out)
    assert q["lead_rows_total"] == 4 and q["lead_rows_rejected"] == 3


def test_duplicates_merge_keep_first_batch_and_count_warnings(pipe, tmp_path):
    d1 = write_drop(tmp_path, "d1", {
        "batch_001": "Asha,9876543210,P1,31/02/2026,₹100,\n",   # bad date -> warning
        "batch_002": "Asha K,+91 98765 43210,P1-NEW,,,hi\n",    # same phone -> merged
    })
    pipe.ingest(d1)
    q = pipe.quality(pipe.report())
    assert q["duplicate_lead_rows_merged"] == 1
    assert q["field_warnings"] == 1
    con = sqlite3.connect(pipe.db)
    batch_id, ref, fields = con.execute("SELECT batch_id, external_ref, fields_json FROM leads").fetchone()
    fields = json.loads(fields)
    assert batch_id == "batch_001"                  # first row's batch, permanently
    assert ref == "P1-NEW"                          # last non-null write wins
    assert fields["premium_amount"] == "100.00"     # kept: the later row had it empty
    assert fields["preferred_language"] == "hi"


def test_default_applies_only_when_still_null(pipe):
    pipe.ingest(write_drop(pipe.tmp, "d1", {"batch_001": "Asha,9876543210,P1,,,Hindi\n"}))  # invalid enum
    pipe.report()
    fields = json.loads(sqlite3.connect(pipe.db).execute("SELECT fields_json FROM leads").fetchone()[0])
    assert fields["preferred_language"] == "en"


def test_renamed_and_reordered_headers(pipe):
    drop = pipe.tmp / "d1" / "leads" / "motor_renewal"
    drop.mkdir(parents=True)
    (drop / "batch_003.csv").write_text(
        "Language,Insured  Name,Policy_No,Mobile No,agent_notes\nTA,Asha,P9,09876543210,x\n", encoding="utf-8")
    pipe.ingest(pipe.tmp / "d1")
    [lead] = pipe.leads(pipe.report())
    assert lead["external_ref"] == "P9" and lead["phone_masked"] == "+91******3210"


def test_no_plaintext_pii_in_database(pipe):
    pipe.ingest(write_drop(pipe.tmp, "d1", {"batch_001": "Asha Verma,9876543210,P1,,,\n"},
                           [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30")]))
    pipe.report()
    con = sqlite3.connect(pipe.db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    raw = Path(pipe.db).read_bytes()
    assert b"Asha Verma" not in raw
    assert b"9876543210" not in raw


# ------------------------------------------------------------------ events


def test_malformed_duplicate_and_blank_lines(pipe):
    e1 = ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30")
    pipe.ingest(write_drop(pipe.tmp, "d1", ONE_LEAD, [e1, e1, '{"event_id": "e2"', '{"no_id": 1}', ""]))
    pipe.ingest(write_drop(pipe.tmp, "d2", None, [e1]))   # duplicate across drops
    q = pipe.quality(pipe.report())
    assert q["events_total"] == 5            # the blank line is not counted
    assert q["malformed_events"] == 2
    assert q["duplicate_events_dropped"] == 2


def test_orphan_event_links_when_lead_arrives_later(pipe):
    early = [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30"),
             ev("e2", "disposition.override", "c1", "2026-10-02T10:00:00+05:30", phone=None,
                disposition_code="renewed")]
    pipe.ingest(write_drop(pipe.tmp, "d1", None, early))
    assert pipe.quality(pipe.report("r1"))["orphan_events"] == 2

    pipe.ingest(write_drop(pipe.tmp, "d2", {"batch_003": "Asha,+919876543210,P1,,,\n"}))
    out = pipe.report("r2")
    assert pipe.quality(out)["orphan_events"] == 0
    [lead] = pipe.leads(out)
    assert lead["total_calls"] == "1" and lead["current_disposition"] == "renewed"


def test_override_beats_set_even_if_set_is_later_and_unknown_codes_are_ignored(pipe):
    events = [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30"),
        ev("e2", "call.connected", "c1", "2026-10-01T10:00:10+05:30"),
        ev("e3", "disposition.override", "c1", "2026-10-01T11:00:00+05:30", phone=None,
           disposition_code="not_interested"),
        ev("e4", "disposition.set", "c1", "2026-10-01T12:00:00+05:30", disposition_code="renewed"),
        ev("e5", "disposition.override", "c1", "2026-10-01T13:00:00+05:30", phone=None,
           disposition_code="RENEWD"),     # unknown: ignored, not the effective one
    ]
    pipe.ingest(write_drop(pipe.tmp, "d1", ONE_LEAD, events))
    out = pipe.report()
    [lead] = pipe.leads(out)
    assert lead["current_disposition"] == "not_interested"
    assert lead["bucket"] == "completed" and lead["ever_success"] == "false"
    assert pipe.quality(out)["unknown_disposition_events"] == 1


def test_call_without_initiated_is_not_counted(pipe):
    pipe.ingest(write_drop(pipe.tmp, "d1", ONE_LEAD, [ev("e1", "call.connected", "c1", "2026-10-01T10:00:00Z")]))
    [lead] = pipe.leads(pipe.report())
    assert lead["total_calls"] == "0" and lead["bucket"] == "yet_to_reach_out"


@pytest.mark.parametrize("calls,bucket", [
    ([], "yet_to_reach_out"),
    ([["failed"], ["failed"]], "in_progress_telephony_failed"),
    ([["failed"], ["connected"]], "in_progress_dialed"),
    ([["connected", "callback_requested"]], "in_progress_dialed"),       # not terminal
    ([["failed"], ["connected", "not_interested"]], "completed"),       # completed wins
])
def test_bucket_logic(pipe, calls, bucket):
    events, n = [], 0
    for i, steps in enumerate(calls):
        call = f"c{i}"
        n += 1
        events.append(ev(f"e{n}", "call.initiated", call, f"2026-10-0{i + 1}T10:00:00+05:30"))
        for step in steps:
            n += 1
            if step == "failed":
                events.append(ev(f"e{n}", "call.telephony_failed", call, f"2026-10-0{i + 1}T10:00:05+05:30"))
            elif step == "connected":
                events.append(ev(f"e{n}", "call.connected", call, f"2026-10-0{i + 1}T10:00:05+05:30"))
            else:
                events.append(ev(f"e{n}", "disposition.set", call, f"2026-10-0{i + 1}T10:05:00+05:30",
                                 disposition_code=step))
    pipe.ingest(write_drop(pipe.tmp, "d1", ONE_LEAD, events))
    [lead] = pipe.leads(pipe.report())
    assert lead["bucket"] == bucket


def test_daily_overview_uses_ist_date_and_success_leads(pipe):
    events = [
        # 20:00Z on the 1st is 01:30 IST on the 2nd
        ev("e1", "call.initiated", "c1", "2026-10-01T20:00:00Z"),
        ev("e2", "call.connected", "c1", "2026-10-01T20:00:10Z"),
        ev("e3", "disposition.set", "c1", "2026-10-01T20:05:00Z", disposition_code="renewed"),
        ev("e4", "call.initiated", "c2", "2026-10-02 09:00:00"),   # naive = IST
    ]
    pipe.ingest(write_drop(pipe.tmp, "d1", ONE_LEAD, events))
    out = pipe.report()
    rows = list(csv.reader((out / "daily_overview.csv").open()))
    assert rows[1:] == [["motor_renewal", "2026-10-02", "2", "1", "0.5000", "1"]]
    [lead] = pipe.leads(out)
    assert lead["last_call_at"] == "2026-10-02T09:00:00+05:30"


# ------------------------------------------------------------------ idempotency


def test_reingest_changes_nothing_and_incremental_equals_all_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_SECRET", "test-secret")
    d1 = write_drop(tmp_path, "d1", ONE_LEAD, [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z")])
    d2 = write_drop(tmp_path, "d2", {"batch_003": "Asha,9876543210,P1,,,ta\nBo,9000000001,P2,,,\n"},
                    [ev("e2", "call.initiated", "c2", "2026-10-08T10:00:00Z", phone="9000000001")])

    a = Pipeline(tmp_path / "a")
    (tmp_path / "a").mkdir()
    a.ingest(d1)
    a.ingest(d2)
    a.ingest(d1)
    a.ingest(d2)       # re-runs must be no-ops
    out_a = a.report()

    b = Pipeline(tmp_path / "b")
    (tmp_path / "b").mkdir()
    b.ingest(d1, d2)
    out_b = b.report()

    for name in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
                 "data_quality.json"]:
        assert (out_a / name).read_text() == (out_b / name).read_text(), name
    assert a.quality(out_a)["duplicate_lead_rows_merged"] == 1


def test_ledger_is_append_only(pipe):
    pipe.ingest(write_drop(pipe.tmp, "d1", ONE_LEAD, [ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z")]))
    con = sqlite3.connect(pipe.db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE events SET event_type = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM events")
