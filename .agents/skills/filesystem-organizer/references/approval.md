# Approval-bound materialization

Read `workflow.md` for the lifecycle and its two approval gates. A successful preflight is point-in-time safety evidence, not authorization.

Materialization Approval can be requested only after the current Plan has received Final Plan Approval, validation and finalization have succeeded, the finalized Plan has been verified, and `materialize-preflight` has succeeded. A response to any earlier materialization question is not Gate 2 approval and cannot be reused after those steps complete.

## Execution commands

After Materialization Approval, invoke the command matching the approved mode:

```console
# Source-preserving
uv run filesystem-organizer materialize ANALYSIS_RUN PLAN_ID --yes

# In-place
uv run filesystem-organizer materialize ANALYSIS_RUN PLAN_ID --in-place --acknowledge-destructive
```

Propagate `--destination EXACT_DESTINATION` only when that exact path appeared in the approved Gate 2 summary; otherwise omit it so the Plan's normal derived destination remains authoritative. Include `--acknowledge-content-empty` when the Final Plan Approval visibly covered a Content-Empty Result. The CLI retains finalized-plan validation, source and Structural Snapshot Revalidation, destination safety, no-overwrite behavior, locking, and journaled recovery.

Interactive source-preserving execution accepts its `yes` prompt. Interactive in-place execution instead requires typing the exact Selected Backup Root. Headless in-place execution uses `--acknowledge-destructive` as its single confirmation; generic `--yes` is neither required nor sufficient.

## After Materialization Approval

Execute all remaining internal steps with no further approval. This includes repeated validation or preflight, retrying safe checks, and recovery through the persisted executor selected at scan; never switch execution mode or executor during execution.

If the materialization command appears to end unexpectedly, times out, loses or truncates output, or returns without clear publication confirmation, do not invoke `materialize` again. First run:

```console
uv run filesystem-organizer status ANALYSIS_RUN
```

Treat this persisted CLI status as authoritative:

- When `status == complete` or materialization is complete, report success and stop without recovery. This overrides speculative recovery assumptions.
- When an active writer is reported, leave that writer undisturbed, wait, and recheck `status`; do not invoke `materialize` while it may still be active.
- When status is inconclusive but actual advisory/kernel lock evidence shows that `.filesystem-organizer.lock` is actively held, treat the original writer as active, wait, and recheck `status`; do not invoke `materialize`. Mere existence of the lock file is not active-lock evidence.
- Resume or retry materialization only after authoritative status or actual lock evidence establishes that no writer is active **and** persisted run state explicitly reports a valid recovery or resume candidate. Then follow the normal workflow in `recovery.md` with the same approved command.
- Otherwise report the exact persisted blocker without starting another writer.

For source-preserving mode, this also includes native-clone fallback, owned staging recovery, staging, durability work, and Safe Publication. Safe Publication prefers Atomic No-Replace Publication and may use the supported Locked Rename Publication Fallback when the destination filesystem explicitly rejects the preferred method. The fallback holds the exclusive destination sidecar lock and verifies absence immediately before atomic rename, but a process that ignores that advisory lock can still create the final name during the check-to-rename interval.

For in-place mode, follow the persisted executor kind, version, recovery state, and exact Selected Backup Root reported by preflight and `status`. Treat the numeric executor version as diagnostic compatibility evidence; do not turn it into a general product label. Report the completed result, mode, destination, executor facts, and returned terminal Run Workspace cleanup status where present.

If publication fails after durable staging, immediately inspect persisted
`status` and follow the mount/filesystem diagnosis in `recovery.md`. The narrowly
bounded normal remount case defined there is a technical recovery step for the same
approved destination. This does not authorize a destination change, forced
write-enable over integrity warnings, deletion of ambiguous staging, repair of a
mounted filesystem, or risky/ambiguous filesystem repair.

## Autonomous safe fallbacks

Choose an alternative implementation automatically when the original check or optimization is unavailable and the alternative supplies equivalent or more conservative evidence, or uses an already-supported safe fallback, without changing the approved Plan, exclusions, destination, or materialization semantics.

Examples include native clone unavailable to streamed copy, an equivalent supported capability probe, rerunning validation or reporting, and continuing after an optional optimization fails. Never weaken a safety check to make a failure pass.

## Exceptional Plan results

Final Plan Approval covers visibly reported User Exclusions, Skipped Entry Findings, and a Content-Empty Result. Pass the corresponding CLI acknowledgement flags from that approved semantic intent; they do not create a separate conversational approval. If reaching the result first requires a retain-versus-exclude choice or another unresolved meaning, obtain that semantic decision while constructing the Plan.

An Unreadable Required Source is a hard blocker. Do not reinterpret it as an optional exclusion merely to continue.

## Semantic decisions

Ask the user when resolution requires changing the destination, adding or removing exclusions, changing conflict handling, materially changing the approved layout, switching between source-preserving and in-place mode, or deleting or replacing an existing destination. A new semantic decision invalidates any approval bound to the superseded facts.

Technical execution choices remain the agent's responsibility.

## Hard blockers

Stop and explain the authoritative CLI evidence for every hard blocker:

- **Source Drift:** source evidence changed after scan or analysis.
- **Destination Conflict:** the target exists and is not proven output owned by this run.
- **Insufficient Disk Space:** safe completion cannot fit.
- **Unreadable Required Source:** required source content cannot be read.
- **Plan Invalid:** the Plan is inconsistent, stale, or unsafe to execute.
- **Lock Conflict:** another process owns the run or destination lock.
- **Ambiguous Recovery:** staging or final output ownership cannot be proven.
- **No Safe Publication:** neither supported publication method can safely publish the result.
- **Permission / I/O / Integrity Failure:** the operating system or storage reports a real failure that safe diagnostics and narrowly bounded normal remediation cannot resolve; integrity-triggered read-only state remains blocking until its repair is proven safe.

Do not substitute a weaker check or ask for approval to bypass one of these conditions.
