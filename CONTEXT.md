# AI Filesystem Organizer Domain Vocabulary

AI Filesystem Organizer helps a person use an AI coding agent to understand,
plan, and safely consolidate or reorganize an explicitly selected, stable
filesystem collection. Repository Agent Skills guide the conversation; the
deterministic CLI remains the only execution and safety authority.

## Language

**Safe Filesystem Consolidation and Reorganization**:
The deliberate, reviewable reduction or restructuring of a stable filesystem collection. The Selected Backup Root remains quiescent for the complete workflow; continuous organization of active or live folders is outside this context.
_Avoid_: Live folder organization, continuous cleanup, autonomous file management

**Historical Backup Consolidation**:
A primary use case for Safe Filesystem Consolidation and Reorganization involving years of copied, renamed, modified, or nested personal backups.
_Avoid_: Product-wide scope, live backup synchronization

**Analysis Run**:
A persisted, read-only record of scanning and relationship-detection results for one explicitly selected, stable filesystem root.
_Avoid_: Live scan, cleanup run

**Metadata Admission**:
A payload-free gate that reuses one source metadata inventory to detect conclusive destination filesystem blockers before content hashing or structural analysis. Renameable path findings and permitted Skipped Entry Findings remain Plan inputs rather than unconditional refusals.
_Avoid_: Content scan, materialization approval, final preflight

**Executor Admission**:
The pre-run selection and real capability probe for an explicit execution mode. For In-Place Consolidation it prefers HardLinkProtection and otherwise selects RenameProtection only after its complete real capability probe succeeds, before Run Workspace creation or source payload reads; the persisted kind and version constrain later execution but do not replace its fresh capability probe.
_Avoid_: Filesystem-name inference, durable execution authorization, generic in-place support claim

**HardLinkProtection**:
The preferred In-Place Consolidation executor, which protects required source names through owned hard links before destructive mutation. It requires real hard-link support and keeps protection staging until final publication and cleanup are durable.
_Avoid_: RenameProtection, source-preserving materialization, filesystem-independent in-place executor

**RenameProtection**:
The fallback In-Place Consolidation executor for a filesystem without hard links but with proven same-filesystem strict no-replace rename, synchronization, and locking support. It evacuates every destructive source action into secret-bound Plan-owned staging before publication and recovers forward from source, staged, and final observations. A staged payload has one owned pathname rather than HardLinkProtection's two simultaneous names.
_Avoid_: Rename fallback, copy fallback, check-then-rename, rollback executor

**Workflow Progress**:
A versioned, throttled observation of a long-running Plan derivation, Custom Layout apply, or Materialization phase, with completed work and an optional deterministic total. It is never Plan evidence, recovery authority, or a durability checkpoint.
_Avoid_: Execution journal, completion proof, recovery decision

**Run Output Root**:
The user-requested output namespace that determines the default Materialized Destination at `OUTPUT/materialized/RUN_ID`; by default it also serves as the Run Workspace Root. It remains separate from the Selected Backup Root.
_Avoid_: Source output, backup folder

**Run Workspace Root**:
The optional CLI-owned storage namespace for internal Analysis Run state. It defaults to the Run Output Root; when supplied separately, the Analysis Run lives at `WORKSPACE/runs/RUN_ID` without changing destination derivation.
_Avoid_: Database root, result destination

**Run Workspace**:
The persisted, agent-owned storage for one Analysis Run, holding its inventory evidence, content identities, Consolidation Plans, and materialization journal; it stays separate from the Selected Backup Root.
_Avoid_: Run store, run folder

**Release Candidate**:
A verified, publishable build for which implementation, documentation, required schema handling, tests, CI, package artifacts, and required smoke tests are complete. It is engineering-complete but not yet a published release.
_Avoid_: Published release, draft build

**Publication Gate**:
The separate, user-controlled authorization boundary for pushes, history and privacy completion, repository visibility, package publication, and the release tag after a Release Candidate has been verified.
_Avoid_: Release Candidate verification, automatic deployment

**Selected Backup Root**:
The user-chosen, quiescent filesystem subtree treated as the complete source collection for one Analysis Run; later changes are handled by a new run rather than continuous change tracking.
_Avoid_: Active workspace, monitored folder, live root

**Skipped Entry Finding**:
A reported, unprocessed filesystem entry such as a symbolic link, unreadable file, or special device; the scanner never follows links outside the Selected Backup Root.
_Avoid_: Silent omission, followed symlink

