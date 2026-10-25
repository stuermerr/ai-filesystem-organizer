# Issue 151: RenameProtection v2 qualification record

## Scope and safety

All commands below create their corpus beneath a temporary directory or a
disposable FAT image in `/tmp`. They never enumerate, mount, read, or write a
real backup device. The image is labelled `FSO151Q` and is not a copy of any
user collection.

## Normal metadata-first benchmark

Command, repeated three times on 2026-10-21:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/benchmark_rename_protection.py \
  --profile many-small --files 1000 --bytes-per-file 4096 --force-rename
```

The fixture contains 1,000 files and 4,096,000 payload bytes. Each fresh run
selected RenameProtection v2, completed normally, and reported zero payload
read files and bytes. It staged and published all 1,000 files using 2,000
renames.

| Run | elapsed seconds | CPU seconds | payload read bytes | checkpoint count |
| --- | ---: | ---: | ---: | ---: |
| 1 | 5.559 | 0.987 | 0 | 24 |
| 2 | 5.492 | 0.896 | 0 | 24 |
| 3 | 5.698 | 0.924 | 0 | 24 |
| median | 5.559 | 0.924 | 0 | 24 |

This is the acceptance measurement for the normal v2 path: its validation
reads 0 of the expected 786 GB on the representative production shape. V1's
pre-mutation global proof necessarily read every file; its validation time is
therefore payload-bound, while v2's normal validation has no payload-I/O term.
The benchmark intentionally does not present total wall-clock speedup as a
hard-coded guarantee because rename and durability-barrier costs depend on the
filesystem and fixture.

## Disposable VFAT exercise

The repository includes a VFAT-only qualification runner:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/qualify_vfat_rename_protection.py \
  /media/user/FSO151Q --high-operation-count 256
```

It checks unique placements, duplicate retention, exclusions, skipped
directories, cycles, type conflicts, high operation count, crash/resume, and
the zero-payload-read normal-path telemetry. The runner forces neither a real
drive nor a production collection.

On this CI workspace, udisks could create and identify the disposable image,
but the sandbox remounted every loop-backed VFAT filesystem read-only on its
first write (`EROFS`). A direct privileged mount also required an unavailable
interactive sudo credential. Consequently this record does **not** claim a
passed VFAT execution for this host. The failure is environment-level and is
kept visible rather than being substituted with a real backup device or
silently relabelled as a VFAT pass. The automated adapter, crash/recovery, and
metadata-read contract are covered by the regression suite; run the command
above on a host that allows a writable disposable loop mount before release.
