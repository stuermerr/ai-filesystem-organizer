# AI Filesystem Organizer

**Organize years of messy backups on your drives with Claude, Codex, or another coding agent — while deterministic CLI commands keep the actual filesystem work controlled, reviewable, and safe.**

You can either:

- 🛡️ **create a new organized copy** and leave your original drive untouched, or
- ⚠️ **reorganize a drive in place** when a second full copy is not practical — for example when the drive is almost full.

The agent helps you understand your files and design the structure.  
The CLI decides what is safe to execute.

👉 See [Safety](#️-safety-why-this-is-safer-than-just-telling-an-agent-to-clean-up-your-drive) for the protections built into the workflow.

---

## 🧭 Workflow

Point your AI coding agent at the **folder, backup drive, or collection you want to reorganize**.

Then:

```text
💾 Your messy drive
       ↓
🔎 Analyze
       ↓
🗂️ Propose a cleaner structure
       ↓
👀 You review it
       ↓
✅ You approve it
       ↓
✨ Organized result
```

- 🧠 **your agent helps you make sense of the mess** using metadata, structural evidence, and selected file contents when useful
- 🔎 **the CLI checks filesystem facts**, finds exact duplicates by file contents, and identifies overlapping backups, conflicts, unreadable files, and links
- 🗂️ **your agent proposes a cleaner structure** that can combine compatible folders and bring scattered photo and media collections together
- ⚔️ **different files and uncertainty stay visible** instead of one version being silently discarded
- 👀 **you review the result before anything happens**
- ✅ **you approve the final plan**
- 🛡️ **your original files stay untouched by default**
- ⚠️ **or, if you explicitly choose it, the same drive can be reorganized in place**
- ♻️ **interrupted work can resume** from persisted run state

Your agent proposes the structure. The deterministic CLI validates and executes it.

> [!IMPORTANT]
> This project is designed for **stable collections** such as old drives and historical backups.
>
> Do not add, edit, or delete files in the source while a run is in progress.

---

# 😵 Before → ✨ After

Imagine you finally connect the old drives containing the photos, documents, phone backups, camera dumps, and folders you have been carefully avoiding for the last eight years.

The folder names made perfect sense when you created them.

Probably.

<table>
<thead>
<tr>
<th width="50%">Before</th>
<th width="50%">After</th>
</tr>
</thead>
<tbody>
<tr>
<td valign="top">
<pre>My old drives/
│
├── LAPTOP OLD - DO NOT DELETE/
│   ├── Desktop/
│   │   ├── Tax 2023 FINAL.pdf
│   │   ├── CV final FINAL 2.docx
│   │   └── New Folder (12)/
│   │       └── passport scan.pdf
│   │
│   ├── Pictures/
│   │   ├── Camera/
│   │   │   ├── IMG_1042.JPG
│   │   │   ├── IMG_1043.JPG
│   │   │   └── IMG_1044.JPG
│   │   ├── Summer Holiday/
│   │   │   ├── 2021-07-14 beach.jpg
│   │   │   └── 2021-07-15 beach.jpg
│   │   └── phone backup old/
│   │       ├── IMG_20190504_182244.jpg
│   │       └── IMG_20190504_182245.jpg
│   │
│   └── Recipes/
│       └── banana-bread.txt
│
├── USB BACKUP NEWEST I THINK/
│   ├── important/
│   │   ├── Tax 2023 FINAL copy.pdf
│   │   └── passport scan copy.pdf
│   │
│   ├── DCIM copy FINAL/
│   │   ├── IMG_1042 copy.JPG
│   │   ├── IMG_1043.JPG
│   │   ├── 2021-07-14 beach copy.jpg
│   │   └── 2022-12-24 family.jpg
│   │
│   ├── WhatsApp Images/
│   │   ├── IMG-20200612-WA0012.jpg
│   │   └── IMG-20200612-WA0013.jpg
│   │
│   └── best newest recipes/
│       ├── banana-bread.txt
│       └── pasta.txt
│
├── PHONE BACKUP DO NOT FORMAT/
│   ├── DCIM/
│   │   ├── Camera/
│   │   │   ├── IMG_1042.JPG
│   │   │   └── IMG_2201.JPG
│   │   └── Screenshots/
│   │       ├── Screenshot_2020-04-02.png
│   │       └── Screenshot_2020-04-03.png
│   │
│   └── Videos/
│       ├── VID_20191231_235959.mp4
│       └── VID_20210715_183011.mp4
│
└── Random Stuff - SORT LATER (2019)/
    ├── CV current.docx
    ├── Family Photos/
    │   ├── 2020-08-03 mountains.jpg
    │   ├── grandma-birthday.jpg
    │   └── Christmas/
    │       └── IMG_20171224_183412.JPG
    ├── Old iPhone photos maybe/
    │   ├── IMG_0001.JPG
    │   ├── IMG_0002.JPG
    │   └── IMG_0003.JPG
    └── random/
        └── notes.txt</pre>
<p>Your collection may contain:</p>
<ul>
<li>the same photos copied across several drives;</li>
<li>camera dumps mixed with phone backups;</li>
<li>screenshots mixed with family photos;</li>
<li>duplicate documents with slightly different filenames;</li>
<li>folders copied from one computer to another;</li>
<li>several generations of the same personal files;</li>
<li>useful files buried inside temporary-looking directories;</li>
<li>and the traditional <code>SORT LATER</code> folder that has now achieved historical significance.</li>
</ul>
</td>
<td valign="top">
<pre>Organized/
│
├── Photos/
│   ├── 2017/
│   │   └── Christmas/
│   │       └── IMG_20171224_183412.JPG
│   │
│   ├── 2019/
│   │   ├── Family/
│   │   │   └── grandma-birthday.jpg
│   │   └── Phone/
│   │       ├── IMG_20190504_182244.jpg
│   │       └── IMG_20190504_182245.jpg
│   │
│   ├── 2020/
│   │   ├── Family/
│   │   │   └── 2020-08-03 mountains.jpg
│   │   ├── WhatsApp/
│   │   │   ├── IMG-20200612-WA0012.jpg
│   │   │   └── IMG-20200612-WA0013.jpg
│   │   └── Screenshots/
│   │       ├── Screenshot_2020-04-02.png
│   │       └── Screenshot_2020-04-03.png
│   │
│   ├── 2021/
│   │   ├── Camera/
│   │   │   ├── IMG_1042.JPG
│   │   │   ├── IMG_1043.JPG
│   │   │   └── IMG_1044.JPG
│   │   └── Holiday/
│   │       ├── 2021-07-14 beach.jpg
│   │       └── 2021-07-15 beach.jpg
│   │
│   └── 2022/
│       ├── Phone/
│       │   └── IMG_2201.JPG
│       └── 2022-12-24 family.jpg
│
├── Videos/
│   ├── 2019/
│   │   └── VID_20191231_235959.mp4
│   └── 2021/
│       └── VID_20210715_183011.mp4
│
├── Documents/
│   ├── Finance/
│   │   └── Taxes/
│   │       └── Tax 2023 FINAL.pdf
│   │
│   └── Personal/
│       ├── CV/
│       │   ├── CV current.docx
│       │   └── CV final FINAL 2.docx
│       └── Identity/
│           └── passport scan.pdf
│
├── Recipes/
│   ├── banana-bread.txt
│   └── pasta.txt
│
└── To Review/
    ├── IMG_0001.JPG
    ├── IMG_0002.JPG
    ├── IMG_0003.JPG
    └── notes.txt</pre>
<p>What happened?</p>
<ul>
<li>🖼️ Your photos from different drives are brought into one clearer structure.</li>
<li>🔎 The copied camera photos are collapsed <strong>only if their contents are actually identical</strong>.</li>
<li>📸 The same applies to copied holiday photos, screenshots, and other media.</li>
<li>📁 Compatible folders can be combined instead of keeping every old backup tree.</li>
<li>🧠 Your agent proposes useful categories from metadata and structural evidence, supplemented by representative file contents when that materially improves the recommendation.</li>
<li>⚔️ Files with the same name but different contents are kept instead of one being silently discarded.</li>
<li>👀 Anything uncertain or conflicting can be kept somewhere explicit such as <code>To Review/</code>.</li>
<li>🛡️ In the default mode, your original source drive remains unchanged.</li>
</ul>
</td>
</tr>
</tbody>
</table>

> [!NOTE]
> This is an example of a final output structure that began with an agent proposal, was customized through user instructions, and was ultimately approved by the user.
>
> You do not have to design the structure from scratch. Your agent starts with paths, filenames, metadata, reports, and directory structure, and may selectively inspect or efficiently batch-extract representative file contents when that would improve the recommendation. It stops when further evidence is unlikely to change the structure. The deterministic engine validates and executes the approved plan; content evidence never bypasses Plan validation.

---

# 🚀 Usage

The fastest way to get started is to paste one of these prompts into Claude Code, Codex, or another agent that can read repository-local .agents/skills/.

## Try the included example

> **"Download and setup [https://github.com/stuermerr/ai-filesystem-organizer.git.](https://github.com/stuermerr/ai-filesystem-organizer.git) Then use the repo's agent skills to organize the example_source/ dir. Keep the source unchanged."**

Your agent should take it from there.

## Use it on your own collection - Keeping your original source unchanged

> **"Download and setup [https://github.com/stuermerr/ai-filesystem-organizer.git.](https://github.com/stuermerr/ai-filesystem-organizer.git) Then use the repo's agent skills to organize my `<path/to/your/drive/>`. Keep the source unchanged."**

## Reorganize the same drive

If your drive does not have enough free space for a second full copy on it, you can explicitly ask for in-place organization:

> **"Download and setup [https://github.com/stuermerr/ai-filesystem-organizer.git.](https://github.com/stuermerr/ai-filesystem-organizer.git) Then use the repo's agent skills to organize my `<path/to/your/drive/>` in-place."**

In-place mode has additional safety checks and requires explicit destructive approval.

Prefer to set up the project yourself or work without an agent? Manual installation and direct CLI instructions are available further below.

---

# 🧭 What happens during a run?

### 🔎 1. Analyze your collection

The CLI inventories the folder or drive you selected and records filesystem evidence.

It can identify things such as:

- exact duplicate files;
- overlapping directory trees;
- structural conflicts;
- unreadable files;
- symbolic links;
- special filesystem entries.

### 🧠 2. Design your new structure

Your agent reviews that evidence with you and proposes a cleaner result. It starts with metadata and structural evidence, then may selectively inspect representative file contents when that is expected to improve grouping, naming, or structural consolidation. You can opt out for the whole workflow by saying not to inspect file contents.

You can say things like:

> **"Put my photos under** `Photos/`**, grouped by year."**

> **"Keep these two versions of my CV."**

> **"Move anything uncertain into** `To Review/`**."**

> **"Keep my videos separate from my photos."**

> **"I don't want that old cache directory in the final result."**

Your agent turns those decisions into exact layout rules.

### 👀 3. Review and iterate on your plan

You see the proposed structure before it is finalized.

Nothing is materialized yet.

### ✅ 4. Approve your final plan

When you are happy with the final plan, approve it. The final structure is then frozen into an exact plan.

### 🛡️ 5. Run the final safety checks

The CLI checks that your source and destination still match the plan you approved.

If something important changed, it stops.

### ✨ 6. Create the result

After your final approval, the CLI creates your organized result.

In source-preserving mode, this means creating a new destination for the results, leaving your messy source folder untouched.

In in-place mode, this means reorganizing your selected messy source directly.

---

# 🛡️ Safety: why this is safer than just telling an agent to clean up your drive

One of the main goals of this project is to let you use an AI agent **without giving it unlimited freedom to improvise with your files**.

Your agent can help you reason about the mess.

The deterministic CLI controls what can actually happen.

## Safety measures

- 🛡️ **Your source stays untouched by default.**  
The normal workflow creates a new organized result somewhere else.
- 👀 **You review the proposed structure first.**  
Your agent does not immediately start moving files because you asked for a cleanup.
- ✅ **You explicitly approve the final plan.**  
The structure that will be created is frozen before execution.
- ✅ **You approve the actual filesystem operation separately.**  
Planning and execution are separate decisions.
- 🔎 **Duplicates are proven by file contents.**  
Two similar filenames are not enough to decide that one can disappear.
- ⚔️ **Different files are preserved.**  
If two files look related but their contents differ, the tool does not silently choose one and throw the other away.
- ⚠️ **Uncertainty is shown to you.**  
Unreadable files, symbolic links, special entries, and conflicts become visible findings instead of silent omissions.
- 🧭 **The source is checked again before execution.**  
If your files changed after the analysis, the CLI can refuse to continue.
- 🚫 **Existing destination data is not intentionally overwritten.**
- 🧱 **Your agent cannot bypass the CLI's validation rules.**  
Natural-language instructions do not directly become filesystem operations.
- ♻️ **Interrupted work has persisted recovery state.**  
The tool does not rely only on the agent remembering what happened.
- ⚠️ **In-place mode requires extra approval.**  
Your agent cannot silently switch from creating a safe separate copy to reorganizing your original drive.
- 🔒 **In-place execution is only admitted when the filesystem supports an approved execution method.**  
Unsupported environments are refused.

Disclaimer: This is still not a forensic data-recovery system and cannot protect against every possible hardware, controller, or filesystem failure.

The goal is simple:

> **Give you the flexibility of an AI agent without letting the AI freely improvise destructive filesystem operations.**

That is much safer than giving an agent a broad command such as:

> **"Clean up this drive."**

---

# 💾 Source-preserving vs. in-place

| | 🛡️ **Source-preserving** | **In-place** |
| --- | --- | --- |
| Your original collection | Left unchanged | Reorganized directly |
| Result | New destination | Same selected root |
| Extra free space needed | Usually enough for a separate result | Much less additional space may be needed |
| Default | ✅ Yes | ❌ No |
| Best fit | You want the extra precaution of retaining the source | Storage or copy time makes a separate result impractical |
| Extra destructive approval | No | ✅ Yes |

### 🛡️ Source-preserving — recommended

The default workflow creates your organized result in a separate destination and leaves the original collection unchanged.

It requires additional storage for the result and can take longer because data may need to be copied. Choose it when you have the necessary space and prefer the extra precaution of keeping the source untouched.

### In-place — space-efficient

In-place mode reorganizes the selected folder or drive directly, avoiding the need for a second complete copy.

It is a practical option when:

- your drive is almost full;
- you do not have another drive large enough for the complete result;
- copying hundreds of gigabytes again would be impractical;
- you are working with a stable historical backup that you explicitly want to reorganize.

Because this mode changes the original collection, the CLI:

- requires you to request in-place mode explicitly;
- checks whether your filesystem supports an approved execution method;
- repeats checks before execution;
- requires explicit destructive acknowledgement;
- refuses unsupported environments.

In-place mode is intentionally opt-in. Review the final plan carefully and use source-preserving mode instead whenever you want the additional precaution of retaining the original collection unchanged.

The underlying executor is an implementation detail. You do not select a “fast mode” or executor version yourself.

---

# 🛠️ Setup and requirements

You can let your coding agent handle this setup, or install the project yourself.

## Requirements

- Linux (currently the only verified supported platform)
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)

