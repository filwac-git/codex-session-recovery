import json
from pathlib import Path
import sqlite3
import sys
import unittest
import shutil


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recover_codex_sessions as recovery


THREAD_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT,
    preview TEXT NOT NULL DEFAULT '',
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    history_mode TEXT NOT NULL DEFAULT 'legacy'
)
"""


def json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture_names = {
            "test_scan_finds_active_archived_and_stale_index": "scan",
            "test_repair_migrates_only_session_metadata_and_rebuilds_database": "repair",
            "test_verify_reports_custom_database_provider": "verify",
        }
        self.home = ROOT / "tests" / "fixtures" / fixture_names[self._testMethodName]
        for child in self.home.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        (self.home / "sessions" / "2026" / "01" / "02").mkdir(parents=True)
        (self.home / "archived_sessions").mkdir()
        (self.home / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n\n'
            '[model_providers.custom]\n'
            'name = "third party"\n'
            'base_url = "https://example.invalid"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n',
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(THREAD_SCHEMA)
        connection.commit()
        connection.close()

    def tearDown(self):
        for child in self.home.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def write_rollout(self, path, session_id, provider="custom", user_text="hello"):
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json_line(
                {
                    "timestamp": "2026-01-02T03:04:05Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "timestamp": "2026-01-02T03:04:05Z",
                        "cwd": "workspace",
                        "source": "vscode",
                        "thread_source": "user",
                        "cli_version": "test",
                        "model_provider": provider,
                    },
                }
            ),
            json_line(
                {
                    "timestamp": "2026-01-02T03:05:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_text}],
                    },
                }
            ),
            json_line(
                {
                    "timestamp": "2026-01-02T03:06:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": 'literal history: "model_provider":"custom"',
                            }
                        ],
                    },
                }
            ),
        ]
        path.write_text("".join(lines), encoding="utf-8")

    def test_scan_finds_active_archived_and_stale_index(self):
        first_id = "11111111-1111-1111-1111-111111111111"
        second_id = "22222222-2222-2222-2222-222222222222"
        self.write_rollout(
            self.home / "sessions" / "2026" / "01" / "02" / f"rollout-{first_id}.jsonl",
            first_id,
        )
        self.write_rollout(
            self.home / "archived_sessions" / f"rollout-{second_id}.jsonl",
            second_id,
        )
        spawned_path = self.home / "archived_sessions" / f"rollout-{second_id}.jsonl"
        spawned_path.write_bytes(spawned_path.read_bytes().replace(second_id.encode(), first_id.encode(), 1))
        (self.home / "session_index.jsonl").write_text(
            json_line({"id": first_id, "thread_name": "Active", "updated_at": "2026-01-02T00:00:00Z"})
            + json_line(
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "thread_name": "Stale",
                    "updated_at": "2026-01-02T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

        result = recovery.discover_sessions(self.home)
        self.assertEqual(set(result.sessions), {first_id, second_id})
        self.assertEqual(result.sessions[first_id].title, "Active")
        self.assertEqual(result.sessions[second_id].location, "archived")
        self.assertIn("33333333-3333-3333-3333-333333333333", result.index_titles)

    def test_repair_migrates_only_session_metadata_and_rebuilds_database(self):
        active_id = "11111111-1111-1111-1111-111111111111"
        archived_id = "22222222-2222-2222-2222-222222222222"
        active_path = self.home / "sessions" / "2026" / "01" / "02" / f"rollout-{active_id}.jsonl"
        archived_path = self.home / "archived_sessions" / f"rollout-{archived_id}.jsonl"
        self.write_rollout(active_path, active_id, user_text="Active request")
        self.write_rollout(archived_path, archived_id, user_text="Archived request")
        original = active_path.read_bytes()

        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, archived) "
            "VALUES (?, ?, 1, 2, 'vscode', 'custom', 'workspace', 'Active title', '{}', 'on-request', 1)",
            (active_id, str(active_path)),
        )
        connection.commit()
        connection.close()

        result = recovery.discover_sessions(self.home)
        backup = recovery.create_backup(self.home, result, self.home / "backups")
        restored = recovery.restore_archived_files(self.home, result)
        migrated = recovery.migrate_rollout_metadata(result)
        inserted, updated = recovery.ensure_database_rows(self.home, result)
        recovery.update_session_index(self.home, result)
        recovery.update_config(self.home, add_compat_alias=True)

        self.assertTrue((backup / "manifest.json").exists())
        self.assertEqual(restored, 1)
        self.assertEqual(migrated, 2)
        self.assertEqual((inserted, updated), (1, 1))

        migrated_bytes = active_path.read_bytes()
        expected = original.replace(b'"model_provider":"custom"', b'"model_provider":"openai"', 1)
        self.assertEqual(migrated_bytes, expected)
        self.assertIn(b'literal history: \\"model_provider\\":\\"custom\\"', migrated_bytes)

        connection = sqlite3.connect(self.home / "state_5.sqlite")
        rows = connection.execute(
            "SELECT id, model_provider, archived FROM threads ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [(active_id, "openai", 0), (archived_id, "openai", 0)])

        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("https://chatgpt.com/backend-api/codex", config)
        self.assertNotIn("example.invalid", config)
        self.assertEqual(recovery.verify(self.home), [])

    def test_verify_reports_custom_database_provider(self):
        session_id = "11111111-1111-1111-1111-111111111111"
        path = self.home / "sessions" / "2026" / "01" / "02" / f"rollout-{session_id}.jsonl"
        self.write_rollout(path, session_id, provider="openai")
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode) "
            "VALUES (?, ?, 1, 2, 'vscode', 'custom', 'workspace', 'Title', '{}', 'on-request')",
            (session_id, str(path)),
        )
        connection.commit()
        connection.close()
        issues = recovery.verify(self.home)
        self.assertTrue(any("数据库仍含 custom" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
