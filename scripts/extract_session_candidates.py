#!/usr/bin/env python3
"""Inspect one Codex session JSONL and print memory candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import mem


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract memory candidates from one session JSONL")
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--max-candidates", type=int, default=20)
    args = parser.parse_args()

    session_file = Path(args.session_file).resolve()
    rows = mem.parse_session_jsonl(session_file)
    session_id, repo_path, candidates = mem.extract_candidates_from_session(
        rows,
        session_file=session_file,
        max_candidates=args.max_candidates,
    )

    print(
        json.dumps(
            {
                "session_id": session_id,
                "repo_path": str(repo_path) if repo_path else None,
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
