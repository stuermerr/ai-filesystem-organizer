# AI Filesystem Organizer: Custom Layout Concept

This document defines the Custom Layout capability within AI Filesystem
Organizer. The product is agent-guided and CLI-enforced: an agent helps the
user express a desired structure, while deterministic validation and
materialization decide what can safely execute.

## Goal

Let a user and agent design a top-down directory structure for a Historical
Backup Consolidation, then have the deterministic CLI validate, persist,
finalize, and materialize that exact structure.

## Scope

- Export an editable Custom Layout Draft from an immutable Baseline Projection.
- Support directory-subtree and individual-entry Placement decisions.
- Support explicit User Exclusion of a file, empty directory, or directory subtree. Separate-destination execution leaves the Selected Backup Root unchanged; explicitly destructive in-place execution applies the exclusion there.
- Support explicitly requested Layout-Owned Directories that did not exist in the backup.
- Compile the draft into a persisted executable Plan Projection only after complete validation.
- Visualize the resulting directory structure at a bounded depth for review.
- Preserve the existing finalize, preflight, exact approval, and materialization sequence.

## Requirements

- **REQ-1:** Every baseline entry resolves to exactly one Placement or exactly one User Exclusion.
- **REQ-2:** A Custom Layout Draft never creates multiple copies of one baseline entry.
- **REQ-3:** Nothing disappears implicitly; every omission is an explicit User Exclusion.
- **REQ-4:** Validation rejects unsafe paths, unresolved output collisions, file/directory conflicts, stale layout input, unknown entries, and destination incompatibility.
- **REQ-5:** Validation reports all discovered conflicts together so the user can resolve them interactively.
- **REQ-6:** Applying a valid layout is atomic and produces the only projection materialization executes.
- **REQ-7:** Finalization freezes the exact resolved projection; later changes require another draft.
- **REQ-8:** Except for a Content-Empty Result, one current, plan-and-destination-bound approval authorizes materialization without a second prompt.
- **REQ-9:** The editable layout uses compact subtree rules plus exact-entry exceptions; the CLI compiles these into complete terminal dispositions.
- **REQ-10:** Layout application containing User Exclusions requires an explicit exclusion acknowledgement but no mandatory free-text reason.
- **REQ-11:** A valid Custom Layout may be materialized to a separate destination or through the explicitly destructive In-Place Consolidation mode.
- **REQ-12:** Every layout selector is evaluated against immutable Baseline Projection paths; the deepest matching subtree rule wins and an exact-entry exception has highest precedence.
- **REQ-13:** Canonical Copy selection controls the proven source occurrence but does not change a stable baseline entry identity or its Placement.
- **REQ-14:** During in-place execution, User Exclusions remain protected against interruption; after successful completion, they are permanently absent and no recovery copy is retained.
- **REQ-15:** Except for a Content-Empty Result, one exact destructive approval after current preflight authorizes the complete in-place execution without a second prompt.
- **REQ-16:** A more-specific subtree rule overrides a broader subtree rule, allowing a child Placement beneath a parent User Exclusion or a child User Exclusion beneath a parent Placement.
- **REQ-17:** Custom Plan Projections use the existing materializer and its journaled, idempotent forward-resumption behavior without introducing a separate rollback protocol.
- **REQ-18:** Executable selectors are limited to exact baseline entry identities and exact baseline directory subtrees; globs, regular expressions, and semantic selectors are not accepted.
- **REQ-19:** An agent may translate a broad user request into proposed exact selectors, but must show the concrete matched paths before layout application.
- **REQ-20:** Custom-layout validation checks feasibility against the actual intended destination filesystem; a destination change requires a current check while the existing materializer remains the execution authority.
- **REQ-21:** Broad User Exclusions are reviewed by exact subtree roots with aggregate entry/byte facts; the complete affected-entry evidence remains available in the layout artifact and pageable CLI reporting.
- **REQ-22:** Explicit Layout-Owned Directories and explicitly placed baseline empty directories survive while empty; implicit parent directories exist only when required by placed descendants.
- **REQ-23:** One draft Plan ID contains an immutable Baseline Projection and monotonically increasing immutable Layout Revisions. Applying against a stale base revision is refused, and finalization freezes the latest revision.
- **REQ-24:** `layout.json` contains its schema version, Plan ID, base revision, fixed preserve-unmatched policy, exact subtree Layout Rules, stable-identity Entry Exceptions, and explicit Layout-Owned Directories.
- **REQ-25:** `place_under` replaces a selected subtree root while retaining descendant paths; `place_at` assigns one exact entry's complete output path.
- **REQ-26:** Validation returns every conflict with a stable conflict identifier, type, affected output path and entries, originating rules, and allowed resolution categories. The CLI supplies facts; the agent recommends contextual resolutions.
- **REQ-27:** A User Exclusion covering a Skipped Entry Finding is acknowledgement-required rather than categorically forbidden. The exact finding must be shown and explicitly accepted before in-place execution.
- **REQ-28:** In-place execution of an acknowledged Unverified Exclusion must stage the filesystem entry without following or interpreting it, preserve forward recovery until terminal success, and permanently remove it only as part of successful completion. Technical inability to stage safely remains a preflight refusal.
- **REQ-29:** Exclusion authority is supplied through explicit CLI acknowledgement arguments bound to exact validation findings and persisted in the applied Layout Revision; changing findings or revision invalidates stale acknowledgements.
- **REQ-30:** Validation classifies findings as unconditionally blocking errors, acknowledgement-required findings, or visible non-blocking warnings.
- **REQ-31:** Layout validation/application proves a mode-neutral Plan Projection. Existing materialization preflight separately proves readiness for the exact separate destination or in-place mode.
- **REQ-32:** `plan-report --section structure --depth N` is the bounded tree-review interface, showing revision, resolved structure, collapsed counts, User Exclusions, Layout-Owned Directories, and outstanding validation findings. Exact evidence remains full or pageable.
- **REQ-33:** Excluding a baseline identity during in-place execution removes every proven source occurrence represented by that identity after all occurrences have remained protected through execution.
- **REQ-34:** The agent groups conflicts by structural cause, may recommend an unapproved concrete resolution, and applies no semantic resolution until the user accepts that proposal or supplies another decision.
- **REQ-35:** Acceptance of the exact rendered structure authorizes validation, acknowledged application, verification, and automatic finalization; materialization remains the only subsequent approval-gated action.
- **REQ-36:** After each user rule or coherent rule batch, the agent runs non-mutating layout validation and immediately presents grouped findings and recommendations.
- **REQ-37:** During in-place planning, each affected Skipped Entry Finding is acknowledgement-required. The user may choose an Unverified Exclusion or the current behavior of Unmanaged Retention at its original path; neither outcome is inferred silently.
- **REQ-38:** Custom layout execution is structure-only: it may place, rename, merge, exclude, and create empty directories but never changes file bytes, converts formats, unpacks archives, or edits content.
- **REQ-39:** Structure reporting and in-place preflight list every Unmanaged Retention at its unchanged original path and distinguish it from the separate-destination omission of the same Skipped Entry Finding.
- **REQ-40:** An intentionally content-empty result is permitted only with a dedicated two-approval exception and complete exclusion facts.
- **REQ-41:** Every Plan-Owned Staging Namespace is an unconditional forbidden Placement target. A colliding baseline path must be assigned another output path before validation succeeds.
- **REQ-42:** A Content-Empty Result requires two distinct, unambiguous approvals: one before applying/finalizing the content-empty Layout Revision and one after current materialization preflight. The skill prescribes required facts and authority, not fixed prompt wording.

## Non-goals

- Modifying or deleting entries in the Selected Backup Root outside explicitly approved In-Place Consolidation.
- Replicating one baseline entry into multiple output locations.
- Allowing overwrite or unresolved latest-wins behavior for distinct content.
- Letting the agent bypass deterministic CLI validation.

## Current CLI surface

```console
filesystem-organizer plan-layout-export RUN PLAN_ID --output layout.json
filesystem-organizer plan-layout-validate RUN PLAN_ID layout.json
filesystem-organizer plan-layout-apply RUN PLAN_ID layout.json
```

## Open questions

None blocking.

## Validation plan

Exercise the public CLI through black-box tests covering Placement, subtree transformations, explicit exclusions, complete conflict reporting, stale layouts, finalization immutability, preflight, materialized tree equality, and unchanged source evidence.
