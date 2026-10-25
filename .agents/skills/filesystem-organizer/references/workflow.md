# Analysis and planning workflow

## Privacy gate

For every scan-only or reorganization flow, first read `privacy.md` and show its privacy/model-context disclosure. State that metadata and structural evidence may enter model context; selective agent-side content inspection is optional, adaptive evidence for improving semantic structural recommendations, may also enter model context, and may not occur; Exact Duplicate and content identity remain local CLI evidence and are never established by that inspection; and the user may opt into metadata-only behavior for the current workflow. That choice is sticky for the workflow. This is an informational disclosure, not a required privacy choice or a promise to inspect content. Completing it is a hard precondition for `source-probe`, `scan-admission`, `scan`, and every other CLI command. This ordering also applies while mode is still ambiguous.

## Choose the branch

- **Scan-only:** inventory, analyze, or report evidence without reorganizing.
- **Reorganization:** consolidate or create an organized result from a stable filesystem collection.

State that the Selected Backup Root must remain quiescent for the complete workflow. Resolve the path roles defined in the top-level skill internally, then disclose only the user-facing fields required below. These are operating facts, not approval questions.

## Mandatory reorganization state sequence

For every reorganization, complete these states in order. Do not skip, merge, or reorder them:

1. **Complete the privacy gate:** read `privacy.md` and render the required disclosure before any CLI command.
2. **Probe Source and mount/access context:** run `source-probe` and establish the resolved source, accessibility, effective mount, and whether the source is the mount root.
3. **Resolve Mode and Final result:** use explicit intent, the normal sibling rule, or the mount-root choices below.
4. **Render the path block:** show the user-facing `Source`, `Mode`, and `Final result` fields before any admission or scan command.
5. **Resolve the mode choice:** pause only when the mode remains ambiguous. Explicit in-place intent skips only this question; it never skips state 4.
6. **Run scan-admission:** use the selected mode and exact resolved path roles.
7. **Run scan and report:** record the Analysis Run and inspect every Skipped Entry Finding.
8. **Create deterministic Plan Revision 0:** run `plan` and `plan-report`.
9. **Render Current Persisted Structure:** label the Revision 0 tree exactly **Current Persisted Structure**.
10. **Use the layout skill:** explicitly use `consolidation-plan-dialogue` and execute its **Optimization-proposal branch**.
11. **Gather deeper evidence:** follow its metadata-first evidence hierarchy and selectively add content evidence when useful.
12. **Render Recommended Result Structure:** show a distinct proposal labeled exactly **Recommended Result Structure**.
13. **Resolve and apply the recommendation:** obtain only unresolved semantic choices, then validate and apply the coherent layout edit.
14. **Render Current Persisted Revision N:** inspect the applied revision and label it exactly **Current Persisted Revision N**.
15. **Gate 1 — Final Plan Approval:** only now ask exactly **Approve this final Plan?**
16. **Validate, finalize, and preflight:** complete the automatic post-Gate-1 preparation.
17. **Gate 2 — Materialization Approval:** separately ask exactly **Proceed with materialization?**
18. **Materialize:** execute the approved current Plan.

Revision 0 is the deterministic baseline, never the **Recommended Result Structure**. Keep **Current Persisted Structure**, **Recommended Result Structure**, and **Current Persisted Revision N** distinct. Gate 1 is unavailable until the `consolidation-plan-dialogue` **Optimization-proposal branch** has completed and its recommendation has been resolved and applied. Gate 1 and Gate 2 are separate questions; never combine them or ask wording such as “Approve the final plan and materialize it?”

Hard sequencing rule, including for small models: state 1 is a blocking precondition for states 2, 6, and 7, and state 4 is a blocking precondition for states 6 and 7. Render the privacy disclosure before `source-probe`, then render the selected `Source`, `Mode`, and `Final result` block in a user-visible response before invoking either `scan-admission` or `scan`. Explicit in-place intent advances directly from state 4 to state 6 and skips only the state 5 mode-choice question.