## Manual installation

```console
git clone https://github.com/stuermerr/ai-filesystem-organizer.git
cd ai-filesystem-organizer
uv sync --locked
```

## How the agent integration works

An agent can analyze the CLI's evidence, propose a useful structure, and refine it from your instructions. You still review and approve the final plan before anything is materialized.

The repository includes project skills for:

- **Claude Code:** [`.claude/skills/`](.claude/skills/)
- **Codex and other agents that can read repository-local Agent Skills:** [`.agents/skills/`](.agents/skills/)

There are two skills:

- `filesystem-organizer` — agent workflow instructions handling scanning, evidence, safety checks, recovery, and materialization.
- `consolidation-plan-dialogue` — helps you design and review your planned folder structure.

When you work with an agent, you do **not** need to invoke the skills manually or understand the underlying CLI workflow. Just tell the agent what you want to do.

---

# ⌨️ Use the CLI directly

The coding-agent workflow is recommended, but you can also use the underlying CLI directly.

The command remains:

```text
filesystem-organizer
```

## Minimal source-preserving workflow

```console
# 1. Analyze your collection.
uv run filesystem-organizer scan /path/to/stable-backups \
  --output-root /path/to/run-output

# Save the returned `analysis_run` as RUN_PATH.

# 2. Review your analysis and create a plan.
uv run filesystem-organizer report RUN_PATH
uv run filesystem-organizer plan RUN_PATH

# Save the returned `plan_id` as PLAN_ID.

# 3. Review your proposed structure.
uv run filesystem-organizer plan-report RUN_PATH \
  --plan-id PLAN_ID \
  --section structure \
  --depth 4

# 4. Materialize the exact approved revision.
uv run filesystem-organizer materialize \
  RUN_PATH PLAN_ID \
  --revision REVISION
```

