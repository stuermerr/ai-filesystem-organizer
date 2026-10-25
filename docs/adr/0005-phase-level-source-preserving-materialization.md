# Use phase-level durability for source-preserving materialization

The normal `0.1.0` path builds one owned partial destination without per-file
durability, flushes the destination filesystem once, publishes the whole tree
atomically with no-replace semantics, and synchronizes the destination parent
before reporting success. Recovery uses coarse Materialization Attempts, a
durable manifest, and the staging root's filesystem identity. Incomplete owned
staging is restarted instead of resumed per file. A complete durable staging tree
whose publication failed for a retryable environmental reason is instead preserved;
recovery proves its recorded root identity, exact structure, and byte equivalence to
the revalidated quiescent source before retrying atomic publication. This deliberately trades
forensic destination verification and exact resume for substantially lower I/O
while the source remains unchanged and available for a safe retry.

## 0.1.1 amendment: resumable staging

Real-world HDD execution showed that restarting a large partial staging tree is
not operationally acceptable. Source-preserving materialization now makes the
owned staging-root identity durable when staging begins. Completed files are
grouped into bounded checkpoints after at most 128 files or when accumulated
bytes reach 1 GiB. The destination filesystem is synchronized once before each
batch's existing Execution Evidence is committed atomically. That ordering
makes each committed batch a durable completion checkpoint rather than an
optimistic progress marker, without introducing a synchronous device barrier
for every file and ancestor directory.

Recovery accepts only a contiguous prefix of those checkpoints. It proves the
staging-root identity and owner payload, rejects unexpected entries, and hashes
each checkpointed staged file against the revalidated source before reuse. Any
uncheckpointed suffix files or temporary file are removed and recopied;
changed checkpointed content, non-contiguous evidence, or any other ambiguous
content fails closed. Whole-tree synchronization, staging-root verification,
and atomic no-replace publication remain the final durability and publication
boundaries.

## 0.1.1 performance and observability amendment

Streamed files are closed without a per-file `fsync`. A `syncfs` descriptor is
opened before staging writes begin and retained through publication. On Linux
5.8 or newer this preserves the writeback-error cursor while allowing each
checkpoint to make the entire destination filesystem durable before its
Execution Evidence transaction commits. A synchronization error therefore
prevents the affected batch from becoming confirmed.

Checkpoint evaluation occurs between completed files and uses all three bounds:
128 files, 1 GiB of copied data, and an adaptive elapsed interval. The interval
starts at five seconds, targets synchronization overhead of at most five percent
using measured checkpoint time, and remains between one and thirty seconds. One
large or blocked file can exceed the time or byte target because no checkpoint
can safely interrupt an individual copy.

Native-clone capability is cached per source/destination device pair. `ENOTTY`
is treated as conclusive for regular files. `EOPNOTSUPP` requires rejection from
two distinct regular source files in the same device pair before the attempt
caches it, because one file may have a file-specific restriction. Cross-device,
permission, space, and I/O errors never create a negative capability entry.

Execution results include versioned telemetry for the active phase, copied and
durably confirmed files and bytes, reused files, logical source and staged bytes
read during recovery, checkpoint counts and elapsed synchronization/database
time, and native-clone attempts. These are logical process I/O counters; they do
not claim physical block-device reads.

### Recovery evidence decision matrix

| Evidence available | Source check | Staged check | Result |
| --- | --- | --- | --- |
| Persisted digest plus ordinary mutable file metadata | Full stable byte read | Full stable byte read | Reuse only when both current digests agree |
| Missing digest or incomplete checkpoint evidence | Existing Plan and metadata checks | Do not trust staged work | Recopy or refuse under the confirmed-prefix contract |
| Root identity, owner payload, advisory lock, or prior durability alone | Insufficient to prove current bytes | Insufficient to prove current bytes | Never select a reduced-read path |
| Filesystem-enforced immutable content identity, independently proven for each side | A future version may use constant-time evidence | A future version may use constant-time evidence | Not implemented by this amendment |

FAT32 and the currently supported ordinary-file evidence have no mechanism that
proves current content without reading it. Recovery therefore retains full byte
verification on both sides, including detection of same-size changes with a
restored modification time. The benchmark reports these reads separately so the
cost and this limitation remain visible.

FAT filesystems can also report a different directory inode for the same staging
root across invocations. A changed inode on the already-recorded destination
device is therefore a recovery candidate rather than immediate proof of a
different tree. For interrupted partial staging, recovery still requires the
exact durable owner payload before inspecting the tree. It then rejects symbolic
links, special files, unexpected paths, non-contiguous checkpoint evidence, and
changed checkpointed bytes. Durable staging receives the same exact-structure and
full source/staged byte validation; its owner marker may already have been removed
for publication. Only after all applicable checks pass does recovery conditionally
replace the recorded inode with the currently observed identity. A missing prior
identity, a device change, an invalid owner marker, unexpected structure, or any
byte mismatch remains ambiguous and fails closed. Published-destination
reconciliation retains the original exact device/inode requirement because the
published tree has no private ownership marker or staging pathname.

The reproducible benchmark is `scripts/benchmark_materialization.py`. It runs
the public CLI workflow on disposable datasets, rotates checkout order, requires
at least three repetitions, verifies source and output trees, and reports medians
and spread for elapsed/CPU time, throughput, logical I/O, synchronization, clone
attempts, SQLite time, memory, filesystem, and kernel. Filesystem mount options,
including FAT `flush`, are an explicit external test label and must be benchmarked
as a separate dimension rather than changed by the tool.