**Consolidation Plan**:
A reviewable, persisted set of proposed filesystem changes derived from an Analysis Run, which may be executed later only with explicit user approval.
_Avoid_: Automatic cleanup, AI decision

**Plan Projection**:
The normalized, persisted record of each baseline entry's Placement or User Exclusion and the selected Structural Unions that gives a Consolidation Plan its fixed meaning across review and materialization.
_Avoid_: Re-derived plan, rendered report

**Baseline Projection**:
The immutable, system-generated set of candidate output entries and stable identities against which every Custom Layout rule is evaluated. Each baseline entry must resolve to exactly one Placement or one User Exclusion.
_Avoid_: Current layout, editable projection

**Custom Layout Draft**:
An editable, validation-bound set of compact subtree rules and exact-entry exceptions describing the desired Materialized Consolidation structure derived from one Baseline Projection.
_Avoid_: AI file plan, materialization script

**Layout Revision**:
An immutable version of the rules, exceptions, validation result, and resolved projection within one draft Consolidation Plan. Finalization freezes the latest Layout Revision.
_Avoid_: New plan, mutable snapshot

**Layout Rule**:
An order-independent instruction selecting one exact Baseline Projection subtree and assigning its entries a target subtree Placement or User Exclusion.
_Avoid_: Glob, sequential move

**Entry Exception**:
An instruction selecting one stable baseline entry identity and overriding every matching Layout Rule with an exact Placement or User Exclusion.
_Avoid_: Special-case script, path guess

**Validation Conflict**:
A structured finding that prevents a Custom Layout Draft from becoming an executable Plan Projection until it is resolved or, where explicitly allowed, acknowledged by the user.
_Avoid_: Runtime surprise, agent warning

**Unverified Exclusion**:
A User Exclusion that covers a Skipped Entry Finding and therefore requires the user to acknowledge the exact unverified entry before in-place execution may stage and permanently omit it.
_Avoid_: Silent skipped-entry deletion, verified exclusion

**Unmanaged Retention**:
An acknowledged in-place outcome that leaves a Skipped Entry Finding unchanged at its original path because it cannot receive a verified Placement. It is an explicit qualification of the custom result, not a baseline entry or silent omission.
_Avoid_: Placement, ignored conflict

**Placement**:
The terminal disposition that assigns one baseline entry to exactly one safe relative output path in the Plan Projection, independently of which Canonical Copy supplies identical bytes.
_Avoid_: Copy, move operation

**User Exclusion**:
The terminal disposition that omits one explicitly selected baseline entry from the Materialized Consolidation. Separate-destination execution leaves the Selected Backup Root unchanged; successful In-Place Consolidation permanently removes the entry after protecting it for interruption recovery during execution.
_Avoid_: Cleanup, silent omission, temporary skip

**Layout-Owned Directory**:
An explicitly requested empty output directory that has no corresponding entry in the Baseline Projection.
_Avoid_: Placeholder file, inferred category

**Plan-Owned Staging Namespace**:
A reserved output-root path used exclusively for materialization protection, temporary publication, and recovery state; no Placement may target it.
_Avoid_: User directory, overridable collision

**Content-Empty Result**:
A resolved custom layout that started with at least one baseline file but assigns no baseline file a Placement, regardless of any empty directories or Unmanaged Retentions.
_Avoid_: Empty backup, failed plan

**Materialized Consolidation**:
A newly created destination tree containing the approved organized result while the Selected Backup Root remains unchanged. The distinct, explicitly acknowledged In-Place Consolidation execution applies that same finalized Plan Projection to a historical Selected Backup Root after complete hard-link protection.
_Avoid_: Implicit in-place cleanup, unprotected destructive reorganization

**Normal Write Evidence**:
The reasonable-safety evidence accepted for source-preserving materialization: required source evidence is satisfied when bytes are read, and successful filesystem copy or native-clone operations plus final publication establish the normal written result. It is not forensic proof against undetected hardware or filesystem corruption.
_Avoid_: Full destination rehash, forensic end-to-end verification

**Execution Evidence**:
Evidence recorded while materializing a finalized plan, such as the digest observed during one stable streamed copy or the successful native-clone outcome. It does not alter the finalized plan, its fingerprint, or the Plan Evidence on which it depended; a file-level digest may be reused only while its bound source observation remains valid.
_Avoid_: Plan evidence, universal destination verification

**Plan Entry Evidence**:
The tagged evidence frozen into one Finalized Plan Entry: a full Content Identity, a Metadata Observation, or a Layout-Owned Directory with its originating revision. Its kind and complete fields participate in the Plan fingerprint; implicit parent directories remain derived structure.
_Avoid_: Execution Evidence, nullable digest convention

