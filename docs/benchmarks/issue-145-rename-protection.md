# Issue 145 RenameProtection qualification and benchmark

This record is populated from reproducible disposable qualification and
benchmark commands used for the issue. The corpus generator creates only new
temporary roots; it never discovers or addresses external drives.

Binding counters are executor telemetry and syscall instrumentation: payload
bytes written, verified/read bytes, rename count, sync count/time, SQLite time,
recovery reads, peak RSS, CPU time, and elapsed time. Wall-clock time is
descriptive because directory operations, FAT allocation, hashing, SQLite/WAL,
syncfs, and host caching vary.

The acceptance condition is approximately zero payload bytes written for a
fresh successful RenameProtection execution. Filesystem and database metadata
writes are intentionally excluded. Disposable VFAT results do not authorize a
run against a real, non-disposable backup drive.

## 2026-10-20 local ext4 adapter run

Command shape:

```console
uv run python scripts/benchmark_rename_protection.py \
  --profile PROFILE --files N --bytes-per-file SIZE --force-rename
```

`--force-rename` changes only the newly generated benchmark run because ext4
correctly admits the preferred HardLinkProtection executor. Materialization
still uses the persisted RenameProtection path and its execution-time probe.

| Profile | Files | Payload | Elapsed | CPU | Peak RSS | Renames | Verified bytes | Payload bytes written |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| many-small | 1,000 | 1,024,000 B | 1.056 s | 0.436 s | 34,460 KiB | 2,000 | 2,048,000 B | 0 |
| medium | 200 | 13,107,200 B | 0.632 s | 0.150 s | 33,672 KiB | 400 | 26,214,400 B | 0 |
| large | 4 | 67,108,864 B | 0.566 s | 0.103 s | 33,084 KiB | 8 | 134,217,728 B | 0 |
| duplicate-heavy | 1,000 | 4,096,000 B | 0.886 s | 0.279 s | 33,164 KiB | 1,001 | 4,100,096 B | 0 |
| deep | 300 | 1,228,800 B | 0.733 s | 0.180 s | 33,144 KiB | 600 | 2,457,600 B | 0 |
| fanout | 300 | 1,228,800 B | 0.735 s | 0.165 s | 32,716 KiB | 600 | 2,457,600 B | 0 |
| exclusions | 200 | 819,200 B | 0.574 s | 0.122 s | 33,136 KiB | 300 | 1,228,800 B | 0 |
| three-way path cycle | 30 | 122,880 B | 0.520 s | 0.077 s | 33,516 KiB | 60 | 245,760 B | 0 |

Profiles recorded 5–19 adaptive filesystem checkpoints (bounded by file,
logical-byte, and elapsed-time policy), zero clone attempts, zero
streamed/copied payload bytes, and zero payload bytes written. The
duplicate-heavy profile quarantined and deleted 999 proven duplicate files
(4,091,904 bytes) after durable output. These are descriptive single-run local
figures, not portable throughput guarantees.

Recovery profiles used 300 1-KiB files and injected termination at binding
executor boundaries:

| Profile | Crash boundary | Total elapsed | Resume elapsed | Recovery reread | Payload bytes written |
| --- | --- | ---: | ---: | ---: | ---: |
| interrupted protection | after first source-to-stage rename | 0.816 s | 0.548 s | 1,024 B staged | 0 |
| interrupted publication | after first stage-to-final rename | 0.892 s | 0.455 s | 306,176 B staged + 2,048 B final/source | 0 |
| durable 128-file prefix | after protection batch commit | 0.815 s | 0.498 s | 131,072 B staged | 0 |

The harness also covers the protection/publication batch-sync and batch-commit
boundaries. CPU, peak RSS, rename, checkpoint, sync, and database timings remain
available in each JSON result; the table highlights the acceptance-sensitive
measurements.

## 2026-10-20 disposable FAT32 qualification

A new 256 MiB image was formatted with `mkfs.vfat -F 32`, attached as a loop
device, and mounted read-write at the uniquely labelled `FSO145` mount. The
qualification script addressed that exact mount only. The image was unmounted
and deleted after the run.

- real admission rejected hard links and automatically selected
  `rename-protection` v1;
- file and directory no-replace moves, occupied-name preservation, syncfs,
  locking, probe cleanup, layout-owned directories, relocation, deep paths,
  and exact-duplicate quarantine completed;
- fresh execution staged 4 files / 1,134,592 bytes, published 3 files /
  1,097,728 bytes, deleted one 36,864-byte duplicate, issued 7 renames and 5
  filesystem checkpoints, and wrote 0 payload bytes;
- recovery completed after injected interruption following a staging rename,
  the protection commit, a publication rename, OUTPUT_DURABLE, quarantine
  deletion, and staging removal;
- a prepared STAGING_DURABLE attempt survived a real unmount, loop detach,
  reattach, and remount; secret/manifest ownership (not inode identity) was
  accepted and forward recovery completed with 0 payload bytes written;
- no unexplained destination was overwritten and no Plan-owned staging
  remained after any successful completion.

The qualification harness additionally defines regular exclusions, an
acknowledged skipped-directory exclusion, a three-way path cycle, and a
configurable reduced-scale high-operation-count corpus. These cases are kept
in the reproducible script rather than inferred from filesystem-type names.
Its complete expanded corpus was also dry-run through the real executor on a
disposable local ext4 root with 300 added files: 310 entries staged, 307
Placements published, three exclusions/duplicates removed, 617 no-replace
renames issued, and zero payload bytes written. That dry-run validates the
harness logic but is not represented as additional VFAT evidence.

This qualifies the implemented behavior on one disposable Linux VFAT setup. It
does not claim transactional FAT durability, arbitrary power-loss immunity, or
media/controller integrity. No real backup drive was a source, target, probe
root, or mutation target; this qualification grants no authorization for a
later real-drive run.