By default, your final result is created below:

```text
RUN_OUTPUT_ROOT/materialized/RUN_ID
```

Use `--destination` if you want an exact destination.

For large runs, `--workspace-root` can keep internal Analysis Run data on another filesystem without changing your final destination. This is recommended when reorganizing older drives that use a filesystem such as VFAT. The Analysis Run's SQLite database can grow beyond that filesystem's maximum file-size limit when the drive contains many files, which would cause the run to fail.

---

## 🗂️ Customize your layout manually

Your agent uses these same public commands behind the scenes.

Advanced users can also work directly with the layout JSON:

```console
uv run filesystem-organizer plan-layout-export \
  RUN_PATH PLAN_ID \
  --output layout.json

uv run filesystem-organizer plan-layout-validate \
  RUN_PATH PLAN_ID layout.json

uv run filesystem-organizer plan-layout-apply \
  RUN_PATH PLAN_ID layout.json

uv run filesystem-organizer plan-report \
  RUN_PATH \
  --plan-id PLAN_ID \
  --section structure \
  --depth 4
```

Layout rules can:

- move files or subtrees;
- rename paths;
- combine compatible structure;
- explicitly exclude entries;
- create intentional empty directories.

They cannot:

- modify file contents;
- convert file formats;
- unpack archives;
- use fuzzy executable selectors;
- silently delete unmatched data.

