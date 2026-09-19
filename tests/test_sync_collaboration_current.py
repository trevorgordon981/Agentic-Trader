from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import sync_collaboration_current as sync


class CollaborationCurrentSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = self.root / "id_ed25519"
        self.identity.write_text("test-key")
        self.identity.chmod(0o600)
        self.output = self.root / "stage" / "collaboration-current.md"
        self.receipt = self.root / "state" / "receipt.json"
        patches = (
            mock.patch.object(sync, "IDENTITY", self.identity),
            mock.patch.object(sync, "OUTPUT", self.output),
            mock.patch.object(sync, "RECEIPT", self.receipt),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def _current(label: str = "new") -> bytes:
        return (
            "# Current shared state\n\n"
            f"Updated: **2026-09-01 22:00 PDT** by Codex (**{label}**)\n"
        ).encode()

    def test_fetch_pins_identity_and_publish_writes_bounded_receipt(self):
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0, self._current(), b"")

        data = sync.fetch_source(runner=runner)
        receipt = sync.publish(
            data,
            generated_at=datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc),
        )
        command = seen["command"]
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn(f"IdentityFile={self.identity}", command)
        self.assertIn("(**new**)", self.output.read_text())
        self.assertIn("raw_work_log_ingested: false", self.output.read_text())
        self.assertEqual(json.loads(self.receipt.read_text())["output_sha256"], receipt["output_sha256"])
        self.assertEqual(os.stat(self.output).st_mode & 0o777, 0o600)

    def test_failed_fetch_does_not_replace_last_known_good(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("last-known-good")

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 255, b"", b"offline")

        with self.assertRaises(sync.SyncError):
            sync.fetch_source(runner=runner)
        self.assertEqual(self.output.read_text(), "last-known-good")

    def test_secret_shaped_current_is_rejected_before_replace(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("last-known-good")
        bad = self._current("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        with self.assertRaises(sync.snapshot.SnapshotError):
            sync.publish(bad)
        self.assertEqual(self.output.read_text(), "last-known-good")

    def test_unchanged_source_is_a_true_noop(self):
        stamp = datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)
        first, changed = sync.sync_current(self._current(), generated_at=stamp)
        self.assertTrue(changed)
        before_bytes = self.output.read_bytes()
        before_mtime = self.output.stat().st_mtime_ns

        second, changed = sync.sync_current(
            self._current(),
            generated_at=datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(changed)
        self.assertEqual(second["output_sha256"], first["output_sha256"])
        self.assertEqual(self.output.read_bytes(), before_bytes)
        self.assertEqual(self.output.stat().st_mtime_ns, before_mtime)

    def test_receipt_output_mismatch_forces_rebuild(self):
        sync.sync_current(self._current("first"))
        self.output.write_text("tampered")
        receipt, changed = sync.sync_current(self._current("first"))
        self.assertTrue(changed)
        self.assertEqual(
            sync.snapshot.sha256(self.output.read_bytes()),
            receipt["output_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