**Materialization Attempt**:
One confirmed execution attempt for a finalized Plan, recorded with coarse state, timestamps, aggregate counters, and a concise outcome or failure classification. A failed retryable Attempt does not make the immutable Plan itself terminal.
_Avoid_: Per-file recovery journal, mutable plan

**Atomic No-Replace Publication**:
The preferred single operation that exposes a completely staged Materialized Consolidation only if its final destination path does not exist at that instant. Any existing path is a conflict and is never replaced. Destination filesystems that explicitly reject this Linux capability may use Locked Rename Publication Fallback instead.
_Avoid_: Check-then-rename, destination overwrite

**Locked Rename Publication Fallback**:
The bounded source-preserving publication mode used only when the destination filesystem explicitly rejects Atomic No-Replace Publication. While holding the exclusive destination sidecar lock, execution proves staging and final are on the same filesystem, verifies the final path is absent immediately before publication, and atomically renames the complete staged tree. It never deliberately replaces an observed destination, but a process that ignores the advisory lock can create the final name during the check-to-rename interval.
_Avoid_: Default publication mode, unlocked rename, manual move, hidden exact no-replace claim

**Metadata Snapshot Revalidation**:
A payload-free comparison of the current Selected Backup Root with its Analysis Run inventory, covering path presence, entry type, byte size, modification time, and processability. It detects ordinary Source Drift without re-establishing Content Identity.
_Avoid_: Content rehash, partial source probe

**Staging Root Identity**:
The Linux filesystem identity (`st_dev`, `st_ino`) of a durable staging root recorded before Atomic No-Replace Publication or Locked Rename Publication Fallback. It proves that a final destination found after a crash is the same directory moved by publication, without adding markers to the user-visible result.
_Avoid_: Content identity, marker file

**Derived Workspace**:
The source-preserving Materialized Consolidation tree that later approved organization stages may transform; it remains separate from the untouched Selected Backup Root.
_Avoid_: Per-stage copy, source-root modification

**Deterministic Consolidation**:
The first processing stage, which inventories every readable regular file and reduces only byte-identical groups using path, filename, and metadata as explainable context rather than as proof of identity.
_Avoid_: Semantic cleanup, AI deduplication

**Structural Backup Consolidation**:
The deterministic analysis and approved materialization of copied or compatible Directory Trees in a stable filesystem collection, using recorded filesystem evidence. Source-preserving materialization is the default; In-Place Consolidation is an explicit destructive exception for a quiescent Selected Backup Root.
_Avoid_: Folder cleanup, automatic directory merge

**Directory Tree**:
A directory chosen as a comparison root together with every descendant entry represented relative to that root; the root directory's own name is not part of its structural identity.
_Avoid_: Folder name, ancestor path

**Directory Fingerprint**:
A versioned deterministic representation of a Directory Tree's descendant structure, including relative paths, entry types, proven file identities, and empty directories.
_Avoid_: Folder hash, semantic signature

**Structural Relationship**:
An explainable, mutually exclusive classification of the readable descendants of two Directory Trees: identical, strict subset, strict superset, union-compatible, or conflicting. It is evidence rather than an instruction to alter files.
_Avoid_: Duplicate verdict, automatic merge

**Evidence Qualification**:
Recorded limits on a Structural Relationship caused by entries that could not be fully processed, such as unreadable files, symbolic links, or special devices; qualifications accompany rather than erase evidence from readable descendants.
_Avoid_: Ignored error, unproven-only result

**Canonical Directory Root**:
The Directory Tree location selected to represent a Structural Relationship during materialization: a strict superset is always selected for a strict-containment relationship; otherwise the shallowest relative path, then lexicographic relative-path order.
_Avoid_: Best folder, guessed original

**Structural Union**:
The materialized combination of Directory Trees whose corresponding readable descendants do not conflict; it retains all copyable descendants and empty directories only after explicit plan approval, while reporting unprocessable exclusions.
_Avoid_: Destructive folder merge, conflict resolution

**Wrapper Pruning**:
The final-layout cleanup that removes a redundant peer root and only its newly empty ancestors after an approved Structural Union; it never removes internal empty directories or unrelated directories.
_Avoid_: Empty-directory cleanup, recursive pruning

**Structural Snapshot Revalidation**:
The pre-materialization check that every descendant of a Directory Tree participating in a Structural Union still matches the Analysis Run inventory; any added, removed, changed, or newly unprocessable entry refuses materialization.
_Avoid_: Best-effort merge, stale-plan execution

