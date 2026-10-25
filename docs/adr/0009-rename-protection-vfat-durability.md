# RenameProtection v1 and VFAT durability boundary

## Status

Accepted

## Context

HardLinkProtection correctly refuses VFAT/FAT32 because those filesystems do
not provide hard links. Copying roughly 516 GB merely to reorganize one stable
filesystem would make the intended workflow impractical. Skipping the hard-link
probe would remove protection and recovery rather than add a valid executor.

VFAT is non-journaled and inode observations may change across remounts. A
check followed by plain rename can also overwrite a destination created by an
uncooperative process in the race interval.

## Decision

Executor Admission prefers HardLinkProtection v1. If its real probe reports an
unsupported capability, RenameProtection v1 is admitted only after real file
and directory moves, exact `renameat2(RENAME_NOREPLACE)` publication, EEXIST
preservation of both occupied entries, syncfs, advisory locking, and probe
cleanup succeed on the Selected Backup Root. Operational errors fail admission.

Before its first destructive move, RenameProtection durably persists a
complete operation manifest and an owner record bound to run, Plan, finalized
Layout Revision, Plan fingerprint, executor kind/version, attempt, selected
root, operation-set digest, and a random secret stored in the Run Workspace.
Operation IDs are deterministic over the same finalized intent.

The pre-mutation payload proof is committed in bounded batches and reports
explicit progress. Each committed proof binds its digest to the observed
device, inode, size, and modification time. Recovery skips re-reading that
payload only when all observations still match. Any drift in a committed prefix
fails closed, and a remount that changes FAT inode observations therefore
requires operator attention rather than treating the proof as portable.

Phase A proves current evidence and evacuates every Placement, exact duplicate,
User Exclusion, and acknowledged Unverified Exclusion into sharded Plan-owned
staging with strict same-filesystem no-replace renames. A global
`PROTECTION_DURABLE` barrier precedes publication. Phase B creates planned
directories and publishes Placements with the same strict primitive. After the
complete output and Unmanaged Retentions are verified, `OUTPUT_DURABLE`
authorizes quarantine deletion. Staging removal is synchronized before
terminal completion. Payload copying is not a fallback.

Recovery is forward-only and observes source, staged, and final names before
database checkpoints. A lagging checkpoint may adopt revalidated filesystem
work; a checkpoint never overrides contrary observations. Missing, foreign,
or ambiguous ownership and unexplained occupants fail closed. FAT inode
identity is supporting invocation-local evidence, never durable ownership.

## Consequences and limitations

RenameProtection has only one owned pathname after evacuation, unlike the two
simultaneous names held by HardLinkProtection. Manual staging deletion or FAT
metadata corruption can therefore remove the sole namespace reference. Neither
executor snapshots bytes or protects against malicious mutation, media failure,
undetected corruption, or arbitrary power-loss outcomes.

`syncfs` requests filesystem writeback and reports errors available to Linux;
it does not make VFAT transactional, prevent torn metadata, or guarantee media
and controller behavior. The Selected Backup Root must remain quiescent and
cooperatively locked. No rollback to the original layout is provided.

Disposable VFAT qualification proves the implemented syscall/recovery contract
on expendable media only. It neither accesses nor authorizes access to a real,
non-disposable backup drive. Such a run requires a later explicit request plus
Final Plan Approval and Materialization Approval.
