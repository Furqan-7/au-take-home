# AI Usage Disclosure

## Tools used
Claude Code (Anthropic).

## What I used them for
- Reading the assignment and profiling the generated data (header variants, timestamp formats, which values fail to parse).
- Writing most of the implementation in pair-programming style: normalisers, SQLite schema, ingestion, SQL reports, CLI.
- Writing the unit and end-to-end tests.
- Writing a separate, naive reference implementation (not submitted) that we compared byte-for-byte against the solution on 6 seeds, to catch bugs in the SQL.
- Drafting `SUBMISSION.md`.

## What I wrote or decided myself
- I chose to build the solution step by step, with Claude Code explaining each part as it was built, rather than having a finished solution generated in one go. I wanted to understand every file well enough to explain and change it live.
- Most of the code and the design were produced by the AI. I'm being upfront about that here rather than claiming them as my own.

## Where the AI was wrong or unhelpful
- The first version of a test helper (`write_drop`) had a confusing, effectively dead conditional expression for prepending the CSV header. It was caught on review and replaced with a one-line version.
- The first draft of `SUBMISSION.md` claimed BOM handling and non-object JSON lines were tested when they weren't. Corrected after checking against the test files.

## How I verified AI-generated code
Verification was done with Claude Code during the session:
- 57 pytest tests, including a test for every bucket branch and for override precedence.
- Ran the pipeline on the real generated data. Checked that re-ingest is a no-op and that incremental vs all-at-once output is byte-identical.
- Diffed the output against an independent reference implementation on seeds 42, 1, 2, 3, 99 and 2026: identical.
- Confirmed the examples in the spec (`0.5625`, `2026-10-14T17:55:24+05:30`) appear in the seed-42 output.
- Timed the large dataset (~64 s).
