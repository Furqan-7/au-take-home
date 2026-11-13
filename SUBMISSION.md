# Submission

## Candidate
- **Name:** Furqan Bodarni
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~4h (building with Claude Code, plus reviewing and studying the code)

## How to run
```bash
# setup (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd solution

# optional: the secret both PII keys are derived from (a dev default is used, with a warning, if unset)
export LEDGER_SECRET='change-me'

# generate data, ingest, report
python ../assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m ledger ingest data/drop_1
python -m ledger ingest data/drop_2          # or both at once: python -m ledger ingest data/drop_1 data/drop_2
python -m ledger report --out reports/

# tests
python -m pytest
```
Options: `--db <file>` (default `ledger.db`), `--schemas <dir>` (default `../assignment-a-data/schemas`).
Delete `ledger.db*` to start from scratch.

## Design

```
solution/ledger/
  normalize.py   pure parsers: header, phone, date, decimal, enum, timestamp (-> IST)
  schema.py      loads <workflow>.json, maps headers, validates one row -> values | reason
  pii.py         HMAC phone index + Fernet encryption, keys derived from LEDGER_SECRET
  db.py          SQLite schema
  ingest.py      one drop: lead CSVs, then event JSONL; one transaction per file
  report.py      links events to leads and aggregates in SQL, writes the 5 files
  __main__.py    CLI
```

**Two-phase design.** `ingest` only *stores* facts: cleaned leads, rejections, and raw events
in the ledger. It never decides whether an event is an orphan. `report` does all linking and
aggregation in SQL over the whole ledger, every time. That is why an event that arrives before
its lead needs no special handling: once the lead exists, the join finds it. It's also why
"drop_1 then drop_2" and "both at once" produce identical output.

**Schema (SQLite):**

| Table | Purpose | Key constraints |
|---|---|---|
| `ingested_files` | One row per file fully ingested, plus its counters for `data_quality.json` | PK `(file_key, sha256)` |
| `leads` | Merged state of each lead | `UNIQUE (workflow_id, phone_hmac)` = lead identity |
| `rejections` | Rejected rows: where and why, no PII | PK `(workflow_id, batch_id, row_number)` |
| `events` | **The append-only interaction ledger** | PK `event_id` (dedupe); triggers make `UPDATE`/`DELETE` fail |
| `dispositions` | Catalogue, reloaded from CSV each run | PK `(workflow_id, code)` |

Overrides are just more rows in `events`. The effective disposition is computed at read time
(`ROW_NUMBER()` over a call's dispositions, overrides first, then latest `occurred_at`), so the
original `disposition.set` is never touched.

**PII at rest.** Phone numbers need to be *matched*, names only *stored*:
- `phone_hmac` = HMAC-SHA256(secret, `+91XXXXXXXXXX`). It's deterministic, so events join to
  leads on an index. It is keyed because a plain SHA-256 of a 10-digit number can be
  brute-forced in seconds.
- `customer_name` and the canonical phone (every field marked `pii: true`) are encrypted with
  Fernet inside `fields_json`, so they can be recovered with the key.
- Only `phone_last4` is stored in clear, for `phone_masked`.
- Events: the raw phone is **not** stored, only its HMAC.
- A test (`test_no_plaintext_pii_in_database`) greps the database file bytes for the name and phone.

**Idempotency.** Each file is ingested in **one transaction**, together with its row in
`ingested_files`. Either the whole file is in, or none of it is. Re-running skips files whose
`(name, sha256)` is already recorded, so counters never double-count, and a crash halfway
through a file just means it gets re-ingested from scratch next time. Event dedupe is a
second safety net: `event_id` is the primary key.

**Incremental (stretch).** Ingest is incremental: drop_2 never re-reads drop_1. Reports are
rebuilt from the full ledger. *Trade-off:* reporting is O(all data) per run (about 9 s on the
large set), but it's simple and can't drift. Maintaining aggregates incrementally would be
faster, but a late lead or a late override would then have to update aggregates that are
already built, and that is exactly where bugs hide.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| A required non-phone field that is present but unparseable → reject with `invalid:<field>` | Spec only names `invalid_phone`; the current schemas only have required `string` + `phone`, so it never happens today | Treat it as a warning |
| `N/A`, `-`, `TBD` are *present but invalid*, not missing → null + warning | Spec: only an empty value counts as missing | Treat placeholder words as missing |
| Indian digit grouping `1,00,000` is invalid | Spec says commas are *thousands* separators | Strip all commas |
| Mixed date separators (`01/12-2026`) and 1-digit day/month are invalid | The spec formats are fixed-width | Be lenient |
| If two columns map to the same field, the left-most wins | Deterministic | Error out |
| Defaults applied on every write to fields still null | Equivalent to "after merging": a later non-null value always overwrites anyway | A separate post-processing pass |
| A changed file under the same name is a new delivery (new hash → ingested again) | Content-hash idempotency; an identical re-run is a no-op | Key by name only and ignore changes |
| Duplicate `event_id` with different content: first seen wins | Spec: "an event_id already seen → skip" | Compare content |
| Override links via `(workflow_id, call_id)` to the lead of the call's phone-linked events | Spec. If several leads somehow share a call_id, the lowest `lead_id` wins (never seen in the data) | — |
| An override whose code is unknown is ignored; effective disposition falls back to the previous valid override, else the latest `set` | Spec says unknown codes are "ignored" | Let it blank the call's disposition |
| Ties on `occurred_at` break by `event_id` (highest wins); `current_disposition` ties on initiated time break by `call_id` | Deterministic output | — |
| Unknown-disposition events are counted only if linked | Spec order: orphan check comes before unknown disposition | Count all |
| An event with an unparseable `occurred_at` is stored but can't form a call | Spec defines malformed narrowly (bad JSON / no `event_id`). Doesn't occur in the data | Mark as malformed |
| Calls are keyed by `(workflow_id, call_id)` | A `call_id` is only guaranteed unique per workflow in the data we have | Global `call_id` |
| `connectivity_rate` rounding: decimal half-up | Avoids float rounding surprises | Python float formatting |
| `rejections_by_reason` lists only reasons that occurred, sorted | Output is stable | List every possible reason with 0 |
| SQLite | Runs from a clean clone, nothing to install; same SQL ports to Postgres | Postgres via docker-compose |

## Failure modes considered
- **Re-run of a drop:** a no-op (file hash check). Tested.
- **Crash halfway through a file:** the transaction rolls back, and the file isn't marked
  ingested, so the next run redoes it. A crash between files leaves earlier files committed.
- **Duplicate events within a file and across drops:** `event_id` PK + `INSERT OR IGNORE`. Tested.
- **Events before their lead / overrides in a later drop than their call:** linking happens at report time. Tested.
- **Malformed JSON, missing `event_id`, blank lines:** counted / skipped. Tested. Valid JSON that isn't an object is also counted as malformed (not tested).
- **Renamed and re-ordered columns, extra columns:** tested. A BOM in the header and short rows are handled but not tested.
- **Workflow folder without a schema:** warning, skipped, and *not* marked ingested, so it can be ingested once the schema exists.

**Not handled / known limits**
- No key rotation for `LEDGER_SECRET`; changing it breaks matching against existing rows.
- Two `ingest` processes running at once on the same DB aren't coordinated (SQLite would
  serialise writes, but both could ingest the same new file; the event PK protects the ledger,
  but the lead counters could double).