---

## ⚠️ Direct in-place execution

```console
uv run filesystem-organizer scan /path/to/stable-backup --in-place

uv run filesystem-organizer plan-finalize \
  RUN_PATH \
  --plan-id PLAN_ID

uv run filesystem-organizer materialize-preflight \
  RUN_PATH PLAN_ID \
  --in-place

uv run filesystem-organizer materialize \
  RUN_PATH PLAN_ID \
  --in-place \
  --acknowledge-destructive
```

Interrupted execution uses persisted evidence to continue forward where possible.

The tool does not offer a general rollback mechanism.

---

# 🧰 CLI reference

Run:

```console
uv run filesystem-organizer COMMAND --help
```

for the full arguments.

| Command | What it does |
| --- | --- |
| `scan` | Analyze your stable collection and create an Analysis Run |
| `resume` | Continue an interrupted scan |
| `runs` | Find your existing Analysis Runs |
| `status` | Show run, plan, and recovery status |
| `report` | Review your analysis findings |
| `plan` | Create a draft organization plan |
| `plan-report` | Review your proposed result |
| `plan-override` | Select another proven-identical source copy |
| `plan-layout-export` | Export your current layout |
| `plan-layout-validate` | Check a layout without applying it |
| `plan-layout-apply` | Save a validated layout revision |
| `plan-finalize` | Freeze the active plan |
| `materialize-preflight` | Check readiness without materializing |
| `materialize` | Create your approved result |
| `cleanup-completed` | Compact eligible completed run state |

