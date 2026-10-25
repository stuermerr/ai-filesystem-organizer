# Admit and durably complete hard-link execution

## Status

Accepted

## Context

In-place execution formerly discovered its mandatory hard-link capability only
after scan, Plan derivation, and layout work. Its recovery journal could also
record completion before nested namespace changes and protection-staging
removal had crossed their corresponding filesystem durability boundaries.
Repeated payload reads, per-entry checkpoints, inventory rescans, and Layout
SQL amplified the cost on large historical collections.

Filesystem labels and sandbox-visible mounts are not authoritative capability
evidence. Progress counters are useful to operators but cannot safely replace
filesystem or journal evidence.

## Decision

Scan has an explicit execution intent. Source-preserving remains the default;
`--in-place` selects `hard-link-protection`. Executor admission performs an
actual link/unlink probe inside the Selected Backup Root before creating an
Analysis Run or reading payloads, removes its probe artifacts, and persists the
mode, executor kind, admission version, and timestamp only after success. New
runs must later use the admitted mode. Legacy runs without an intent row retain
the existing mode selection and receive the same execution-time probe.

Public in-place preflight is metadata-only. Execution re-probes capability and
builds one invocation context. Content evidence is keyed by the current device,
inode, type, size, and timestamps and is reused only in that invocation. Every
required destructive proof completes before the first unlink; metadata-only
Plan evidence is not upgraded by calculating an unused digest.

HardLinkProtection uses bounded file, byte, and elapsed-time checkpoints. Each
authoritative checkpoint follows successful `syncfs`. Protection is durable
before destructive work. Publication, duplicate cleanup, exclusion cleanup,
and final verification cross an output-durability barrier before protection
staging is removed. Staging removal is synchronized before terminal completion
is recorded. Empty-directory cleanup visits only affected ancestors.

Plan derivation shares one sorted, binary-searchable inventory projection across
structural and conflict work. Custom Layout validates occupancy with indexed
temporary tables and persists active state set-wise. Both retain their prior
deterministic semantics and atomic authoritative transactions.

A generic workflow-progress table records coarse Plan, Layout, and
Materialization observations. It is additive and tolerant of legacy runs. No
progress row participates in fingerprints, recovery, or durability decisions.

## Consequences

Unsupported hard-link filesystems fail before costly analysis with wording
specific to the current executor, leaving room for a future RenameProtection
executor. A changed environment still fails safely at execution. Recovery can
trust that database phase evidence never leads the filesystem durability it
describes, and successful completion implies durable staging removal.

The Selected Backup Root must remain quiescent: hard links do not isolate bytes
from writes through another name. Legacy completion records cannot be promoted
to stronger historical evidence. Wall-clock performance remains dependent on
storage, so deterministic inventory-load, descendant-visit, SQL-statement,
payload-read, traversal, checkpoint, and synchronization counters are the
binding regression evidence.
