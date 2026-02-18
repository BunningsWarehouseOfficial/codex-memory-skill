# Personal Continual Memory

Persistent memory for Codex across sessions and repositories.

This README is intentionally brief.  
Use `SKILL.md` as the operational source of truth for workflow, policy, storage layout, and command semantics.

## AGENTS.md Auto-Trigger Example

Use this as an example `~/.codex/AGENTS.md` so agents automatically run retrieval each task and only write memories when appropriate:

```markdown
# Global Codex Instructions

These rules apply across all repositories.

## Global AGENTS location

- The global instruction file is `~/.codex/AGENTS.md`.
- Optional override file is `~/.codex/AGENTS.override.md`.
- Do not rely on `/mnt/data/code/AGENTS.md` for global behavior.

## Continual memory system

Use the `personal-continual-memory` persistent memory skill for both retrieval and storage.
- Retrieval: at the start of each task, run the skill's required retrieval workflow (`mem.py sync-local --repo-path <repo>` and read `<repo>/.codex-local/memory-context.md`).
- Storage: only add new memories when you learn a useful lesson or encounter reusable high-value context from yourself or the user.

- Operational source of truth for the skill's commands/workflow: `~/.codex/skills/personal-continual-memory/SKILL.md`
- Storage layout, memory policy constraints, and commit policy for this system are defined in `SKILL.md`.
```

## Manual Quick Start

This is usually run by agents, but you can run it manually:

At the start of work in a repo:

```bash
python3 ~/.codex/skills/personal-continual-memory/scripts/mem.py sync-local --repo-path /path/to/repo
cat /path/to/repo/.codex-local/memory-context.md
```

Add a curated memory during work:

```bash
python3 ~/.codex/skills/personal-continual-memory/scripts/mem.py add \
  --repo-path /path/to/repo \
  --kind gotcha \
  --scope repo \
  --title "PDM env missing pip in worktree" \
  --body-file /tmp/memory.md \
  --tags pdm,venv,setup
```

At the end of work (if entries changed):

```bash
python3 ~/.codex/skills/personal-continual-memory/scripts/mem.py compact --repo <repo-slug>
```

## Notes

- Operational source of truth: `SKILL.md`
- CLI entrypoint: `scripts/mem.py` (`python3 ~/.codex/skills/personal-continual-memory/scripts/mem.py --help`)
- Global policy anchor: `~/.codex/AGENTS.md`
