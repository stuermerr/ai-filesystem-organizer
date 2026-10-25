# Issue 77: 62k-entry corpus acceptance

Recorded 2026-08-10/11 against the authorized local `testdir` corpus on
`feature/77-validate-62k-corpus` at `789dc7b`.

## Safety and correctness

- The Selected Backup Root contained 62,676 entries: 54,787 readable regular
  files and 7,889 directories. The CLI reported no skipped entries.
- The Run Output Root was `testdir-output-v2`, outside the Selected Backup
  Root. The source entry counts remained unchanged after the completed run.
- The finalized plan contained 37,403 copy operations and 1,679 explicit
  directories, yielding 39,082 output entries. The materialized destination
  contains exactly 37,403 files and 6,158 directories.
- Materialization completed with the CLI's `materialized` status. It uses the
  latest-primary, lossless conflict projection recorded in the finalized plan;
  no source files were modified.

## Fresh workflow measurements

| Stage | Wall time | Result |
| --- | ---: | --- |
| Scan | 58.00 s | 223,765 structural candidates/comparisons; no skipped entries |
| Concise run report | 0.90 s | 49,935,327 bytes |
| Full run report | 3.03 s | 194,003,962 bytes |
| Plan | 252.24 s | 37,403 operations; 1,679 explicit directories |
| Concise plan report | 0.12 s | 3,281,899 bytes |
| Full plan report | 2.52 s | 40,097,701 bytes |
| Finalize | 0.14 s | plan `0af5c6d5-d774-4e4f-9ae5-970decfeb085` |
| Materialization preflight | 38.76 s | 11,750,013,263 bytes required; sufficient free space |
| Materialization | 1,725.90 s | completed; 37,403 operations |

The scan Run Workspace is 1,041,084,416 bytes (SQLite database:
1,041,051,648 bytes). The materialized destination is 11,750,013,263 bytes.

## Comparison

The pre-cutover reference facts in #69 were 293,902 structural comparisons,
a 1.344-GB database, and an 86.0-MB plan report that took 10.14 seconds and
1.29 GB RSS to render. This run reduced candidate/comparison count by 23.9%
and the database by about 22.5%. Its concise plan report is 3.28 MB and took
0.12 seconds; its full plan report is 40.10 MB and took 2.52 seconds.

The cumulative `example_source` history remains a three-run median series over
a 66-entry public fixture. Its latest issue-76 median is 0.294 s scan, 0.194 s
plan, 1.578 s materialization, and 2.425 s end-to-end. Those figures establish
that the optimized public path remains fast on the fixture, but they are not
directly comparable to this 62,676-entry, 17.23-GB corpus run.

## Performance findings

1. **Materialization is the dominant bottleneck.** It took 28m46s, after the
   bulk output appeared within roughly the first six minutes. During the long
   tail the process repeatedly waited for ext4 journal and block-device I/O,
   while continuing to advance its read/write counters. This is consistent
   with the safety protocol's per-operation staging, `fsync`, content
   re-hash, and durable publication across 37,403 operations—not a stalled
   run or source reorganization.
2. **The default run report is not concise on a conflict-heavy corpus.** It
   was 49.94 MB despite the concise mode, because it includes a large
   unresolved-conflict listing. This needs a bounded conflict summary with
   explicit pagination/detail for the complete listing.
3. **Plan construction is the next CPU bottleneck.** The 252.24-second plan
   time is much larger than scan and report rendering. It should be profiled
   before changing semantics; the high candidate count and lossless conflict
   projection are the primary suspects.

These are observed bottlenecks, not correctness failures. The completed run
preserves the source and produces the approved, lossless derived workspace.
