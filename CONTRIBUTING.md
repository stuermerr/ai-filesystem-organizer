# Contributing

Thanks for helping improve AI Filesystem Organizer.

Before a substantial change, open a GitHub issue so the behavior and safety
boundary can be agreed first. Keep pull requests focused and include tests for
observable behavior changes.

The public project lives at
<https://github.com/stuermerr/ai-filesystem-organizer>.

Set up the locked development environment and run the complete quality gate:

```console
uv sync --locked
uv run ruff check filesystem_organizer tests scripts
uv run mypy filesystem_organizer tests
uv run pytest
```

Use the domain vocabulary in [`CONTEXT.md`](CONTEXT.md). Changes to source
safety, evidence, approval, recovery, or materialization semantics should also
update the relevant tests and architecture documentation.
