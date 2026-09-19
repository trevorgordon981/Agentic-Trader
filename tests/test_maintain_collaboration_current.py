from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import maintain_collaboration_current as maintain


class CollaborationCurrentMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "collaboration-current.md"
        self.state = self.root / "maintenance.json"
        self.output.write_text("snapshot")
        patches = (
            mock.patch.object(maintain.sync, "OUTPUT", self.output),
            mock.patch.object(maintain, "STATE", self.state),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.output_sha = maintain.snapshot.sha256(self.output.read_bytes())
        self.source_sha = "a" * 64

    def _manifest(self, expected):
        self.assertEqual(expected, self.output_sha)
        return {
            "source_path": maintain.RAG_HOST_PATH,
            "file_sha256": self.output_sha,
            "file_bytes": len(self.output.read_bytes()),
            "file_mtime": 1.0,
            "manifest_mtime": 1.0,
            "chunk_count": 1,
            "chunk_ids": ["1" * 16],
        }

    @staticmethod
    def _health():
        return {"status": "ok", "lancedb": 10, "tantivy": 10}

    def _syncer(self, changed=False):
        return ({
            "source": "source_host:/home/example-user/ai-collaboration/CURRENT.md",
            "source_sha256": self.source_sha,
            "output_sha256": self.output_sha,
        }, changed)

    def test_unchanged_verified_state_does_not_publish_or_ingest(self):
        publisher = mock.Mock()
        awaiter = mock.Mock()
        state = maintain.maintain_once(
            now=datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc),
            syncer=self._syncer,
            remote_hasher=lambda: self.output_sha,
            publisher=publisher,
            searcher=lambda source: source == self.source_sha,
            awaiter=awaiter,
            manifest_reader=self._manifest,
            health_reader=self._health,
        )
        self.assertEqual(state["status"], "VERIFIED_CURRENT")
        self.assertFalse(state["sync_changed"])
        self.assertFalse(state["published"])
        publisher.assert_not_called()
        awaiter.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["indexed_source_sha256"], self.source_sha)

    def test_changed_state_publishes_and_waits_for_exact_index(self):
        self.state.write_text(json.dumps({
            "schema": maintain.SCHEMA,
            "status": "VERIFIED_CURRENT",
            "last_verified_at": "2026-09-02T07:00:00Z",
            "source_sha256": "c" * 64,
        }))
        remote_values = iter(["b" * 64, self.output_sha, self.output_sha])
        publisher = mock.Mock()
        awaiter = mock.Mock(return_value={"status": "started", "pid": 42})
        searches = iter([False, True])
        state = maintain.maintain_once(
            syncer=lambda: self._syncer(changed=True),
            remote_hasher=lambda: next(remote_values),
            publisher=publisher,
            searcher=lambda source: next(searches),
            awaiter=awaiter,
            manifest_reader=self._manifest,
            health_reader=self._health,
        )
        publisher.assert_called_once_with(self.output_sha)
        awaiter.assert_called_once_with(self.source_sha)
        self.assertTrue(state["published"])
        self.assertEqual(state["ingest_status"], "started")

    def test_source_change_marks_unverified_before_publication(self):
        self.state.write_text(json.dumps({
            "schema": maintain.SCHEMA,
            "status": "VERIFIED_CURRENT",
            "last_verified_at": "2026-09-02T07:00:00Z",
            "source_sha256": "c" * 64,
        }))

        def publisher(expected):
            interim = json.loads(self.state.read_text())
            self.assertEqual(interim["status"], "REFRESHING_UNVERIFIED")
            self.assertEqual(interim["source_sha256"], self.source_sha)

        remote_values = iter(["c" * 64, self.output_sha, self.output_sha])
        maintain.maintain_once(
            syncer=lambda: self._syncer(changed=True),
            remote_hasher=lambda: next(remote_values),
            publisher=publisher,
            searcher=lambda source: True,
            manifest_reader=self._manifest,
            health_reader=self._health,
        )

    def test_failure_receipt_is_fail_closed_and_preserves_last_verified(self):
        self.state.write_text(json.dumps({
            "schema": maintain.SCHEMA,
            "status": "VERIFIED_CURRENT",
            "last_verified_at": "2026-09-02T07:00:00Z",
            "source_sha256": self.source_sha,
            "output_sha256": self.output_sha,
            "published_output_sha256": self.output_sha,
            "indexed_source_sha256": self.source_sha,
        }))
        maintain._write_failure(
            maintain.MaintenanceError("source_host timeout"),
            now=datetime(2026, 9, 2, 8, 1, tzinfo=timezone.utc),
        )
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "STALE_UNVERIFIED")
        self.assertEqual(state["last_verified_at"], "2026-09-02T07:00:00Z")
        self.assertIn("source_host timeout", state["error"])

    def test_await_index_reposts_after_busy_ingest_until_search_matches(self):
        clock = iter([0.0, 1.0, 2.0, 3.0])
        searches = iter([False, True])
        ingest = mock.Mock(return_value={"status": "already_running", "pid": 7})
        response = maintain._await_index(
            self.source_sha,
            searcher=lambda source: next(searches),
            ingester=ingest,
            sleeper=lambda seconds: None,
            monotonic=lambda: next(clock),
        )
        self.assertEqual(response["status"], "already_running")
        self.assertEqual(ingest.call_count, 2)


if __name__ == "__main__":
    unittest.main()
