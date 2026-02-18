---
name: personal-continual-memory
description: Persistent cross-repo Codex memory for workflows, gotchas, facts, and helpers under /mnt/data/code with central git-backed storage and local repo cache sync.
---

# Personal Continual Memory

Use this skill whenever work should compound across sessions and repositories.
This file is the operational source of truth for memory workflow and CLI usage.

## Global policy anchor

- Global behavior is defined in `~/.codex/AGENTS.md`.
- Optional global overrides can be placed in `~/.codex/AGENTS.override.md`.
- Do not rely on `/mnt/data/code/AGENTS.md` for global behavior.

## Canonical storage

- Central git-backed store: `/mnt/data/code/.codex-memory`
- Local repo cache: `<repo>/.codex-local/memory-context.md`
- Local retry queue: `<repo>/.codex-local/outbox.jsonl`
- Git worktree rule: memory identity is canonicalized to the primary repo root (via git common-dir), so worktree names do not create separate folders under `/mnt/data/code/.codex-memory/repos`.

## Scope semantics

- `scope=repo`: memory that is only relevant when working inside that repository.
  Examples: repo-specific commands, local build/test workflow, repo-only gotchas.
- `scope=global`: memory that can be relevant across repositories.
  Examples: how repos/services connect, interdependencies/integration constraints,
  and end-user product behavior of the combined application.

Default classification rule:

- Use `scope=repo` unless cross-repo reuse is genuinely expected.

## Command surface

Use `scripts/mem.py`.

- `mem.py add --repo <name> --repo-path <path> --kind <fact|workflow|gotcha|helper> --scope <repo|global> --title <text> --body-file <path> --tags <csv> --source-session <id>`
- `mem.py context --repo <name> --repo-path <path> --max-items <n>`
- `mem.py context --include-cross-repo --relevant-repos <repo-a,repo-b,...> --cross-repo-max-per-repo 4 --global-max 4 --relevant-repo-cap 5`
- `mem.py sync-local --repo-path <path>`
- `mem.py sync-local --include-cross-repo --relevant-repos <repo-a,repo-b,...> --cross-repo-max-per-repo 4 --global-max 4 --relevant-repo-cap 5`
- `mem.py ingest-session --session-file <jsonl>`
- `mem.py ingest-new --since-watermark`
- `mem.py compact --repo <name> | --all`

## Retrieval policy

- Default mode is repo-local retrieval only.
- Cross-repo retrieval is opt-in via `--include-cross-repo` and requires explicit `--relevant-repos`.
- Only selected relevant repos are searched for cross-repo entries.
- Caps per selected repo:
  - `cross_repo_max_per_repo=4` for `scope=repo` entries.
  - `global_max=4` for `scope=global` entries.
- Max selected relevant repos per retrieval: `5`.
- There is no separate total cross-repo candidate pool cap; final output is still bounded by `--max-items`.

## Required workflow

1. Start of task: run `mem.py sync-local --repo-path <repo>` and read `<repo>/.codex-local/memory-context.md`.
2. During task: add high-value curated entries with `mem.py add`.
3. End of task: run `mem.py compact --repo <name>` when entries changed.
4. Periodically: run `mem.py ingest-new --since-watermark` to backfill from session logs.

## Guardrails

- Default to `scope=repo`; use `scope=global` only when cross-repo reuse is expected.
- Never store secrets, credentials, tokens, customer private data, or raw logs.
- Prefer stable patterns and workflows over verbose journals.
- Prioritize decision-making insights over trivia:
  - Save memories that influence future implementation choices, debugging direction, risk handling, or architecture decisions.
  - Do not save obvious one-liner commands a new agent can infer from `--help`, README, or standard tooling defaults.
- For helper memories specifically, require non-trivial repo specificity:
  - Keep only commands tied to repo files/paths/options and explain why they are non-obvious and reusable.
- Use one concise idea per entry title.
- If a memory is obsolete, remove it instead of superseding it.
- Every memory must include a concrete rationale for why it was saved and likely to be reused.
- Required format for all kinds (`fact`, `workflow`, `gotcha`, `helper`): first line must start with `Why this memory was saved: <specific rationale>`.

## Commit policy

- Auto-commits are allowed only in `/mnt/data/code/.codex-memory` on branch `memory/main`.
- Do not auto-commit product repositories for memory writes.
