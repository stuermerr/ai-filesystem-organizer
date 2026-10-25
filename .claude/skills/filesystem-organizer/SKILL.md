---
name: filesystem-organizer
description: Orchestrate safe consolidation and structural reorganization of an explicitly selected, stable filesystem collection through this repository's deterministic CLI. Use for scan-only analysis, consolidation, or recovery when the Selected Backup Root remains quiescent. Exclude continuous organization of active folders, generic Downloads cleanup, indiscriminate whole-collection content classification, and autonomous file management.
---

# AI Filesystem Organizer

Treat the CLI as the sole execution and safety boundary. Invoke its public commands and explain their artifacts; keep the Selected Backup Root unchanged except during explicitly approved mount-root preparation or in-place materialization, and leave Run Workspace SQLite state to the CLI.

## Start

1. **Privacy gate:** read [privacy.md](references/privacy.md) and make its informational disclosure before the first CLI command—including `source-probe` while mode is unresolved—and before any file-content inspection or extraction, whichever would happen first. This is a hard precondition for `source-probe`, `scan-admission`, and `scan`, but it is not a required privacy choice or a promise that content inspection will occur. A metadata-only choice is sticky for the current workflow; do not ask again or inspect contents in later recommendation or revision cycles.
2. Classify the request as scan-only, reorganization, recovery, or out of scope.
3. For scan-only or reorganization, read [workflow.md](references/workflow.md) and follow the matching branch.
4. For recovery of existing work, read [recovery.md](references/recovery.md) before selecting any Analysis Run.
5. For preparation after Final Plan Approval or any materialization, read [approval.md](references/approval.md) before acting.

Require the Selected Backup Root to remain quiescent throughout the workflow. Refuse active-folder monitoring, generic cleanup, indiscriminate whole-collection content inspection or mass classification, autonomous file-by-file taxonomy assignment, arbitrary per-file semantic relocation, deletion, pruning, overwrite, unapproved mount-root preparation, and unapproved in-place source changes. Selective content evidence for coherent structural recommendations remains in scope under [workflow.md](references/workflow.md). Explain that historical backups are the primary use case, while other stable collections use the same safety model.

Treat repository issue tracking as a product-development convention only. Ordinary scans, Plans, Layout Revisions, and materialization do not create or update repository issues unless the user explicitly asks for issue tracking.

## Path roles

- **Selected Backup Root:** the user-chosen, quiescent source collection for one Analysis Run.
- **Run Output Root:** the CLI-managed directory controlling the normal result namespace. Ordinary wording such as `output dir OUTPUT` names this role. It must remain outside the Selected Backup Root.
- **Run Workspace Root:** optional CLI-owned internal-state storage. It defaults to the Run Output Root; when supplied separately with `--workspace-root WORKSPACE`, the Analysis Run lives at `WORKSPACE/runs/RUN_ID`.
- **Exact Materialized Destination:** an optional exact final result path, used only when the user clearly requests that path and represented by `--destination` throughout the workflow.

Without an Exact Materialized Destination, the normal destination is `OUTPUT/materialized/RUN_ID` beneath the Run Output Root. Never use the same normalized path for the Run Output Root and Exact Materialized Destination.

Run Workspace Root never changes destination derivation. Record the exact `analysis_run` returned by `scan`; discovery and recovery use the actual Run Workspace Root when it differs from the Run Output Root.

Treat every user-supplied absolute filesystem path literally and independently: never join, rebase, relativize, or move one beneath another. Resolve and normalize the actual supplied paths before claiming that one contains another. If the user supplies `output dir OUTPUT`, use that exact path as the Run Output Root; do not substitute a temporary Run Output Root or reinterpret it as an Exact Materialized Destination.

For example, `materialize /src with output dir /out` means:

- Selected Backup Root: `/src`
- Run Output Root: `/out`
- Run Workspace Root: `/out` by default, or an independently supplied path
- Exact Materialized Destination: none
- Normal derived destination: `/out/materialized/RUN_ID`

It never becomes `/src/out`, and `/out` is not passed as `--destination` merely because it was called “output dir”.

## Filesystem evidence invariant

Never infer filesystem type, mount topology, same-device or different-device status, writability, or publication capability from path spelling. Report a fact only when the CLI supplies it or an explicit operating-system probe establishes it; otherwise report it as unknown.

## Command boundary

- Run commands from the repository with `uv run filesystem-organizer ...`. Inspect every scan-only or reorganization source first with `source-probe`.
- If the locked environment is unavailable, run `uv sync --locked`, then retry.
- Use CLI JSON and Markdown output as evidence. Never reproduce hashing, relationship, Canonical Copy, planning, Structural Union, or materialization logic.
- Never edit `analysis.sqlite3` or other Run Workspace artifacts directly.
- Treat an explicit reorganization or materialization request as authorization for non-mutating preparation. Scan, analyze, create and inspect the Plan, validate, and preflight without separate permission. Mount-root preparation remains a source mutation and requires its own explicit confirmation. Carry every Skipped Entry Finding into the final Plan summary.
- Treat the deterministic Canonical Copy policy as part of plan construction, not as a user approval gate. State the policy and summarize any existing overrides; inspect or change an individual choice with draft `plan-override` only when the user raises a specific concern.
- Follow the lifecycle and exactly two mandatory approval gates in [workflow.md](references/workflow.md). Semantic questions construct the Plan and are not additional approval gates; [approval.md](references/approval.md) owns execution after approval.
- Do not offer source, payload, or result cleanup, deletion, pruning, overwrite, or source changes after materialization. Automatic CLI-owned terminal Run Workspace cleanup is allowed: report its returned `workspace_cleanup` status and explain that a successful cleanup replaces terminal SQLite state with `completion.json` while preserving the result.
