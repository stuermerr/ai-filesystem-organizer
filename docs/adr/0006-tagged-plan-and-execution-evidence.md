# ADR 0006: Separate Plan Evidence from Execution Evidence

## Status

Accepted for the `0.1.0` pipeline redesign in issue #98.

## Decision

Analysis Run schema version 10 stores source observations independently from
optional Content Identity. It also reserves constrained, resumable records for
duplicate-size candidates, structural-pair candidates, identity work, and
aggregate discovery progress. A Content Identity belongs to exactly one
persisted source observation.

Consolidation Plan schema version 8 gives every finalized entry exactly one
tagged Evidence variant:

- `content-identity` binds algorithm, algorithm version, byte size, and digest;
- `metadata-observation` binds entry type, applicable byte size, and `mtime_ns`;
- `layout-owned-directory` binds the output path and originating Layout
  Revision.

SQLite constraints reject incomplete variants and rows containing fields from
more than one variant. The finalized projection fingerprint uses a versioned
`final-plan-v3` domain and includes the Evidence tag and all variant fields.

Materialization Attempt schema version 1 records one confirmed execution with
coarse state, timestamps, aggregate counters, and an optional terminal failure
classification. Execution Manifest version 3 is owned by an Attempt and binds
the mode, canonical destination, finalized Plan fingerprint, staging owner,
staging path, and the staging root's device/inode pair when known. Streamed
copy or native-clone evidence is stored separately in
`execution_file_evidence`; inserting it cannot alter the finalized projection
or its fingerprint.

The new normal materializer may use only `ADMITTED`, `STAGING_STARTED`,
`STAGING_DURABLE`, `PUBLISHED`, `COMPLETE`, and terminal `FAILED`. The existing
in-place journal remains available for its stricter recovery engine.

## Consequences

Proof-driven scanning can leave a unique preserved file without a digest while
still producing an explicit, deterministic Plan. Later streamed-copy evidence
can add a digest without changing what the operator reviewed. Execution retries
can create new Attempts without overwriting the facts of earlier attempts.

The schema cutover deliberately refuses unsupported persisted versions with a
fresh-scan, fresh-plan, or materialization-restart instruction. Deterministic
legacy conversion may be added only where every required tagged field can be
reconstructed without reading source payloads or inventing evidence.
