# Metadata-first, component-wise RenameProtection v2

## Status

Accepted; supersedes ADR 0009's global current-payload-proof requirement for
RenameProtection, while retaining its capability, ownership, no-overwrite, and
durability boundaries. HardLinkProtection and source-preserving materialization
are unchanged.

## Context

RenameProtection v1 proved every destructive file by a fresh full read before
the first namespace mutation. On the observed 516 GB Selected Backup Root this
read hundreds of gigabytes for nearly fifteen hours, then one latent `EIO`
stopped the whole attempt before any otherwise independent work progressed.
The finalized Plan and Analysis Run already persisted the Content Identities,
Placements, Exact Duplicate Groups, and approved User Exclusions needed to
execute an unchanged, quiescent collection.

## Decision

RenameProtection v2 compiles an additive versioned manifest bound to the run,
Plan, finalized Layout Revision, Plan fingerprint, destination, executor policy,
attempt-specific staging ownership, operation set, identity groups, candidate
order, concrete dependencies, and expected metadata.

Immediately before mutation, normal operations validate exact relative path,
non-symlink type, byte size, available modification-time evidence, and relevant
namespace occupancy. Unchanged unique Placements, Exact Duplicate Groups, User
Exclusions, acknowledged Unverified Exclusions, and directory operations do not
open payloads. Result validation is likewise namespace- and metadata-oriented.

Execution uses bounded concrete dependency components. Sharing an ordinary
parent does not couple siblings. Cycles, path occupancy, duplicate-retention
prerequisites, and necessary ancestor cleanup do. Each component stages with
strict same-filesystem no-replace rename, crosses a filesystem durability
barrier before its durable database state, publishes retained output with the
same primitive, crosses another barrier, and only then removes authorized
quarantine. Adaptive batching may share a barrier without creating semantic
dependencies.

A namespace failure receives an initial attempt plus at most two retries, with
source/staging/final reconciliation after every failure. A local conflict,
drift, permission failure, `EIO`, missing source, or bounded ambiguity records
the operation as `NEEDS_ATTENTION`; concrete dependents become
`BLOCKED_BY_DEPENDENCY`; unrelated components continue. Authoritative database
failure, failed durability barrier, lock loss, foreign staging ownership, or an
unlocalizable manifest contradiction stops the entire invocation.

An Exact Duplicate Group publishes a metadata-matching persisted known
occurrence before duplicate quarantine is permanently removed. If the intended
occurrence is unavailable, an alternate is selected in stable order and must
complete a stable full read matching the persisted algorithm, version, size,
and digest before its supplying role changes durably. Failed or ambiguous
occurrences remain preserved. Exclusions never trigger fallback hashing.

Persisted v2 operation states are `PENDING`, `STAGED_DURABLE`,
`PUBLISHED_DURABLE`, `COMPLETE`, `NEEDS_ATTENTION`, and
`BLOCKED_BY_DEPENDENCY`. The v2 Attempt state is authoritative and includes
`ADMITTED`, `EXECUTING`, `NEEDS_ATTENTION`, `COMPLETE`, `FAILED`, and
`SUPERSEDED`. Workflow Progress is observational but every invocation closes it
consistently. Required unresolved work produces a non-success process result.

A failed v1 attempt may be superseded only after proving its owner binding, that
all v1 operations remain pre-namespace-mutation, and that its staging contains
no payload. Unexpected payload is preserved and supersession refuses. V2 then
reuses the existing Analysis Run, finalized Plan, Custom Layout, and persisted
Content Identities in a new attempt-specific staging namespace.

## Consequences and limitations

V2 removes the normal full-drive reread and global proof barrier, localizes
bounded failures, and makes recovery forward-only at durable operation
boundaries. It preserves strict no-overwrite publication, secret-bound staging,
filesystem-before-database barriers, and the sole-owned-name warning inherited
from v1.

The accepted performance-first tradeoff is that same-metadata replacement,
silent corruption, and latent unreadability may remain undetected during normal
execution. V2 guarantees retention of a metadata-matching persisted known
occurrence before duplicate deletion; without exceptional proof it does not
guarantee that occurrence is currently physically readable.

Disposable VFAT qualification is evidence for the implemented syscall,
recovery, and performance contract only. It neither accesses nor authorizes a
real, non-disposable backup drive.
