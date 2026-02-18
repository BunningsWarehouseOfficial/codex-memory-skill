#!/usr/bin/env python3
"""Bootstrap rollout across git repos under /mnt/data/code."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

DEFAULT_ROOT = Path("/mnt/data/code")
SCRIPT_DIR = Path(__file__).resolve().parent
MEM_SCRIPT = SCRIPT_DIR / "mem.py"


def discover_repos(root: Path) -> list[Path]:
    repos: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / ".git").is_dir():
            repos.append(child)
    return repos


def ensure_git_exclude(repo: Path) -> bool:
    exclude_path = repo / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    line = ".codex-local/"
    if line in {x.strip() for x in existing.splitlines()}:
        return False
    with exclude_path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(line + "\n")
    return True


def sync_local(repo: Path, max_items: int) -> tuple[bool, str]:
    proc = subprocess.run(
        [
            "python3",
            str(MEM_SCRIPT),
            "sync-local",
            "--repo-path",
            str(repo),
            "--max-items",
            str(max_items),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    ok = proc.returncode == 0
    out = proc.stdout.strip() or proc.stderr.strip()
    return ok, out


def main() -> int:
    parser = argparse.ArgumentParser(description="Roll out .codex-local and sync for repos")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--pilot-only", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    repos = discover_repos(root)
    if args.pilot_only:
        allowed = {"icetana-algorithm", "icetana-frontend"}
        repos = [r for r in repos if r.name in allowed]

    print(f"Discovered repos: {len(repos)}")
    for repo in repos:
        changed = ensure_git_exclude(repo)
        ok, detail = sync_local(repo, max_items=args.max_items)
        status = "ok" if ok else "error"
        marker = "updated" if changed else "unchanged"
        print(f"[{status}] {repo} exclude={marker} -> {detail}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
