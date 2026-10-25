# Recovery through public state

Recover from persisted CLI state, not conversational memory.

The `status` command is the first diagnostic for an explicitly selected Analysis Run and its persisted result is authoritative. A `.filesystem-organizer.lock` file is the persistent path used for kernel `flock`; its existence alone proves neither an active lock nor a stale lock. Diagnose lock ownership only from current CLI status or actual advisory/kernel lock evidence. When status is inconclusive but that lock is actively held, treat the original writer as active, wait, and recheck status without invoking `materialize`. Evidence that the lock is not held does not by itself authorize a retry.

After successful terminal Run Workspace cleanup, `completion.json` may remain, `analysis.sqlite3` may be intentionally absent, and `.filesystem-organizer.lock` may remain. This is a normal completed state. Never describe `completion.json` present with `analysis.sqlite3` absent as corruption or broken recovery state without contrary persisted CLI evidence. Run `status` first; when it reports completion, that completed persisted status overrides speculative recovery assumptions.

1. Resolve and disclose the actual Run Workspace Root. It defaults to the Run Output Root, but a scan using `--workspace-root WORKSPACE` must recover from that exact workspace location.
2. For the default layout, run `uv run filesystem-organizer runs RUN_OUTPUT_ROOT`. For a separate workspace, run `uv run filesystem-organizer runs --workspace-root RUN_WORKSPACE_ROOT`.
3. If no Analysis Run matches, report that fact and ask whether to start a new scan.
4. If multiple runs could match—or multiple runs are incomplete—list their Analysis Run paths, IDs, Selected Backup Roots, statuses, and timestamps. Require the user to select one; never choose the newest implicitly.
5. Run `uv run filesystem-organizer status ANALYSIS_RUN` for the explicitly selected path.
6. Stop safely on missing, malformed, unsupported, or ambiguous state.
7. If the selected run is incomplete and `recovery_available` is true, run `uv run filesystem-organizer resume ANALYSIS_RUN`, then inspect `status` and render `report` again.
8. For a complete run, continue from its reported plan lifecycle: create a plan when none exists, review a draft, or review a finalized plan. Keep every action bound to an explicit plan ID.

## Active projection recovery

When a draft Plan operation reports `active-projection-inconsistent`, verify the rebuildable projection through the public CLI:

```console
uv run filesystem-organizer plan-layout-rebuild-active ANALYSIS_RUN PLAN_ID --verify
```

If verification reports inconsistency, repair only the derived active projection of that same draft Plan:

```console
uv run filesystem-organizer plan-layout-rebuild-active ANALYSIS_RUN PLAN_ID --repair
```

Inspect the returned revision, fingerprints, `consistent`, and `repaired` facts, then rerun the operation that detected the inconsistency. Repair is unavailable for a finalized Plan. Never repair this state through SQLite.

## Materialization recovery

Enter materialization recovery only after authoritative status or actual lock evidence establishes that no writer is active and persisted run state explicitly reports a valid recovery or resume candidate. A writer-free result without that persisted recovery state is a blocker, not permission to invoke `materialize` again.

When `status` reports a `resume_candidate`, preserve the exact persisted Attempt and inspect its executor kind, executor version, state, failure or attention evidence, and resume strategy. Never infer these facts from conversation, discard proven recovery state, rescan, or replan.

When `executor_kind` is `rename-protection` and `executor_version` is `3`, ordinary recovery has no staging tree. Inspect `high_watermark`, `attention`, `blocked_dependencies`, `metrics`, and any `active_pivot`. Preserve an active pivot and its sole temporary pathname. Rerun the same approved in-place materialization command when `resume_candidate` is true; the backend reconstructs the deterministic Plan schedule and reconciles sparse failures and the active pivot. Do not apply source-preserving staging ownership or publication instructions to this state. Refer to the executor family as RenameProtection; surface its numeric version only when reporting persisted diagnostic facts.

For a source-preserving or legacy staged Attempt, inspect the failure classification, staging path, ownership result, staging identity, and resume strategy. Do not discard proven staging or restart copying from zero. The backend reuses verified checkpointed files and recopies only incomplete or uncheckpointed work; a fully staged result continues directly toward publication.

When `staging_identity` is `changed-on-same-device` and
`staging_identity_revalidation_required` is true, preserve the staging tree and
rerun the same approved materialization command. This is an explicit recovery
candidate for filesystems such as FAT32 whose reported directory inode can change.
The backend must validate the owner marker where applicable, exact structure,
checkpoint evidence, and source/staged bytes before it rebinds the identity. A
device change, `not-proven` identity, invalid marker, unexpected entry, or byte
mismatch remains a blocker.

For a read-only filesystem blocker, automatically inspect the effective mount for
the affected path reported by the CLI—the staging/final parent for source-preserving mode or the Selected Backup Root for in-place mode—with `findmnt --target` and explicit
`TARGET,SOURCE,FSTYPE,OPTIONS,VFS-OPTIONS,FS-OPTIONS` columns. Confirm the topology
in `/proc/self/mountinfo` and inspect recent kernel and filesystem diagnostics.
Distinguish a read-only bind/VFS mount or ordinary mount option from a read-only
filesystem superblock and from a kernel/filesystem safety remount caused by I/O,
corruption, journal, device, or media errors.

A single normal read-write remount may be attempted autonomously only when all of
these facts are proven: the target is the exact dedicated user-owned removable
destination mount; it is not `/`, a system mount, or shared with unrelated data or
users; the read-only state is solely an ordinary mount/VFS option; no kernel or
filesystem integrity warning is present; and the platform provides its normal
supported remount operation. Use bind-remount semantics for a read-only bind mount.
Never force a remount, suppress filesystem safety behavior, or write-enable an
integrity-triggered safety remount.

Verify the effective mount options afterward and create/remove a uniquely named
write probe in the affected writable parent. If repair is indicated, identify the exact
filesystem-specific procedure and run read-only diagnostics only. Never repair a
mounted filesystem or automatically run a metadata-changing repair where device,
unmount, data-loss, shared-use, or unrelated-data impact is possible; that is a real
user/environment decision.

After write capability is verified, rerun `status`, then invoke the materialization command with the same Analysis Run, plan ID, revision, mode, and destination. Source-preserving execution revalidates source, ownership, staging structure and bytes, and Safe Publication capability. In-place execution re-probes the persisted executor and reconciles its reported recovery state without switching executor versions.

For a finalized Plan, read [approval.md](approval.md) before proceeding. Resuming a
conversation does not itself invalidate Materialization Approval. Reuse prior
approval only when persisted state and the approval record prove that it covers the
same Plan, active Layout Revision, mode, and exact resolved destination. When that
binding cannot be established, obtain Gate 2 Materialization Approval because the
authorization is unproven; recovery does not create another approval gate.