Meaningful structural optimization is evidence-driven. Start with persisted paths, directory names, filenames, timestamps and other metadata, provenance patterns, available CLI-provided Duplicate or content-identity evidence, and other CLI evidence. When those signals are insufficient or content inspection would materially improve the recommendation, the agent may selectively inspect relevant contents under the `consolidation-plan-dialogue` evidence hierarchy. This optional evidence informs semantic structural recommendations; it neither establishes Exact Duplicate or content identity nor replaces CLI Plan validation or authorizes autonomous file-by-file taxonomy moves.

The privacy disclosure must occur before the first CLI command and before any file-content inspection or extraction, whichever would happen first. A user's metadata-only choice is sticky for the current workflow: do not ask again or inspect contents during later recommendation or revision cycles; use only paths, names, timestamps, provenance, hashes or duplicate evidence, and CLI evidence.

## Resolve mode and paths

Before destination derivation, run:

```console
uv run filesystem-organizer source-probe SELECTED_BACKUP_ROOT
```

Use its resolved path, access, effective mount root, filesystem, read-only state, and device evidence. If the source root is not readable or traversable by the coding-agent environment, stop before a long scan and explain the inaccessible source precisely. Preserve explicit intent:

- An explicit in-place request selects in-place mode. Do not offer the mode choice again.
- An explicit Run Output Root selects source-preserving mode unless the user also explicitly requested in-place mode. Pass the supplied root literally as `--output-root` to admission and scan.
- An explicit source-preserving request selects source-preserving mode.

When no Run Output Root is supplied and `source-probe` reports that the Selected Backup Root is not the effective mount root, derive one internally as an absolute sibling of the Selected Backup Root; never use the CLI fallback `./output`, `/tmp`, or an agent working directory. Replace a final `_source` or `-source` suffix with `_results` or `-results`; otherwise append `-result` to the source directory name. Thus `/home/user/workspace` derives `/home/user/workspace-result`, `REPOSITORY_ROOT/example_source` derives `REPOSITORY_ROOT/example_results`, and `/media/user/USB/backups` derives `/media/user/USB/backups-result`. The derived root must be outside the Selected Backup Root and its parent must remain on the source's effective filesystem according to probe evidence. Use the derived root for in-place Analysis Run state too, while keeping the in-place Final result equal to the Selected Backup Root.

A derived sibling is a proposal until checked. If it already exists, cannot be safely formed, or an explicit operating-system probe or `scan-admission` reports it as unwritable or otherwise unsuitable, stop. Explain the concrete issue, suggest a suitable unused sibling path, and let the user resolve the location; never overwrite the path or silently select another one.

When neither mode nor output location was specified, present source-preserving as the recommended default and pause for the mode choice before admission or scan. A recommendation is not a selection: do not select source-preserving on the user's behalf. Render both complete blocks before the question. Show only these user-facing path fields; do not display the internal Run Output Root:

```text
Source-preserving (recommended)

Source:        /absolute/source
Mode:          source-preserving
Final result:  /absolute/source-result/materialized/<generated RUN ID>
```

```text
In-place

Source:        /absolute/source
Mode:          in-place
Final result:  /absolute/source
```

Only after both blocks are visible, ask exactly: **Or do you want to reorganize the source in place instead?** Stop there. Do not run `scan-admission` or `scan` until the user explicitly selects one of the two modes.

If the user explicitly selected a mode or supplied an output location, do not ask this choice. Before `scan-admission` and `scan`, show the selected mode using only `Source`, `Mode`, and `Final result`. Explicit in-place intent skips only the mode-choice question; it does not skip this pre-scan path display. For an explicit in-place request, show:

```text
Source:        /absolute/source
Mode:          in-place
Final result:  /absolute/source
```

This exact user-visible block is a hard pre-scan checkpoint. Only after rendering it may the workflow invoke `scan-admission` or `scan`.

For source-preserving mode with an Exact Materialized Destination, show that exact destination; otherwise show `RUN_OUTPUT_ROOT/materialized/<generated RUN ID>`. After scan generates the run ID, use the actual resolved destination in later summaries.

