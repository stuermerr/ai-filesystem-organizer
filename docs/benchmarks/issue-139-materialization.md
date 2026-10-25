# Issue 139 materialization benchmark

These exploratory full-CLI measurements compare commit
`616709235267d55e513c1f984996332ff9e9f161` with the issue 139 working tree.
They used Linux 7.0 on x86-64, a 13th Gen Intel Core i9-13900H, and the ext4
`/tmp` filesystem on a Samsung MZVL21T0HCLR-00B NVMe drive, with warm
process-visible caches, identical generated datasets, alternating checkout
order, and three repetitions. Counters are logical process I/O, not physical
device reads.

| Scenario | Baseline median (range) | Candidate median (range) | Result |
| --- | ---: | ---: | ---: |
| Fresh, 512 × 4 KiB | 4.410 s (4.378–4.459) | 0.465 s (0.442–0.465) | 9.48× faster |
| Fresh, 16 × 64 MiB | 2.159 s (1.767–3.711) | 1.893 s (1.664–6.853) | 1.14× median; high storage variance |
| Resume, 5% of 128 × 4 KiB confirmed | 1.132 s (1.125–1.164) | 0.170 s (0.165–0.185) | 6.65× faster |
| Resume, 75% of 128 × 4 KiB confirmed | 0.433 s (0.432–0.441) | 0.187 s (0.177–0.194) | 2.32× faster |

The benchmark's remaining safety scenarios were exercised with eight 4 KiB
files and three repetitions each: same-size/restored-mtime staged tampering,
same-size/restored-mtime source drift, and previous-version evidence were
explicitly refused without publication; interrupted unconfirmed work resumed
and reproduced the source tree. The automated suite separately fixes these
behaviors as regression tests rather than treating timings as safety evidence.

For the small-file fresh run, synchronization calls fell from 525 to 13 and
clone attempts from 512 to 2. For the large-file run, synchronization calls fell
from 26 to 10, but synchronization-time variance dominated one candidate run.
The large-file result therefore does not support a broad speedup claim.

Recovery read volume did not fall. The 5% prefix read 24,576 bytes from each
side; the 75% prefix read 393,216 bytes from each side, then read 131,072 source
bytes to copy the remainder. These counts were identical across versions. The
currently available mutable-file evidence cannot safely waive either
verification read. The improvement came from fewer synchronization and
unsupported-clone operations.

The upgrade recommendation is workload-dependent. Small-file materializations
with much work remaining benefit immediately. Interrupting mainly to upgrade is
less attractive when a large confirmed prefix must first be fully reread from
both source and staging; compare that verification volume with the remaining
copy volume and device throughput. Existing per-entry checkpoints remain
compatible and require no staging reset.

FAT32 and its `flush` mount option were not benchmarked because no disposable
FAT32 device was available. Run `scripts/benchmark_materialization.py` on
disposable storage with an explicit `--storage-label` for each mount-option
variant; do not remount an active Selected Backup Root.
