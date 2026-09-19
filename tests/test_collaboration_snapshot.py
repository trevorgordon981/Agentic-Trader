from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import build_collaboration_snapshot as snapshot


class CollaborationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _source(self, body: str) -> Path:
        path = self.root / "CURRENT.md"
        path.write_text(body)
        return path

    def test_selects_only_leading_bounded_updates_and_excludes_history(self):
        source = self._source(
            "# Current shared state\n\n"
            "Updated: **2026-08-28 12:00 PDT** by Codex (**new**)\n\n"
            "Updated: **2026-08-28 11:00 PDT** by Codex (**older**)\n\n"
            "### Historical detail\nsecret-free old prose\n"
        )
        rendered, receipt = snapshot.build_snapshot(
            source, max_entries=1, max_chars=2_000,
            generated_at=datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc),
        )
        text = rendered.decode()
        self.assertIn("(**new**)", text)
        self.assertNotIn("(**older**)", text)
        self.assertNotIn("Historical detail", text)
        self.assertFalse(receipt["raw_work_log_ingested"])
        self.assertEqual(receipt["selected_entries"], 1)

    def test_rejects_secret_shaped_selected_value(self):
        source = self._source(
            "# Current shared state\n\n"
            "Updated: **2026-08-28 12:00 PDT** by Codex (**Authorization: Bearer abcdefghijklmnopqrstuvwxyz**)\n"
        )
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.build_snapshot(
                source, max_entries=2, max_chars=2_000,
                generated_at=datetime.now(timezone.utc),
            )

    def test_rejects_symlink_source_and_destination(self):
        real = self._source(
            "# Current shared state\n\n"
            "Updated: **2026-08-28 12:00 PDT** by Codex (**new**)\n"
        )
        link = self.root / "CURRENT-link.md"
        link.symlink_to(real)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.build_snapshot(
                link, max_entries=2, max_chars=2_000,
                generated_at=datetime.now(timezone.utc),
            )
        out = self.root / "out.md"
        out.symlink_to(real)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot._atomic_write(out, b"no")
        broken = self.root / "broken-out.md"
        broken.symlink_to(self.root / "missing-target")
        with self.assertRaises(snapshot.SnapshotError):
            snapshot._atomic_write(broken, b"no")

    def test_atomic_replace_and_receipt_hash_match(self):
        source = self._source(
            "# Current shared state\n\n"
            "Updated: **2026-08-28 12:00 PDT** by Codex (**new**)\n"
        )
        rendered, receipt = snapshot.build_snapshot(
            source, max_entries=2, max_chars=2_000,
            generated_at=datetime.now(timezone.utc),
        )
        output = self.root / snapshot.OUTPUT_NAME
        snapshot._atomic_write(output, rendered)
        self.assertEqual(snapshot.sha256(output.read_bytes()), receipt["output_sha256"])
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        json.dumps(receipt)


if __name__ == "__main__":
    unittest.main()