## Mount-root destination choices

When `source-probe` reports that the Selected Backup Root equals its effective mount root, do not derive `MOUNT_NAME-result` beside the mount point. Present all three choices below, including the path block for each choice and its current availability from the current `source-probe` and destination evidence. Mark a blocked choice as **currently unavailable** and give its exact evidence-based reason; do not omit unavailable choices.

1. **Prepare this drive for a source-preserving run on the same drive.** Show its availability and path facts:

   ```text
   Source:        /media/user/MY_USB/original-source-dir
   Mode:          source-preserving
   Final result:  /media/user/MY_USB/result-dir/materialized/<generated RUN ID>
   ```

   Then show the exact mount-root preview:

   ```text
   Current drive:
   /media/user/MY_USB/

   After preparation:
   /media/user/MY_USB/
   ├── original-source-dir/
   └── result-dir/
   ```

   Explain that the preparation normally uses fast same-filesystem renames to move every existing top-level entry into `original-source-dir/`. Run `prepare-mount-root MOUNT_ROOT` without `--yes` to obtain the exact move list. Show that list and ask for explicit confirmation before any move. Only after confirmation run `prepare-mount-root MOUNT_ROOT --yes`; never perform preparation silently. The command must establish that the path is the effective mount root and writable, that both reserved directories are absent, that every moved entry belongs to the selected filesystem, and that the root stayed quiescent. On any failure, stop rather than improvising moves. After success use `result-dir` as the Run Output Root. Admit a separate Run Workspace Root if the drive cannot support SQLite WAL or workspace capacity.

2. **Organize the mounted drive in place.** Show its availability and path facts:

   ```text
   Source:        /media/user/MY_USB
   Mode:          in-place
   Final result:  /media/user/MY_USB
   ```

   Continue through the existing in-place admission and safety workflow.

3. **Put a source-preserving result on another filesystem.** Show its availability and path facts. Offer a concrete Run Output Root only when actual environment evidence supports its filesystem, writability, and capacity, then confirm it through `scan-admission`:

   ```text
   Source:        /media/user/MY_USB
   Mode:          source-preserving
   Final result:  <other-filesystem-result>/materialized/<generated RUN ID>
   ```

   Never hard-code `/home/user` or infer another-filesystem suitability from its spelling. When no suitable destination is yet established, say so and ask only for the narrow destination path needed to evaluate this option.

For example, when the current probe shows that the coding agent can read the mounted drive but cannot write to it, keep Options 1 and 2 visible and label each `currently unavailable: agent currently has read-only access`. Option 3 may remain available when a separate writable destination has been established.

## Source and permission findings

Interpret `source-probe` and `scan-admission` narrowly:

- Treat the current `source-probe` fields as authoritative and report them consistently; do not independently paraphrase or infer writability. `mount_read_only=true` must never be described as writable. `source_writable=false` establishes that the agent cannot currently write to the source, but by itself does not prove that the physical drive or host mount is inherently read-only.
- A readable, traversable source that the agent cannot write can still use source-preserving mode with a writable admitted destination and workspace. Report `Access: readable, read-only`, state that in-place is currently unavailable, and also mark same-drive preparation unavailable where applicable.
- When the agent can read but cannot write and independent mount evidence does not establish a read-only host mount, explain: “I can read this drive, but this coding-agent environment currently cannot write to it. This may be a sandbox/permission restriction rather than a problem with the drive itself. If your coding-agent UI supports filesystem or external-drive permissions, grant this agent write access to the drive and I can probe it again.” Request only access to the selected source or destination path, never generic “full filesystem access.”
- After the user changes coding-agent filesystem or external-drive permissions, rerun `source-probe` and the relevant `scan-admission` or preflight checks. Use the fresh facts; do not reuse stale access findings or require a remount when the renewed probe establishes writability.
- Describe the host mount as read-only or recommend a remount only when explicit operating-system or mount evidence independently establishes that condition. Lack of agent write capability alone is not evidence that the drive is mounted incorrectly or must be remounted read-write.
- A source that the coding-agent environment cannot read or traverse blocks both modes before scan. Name the missing source permission; do not request generic “full filesystem access.”
- A writable source does not prove HardLinkProtection or RenameProtection. In-place capability exists only after `scan-admission --in-place` succeeds.
- When no capability probe has run, report the filesystem feature as **not established**, never supported. When read-only access prevents the in-place probe from creating its temporary entry, report the filesystem primitives as **not established due to read-only access**, not unsupported.
- A destination or Run Workspace refusal is independent of source readability. Report which path and narrow capability failed.

