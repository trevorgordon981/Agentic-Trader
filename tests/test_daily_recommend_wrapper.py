"""Public API contract; production-derived narrative omitted."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


WRAPPER = Path(__file__).resolve().parents[1] / "run_daily_recommend.sh"


class DailyRecommendWrapperTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="daily-slate-wrapper-")
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        self.fake_home = self.root / "home"
        self.project = self.fake_home / "exitmgr-app"
        self.project.mkdir(parents=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.calls = self.root / "calls.jsonl"
        self.posts = self.root / "posts.jsonl"
        self.venv_python = self.fake_home / "ib-grader-venv/bin/python"
        self.venv_python.parent.mkdir(parents=True)
        self.venv_python.symlink_to(sys.executable)
        (self.fake_home / ".hermes").mkdir()
        (self.fake_home / ".hermes/.env").write_text("SLACK_BOT_TOKEN=fixture-token\n")
        (self.project / "config.yaml").write_text("trading:\n  slack_channel: C_FIXTURE\n")
        package = self.project / "exitmgr"
        package.mkdir()
        (package / "__init__.py").write_text("")
        log_call = """
            import json, os, pathlib, sys
            with pathlib.Path(os.environ["SLATE_TEST_CALLS"]).open("a") as log:
                log.write(json.dumps(sys.argv) + "\\n")
        """
        (package / "entry_safety.py").write_text(textwrap.dedent(log_call) +
            '\nraise SystemExit(int(os.environ.get("SLATE_TEST_PREFLIGHT_RC", "0")))\n')
        (self.project / "daily_recommend.py").write_text(textwrap.dedent(log_call) + """
if os.environ.get("SLATE_TEST_SLEEP") == "1":
    import time
    pathlib.Path(os.environ["SLATE_TEST_CHILD_PID"]).write_text(str(os.getpid()))
    time.sleep(60)
if os.environ.get("SLATE_TEST_IMPORT_FAILURE") == "1":
    raise RuntimeError("fixture: legacy entry reservation fence is missing after migration")
