# Proof-Driven Scan Discovery

## Status

Accepted.

## Decision

The scan records inventory metadata for every filesystem entry, then reads file
payload only when it is needed to prove an Exact Duplicate or a Directory Tree
relationship. Equal byte-size groups and same relative descendant paths with
equal sizes are candidates, not conclusions. Empty files use their known
BLAKE3 identity without a payload read.

Candidate and identity-work rows are durable and restartable. Structural
candidate expansion uses adjacent ordered roots, avoiding an all-pairs matrix.
The Consolidation Plan projects persisted scan evidence: a file has either
Content Identity or a Metadata Observation containing its size and modification
time. Plan creation never opens source files.

Execution carries the metadata observation into its immutable manifest and
checks it again before publication. This preserves source-drift detection for
metadata-only files without moving source reads back into planning.

## Consequences

Unique files can be scanned and planned without content reads. Duplicate and
structural conclusions remain content-proven. A changed metadata observation
requires a new scan and plan before materialization.