## Authorized preparation

An explicit materialization request to consolidate the Selected Backup Root using Run Output Root wording such as `output dir OUTPUT`, or to use a clearly named Exact Materialized Destination, authorizes all non-mutating preparation: scan, analysis, Plan creation, reporting and inspection, validation, lightweight preflight, and capability checks. Run these steps without separate approval. The request does not authorize materialization or in-place source changes.

For scan-only work, run `scan` and `report`, summarize the evidence and every Skipped Entry Finding, then stop. A scan-only request does not authorize Plan creation.

For reorganization:

1. Before `scan`, run `scan-admission SELECTED_BACKUP_ROOT --output-root RUN_OUTPUT_ROOT`. When a separate Run Workspace Root exists, pass `--workspace-root RUN_WORKSPACE_ROOT` to both admission and scan. For source-preserving execution, pass `--destination EXACT_DESTINATION` to both commands only when that exact path was requested; otherwise omit it. For in-place execution, pass `--in-place` to both commands and never pass `--destination`. This payload-free phase separately probes workspace filesystem type, per-file capacity, free space, directory creation, SQLite WAL, and the capability required by the selected mode. Stop immediately on a refusal. It does not replace later metadata admission or Plan-specific preflight.
2. Run `scan` with the same mode and path arguments used for admission, then record the exact returned Analysis Run path, run ID, snapshot ID, execution mode, executor kind, and executor version. The scan reuses its single metadata inventory for Metadata Admission and Run Workspace capacity admission before hashing or structural content analysis. Workspace admission computes the exact projected structural-evidence row count with the analyzer's ancestor semantics, then applies a documented schema/path-aware SQLite size and DB/WAL peak estimate with safety margin. Treat filesystem limits as facts and projected byte sizes as estimates. A refusal reports the workspace filesystem, maximum single-file size, projected rows, estimated requirement, free space, and separate-workspace remediation. The persisted execution mode is binding: a later change between source-preserving and in-place requires a new Analysis Run.
3. Run `report` and inspect every Skipped Entry Finding. Carry skipped findings and their effect into the Plan summary; do not pause merely to obtain permission to plan.
4. Run `plan`, record its exact plan ID and active Layout Revision, then run `plan-report`.
5. Summarize the destination, operation counts, Structural Unions, conflict projections, explicit directories, Skipped Entry Findings, exclusions, deterministic Canonical Copy policy, and rough approximate of storage reduction percentage, if applicable. Canonical Copy selection retains one occurrence of proven identical bytes: newest modification timestamp wins, with deterministic relative-path fallback for a tie or unavailable timestamp.
6. Render the user-facing persisted structure with `plan-report --section structure --depth 4`. Direct destination directories are level 1; omit a synthetic root label and summarize file counts instead of enumerating files.
7. Always enter the `consolidation-plan-dialogue` optimization-proposal branch for a reorganization. Gather the deeper structural evidence it defines and visibly render its concrete **Recommended Result Structure** in the first planning response; do not merely offer to produce it later. Keep that recommendation distinct from the persisted user-facing tree. Then ask only for any unresolved semantic decision or confirmation to apply the recommendation, and enter the customization loop below.

Keep every command bound to the explicit Analysis Run and plan ID.

## Customization loop

Use the existing `consolidation-plan-dialogue` skill for iterative Custom Layout work. It owns evidence gathering, recommendations, export, validation, apply, and persisted-result inspection.