Progress is written to `stderr`.

Structured command results are written to `stdout`.

---

# 🚧 Current scope

### Supported

- ✅ Linux
- ✅ Python 3.12+
- ✅ stable / historical filesystem collections
- ✅ old drives and backup collections
- ✅ large photo and media collections
- ✅ overlapping backups
- ✅ exact duplicate detection
- ✅ custom folder layouts
- ✅ source-preserving organization
- ✅ explicitly approved in-place organization
- ✅ interrupted-run recovery
- ✅ Claude Code
- ✅ Codex
- ✅ all other agents that can work with repository-local .agents/skills/.

### Not currently the goal

- ❌ live folder monitoring
- ❌ synchronization
- ❌ automatic Downloads-folder cleanup
- ❌ autonomous deletion
- ❌ indiscriminate whole-collection content inspection or mass classification
- ❌ autonomous file-by-file taxonomy assignment or arbitrary semantic relocation
- ❌ Windows support claims
- ❌ forensic data recovery

Your source must remain stable while a run is active.

---

# 🔐 Privacy

The project is local-first.

Filesystem analysis and materialization are performed by the local CLI.

Your coding agent may see filenames, paths, metadata, reports, or other information you expose through your local agent environment. Selective inspection or batch extraction of representative file contents is optional, adaptive evidence for improving semantic structural recommendations; it may not occur, and any inspected contents may enter the agent provider's model context. This inspection never establishes Exact Duplicate or content identity: those remain local CLI evidence. You may opt into metadata-only behavior for the current workflow by saying `don't inspect file contents`.

This informational disclosure must be shown before the first CLI command and before any file-content inspection or extraction; it is not a required privacy choice or a promise that inspection will occur. Temporary analysis artifacts may be created outside the selected source and Run Workspace, but extracted contents are not persisted as CLI evidence or product/run state.

Review the privacy behavior and permissions of the agent you use.

Do not include sensitive personal paths, filenames, credentials, or filesystem metadata in public bug reports.

See [SECURITY.md](SECURITY.md) for security reporting.

---

# 🧪 Development

Install the locked development environment:

```console
uv sync --locked
```

Run the quality checks:

```console
uv run ruff check filesystem_organizer tests scripts
uv run mypy filesystem_organizer tests
uv run pytest
```

Use:

```console
uv add PACKAGE
```

for application dependencies, and:

```console
uv add --dev PACKAGE
```

for development dependencies.

The checked-in end-to-end fixture is:

- [`example_source/`](example_source/) — example overlapping backup input
- [`tests/test_milestone_gate.py`](tests/test_milestone_gate.py) — end-to-end lifecycle and recovery coverage

See [CONTRIBUTING.md](CONTRIBUTING.md) before making substantial changes.

---

# 📚 Documentation

- 🧠 [Product boundaries and requirements](CONCEPT.md)
- 🏗️ [Architecture](ARCHITECTURE.md)
- 📖 [Domain vocabulary](CONTEXT.md)
- 🤖 [Agent Skills for Codex and compatible agents](.agents/skills/)
- 🤖 [Claude Code skills](.claude/skills/)
- 🔐 [Security policy](SECURITY.md)
- 🛠️ [Contribution guide](CONTRIBUTING.md)
- 💬 [GitHub Issues](https://github.com/stuermerr/ai-filesystem-organizer/issues)

The directories under [`docs/adr/`](docs/adr/) and [`docs/benchmarks/`](docs/benchmarks/) preserve engineering decisions and benchmark history.

Older executor versions described there are historical records, not modes you need to choose.

---

# 📄 License

AI Filesystem Organizer is maintained by [@stuermerr](https://github.com/stuermerr) and contributors.

Released under the [MIT License](LICENSE).