print("fixture slate output")
raise SystemExit(int(os.environ.get("SLATE_TEST_CHILD_RC", "0")))
""")
        self._executable("python3", """
            import os
            if os.environ.get("SLATE_TEST_CONFIG_FAILURE") == "1":
                raise SystemExit(1)
            print("C_FIXTURE")
        """)
        self._executable("curl", """
            import json, os, pathlib, sys
            payload = json.loads(sys.argv[sys.argv.index("-d") + 1])
            with pathlib.Path(os.environ["SLATE_TEST_POSTS"]).open("a") as log:
                log.write(json.dumps(payload) + "\\n")
            print(os.environ.get("SLATE_TEST_SLACK_RESPONSE", '{"ok":true}'))
            raise SystemExit(int(os.environ.get("SLATE_TEST_CURL_RC", "0")))
        """)
        self._executable("date", """
            import os
            print(os.environ.get("SLATE_TEST_DATE", "2026-09-08"))
        """)

        self.env = {
            "HOME": str(self.fake_home),
            "PATH": str(self.bin_dir) + ":/usr/bin:/bin",
            "SLATE_TEST_CALLS": str(self.calls),
            "SLATE_TEST_POSTS": str(self.posts),
        }

    def _executable(self, name, source):
        path = self.bin_dir / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(source))
        path.chmod(0o755)

    def _run(self, **overrides):
        self.env.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run(["/bin/bash", str(WRAPPER)], env=self.env,
                              cwd=self.root, capture_output=True, text=True, timeout=10)

    def _read(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def _assert_failure(self, result, code, phase):
        self.assertEqual(result.returncode, code, result.stderr)
        posts = self._read(self.posts)
        self.assertEqual(len(posts), 1, posts)
        self.assertEqual(posts[0]["channel"], "C_FIXTURE")
        self.assertIn("Daily slate failed during " + phase, posts[0]["text"])
        self.assertIn("exit " + str(code), posts[0]["text"])
        self.assertNotIn("fixture-token", posts[0]["text"])

    def test_success_preserves_output_argv_and_posts_nothing(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture slate output", result.stdout)
        calls = self._read(self.calls)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1:], ["--config", "config.yaml"])
        self.assertEqual(calls[1], ["daily_recommend.py", "--watch-mins", "360"])
        self.assertEqual(self._read(self.posts), [])

    def test_import_failure_posts_once_and_preserves_traceback(self):
        result = self._run(SLATE_TEST_IMPORT_FAILURE=1)
        self._assert_failure(result, 1, "slate startup or execution")
        self.assertIn("RuntimeError: fixture: legacy entry reservation fence is missing", result.stderr)

    def test_nonstandard_child_status_is_preserved(self):
        self._assert_failure(self._run(SLATE_TEST_CHILD_RC=23), 23, "slate startup or execution")

    def test_preflight_failure_posts_once_and_never_runs_slate(self):
        result = self._run(SLATE_TEST_PREFLIGHT_RC=19)
        self._assert_failure(result, 19, "entry safety preflight")
        self.assertEqual(len(self._read(self.calls)), 1)

    def test_missing_interpreter_posts_once_and_preserves_127(self):
        self.venv_python.unlink()
        self._assert_failure(self._run(), 127, "entry safety preflight")
        self.assertEqual(self._read(self.calls), [])

    def test_missing_project_posts_once_and_preserves_startup_failure(self):
        shutil.rmtree(self.project)
        self._assert_failure(self._run(), 1, "startup")
        self.assertEqual(self._read(self.calls), [])

    def test_explicit_skip_keeps_single_skip_notice_and_does_not_run_slate(self):
        result = self._run(SLATE_TEST_DATE="2026-06-17")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._read(self.calls)), 1)
        posts = self._read(self.posts)
        self.assertEqual(len(posts), 1)
        self.assertIn("Daily slate skipped today", posts[0]["text"])
        self.assertNotIn("failed", posts[0]["text"])

    def test_slack_api_failure_does_not_mask_child_status_or_retry(self):
        result = self._run(SLATE_TEST_CHILD_RC=23,
                           SLATE_TEST_SLACK_RESPONSE='{"ok":false,"error":"channel_not_found"}')
        self._assert_failure(result, 23, "slate startup or execution")
        self.assertIn("SLACK POST FAILED", result.stderr)

    def test_slack_transport_failure_does_not_mask_child_status_or_retry(self):
        result = self._run(SLATE_TEST_CHILD_RC=23, SLATE_TEST_CURL_RC=28,
                           SLATE_TEST_SLACK_RESPONSE="")
        self._assert_failure(result, 23, "slate startup or execution")
        self.assertIn("SLACK POST FAILED", result.stderr)

    def test_unreadable_channel_config_uses_existing_fallback(self):
        result = self._run(SLATE_TEST_CHILD_RC=23, SLATE_TEST_CONFIG_FAILURE=1)
        self.assertEqual(result.returncode, 23, result.stderr)
        posts = self._read(self.posts)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["channel"], "CHANNEL_ID_PLACEHOLDER")

    def _assert_parent_signal_reaps_child(self, requested_signal, exit_status, *, group=False):
        pid_path = self.root / "child.pid"
        self.env.update(SLATE_TEST_SLEEP="1", SLATE_TEST_CHILD_PID=str(pid_path))
        parent = subprocess.Popen(["/bin/bash", str(WRAPPER)], env=self.env,
                                  cwd=self.root, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                if parent.poll() is not None:
                    self.fail("Wrapper exited before fixture child became ready")
                time.sleep(0.01)
            self.assertTrue(pid_path.exists(), "Fixture child did not become ready")
            child_pid = int(pid_path.read_text())
            if group:

                os.killpg(parent.pid, requested_signal)
            else:

                parent.send_signal(requested_signal)
            stdout, stderr = parent.communicate(timeout=5)
            result = subprocess.CompletedProcess(parent.args, parent.returncode, stdout, stderr)
            self._assert_failure(result, exit_status, "slate startup or execution")
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.communicate(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_parent_term_terminates_and_reaps_child_then_exits_143(self):
        self._assert_parent_signal_reaps_child(signal.SIGTERM, 143)

    def test_parent_int_terminates_and_reaps_child_then_exits_130(self):
        self._assert_parent_signal_reaps_child(signal.SIGINT, 130)

    def test_process_group_term_reaps_child_then_exits_143(self):
        self._assert_parent_signal_reaps_child(signal.SIGTERM, 143, group=True)


if __name__ == "__main__":
    unittest.main()
