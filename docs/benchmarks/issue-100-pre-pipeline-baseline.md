# Issue 100: pre-pipeline public-fixture baseline

Recorded 2026-10-07 against the public `example_source/` fixture on
`chore/100-test-corpus-baseline` at `a35f8bf`. The source tree was clean when
the benchmark started. The complete three-run record is the
`issue-100-pre-pipeline` entry in
[`benchmarks/scalability-example.json`](../../benchmarks/scalability-example.json).

## Reproduction

```console
uv run python scripts/benchmark_example_fixture.py \
  --label issue-100-pre-pipeline \
  --history benchmarks/scalability-example.json
```

The benchmark makes a fresh copy of the public fixture for every repetition.
It does not discover or select a private or large local corpus.

## Environment and input

| Property | Value |
| --- | --- |
| Operating system | Linux 6.17.0-35-generic, x86_64 |
| Python | CPython 3.12.3 |
| uv | 0.10.1 |
| Revision | `a35f8bfadbbcdde0aec4bec4b38a319c5254b778` |
| Source tree | `2ec4688e6449d88a1a3332704ff82ac9d199ce18` |
| Source fixture | `example_source/` |
| Regular files | 34 |
| Regular-file bytes | 1,702 |

## Three-run medians

| Measurement | Median |
| --- | ---: |
| Scan | 0.353 s |
| Run report | 0.078 s |
| Full run report | 0.083 s |
| Plan | 0.240 s |
| Plan report | 0.081 s |
| Full plan report | 0.101 s |
| Plan finalize | 0.126 s |
| Materialize | 0.441 s |
| Complete workflow | 1.502 s |
| Peak resident memory | 27,436 KiB |
| Run workspace | 774,144 bytes |
| SQLite database | 741,376 bytes |
| Process filesystem input | 0 blocks |
| Process filesystem output | 5,456 blocks |
| Materialization input | 0 blocks |
| Materialization output | 880 blocks |
| Identity rows / bytes | 33 / 1,638 |
| Materialization journal rows / commits | 33 / 4 |
| Materialized files | 30 |

This entry is the old-workflow comparison point for issue 98. Package 8 must
run the post-pipeline benchmark on the same environment and compare it with
this entry.
