#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class MemCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.repo_root = self.tmpdir / "code"
        self.repo_root.mkdir(parents=True)

        self.repo_a = self.repo_root / "repo-a"
        self.repo_b = self.repo_root / "repo-b"
        self.repo_c = self.repo_root / "repo-c"
        self.repo_d = self.repo_root / "repo-d"
        self.repo_e = self.repo_root / "repo-e"
        self.repo_f = self.repo_root / "repo-f"
        self.repo_g = self.repo_root / "repo-g"
        for repo in [
            self.repo_a,
            self.repo_b,
            self.repo_c,
            self.repo_d,
            self.repo_e,
            self.repo_f,
            self.repo_g,
        ]:
            repo.mkdir()

        self.memory_home = self.tmpdir / ".codex-memory"
        self.session_root = self.tmpdir / "sessions"
        self.session_root.mkdir()

        self.mem_script = Path.home() / ".codex/skills/personal-continual-memory/scripts/mem.py"

        self.env = os.environ.copy()
        self.env["CODEX_MEMORY_HOME"] = str(self.memory_home)
        self.env["CODEX_SESSION_ROOT"] = str(self.session_root)
        self.env["CODEX_REPO_ROOT"] = str(self.repo_root)

        self.body_index = 0

    def tearDown(self) -> None:
        self.tmpdir_obj.cleanup()

    def run_mem(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(self.mem_script), *args],
            text=True,
            capture_output=True,
            check=False,
            env=self.env,
        )

    def write_body(self, text: str) -> Path:
        self.body_index += 1
        body = self.tmpdir / f"body-{self.body_index}.md"
        body.write_text(text, encoding="utf-8")
        return body

    def create_git_repo_with_worktree(
        self,
        *,
        main_repo_name: str,
        worktree_name: str,
        branch_name: str = "feature-worktree",
    ) -> tuple[Path, Path]:
        main_repo = self.repo_root / main_repo_name
        worktree_repo = self.repo_root / worktree_name
        main_repo.mkdir()

        subprocess.run(["git", "-C", str(main_repo), "init"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(main_repo), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(main_repo), "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
            text=True,
        )
        readme = main_repo / "README.md"
        readme.write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(main_repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(main_repo), "commit", "-m", "init"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(main_repo),
                "worktree",
                "add",
                str(worktree_repo),
                "-b",
                branch_name,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return main_repo, worktree_repo

    def add_entry(
        self,
        *,
        repo: str,
        repo_path: Path,
        kind: str,
        title: str,
        body_text: str,
        scope: str = "repo",
    ) -> subprocess.CompletedProcess[str]:
        body = self.write_body(body_text)
        return self.run_mem(
            "add",
            "--repo",
            repo,
            "--repo-path",
            str(repo_path),
            "--kind",
            kind,
            "--scope",
            scope,
            "--title",
            title,
            "--body-file",
            str(body),
        )

    def read_entries(self) -> list[dict]:
        path = self.memory_home / "index" / "entries.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def test_add_writes_schema_entry_with_default_scope(self) -> None:
        proc = self.add_entry(
            repo="repo-a",
            repo_path=self.repo_a,
            kind="fact",
            title="Package manager",
            body_text="Use pdm, not pip.",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        entries = self.read_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        for key in [
            "id",
            "timestamp_utc",
            "repo_name",
            "repo_path",
            "session_id",
            "kind",
            "scope",
            "title",
            "body_markdown",
            "tags",
            "confidence",
            "evidence",
            "fingerprint_sha256",
            "status",
            "origin",
        ]:
            self.assertIn(key, entry)
        self.assertEqual(entry["scope"], "repo")
        self.assertIn("Why this memory was saved:", entry["body_markdown"])

    def test_scope_flag_writes_global(self) -> None:
        proc = self.add_entry(
            repo="repo-a",
            repo_path=self.repo_a,
            kind="workflow",
            scope="global",
            title="System integration",
            body_text="Service A and B communicate over gRPC.",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        entries = self.read_entries()
        self.assertEqual(entries[0]["scope"], "global")
        self.assertIn("Why this memory was saved:", entries[0]["body_markdown"])

    def test_add_injects_rationale_for_all_kinds(self) -> None:
        for kind in ("fact", "workflow", "gotcha", "helper"):
            proc = self.add_entry(
                repo="repo-a",
                repo_path=self.repo_a,
                kind=kind,
                title=f"{kind}-entry",
                body_text=f"Body for {kind}",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        entries = self.read_entries()
        self.assertEqual(len(entries), 4)
        for entry in entries:
            self.assertIn("Why this memory was saved:", entry["body_markdown"])

    def test_duplicate_is_deduped(self) -> None:
        for _ in range(2):
            proc = self.add_entry(
                repo="repo-a",
                repo_path=self.repo_a,
                kind="helper",
                title="test command",
                body_text="Run tests with pdm run pytest",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        entries = self.read_entries()
        self.assertEqual(len(entries), 1)

    def test_obsolete_entry_is_deleted_on_replacement(self) -> None:
        proc1 = self.add_entry(
            repo="repo-b",
            repo_path=self.repo_b,
            kind="workflow",
            title="frontend run command",
            body_text="Use yarn start",
        )
        self.assertEqual(proc1.returncode, 0, msg=proc1.stderr)

        proc2 = self.add_entry(
            repo="repo-b",
            repo_path=self.repo_b,
            kind="workflow",
            title="frontend run command",
            body_text="Use yarn dev",
        )
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)

        entries = self.read_entries()
        self.assertEqual(len(entries), 1)
        active = [e for e in entries if e["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertIn("Use yarn dev", active[0]["body_markdown"])
        self.assertIn("Why this memory was saved:", active[0]["body_markdown"])

    def test_ingest_session_extracts_helper_commands_with_repo_scope(self) -> None:
        session_file = self.session_root / "sample.jsonl"
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": "sid-1", "cwd": str(self.repo_a)},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "pdm run pytest tests/test_smoke.py"}),
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        proc = self.run_mem("ingest-session", "--session-file", str(session_file))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        entries = self.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "helper")
        self.assertEqual(entries[0]["origin"], "post_session")
        self.assertEqual(entries[0]["scope"], "repo")
        self.assertIn("Why this memory was saved:", entries[0]["body_markdown"])

    def test_ingest_session_skips_generic_non_repo_specific_command(self) -> None:
        session_file = self.session_root / "generic.jsonl"
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": "sid-generic", "cwd": str(self.repo_a)},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "pdm run pytest"}),
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        proc = self.run_mem("ingest-session", "--session-file", str(session_file))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn('"ingested": 0', proc.stdout)
        self.assertEqual(self.read_entries(), [])

    def test_ingest_session_skips_help_discovery_command(self) -> None:
        session_file = self.session_root / "help.jsonl"
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": "sid-help", "cwd": str(self.repo_a)},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "python scripts/tool.py --help"}),
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        proc = self.run_mem("ingest-session", "--session-file", str(session_file))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn('"ingested": 0', proc.stdout)
        self.assertEqual(self.read_entries(), [])

    def test_default_context_is_repo_local_only(self) -> None:
        self.add_entry(
            repo="repo-a",
            repo_path=self.repo_a,
            kind="workflow",
            title="A",
            body_text="Workflow A",
        )
        self.add_entry(
            repo="repo-b",
            repo_path=self.repo_b,
            kind="workflow",
            title="B",
            body_text="Workflow B",
        )

        proc = self.run_mem(
            "context",
            "--repo",
            "repo-b",
            "--repo-path",
            str(self.repo_b),
            "--max-items",
            "10",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        text = proc.stdout
        self.assertIn("## [workflow] B", text)
        self.assertNotIn("## [workflow] A", text)

    def test_cross_repo_requires_relevant_repos(self) -> None:
        proc = self.run_mem(
            "context",
            "--repo",
            "repo-b",
            "--repo-path",
            str(self.repo_b),
            "--include-cross-repo",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("requires --relevant-repos", proc.stderr)

    def test_cross_repo_caps_and_selected_repo_filtering(self) -> None:
        for idx in range(6):
            self.add_entry(
                repo="repo-a",
                repo_path=self.repo_a,
                kind="helper",
                scope="repo",
                title=f"repo-scope-{idx}",
                body_text=f"repo scope body {idx}",
            )
        for idx in range(6):
            self.add_entry(
                repo="repo-a",
                repo_path=self.repo_a,
                kind="helper",
                scope="global",
                title=f"global-scope-{idx}",
                body_text=f"global scope body {idx}",
            )

        self.add_entry(
            repo="repo-c",
            repo_path=self.repo_c,
            kind="helper",
            scope="repo",
            title="c-only",
            body_text="should not appear",
        )

        proc = self.run_mem(
            "context",
            "--repo",
            "repo-b",
            "--repo-path",
            str(self.repo_b),
            "--include-cross-repo",
            "--relevant-repos",
            "repo-a",
            "--cross-repo-max-per-repo",
            "4",
            "--global-max",
            "4",
            "--max-items",
            "100",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        text = proc.stdout
        self.assertIn("relevant repos selected (1): repo-a", text)
        self.assertIn("repo-a: repo_scope=4 global_scope=4 total=8", text)
        self.assertEqual(text.count("repo: repo-a |"), 8)
        self.assertNotIn("repo: repo-c |", text)

    def test_relevant_repo_cap_applied(self) -> None:
        for repo_slug, repo_path in [
            ("repo-a", self.repo_a),
            ("repo-c", self.repo_c),
            ("repo-d", self.repo_d),
            ("repo-e", self.repo_e),
            ("repo-f", self.repo_f),
            ("repo-g", self.repo_g),
        ]:
            self.add_entry(
                repo=repo_slug,
                repo_path=repo_path,
                kind="fact",
                scope="repo",
                title=f"{repo_slug}-entry",
                body_text=f"{repo_slug} body",
            )

        proc = self.run_mem(
            "context",
            "--repo",
            "repo-b",
            "--repo-path",
            str(self.repo_b),
            "--include-cross-repo",
            "--relevant-repos",
            "repo-a,repo-c,repo-d,repo-e,repo-f,repo-g",
            "--relevant-repo-cap",
            "5",
            "--max-items",
            "100",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        text = proc.stdout
        self.assertIn("relevant repos selected (5)", text)
        self.assertNotIn("repo-g: repo_scope=", text)

    def test_legacy_entry_without_scope_defaults_to_repo(self) -> None:
        self.add_entry(
            repo="repo-b",
            repo_path=self.repo_b,
            kind="fact",
            title="seed",
            body_text="seed",
        )
        path = self.memory_home / "index" / "entries.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows[0].pop("scope", None)
        path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")

        proc = self.run_mem(
            "context",
            "--repo",
            "repo-b",
            "--repo-path",
            str(self.repo_b),
            "--max-items",
            "10",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("scope: repo", proc.stdout)

    def test_migrate_scope_backfills_missing_rationale(self) -> None:
        self.add_entry(
            repo="repo-a",
            repo_path=self.repo_a,
            kind="workflow",
            title="legacy workflow",
            body_text="Initial body",
        )
        path = self.memory_home / "index" / "entries.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows[0]["body_markdown"] = "Legacy body without rationale."
        rows[0]["scope"] = "repo"
        path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")

        proc = self.run_mem("migrate-scope")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result.get("rationales_updated"), 1)

        entries = self.read_entries()
        self.assertIn("Why this memory was saved:", entries[0]["body_markdown"])

    def test_compact_writes_consistent_profile_and_entry_metadata(self) -> None:
        self.add_entry(
            repo="repo-a",
            repo_path=self.repo_a,
            kind="helper",
            title="metadata check",
            body_text="echo hello",
            scope="repo",
        )
        proc = self.run_mem("compact", "--repo", "repo-a")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        profile = (self.memory_home / "repos" / "repo-a" / "profile.md").read_text(encoding="utf-8")
        helpers = (self.memory_home / "repos" / "repo-a" / "helpers.md").read_text(encoding="utf-8")

        self.assertIn("Active entries: 1", profile)
        self.assertIn("- helper: 1", profile)
        self.assertIn("- kind: helper", helpers)
        self.assertIn("- scope: repo", helpers)
        self.assertIn("- origin:", helpers)
        self.assertIn("- session_id:", helpers)
        self.assertIn("- evidence_count:", helpers)

    def test_add_from_worktree_routes_to_canonical_repo_slug(self) -> None:
        main_repo, worktree_repo = self.create_git_repo_with_worktree(
            main_repo_name="hazard-core",
            worktree_name="hazard-core-sam3-batched",
            branch_name="sam3-batched",
        )

        body = self.write_body("Use explicit SAM3 paths.")
        proc = self.run_mem(
            "add",
            "--repo",
            "hazard-core-sam3-batched",
            "--repo-path",
            str(worktree_repo),
            "--kind",
            "workflow",
            "--title",
            "Pass explicit SAM3 paths for benchmarks",
            "--body-file",
            str(body),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        entries = self.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repo_name"], "hazard-core")
        self.assertEqual(entries[0]["repo_path"], str(main_repo.resolve()))

        compact_proc = self.run_mem("compact", "--repo", "hazard-core")
        self.assertEqual(compact_proc.returncode, 0, msg=compact_proc.stderr)
        self.assertTrue((self.memory_home / "repos" / "hazard-core").exists())
        self.assertFalse((self.memory_home / "repos" / "hazard-core-sam3-batched").exists())

    def test_sync_local_from_worktree_reads_canonical_repo_context(self) -> None:
        main_repo, worktree_repo = self.create_git_repo_with_worktree(
            main_repo_name="hazard-core",
            worktree_name="hazard-core-sam3-batched",
            branch_name="sam3-batched-sync",
        )

        self.add_entry(
            repo="hazard-core",
            repo_path=main_repo,
            kind="gotcha",
            title="Use --fps 0 for throughput tests",
            body_text="Use --fps 0 for max throughput measurements.",
        )

        sync_proc = self.run_mem("sync-local", "--repo-path", str(worktree_repo), "--max-items", "10")
        self.assertEqual(sync_proc.returncode, 0, msg=sync_proc.stderr)

        context_path = worktree_repo / ".codex-local" / "memory-context.md"
        self.assertTrue(context_path.exists())
        context_text = context_path.read_text(encoding="utf-8")
        self.assertIn("Use --fps 0 for throughput tests", context_text)


if __name__ == "__main__":
    unittest.main()
