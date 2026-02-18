#!/usr/bin/env python3
"""Persistent cross-repo memory manager for Codex."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KIND_CHOICES = ("fact", "workflow", "gotcha", "helper")
SCOPE_REPO = "repo"
SCOPE_GLOBAL = "global"
SCOPE_CHOICES = (SCOPE_REPO, SCOPE_GLOBAL)
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
ORIGIN_IN_TASK = "in_task"
ORIGIN_POST_SESSION = "post_session"
RATIONALE_PREFIX = "Why this memory was saved:"
MIN_RATIONALE_WORDS = 5
LOW_QUALITY_RATIONALE_FRAGMENTS = (
    "reusable command observed",
    "saved for later",
)

DEFAULT_MEMORY_HOME = Path(os.environ.get("CODEX_MEMORY_HOME", "/mnt/data/code/.codex-memory"))
DEFAULT_SESSION_ROOT = Path(os.environ.get("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
DEFAULT_REPO_ROOT = Path(os.environ.get("CODEX_REPO_ROOT", "/mnt/data/code"))


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def write_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True))
            handle.write("\n")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True))
        handle.write("\n")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "unknown"


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def compute_fingerprint(repo_name: str, kind: str, scope: str, title: str, body: str) -> str:
    payload = "\n".join(
        [
            normalize_text(repo_name),
            normalize_text(kind),
            normalize_text(scope),
            normalize_text(title),
            normalize_text(body),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_git(args: list[str], repo: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def git_stdout(args: list[str], repo: Path) -> str | None:
    proc = run_git(args, repo=repo, check=False)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def canonical_repo_root(repo_path: Path) -> Path:
    resolved = repo_path.resolve()

    toplevel_raw = git_stdout(["rev-parse", "--show-toplevel"], repo=resolved)
    worktree_root = Path(toplevel_raw).resolve() if toplevel_raw else resolved

    common_dir_raw = git_stdout(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo=resolved)
    if common_dir_raw is None:
        common_dir_raw = git_stdout(["rev-parse", "--git-common-dir"], repo=resolved)

    if common_dir_raw is not None:
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = (worktree_root / common_dir).resolve()
        if common_dir.name == ".git" and common_dir.parent.exists():
            return common_dir.parent.resolve()

    return worktree_root


def canonical_repo_name(repo_path: Path) -> str:
    return slugify(canonical_repo_root(repo_path).name)


@dataclass
class MemoryPaths:
    root: Path
    index_entries: Path
    state_watermark: Path
    logs_ops: Path


class MemoryStore:
    def __init__(self, root: Path):
        self.root = root
        self.paths = MemoryPaths(
            root=root,
            index_entries=root / "index" / "entries.jsonl",
            state_watermark=root / "state" / "ingest_watermark.json",
            logs_ops=root / "logs" / "memory-ops.log",
        )
        self.lock_path = root / ".memory.lock"

    def ensure_layout(self) -> None:
        (self.root / "index").mkdir(parents=True, exist_ok=True)
        (self.root / "repos").mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)

        if not self.paths.index_entries.exists():
            self.paths.index_entries.write_text("", encoding="utf-8")
        if not self.paths.state_watermark.exists():
            self.paths.state_watermark.write_text(
                json.dumps({"last_ingested_mtime": 0.0}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        if not self.paths.logs_ops.exists():
            self.paths.logs_ops.write_text("", encoding="utf-8")

        self._ensure_git_repo()

    def _ensure_git_repo(self) -> None:
        git_dir = self.root / ".git"
        if not git_dir.exists():
            run_git(["init"], repo=self.root)
        current_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo=self.root, check=False)
        if current_branch.returncode != 0:
            run_git(["checkout", "-b", "memory/main"], repo=self.root)
            return

        branch_name = current_branch.stdout.strip()
        if branch_name != "memory/main":
            chk = run_git(["show-ref", "--verify", "refs/heads/memory/main"], repo=self.root, check=False)
            if chk.returncode == 0:
                run_git(["checkout", "memory/main"], repo=self.root)
            else:
                run_git(["checkout", "-b", "memory/main"], repo=self.root)

    def append_log(self, message: str) -> None:
        ts = now_utc()
        with self.paths.logs_ops.open("a", encoding="utf-8") as handle:
            handle.write(f"[{ts}] {message}\n")

    def commit_if_needed(self, message: str) -> bool:
        run_git(["add", "-A"], repo=self.root)
        diff = run_git(["diff", "--cached", "--quiet"], repo=self.root, check=False)
        if diff.returncode == 0:
            return False

        run_git(
            [
                "-c",
                "user.name=Codex Memory",
                "-c",
                "user.email=codex-memory@local",
                "commit",
                "-m",
                message,
            ],
            repo=self.root,
        )
        return True

    def all_entries(self) -> list[dict[str, Any]]:
        return read_entries(self.paths.index_entries)

    def write_all_entries(self, entries: list[dict[str, Any]]) -> None:
        write_entries(self.paths.index_entries, entries)

    @contextlib.contextmanager
    def global_lock(self, timeout_seconds: float = 30.0):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            start = time.monotonic()
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - start > timeout_seconds:
                        raise TimeoutError(
                            f"timed out waiting for memory lock: {self.lock_path}"
                        )
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_repo_name(repo_name: str | None, repo_path: Path | None) -> str:
    if repo_path:
        return canonical_repo_name(repo_path)
    if repo_name:
        return slugify(repo_name)
    raise ValueError("repo name is required when repo path is not provided")


def is_in_allowed_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(DEFAULT_REPO_ROOT.resolve())
        return True
    except ValueError:
        return False


def read_body(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def parse_tags(tags_raw: str | None) -> list[str]:
    if not tags_raw:
        return []
    out = []
    for tag in tags_raw.split(","):
        t = tag.strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def normalize_scope(scope: str | None) -> str:
    value = (scope or SCOPE_REPO).strip().lower()
    if value not in SCOPE_CHOICES:
        return SCOPE_REPO
    return value


def parse_relevant_repos(raw: str | None, current_repo: str, cap: int) -> list[str]:
    if not raw:
        return []
    repos: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        slug = slugify(item)
        if not slug or slug == current_repo or slug in seen:
            continue
        seen.add(slug)
        repos.append(slug)
    return repos[: max(0, cap)]


def default_rationale(kind: str, scope: str, repo_name: str, title: str) -> str:
    if kind == "workflow":
        base = "This workflow captures a repeated execution path that improves speed and consistency."
    elif kind == "gotcha":
        base = "This captures a recurring pitfall that can cause regressions if forgotten."
    elif kind == "fact":
        base = "This fact constrains implementation choices and prevents incorrect assumptions."
    else:
        base = (
            "This helper command/pattern reduces repeat lookup/debug time and should be reused in similar tasks."
        )

    if normalize_scope(scope) == SCOPE_GLOBAL:
        base += " It can affect decisions across multiple repositories in the product."
    else:
        base += f" It is primarily relevant when working in `{repo_name}`."
    return f"{base} (Memory: {title})"


def extract_rationale_text(body: str) -> str | None:
    match = re.search(r"(?im)^why this memory was saved\s*:\s*(.+?)\s*$", body.strip())
    if not match:
        return None
    return " ".join(match.group(1).split())


def rationale_is_meaningful(rationale: str | None) -> bool:
    if not rationale:
        return False
    normalized = " ".join(rationale.split()).strip()
    if len(normalized) < 24:
        return False
    if len(normalized.split()) < MIN_RATIONALE_WORDS:
        return False
    lowered = normalized.lower()
    if any(fragment in lowered for fragment in LOW_QUALITY_RATIONALE_FRAGMENTS):
        return False
    return True


def ensure_memory_rationale(body: str, kind: str, scope: str, repo_name: str, title: str) -> str:
    trimmed = body.strip()
    generated_rationale = default_rationale(
        kind=kind,
        scope=scope,
        repo_name=repo_name,
        title=title,
    )

    if not trimmed:
        return f"{RATIONALE_PREFIX} {generated_rationale}"

    existing_rationale = extract_rationale_text(trimmed)
    if rationale_is_meaningful(existing_rationale):
        return trimmed

    replacement_rationale = generated_rationale
    if existing_rationale:
        replacement_rationale = f"{existing_rationale} {generated_rationale}"

    if re.search(r"(?im)^why this memory was saved\s*:", trimmed):
        without_existing = re.sub(
            r"(?im)^why this memory was saved\s*:\s*.*(?:\n|$)",
            "",
            trimmed,
            count=1,
        ).strip()
        if not without_existing:
            return f"{RATIONALE_PREFIX} {replacement_rationale}"
        return f"{RATIONALE_PREFIX} {replacement_rationale}\n\n{without_existing}"

    return f"{RATIONALE_PREFIX} {replacement_rationale}\n\n{trimmed}"


def build_entry(
    *,
    repo_name: str,
    repo_path: Path,
    kind: str,
    scope: str,
    title: str,
    body: str,
    tags: list[str],
    session_id: str,
    origin: str,
    confidence: float,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_scope = normalize_scope(scope)
    normalized_body = ensure_memory_rationale(
        body=body,
        kind=kind,
        scope=normalized_scope,
        repo_name=repo_name,
        title=title.strip(),
    )
    return {
        "id": str(uuid.uuid4()),
        "timestamp_utc": now_utc(),
        "repo_name": repo_name,
        "repo_path": str(repo_path.resolve()),
        "session_id": session_id,
        "kind": kind,
        "scope": normalized_scope,
        "title": title.strip(),
        "body_markdown": normalized_body,
        "tags": tags,
        "confidence": round(float(confidence), 3),
        "evidence": evidence,
        "fingerprint_sha256": compute_fingerprint(
            repo_name,
            kind,
            normalized_scope,
            title,
            normalized_body,
        ),
        "status": STATUS_ACTIVE,
        "origin": origin,
    }


def add_entry_to_store(
    store: MemoryStore,
    entry: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    entries = store.all_entries()

    for existing in entries:
        if (
            existing.get("status") == STATUS_ACTIVE
            and existing.get("repo_name") == entry["repo_name"]
            and existing.get("kind") == entry["kind"]
            and normalize_scope(existing.get("scope")) == normalize_scope(entry.get("scope"))
            and existing.get("fingerprint_sha256") == entry["fingerprint_sha256"]
        ):
            store.append_log(
                "dedupe "
                f"repo={entry['repo_name']} kind={entry['kind']} scope={entry.get('scope')} title={entry['title']}"
            )
            return "deduped", existing

    normalized_title = normalize_text(entry["title"])
    filtered_entries: list[dict[str, Any]] = []
    deleted_count = 0
    for existing in entries:
        should_delete_obsolete = (
            existing.get("status") == STATUS_ACTIVE
            and existing.get("repo_name") == entry["repo_name"]
            and existing.get("kind") == entry["kind"]
            and normalize_scope(existing.get("scope")) == normalize_scope(entry.get("scope"))
            and normalize_text(str(existing.get("title", ""))) == normalized_title
            and existing.get("fingerprint_sha256") != entry["fingerprint_sha256"]
        )
        if should_delete_obsolete:
            deleted_count += 1
            continue
        filtered_entries.append(existing)

    filtered_entries.append(entry)
    store.write_all_entries(filtered_entries)
    store.append_log(
        "add "
        f"repo={entry['repo_name']} kind={entry['kind']} scope={entry.get('scope')} title={entry['title']}"
    )
    if deleted_count:
        store.append_log(
            "delete_obsolete "
            f"repo={entry['repo_name']} kind={entry['kind']} scope={entry.get('scope')} "
            f"title={entry['title']} deleted={deleted_count}"
        )
    return "added", entry


def render_repo_documents(store: MemoryStore, repo_name: str) -> None:
    repo_dir = store.root / "repos" / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)

    entries = [
        row
        for row in store.all_entries()
        if row.get("repo_name") == repo_name and row.get("status") == STATUS_ACTIVE
    ]

    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in KIND_CHOICES}
    for row in entries:
        kind = row.get("kind")
        if kind in by_kind:
            by_kind[kind].append(row)

    for kind in by_kind:
        by_kind[kind].sort(key=lambda r: (r.get("timestamp_utc", ""), r.get("title", "")), reverse=True)

    profile = [
        f"# {repo_name} memory profile",
        "",
        f"Updated: {now_utc()}",
        f"Active entries: {len(entries)}",
        "",
        "Counts by kind:",
    ]
    for kind in KIND_CHOICES:
        profile.append(f"- {kind}: {len(by_kind[kind])}")
    profile.append("")
    profile.append("Top tags:")

    tag_counts: dict[str, int] = {}
    for row in entries:
        for tag in row.get("tags", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    for tag, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:25]:
        profile.append(f"- {tag}: {count}")

    if not tag_counts:
        profile.append("- none")
    profile.append("")
    profile.append("Counts by scope:")
    profile.append(f"- repo: {sum(1 for row in entries if normalize_scope(row.get('scope')) == SCOPE_REPO)}")
    profile.append(f"- global: {sum(1 for row in entries if normalize_scope(row.get('scope')) == SCOPE_GLOBAL)}")

    (repo_dir / "profile.md").write_text("\n".join(profile).strip() + "\n", encoding="utf-8")

    mapping = {
        "facts.md": "fact",
        "workflows.md": "workflow",
        "gotchas.md": "gotcha",
        "helpers.md": "helper",
    }
    for filename, kind in mapping.items():
        lines = [f"# {repo_name} {kind}s", ""]
        rows = by_kind[kind]
        if not rows:
            lines.append("No entries yet.")
        else:
            for row in rows:
                lines.append(f"## {row['title']}")
                lines.append("")
                lines.append(f"- id: {row['id']}")
                lines.append(f"- ts: {row['timestamp_utc']}")
                lines.append(f"- kind: {row.get('kind', kind)}")
                lines.append(f"- scope: {normalize_scope(row.get('scope'))}")
                lines.append(f"- origin: {row.get('origin', '')}")
                lines.append(f"- session_id: {row.get('session_id', '')}")
                lines.append(f"- confidence: {row.get('confidence', 0.0)}")
                lines.append(f"- evidence_count: {len(row.get('evidence', []))}")
                if row.get("tags"):
                    lines.append(f"- tags: {', '.join(row['tags'])}")
                else:
                    lines.append("- tags: none")
                lines.append("")
                lines.append(row.get("body_markdown", "").strip())
                lines.append("")
        (repo_dir / filename).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def context_score(row: dict[str, Any], repo_name: str) -> float:
    score = 0.0
    if row.get("repo_name") == repo_name:
        score += 5.0
    kind = row.get("kind", "")
    if kind == "workflow":
        score += 1.2
    if kind == "gotcha":
        score += 1.0
    if kind == "helper":
        score += 0.8
    score += float(row.get("confidence", 0.0)) * 2.0

    try:
        ts = datetime.fromisoformat(str(row.get("timestamp_utc", "")).replace("Z", "+00:00"))
        days = max((datetime.now(timezone.utc) - ts).days, 0)
        score += max(0.0, 2.0 - min(days / 30.0, 2.0))
    except ValueError:
        pass

    return score


def build_context_markdown(
    entries: list[dict[str, Any]],
    *,
    repo_name: str,
    max_items: int,
    include_cross_repo: bool = False,
    relevant_repos_raw: str | None = None,
    cross_repo_max_per_repo: int = 4,
    global_max: int = 4,
    relevant_repo_cap: int = 5,
) -> str:
    active = [e for e in entries if e.get("status") == STATUS_ACTIVE]
    local_rows = [row for row in active if row.get("repo_name") == repo_name]

    selected_repos: list[str] = []
    cross_rows: list[dict[str, Any]] = []
    selected_stats: dict[str, dict[str, int]] = {}

    if include_cross_repo:
        selected_repos = parse_relevant_repos(relevant_repos_raw, repo_name, relevant_repo_cap)
        for selected_repo in selected_repos:
            rows_for_repo = [row for row in active if row.get("repo_name") == selected_repo]
            repo_scope_rows = [
                row for row in rows_for_repo if normalize_scope(row.get("scope")) == SCOPE_REPO
            ]
            global_scope_rows = [
                row for row in rows_for_repo if normalize_scope(row.get("scope")) == SCOPE_GLOBAL
            ]

            repo_scope_ranked = sorted(
                repo_scope_rows,
                key=lambda row: context_score(row, repo_name),
                reverse=True,
            )
            global_scope_ranked = sorted(
                global_scope_rows,
                key=lambda row: context_score(row, repo_name),
                reverse=True,
            )

            repo_take = repo_scope_ranked[: max(0, cross_repo_max_per_repo)]
            global_take = global_scope_ranked[: max(0, global_max)]
            cross_rows.extend(repo_take)
            cross_rows.extend(global_take)
            selected_stats[selected_repo] = {
                "repo_scope": len(repo_take),
                "global_scope": len(global_take),
                "total": len(repo_take) + len(global_take),
            }

    candidates_by_id: dict[str, dict[str, Any]] = {}
    for row in [*local_rows, *cross_rows]:
        row = dict(row)
        row["scope"] = normalize_scope(row.get("scope"))
        row_id = str(row.get("id"))
        candidates_by_id[row_id] = row

    ranked = sorted(candidates_by_id.values(), key=lambda row: context_score(row, repo_name), reverse=True)

    lines = [
        f"# Codex memory context for {repo_name}",
        "",
        f"Generated: {now_utc()}",
        f"Mode: {'cross-repo' if include_cross_repo else 'repo-local'}",
        f"Max items: {max_items}",
        "",
    ]
    lines.append("Retrieval policy:")
    lines.append(f"- current repo only by default: {not include_cross_repo}")
    if include_cross_repo:
        lines.append(f"- relevant repos selected ({len(selected_repos)}): {', '.join(selected_repos) or '(none)'}")
        lines.append(f"- cross_repo_max_per_repo: {cross_repo_max_per_repo}")
        lines.append(f"- global_max (per selected repo): {global_max}")
        lines.append(f"- relevant_repo_cap: {relevant_repo_cap}")
        lines.append("- per-repo inclusion counts:")
        for repo in selected_repos:
            stats = selected_stats.get(repo, {"repo_scope": 0, "global_scope": 0, "total": 0})
            lines.append(
                f"  {repo}: repo_scope={stats['repo_scope']} global_scope={stats['global_scope']} total={stats['total']}"
            )
    lines.append("")

    for row in ranked[: max(0, max_items)]:
        lines.append(f"## [{row.get('kind', 'unknown')}] {row.get('title', 'untitled')}")
        lines.append("")
        lines.append(
            "repo: "
            f"{row.get('repo_name')} | scope: {normalize_scope(row.get('scope'))} | confidence: {row.get('confidence', 0.0)}"
        )
        if row.get("tags"):
            lines.append(f"tags: {', '.join(row['tags'])}")
        lines.append("")
        lines.append(str(row.get("body_markdown", "")).strip())
        lines.append("")

    if len(lines) <= 8:
        lines.append("No active memory entries yet.")

    return "\n".join(lines).strip() + "\n"


def repo_local_dir(repo_path: Path) -> Path:
    return repo_path / ".codex-local"


def append_to_outbox(repo_path: Path, entry: dict[str, Any], reason: str) -> None:
    outbox = repo_local_dir(repo_path) / "outbox.jsonl"
    payload = dict(entry)
    payload["queued_reason"] = reason
    payload["queued_at"] = now_utc()
    append_jsonl(outbox, payload)


def parse_session_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def command_has_repo_specific_signal(command: str, repo_name: str) -> bool:
    lowered = command.lower()
    repo_alias = repo_name.replace("-", "_")
    if "/" in command:
        return True
    if re.search(r"\b[\w.-]+\.(py|ts|tsx|js|jsx|toml|yaml|yml|json|ini|cfg|sql|sh)\b", lowered):
        return True
    if any(
        flag in lowered
        for flag in (
            "--config",
            "--input",
            "--output",
            "--profile",
            "--dataset",
            "--schema",
            "--target",
            "--path",
            "--repo",
        )
    ):
        return True
    return repo_name in lowered or repo_alias in lowered


def command_importance_reason(command: str, repo_name: str) -> str | None:
    lowered = command.strip().lower()
    if not lowered:
        return None

    low_signal_prefixes = (
        "codex ",
        "git diff",
        "git rev-parse",
        "nl -ba",
        "which ",
        "cd /mnt/data/code/",
        "python - <<",
        "awk -f",
        "awk -f",
        "tr -d",
        "set -a",
        "/home/icetana/.codex/skills/.system/skill-installer/scripts/list-skills.py",
        "python3 /home/icetana/.codex/skills/.system/skill-installer/scripts/list-skills.py",
    )
    if any(lowered.startswith(prefix) for prefix in low_signal_prefixes):
        return None
    if "list-skills.py" in lowered:
        return None
    if re.search(r"(^|\s)(-h|--help)(\s|$)", lowered):
        return None
    if "--version" in lowered:
        return None

    # Auto-ingest should keep only decision-useful, repo-specific helpers.
    if not command_has_repo_specific_signal(command, repo_name):
        return None

    if any(token in lowered for token in ("pytest", "trunk check", "trunk fmt", "ruff", "mypy", "flake8")):
        return (
            "Targeted validation command tied to repo-specific files/options; "
            "it reduces regression risk when changing related code."
        )
    if any(token in lowered for token in ("python -m py_compile", "python -m compileall", "bash -n")):
        return (
            "Repo-targeted syntax/compile sanity-check command that catches breakage quickly "
            "before slower test runs."
        )
    if lowered.startswith("chmod +x") or lowered.startswith("mkdir -p"):
        return "Project bootstrap command used to prepare scripts/directories in repeatable setup workflows."
    if any(token in lowered for token in ("python ", "pdm ", "trunk ", "yarn ", "npm ", "pnpm ")):
        return (
            "Repo-specific command captured because it references local artifacts/flags "
            "that are non-obvious and likely to be reused."
        )
    return None


def extract_candidates_from_session(
    rows: list[dict[str, Any]],
    *,
    session_file: Path,
    max_candidates: int,
) -> tuple[str | None, Path | None, list[dict[str, Any]]]:
    session_id: str | None = None
    repo_path: Path | None = None

    commands: list[tuple[str, int]] = []
    shell_noise = {"ls", "pwd", "cat", "sed", "rg", "echo", "find", "head", "tail", "wc", "git status"}

    for idx, row in enumerate(rows):
        if row.get("type") == "session_meta":
            payload = row.get("payload", {})
            if isinstance(payload, dict):
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd")
                if cwd:
                    repo_path = Path(str(cwd)).resolve()

        if row.get("type") == "response_item":
            payload = row.get("payload", {})
            if payload.get("type") == "function_call":
                name = str(payload.get("name", ""))
                if name not in {"shell_command", "exec_command"}:
                    continue
                args_raw = payload.get("arguments")
                cmd = ""
                if isinstance(args_raw, str):
                    try:
                        args_obj = json.loads(args_raw)
                        cmd = str(args_obj.get("command") or args_obj.get("cmd") or "").strip()
                    except json.JSONDecodeError:
                        cmd = ""
                elif isinstance(args_raw, dict):
                    cmd = str(args_raw.get("command") or args_raw.get("cmd") or "").strip()

                if not cmd:
                    continue

                lowered = cmd.lower().strip()
                if any(lowered == n or lowered.startswith(n + " ") for n in shell_noise):
                    continue
                commands.append((cmd, idx + 1))

    if not repo_path or not is_in_allowed_repo(repo_path):
        return session_id, repo_path, []

    canonical_path = canonical_repo_root(repo_path)
    repo_name = slugify(canonical_path.name)
    unique_commands: list[tuple[str, int]] = []
    seen = set()
    for command, line_no in commands:
        key = normalize_text(command)
        if key in seen:
            continue
        seen.add(key)
        unique_commands.append((command, line_no))

    candidates: list[dict[str, Any]] = []
    for command, line_no in unique_commands[:max_candidates]:
        reason = command_importance_reason(command, repo_name=repo_name)
        if not reason:
            continue
        title_tokens = command.split()
        title = "Reusable command"
        if title_tokens:
            title = f"Command: {' '.join(title_tokens[:2])}"
        body = (
            f"Why this memory was saved: {reason}\n\n"
            f"This command was observed in session `{session_id or 'unknown'}` for `{repo_name}` "
            "and retained because it is likely to be reused.\n\n"
            f"```bash\n{command}\n```"
        )
        candidates.append(
            {
                "repo_name": repo_name,
                "repo_path": str(canonical_path),
                "kind": "helper",
                "title": title,
                "body_markdown": body,
                "tags": ["session-import", "command"],
                "scope": SCOPE_REPO,
                "session_id": session_id or "unknown",
                "origin": ORIGIN_POST_SESSION,
                "confidence": 0.55,
                "evidence": [
                    {
                        "session_file": str(session_file),
                        "line": line_no,
                        "type": "function_call",
                    }
                ],
            }
        )

    return session_id, repo_path, candidates


def add_from_payload(
    *,
    store: MemoryStore,
    payload: dict[str, Any],
    commit_message: str,
    queue_on_fail: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    repo_path = Path(str(payload["repo_path"]))
    entry = build_entry(
        repo_name=payload["repo_name"],
        repo_path=repo_path,
        kind=payload["kind"],
        scope=str(payload.get("scope", SCOPE_REPO)),
        title=payload["title"],
        body=payload["body_markdown"],
        tags=list(payload.get("tags", [])),
        session_id=str(payload.get("session_id", "unknown")),
        origin=str(payload.get("origin", ORIGIN_IN_TASK)),
        confidence=float(payload.get("confidence", 0.7)),
        evidence=list(payload.get("evidence", [])),
    )

    try:
        status, written = add_entry_to_store(store, entry)
        if status == "added":
            render_repo_documents(store, entry["repo_name"])
            store.commit_if_needed(commit_message)
        return status, written
    except Exception as exc:  # pylint: disable=broad-except
        store.append_log(f"error add payload: {exc}")
        if queue_on_fail:
            append_to_outbox(repo_path, payload, reason=str(exc))
        return "queued", None


def cmd_add(args: argparse.Namespace) -> int:
    raw_repo_path = Path(args.repo_path).resolve() if args.repo_path else Path.cwd().resolve()
    repo_path = canonical_repo_root(raw_repo_path)
    repo_name = ensure_repo_name(args.repo, raw_repo_path)

    if not is_in_allowed_repo(raw_repo_path) or not is_in_allowed_repo(repo_path):
        print(f"repo path must be under {DEFAULT_REPO_ROOT}: {repo_path}", file=sys.stderr)
        return 2

    if args.kind not in KIND_CHOICES:
        print(f"invalid kind: {args.kind}", file=sys.stderr)
        return 2

    body = read_body(Path(args.body_file))
    if not body:
        print("body file is empty", file=sys.stderr)
        return 2

    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    payload = {
        "repo_name": repo_name,
        "repo_path": str(repo_path),
        "kind": args.kind,
        "scope": normalize_scope(args.scope),
        "title": args.title,
        "body_markdown": body,
        "tags": parse_tags(args.tags),
        "session_id": args.source_session or "manual",
        "origin": ORIGIN_IN_TASK,
        "confidence": float(args.confidence),
        "evidence": [],
    }

    msg = f"mem({repo_name}): {args.kind} - {args.title} [session:{payload['session_id']}]"
    with store.global_lock():
        status, entry = add_from_payload(store=store, payload=payload, commit_message=msg)

    print(json.dumps({"status": status, "entry_id": entry.get("id") if entry else None}, sort_keys=True))
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo_path).resolve() if args.repo_path else None
    if repo_path is None and not args.repo:
        repo_path = Path.cwd().resolve()
    repo_name = ensure_repo_name(args.repo, repo_path)
    if args.include_cross_repo and not args.relevant_repos:
        print("--include-cross-repo requires --relevant-repos", file=sys.stderr)
        return 2

    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    text = build_context_markdown(
        store.all_entries(),
        repo_name=repo_name,
        max_items=args.max_items,
        include_cross_repo=args.include_cross_repo,
        relevant_repos_raw=args.relevant_repos,
        cross_repo_max_per_repo=args.cross_repo_max_per_repo,
        global_max=args.global_max,
        relevant_repo_cap=args.relevant_repo_cap,
    )
    print(text)
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    repos: list[str]
    if args.all:
        repos = sorted({row.get("repo_name", "") for row in store.all_entries() if row.get("repo_name")})
    else:
        repos = [slugify(args.repo)]

    with store.global_lock():
        for repo_name in repos:
            render_repo_documents(store, repo_name)

        target = "all" if args.all else repos[0]
        store.commit_if_needed(f"mem({target}): compact memory views [session:system]")
    print(json.dumps({"status": "ok", "repos": repos}, sort_keys=True))
    return 0


def cmd_migrate_scope(args: argparse.Namespace) -> int:
    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    with store.global_lock():
        entries = store.all_entries()
        missing_scope = 0
        rationales_updated = 0
        updated_fingerprint = 0

        for row in entries:
            had_scope = "scope" in row
            scope = normalize_scope(row.get("scope"))
            if not had_scope:
                row["scope"] = scope
                missing_scope += 1

            repo_name = str(row.get("repo_name", "unknown"))
            kind = str(row.get("kind", "fact"))
            title = str(row.get("title", "untitled"))
            body_markdown = str(row.get("body_markdown", ""))
            normalized_body = ensure_memory_rationale(
                body=body_markdown,
                kind=kind,
                scope=scope,
                repo_name=repo_name,
                title=title,
            )
            if normalized_body != body_markdown:
                row["body_markdown"] = normalized_body
                rationales_updated += 1

            expected_fp = compute_fingerprint(
                repo_name,
                kind,
                scope,
                title,
                str(row.get("body_markdown", "")),
            )
            if row.get("fingerprint_sha256") != expected_fp:
                row["fingerprint_sha256"] = expected_fp
                updated_fingerprint += 1

        if missing_scope or rationales_updated or updated_fingerprint:
            store.write_all_entries(entries)
            repo_names = sorted({str(r.get("repo_name", "")) for r in entries if r.get("repo_name")})
            for repo_name in repo_names:
                render_repo_documents(store, repo_name)
            store.commit_if_needed(
                "mem(system): migrate scope/rationale fields and fingerprints [session:system]"
            )

    print(
        json.dumps(
            {
                "status": "ok",
                "missing_scope_filled": missing_scope,
                "rationales_updated": rationales_updated,
                "fingerprints_updated": updated_fingerprint,
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_ingest_session(args: argparse.Namespace) -> int:
    session_file = Path(args.session_file).resolve()
    if not session_file.exists():
        print(f"missing session file: {session_file}", file=sys.stderr)
        return 2

    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    rows = parse_session_jsonl(session_file)
    session_id, repo_path, candidates = extract_candidates_from_session(
        rows,
        session_file=session_file,
        max_candidates=args.max_candidates,
    )

    if not candidates:
        print(json.dumps({"status": "ok", "ingested": 0, "session_id": session_id}, sort_keys=True))
        return 0

    ingested = 0
    with store.global_lock():
        for candidate in candidates:
            msg = (
                f"mem({candidate['repo_name']}): {candidate['kind']} - {candidate['title']} "
                f"[session:{candidate['session_id']}]"
            )
            status, _ = add_from_payload(
                store=store,
                payload=candidate,
                commit_message=msg,
                queue_on_fail=False,
            )
            if status == "added":
                ingested += 1

    print(
        json.dumps(
            {
                "status": "ok",
                "ingested": ingested,
                "candidates": len(candidates),
                "repo_path": str(repo_path) if repo_path else None,
                "session_id": session_id,
            },
            sort_keys=True,
        )
    )
    return 0


def discover_session_files(session_root: Path) -> list[Path]:
    if not session_root.exists():
        return []
    return sorted(session_root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def cmd_ingest_new(args: argparse.Namespace) -> int:
    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    watermark = safe_read_json(store.paths.state_watermark, {"last_ingested_mtime": 0.0})
    since = float(watermark.get("last_ingested_mtime", 0.0))

    files = discover_session_files(DEFAULT_SESSION_ROOT)
    candidates = [path for path in files if path.stat().st_mtime > since]

    ingested_total = 0
    processed = 0
    latest_mtime = since

    with store.global_lock():
        for session_file in candidates:
            processed += 1
            rows = parse_session_jsonl(session_file)
            _, _, extracted = extract_candidates_from_session(
                rows,
                session_file=session_file,
                max_candidates=args.max_candidates,
            )
            for candidate in extracted:
                msg = (
                    f"mem({candidate['repo_name']}): {candidate['kind']} - {candidate['title']} "
                    f"[session:{candidate['session_id']}]"
                )
                status, _ = add_from_payload(
                    store=store,
                    payload=candidate,
                    commit_message=msg,
                    queue_on_fail=False,
                )
                if status == "added":
                    ingested_total += 1

            latest_mtime = max(latest_mtime, session_file.stat().st_mtime)

        if processed > 0:
            store.paths.state_watermark.write_text(
                json.dumps({"last_ingested_mtime": latest_mtime}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            store.commit_if_needed("mem(system): update ingest watermark [session:system]")

    print(
        json.dumps(
            {
                "status": "ok",
                "processed_files": processed,
                "ingested_entries": ingested_total,
                "since": since,
                "latest_mtime": latest_mtime,
            },
            sort_keys=True,
        )
    )
    return 0


def flush_outbox(
    store: MemoryStore,
    local_repo_path: Path,
    canonical_repo_path: Path,
    canonical_repo_name: str,
) -> dict[str, int]:
    outbox = repo_local_dir(local_repo_path) / "outbox.jsonl"
    stats = {"flushed": 0, "kept": 0}

    if not outbox.exists():
        return stats

    rows = read_entries(outbox)
    keep: list[dict[str, Any]] = []
    for row in rows:
        payload = {
            # Route any queued worktree writes to the canonical shared repo memory namespace.
            "repo_name": canonical_repo_name,
            "repo_path": str(canonical_repo_path),
            "kind": row.get("kind", "helper"),
            "title": row.get("title", "queued memory"),
            "body_markdown": row.get("body_markdown", ""),
            "tags": row.get("tags", []),
            "scope": row.get("scope", SCOPE_REPO),
            "session_id": row.get("session_id", "unknown"),
            "origin": row.get("origin", ORIGIN_IN_TASK),
            "confidence": row.get("confidence", 0.7),
            "evidence": row.get("evidence", []),
        }
        msg = (
            f"mem({payload['repo_name']}): {payload['kind']} - {payload['title']} "
            f"[session:{payload['session_id']}]"
        )
        status, _ = add_from_payload(store=store, payload=payload, commit_message=msg, queue_on_fail=False)
        if status == "queued":
            keep.append(row)
            stats["kept"] += 1
        else:
            stats["flushed"] += 1

    write_entries(outbox, keep)
    return stats


def cmd_sync_local(args: argparse.Namespace) -> int:
    local_repo_path = Path(args.repo_path).resolve()
    if not local_repo_path.exists():
        print(f"missing repo path: {local_repo_path}", file=sys.stderr)
        return 2
    if not is_in_allowed_repo(local_repo_path):
        print(f"repo path must be under {DEFAULT_REPO_ROOT}: {local_repo_path}", file=sys.stderr)
        return 2
    if args.include_cross_repo and not args.relevant_repos:
        print("--include-cross-repo requires --relevant-repos", file=sys.stderr)
        return 2

    canonical_repo_path = canonical_repo_root(local_repo_path)
    if not is_in_allowed_repo(canonical_repo_path):
        print(f"repo path must be under {DEFAULT_REPO_ROOT}: {canonical_repo_path}", file=sys.stderr)
        return 2
    repo_name = slugify(canonical_repo_path.name)
    local_dir = repo_local_dir(local_repo_path)
    local_dir.mkdir(parents=True, exist_ok=True)
    outbox_path = local_dir / "outbox.jsonl"
    outbox_path.touch(exist_ok=True)

    store = MemoryStore(DEFAULT_MEMORY_HOME)
    store.ensure_layout()

    with store.global_lock():
        outbox_stats = flush_outbox(
            store,
            local_repo_path=local_repo_path,
            canonical_repo_path=canonical_repo_path,
            canonical_repo_name=repo_name,
        )

        context_text = build_context_markdown(
            store.all_entries(),
            repo_name=repo_name,
            max_items=args.max_items,
            include_cross_repo=args.include_cross_repo,
            relevant_repos_raw=args.relevant_repos,
            cross_repo_max_per_repo=args.cross_repo_max_per_repo,
            global_max=args.global_max,
            relevant_repo_cap=args.relevant_repo_cap,
        )
    context_path = local_dir / "memory-context.md"
    context_path.write_text(context_text, encoding="utf-8")

    with store.global_lock():
        store.commit_if_needed(f"mem({repo_name}): sync local cache [session:system]")
    print(
        json.dumps(
            {
                "status": "ok",
                "repo": repo_name,
                "context_path": str(context_path),
                "outbox": outbox_stats,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex continual memory manager")
    sub = parser.add_subparsers(dest="command", required=True)

    parser_context = sub.add_parser("context", help="Render context for a repo")
    parser_context.add_argument("--repo", required=False, help="Repository slug/name")
    parser_context.add_argument("--repo-path", required=False, help="Repository path")
    parser_context.add_argument("--max-items", type=int, default=25, help="Maximum items")
    parser_context.add_argument("--include-cross-repo", action="store_true")
    parser_context.add_argument("--relevant-repos", required=False, help="Comma-separated repo slugs")
    parser_context.add_argument("--cross-repo-max-per-repo", type=int, default=4)
    parser_context.add_argument("--global-max", type=int, default=4)
    parser_context.add_argument("--relevant-repo-cap", type=int, default=5)
    parser_context.set_defaults(func=cmd_context)

    parser_add = sub.add_parser("add", help="Add one curated memory entry")
    parser_add.add_argument("--repo", required=False, help="Repository slug/name")
    parser_add.add_argument("--repo-path", required=False, help="Repository path")
    parser_add.add_argument("--kind", required=True, choices=KIND_CHOICES)
    parser_add.add_argument("--title", required=True)
    parser_add.add_argument("--body-file", required=True)
    parser_add.add_argument("--tags", required=False)
    parser_add.add_argument("--source-session", required=False)
    parser_add.add_argument("--confidence", required=False, type=float, default=0.8)
    parser_add.add_argument("--scope", required=False, choices=SCOPE_CHOICES, default=SCOPE_REPO)
    parser_add.set_defaults(func=cmd_add)

    parser_ingest_session = sub.add_parser("ingest-session", help="Ingest one session jsonl file")
    parser_ingest_session.add_argument("--session-file", required=True)
    parser_ingest_session.add_argument("--max-candidates", type=int, default=20)
    parser_ingest_session.set_defaults(func=cmd_ingest_session)

    parser_ingest_new = sub.add_parser("ingest-new", help="Ingest sessions newer than watermark")
    parser_ingest_new.add_argument("--since-watermark", action="store_true")
    parser_ingest_new.add_argument("--max-candidates", type=int, default=20)
    parser_ingest_new.set_defaults(func=cmd_ingest_new)

    parser_sync = sub.add_parser("sync-local", help="Sync local repo cache and flush outbox")
    parser_sync.add_argument("--repo-path", required=True)
    parser_sync.add_argument("--max-items", type=int, default=25)
    parser_sync.add_argument("--include-cross-repo", action="store_true")
    parser_sync.add_argument("--relevant-repos", required=False, help="Comma-separated repo slugs")
    parser_sync.add_argument("--cross-repo-max-per-repo", type=int, default=4)
    parser_sync.add_argument("--global-max", type=int, default=4)
    parser_sync.add_argument("--relevant-repo-cap", type=int, default=5)
    parser_sync.set_defaults(func=cmd_sync_local)

    parser_compact = sub.add_parser("compact", help="Regenerate summary docs")
    group = parser_compact.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo", help="One repo")
    group.add_argument("--all", action="store_true", help="All repos")
    parser_compact.set_defaults(func=cmd_compact)

    parser_migrate_scope = sub.add_parser(
        "migrate-scope",
        help="Backfill scope + rationale fields on legacy entries and refresh fingerprints",
    )
    parser_migrate_scope.set_defaults(func=cmd_migrate_scope)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