The user's requested semantic choice authorizes the corresponding coherent layout edit. Apply requested changes, validate them, inspect the persisted result, fix obvious residual placements that clearly follow the stated intent, and rerun reports or validation without approval after each operation. Ask for a semantic decision when the correct result would change destination, exclusions, conflict handling, material layout, or mode and is not already determined by the user's stated intent. Such a decision constructs the Plan; it is not an additional approval gate.

CLI acknowledgement flags for a requested User Exclusion or Content-Empty Result carry the already-stated semantic intent. Surface those outcomes prominently in the final summary. Do not turn the flags into separate conversational approval gates.

Continue until the persisted result is stable and every unresolved finding either blocks safely or requires a genuine semantic decision.

## Gate 1 — Final Plan Approval

Show one complete final Plan summary containing:

- the major result structure, rendered as the current persisted readable tree, and relevant placement changes;
- file count and total bytes;
- every material exclusion, including a Content-Empty Result;
- conflicts and their projected handling; and
- Skipped Entry Findings and their result impact.

Immediately before composing this same Gate 1 response, rerun or inspect `plan-report ANALYSIS_RUN --plan-id PLAN_ID --section structure --depth 4`. Include that current persisted tree directly in the response that asks the Gate 1 question; a tree shown in an earlier response or the earlier **Recommended Result Structure** is not sufficient. Identify the current Plan ID and active Layout Revision where useful.

Ask exactly: **Approve this final Plan?**

Only an unambiguous approval response directed at the currently displayed Plan, plan ID, and active Layout Revision satisfies Gate 1. Accept clear equivalents such as `approve`, `approved`, or `yes, approve it`. Praise or conversational acknowledgement such as `nice`, `looks good`, `great`, or `thanks` is not approval by itself; ask **Approve this final Plan?** again instead of inferring approval.

A semantic change invalidates Final Plan Approval. Any user-requested change to layout, exclusions, a Skipped Entry action, destination, conflicts, or other material Plan facts must return to the customization loop. If the Plan was already finalized, create a new draft Plan rather than mutating it. Apply and validate the change, render the new **Current Persisted Revision N**, then render a new complete Gate 1 summary with its current persisted tree and ask **Approve this final Plan?** again. Gate 2 must never follow a Plan-changing request without this fresh Gate 1 approval.

After approval, automatically validate the current Plan, finalize it where required, verify the finalized Plan report, and run the current mode-specific `materialize-preflight`. Pass the same `--destination EXACT_DESTINATION` when one exists; otherwise omit it so the Plan's normal derived destination remains authoritative. Also run the CLI's lightweight capability, storage, recovery, and admission checks. Retry safe read-only checks when useful. A hard blocker ends the workflow before Gate 2.

The required order is: Gate 1 approval → successful validation/finalization → verified finalized Plan → successful `materialize-preflight` → Gate 2 summary → **Proceed with materialization?** → materialization. Gate 2 does not exist before successful post-Gate-1 preparation. An answer to a premature Gate 2 question is invalid and cannot be reused after preparation succeeds; render the current Gate 2 summary and obtain a new approval.

## Gate 2 — Materialization Approval

After successful preparation, show one concise materialization summary containing:

- exact destination and mode;
- files and bytes;
- exclusions and important warnings;
- for source-preserving mode, whether native clone or streamed copy may be used; and
- for in-place mode, the exact Selected Backup Root, persisted executor kind and version, destructive scope, and reported recovery model.

For in-place mode, identify it as destructive and state what will change. Treat the executor version as a diagnostic compatibility fact, not as general product terminology.

Ask exactly: **Proceed with materialization?**

Only direct approval of the displayed current Plan, active Layout Revision, mode, and exact resolved destination satisfies Gate 2. A semantic change to the Plan, exclusions, destination, conflict handling, or mode invalidates the approval and returns to the applicable planning step.

After approval, execute immediately through the command described in `approval.md`. Do not introduce another approval for validation, finalization, repeated preflight, fallback selection, recovery, or publication.