- A corrected re-delivery of a lead file is added on top of the original, not treated as a replacement.

## Testing
`python -m pytest` runs 57 tests in under 1 s:
- `test_normalize.py`: every phone format, invalid phones, all date formats, impossible dates, currency formats, rounding, enum case, UTC/offset/naive → IST including a date roll-over.
- `test_pipeline.py`: end-to-end on tiny hand-written drops. Covers rejection order, blank rows, merge semantics (first batch wins, last non-null wins), defaults, renamed headers, PII not in the DB file, malformed/duplicate/blank lines, orphan-then-linked, override precedence and unknown codes, calls without `call.initiated`, all bucket branches, IST dates in `daily_overview`, re-ingest + incremental == all at once, and append-only triggers.

Not tested: Postgres, concurrent ingests, very large single files.

## Results
- Small (seed 42): ingest drop_1 0.3 s, drop_2 0.2 s, report < 0.1 s.
- Large (seed 7, 144k lead rows, 632k event lines): ingest drop_1 25 s, drop_2 29 s, report 9 s, **~64 s total** on the dev container.
- Surprising in the data: ~5% of events are orphans even at the end (calls to rejected leads
  and to numbers never uploaded). After drop_1 alone the orphan count is more than double
  (1123 vs 462 for seed 42), because many leads only arrive in drop_2.
- Seed 42 `data_quality.json`: 2057 lead rows, 108 rejected, 57 merged, 122 warnings;
  8745 event lines, 8 malformed, 237 duplicates, 462 orphans, 18 unknown dispositions.

## Running this in production (100k leads/day)
- **Scheduling:** run one ingest per drop as it lands (an object-storage event or a cron job
  feeding an orchestrator like Airflow), then report. Each file is one idempotent unit, so
  retries are always safe.
- **Database:** Postgres. Partition `events` by month, add `UNIQUE (event_id)`, enforce
  append-only with revoked `UPDATE`/`DELETE` grants plus a trigger.
- **Reports:** at this volume full recompute is still fine (it's minutes). Beyond that, keep a
  per-call summary table updated only for calls touched by new events.
- **Failures and backfills:** a bad file goes to a quarantine bucket with an alert. A backfill
  is "ingest the old drop"; because linking happens at report time, the next report picks it up
  without any special code.
- **Secrets:** `LEDGER_SECRET` from a secrets manager / KMS, with key versioning stored per row.

## If I had another day
1. Lock the DB during ingest (or claim each file with an `INSERT` before processing it) so concurrent runs are safe.
2. Batch inserts with `executemany` to cut large-scale ingest time roughly in half.
3. Property-based tests (Hypothesis) for the phone and date parsers.
4. Key rotation for the PII secret.
