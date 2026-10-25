# Issue 143 Package A benchmark

These measurements compare `origin/main` at
`2c3c64fd199256ff988b8c7b7624f9499d262d38` with the Package A working tree on
the same Linux/ext4 host. Each 10,000-entry Plan run alternated main then
candidate three times. The generated scan is outside `plan_seconds`.

| Workflow | Main median | Candidate median | Deterministic candidate evidence |
| --- | ---: | ---: | --- |
| Plan, 1,000 mirrored structural pairs | 25.900 s | 0.757 s | one 12,005-row inventory load; 2,006 indexed ranges; 46,000 descendant visits |
| Root-wide Layout, 10,000 entries | 0.496 s | 0.863 s | 103 traced SQL statements, including 39 throttled progress writes |

The Plan sample is 34.2 times faster while producing the same 6,001 output
entries. The small Layout sample does not establish a wall-clock improvement;
its candidate median is about 74 percent slower and is reported rather than
discarded. Much of the difference is the new durable progress observation on
this memory-cache-heavy fixture. Its acceptance evidence is the bounded
statement count: it no longer grows by issuing one occupancy query or
active-state update statement per entry. Storage, cache, and SQLite variance
make the deterministic counters the binding regression gates.

Reproduce the full intended size matrix with at least three repetitions:

```console
uv run python scripts/benchmark_plan_derivation.py \
  --sizes 10000 100000 500000 --repetitions 3 \
  --output benchmarks/issue-143-plan.json
uv run python scripts/benchmark_custom_layout.py \
  --sizes 10000 100000 1000000 \
  --scenarios one-entry subtree root-wide --repetitions 3 \
  --output benchmarks/issue-143-layout.json
```

For main-versus-candidate comparison, run the same script and arguments from
clean worktrees at the exact revisions, alternate checkout order, and preserve
the JSON outputs. The Plan script detects whether the checkout exposes Package
A counters, so it can drive the pre-change API as well. The existing
`scripts/benchmark_materialization.py` remains the full-CLI payload,
checkpoint, synchronization, recovery-read, and correctness benchmark; run it
on disposable storage and record mount options as described by ADR 0005.

Automated tests bind the non-timing claims: one global Plan inventory load,
indexed descendant visits below projection-by-inventory work, fewer than 1,000
Layout statements for both subtree and root-wide 100,000-entry changes, at-most-
once in-place hashing for a stable inode observation, no full-tree empty-
directory walk, bounded checkpoint ordering, and durable-staging recovery.