**Structural Conflict**:
A Structural Relationship in which corresponding relative paths have different proven file identities or entry types; it is projected with retained variants rather than treated as a Compatible Structural Union.
_Avoid_: Safe union, silently discarded version

**Lossless Conflict Projection**:
A deterministic Materialized Consolidation projection in which a uniquely newest conflicting regular file retains the original output path and every older version is retained under a distinct conflict path with source provenance. Ties, unavailable timestamps, and type conflicts retain every variant only under conflict paths.
_Avoid_: Compatible Structural Union, latest-wins omission

**Copyable Descendant**:
A regular file with a stable, full proven content identity, or an empty directory represented by the Analysis Run, that may be retained in a Structural Union.
_Avoid_: Accessible-looking file, assumed duplicate

**Unverified Counterpart**:
An unprocessable entry at a relative path where another related Directory Tree supplies a Copyable Descendant. The copyable entry may be materialized, while the unverified counterpart is reported as neither equal nor conflicting.
_Avoid_: Duplicate, resolved conflict

**Structural Candidate**:
A pair of Directory Trees selected for deterministic comparison from indexable evidence, such as a matching Directory Fingerprint or shared relative proven file identity; candidate status is not itself a relationship finding.
_Avoid_: Possible duplicate, fuzzy match

**Relationship Actionability**:
The evidence-strength gate between structural evidence and an actionable exact component or persisted Structural Relationship. Matching complete relative-root context of at least two path components beneath different direct backup wrappers is actionable. Otherwise, same-path byte-identical regular files must strictly dominate every other regular-file position in each participating Directory Tree and span at least two independent top-level descendant directory regions. Root-level files contribute overlap but not independent boundary evidence. Exact whole-tree identity alone proves matching contents, not that the roots represent the same copied unit.
For `0.1.0`, the policy is deliberately conservative: false negatives are acceptable, weak evidence leaves Directory Trees separate, and semantic ignore lists or a fixed shared-file threshold do not substitute for evidence strength.
_Avoid_: Candidate-as-proof, fixed anchor count

**Directory Relationship Graph**:
A persisted typed evidence graph whose nodes are Directory Trees and whose edges are proven Structural Relationships with any Evidence Qualifications. It supports reporting and planning, but a connected component is never by itself authority to form a Structural Union.
_Avoid_: Merge graph, duplicate proof

**Maximal Structural Group**:
An actionable group of related Directory Trees not wholly contained in another actionable Structural Union; plans operate on these groups once while retaining nested relationships as evidence.
_Avoid_: Every matching folder, duplicate plan operation

**Pairwise Structural Relationship**:
A non-identical Structural Relationship evaluated and planned between two Directory Trees in Milestone 2. Exact-identity components may contain any number of trees; compatible relationships do not form larger groups because compatibility is not transitive.
_Avoid_: Multi-tree compatible group, transitive compatibility

**Content Enrichment**:
A later, optional processing stage that analyzes selected files beyond deterministic evidence to make additional grouping or restructuring suggestions.
_Avoid_: First-pass scan, automatic reorganization

**Actionable Recommendation**:
A proposed organization change supported by recorded evidence that enters the default Consolidation Plan; Milestone 2 provides no per-relationship exclusion, and execution still requires explicit user approval.
_Avoid_: Automatic action, advisory-only suggestion

**Canonical Copy**:
The single retained path chosen for a byte-identical group in a Materialized Consolidation, normally ranked by modification time with a logged deterministic fallback and optional user override.
_Avoid_: Best file, deleted duplicate

**Exact Duplicate Group**:
Two or more files in one Analysis Run proven byte-identical by full content identity; the Canonical Copy represents the group in a Materialized Consolidation.
_Avoid_: Duplicate files, dedup group

**Content Identity**:
A full byte-content digest established when identity is required as proof, including Exact Duplicate selection and Structural Relationship evidence. It is not a mandatory property of every inventoried or uniquely placed file.
_Avoid_: File metadata, universal inventory digest

**Content Analysis Cache**:
A persisted reuse record for extracted file-content evidence, keyed by content identity and the analysis method version so equivalent content is not processed twice.
_Avoid_: Per-path AI result, repeated model call

**Provider Approval**:
The user’s explicit, per-run authorization for content to be processed by an LLM provider after receiving file, request, and—where applicable—cost estimates.
_Avoid_: Implicit opt-in, background API use
